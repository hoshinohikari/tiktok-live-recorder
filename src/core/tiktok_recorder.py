import re
import subprocess
import time
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4

from requests import RequestException

try:
    from curl_cffi.requests.exceptions import RequestException as CurlRequestException
except ImportError:  # Termux can use requests without curl_cffi.
    CurlRequestException = RequestException

from core.tiktok_api import TikTokAPI
from upload.telegram import Telegram
from utils.custom_exceptions import LiveNotFound, TikTokRecorderError, UserLiveError
from utils.enums import Error, Mode, RoomStatus, TikTokError, TimeOut
from utils.logger_manager import logger
from utils.media_validation import MediaValidationError, inspect_video_file
from utils.recorder_config import RecorderConfig
from utils.url_sanitizer import redact_url, redact_urls_in_text
from utils.video_management import VideoManagement

SELECTION_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class FFmpegResult:
    returncode: int
    output: str
    timed_out: bool = False
    interrupted: bool = False


class TikTokRecorder:
    def __init__(self, config: RecorderConfig):
        self.tiktok = TikTokAPI(proxy=config.proxy, cookies=config.cookies)

        self.url = config.url
        self.user = config.user
        self.room_id = config.room_id
        self.mode = config.mode
        self.automatic_interval = config.automatic_interval
        self.duration = config.duration
        self.output = config.output
        self.bitrate = config.bitrate
        self.use_telegram = config.use_telegram
        self.use_ffmpeg = config.use_ffmpeg
        self._proxy = config.proxy
        self._cookies = config.cookies

        self.ffmpeg_retries = 3
        self.ffmpeg_retry_delay = 5
        self.ffmpeg_rw_timeout = 30
        self._recording_attempt_lock = Lock()
        self._recording_attempt_number = 0

    def _setup(self):
        """Resolve user/room data and validate prerequisites via network calls."""
        if self.mode == Mode.FOLLOWERS:
            self.check_country_blacklisted()
            self.sec_uid = self.tiktok.get_sec_uid()
            if self.sec_uid is None:
                raise TikTokRecorderError("Failed to retrieve sec_uid.")

            logger.info("Followers mode activated\n")
        else:
            if self.url:
                self.user, self.room_id = self.tiktok.get_room_and_user_from_url(
                    self.url
                )

            if not self.user:
                self.user = self.tiktok.get_user_from_room_id(self.room_id)

            if not self.room_id:
                self.room_id = self.tiktok.get_room_id_from_user(self.user)

            self.check_country_blacklisted()
            logger.info(f"USERNAME: {self.user}" + ("\n" if not self.room_id else ""))
            if self.room_id:
                room_status = self._room_status(self.room_id)
                logger.info(
                    f"ROOM_ID:  {self.room_id}"
                    + ("\n" if room_status is not RoomStatus.LIVE else "")
                )

        # If proxy was used for the initial checks, switch to a direct connection
        # for the actual stream download to avoid proxy bottlenecks
        if self._proxy:
            self.tiktok = TikTokAPI(proxy=None, cookies=self._cookies)

    def run(self):
        """
        Resolves prerequisites and runs the recorder in the selected mode.

        If the mode is MANUAL, it checks if the user is currently live and
        if so, starts recording.

        If the mode is AUTOMATIC, it continuously checks if the user is live
        and if not, waits for the specified timeout before rechecking.
        If the user is live, it starts recording.

        if the mode is FOLLOWERS, it continuously checks the followers of
        the authenticated user. If any follower is live, it starts recording
        their live stream in a separate process.
        """
        self._setup()

        if self.mode == Mode.MANUAL:
            self.manual_mode()

        elif self.mode == Mode.AUTOMATIC:
            self.automatic_mode()

        elif self.mode == Mode.FOLLOWERS:
            self.followers_mode()

    def manual_mode(self):
        status = self._room_status(self.room_id)
        if status is RoomStatus.OFFLINE:
            raise UserLiveError(f"@{self.user}: {TikTokError.USER_NOT_CURRENTLY_LIVE}")
        if status is RoomStatus.UNKNOWN:
            raise TikTokRecorderError(
                f"Unable to determine whether @{self.user} is live; status is unknown."
            )

        self.start_recording(self.user, self.room_id)

    def automatic_mode(self):
        failure_count = 0
        while True:
            try:
                self.room_id = self.tiktok.get_room_id_from_user(self.user)
                status = self._room_status(self.room_id)

                if status is RoomStatus.OFFLINE:
                    logger.info(
                        f"@{self.user} is confirmed offline. Waiting "
                        f"{self.automatic_interval} seconds before recheck."
                    )
                    failure_count = 0
                    time.sleep(self.automatic_interval)
                    continue

                if status is RoomStatus.UNKNOWN:
                    raise TikTokRecorderError("Live status is unknown.")

                self.start_recording(self.user, self.room_id)
                failure_count = 0

            except LiveNotFound as ex:
                logger.info(ex)
                logger.info(
                    f"Waiting {self.automatic_interval} seconds before recheck\n"
                )
                time.sleep(self.automatic_interval)

            # The full recording round must back off for every unexpected failure.
            except Exception as ex:  # noqa: BLE001
                failure_count += 1
                delay = self._automatic_backoff_seconds(failure_count)
                logger.error(
                    f"Recording round failed: {redact_urls_in_text(str(ex))}. "
                    f"Retrying in {delay} seconds",
                    exc_info=not isinstance(
                        ex, (RequestException, CurlRequestException, ConnectionError)
                    ),
                )
                time.sleep(delay)

    @staticmethod
    def _automatic_backoff_seconds(failure_count: int) -> int:
        schedule = (5, 10, 20, 30)
        return schedule[min(max(failure_count - 1, 0), len(schedule) - 1)]

    def _room_status(self, room_id: str) -> RoomStatus:
        status_getter = getattr(self.tiktok, "get_room_status", None)
        try:
            if callable(status_getter):
                status = status_getter(room_id)
                if isinstance(status, RoomStatus):
                    return status
                try:
                    return RoomStatus(status)
                except (ValueError, TypeError):
                    return RoomStatus.UNKNOWN

            alive_getter = getattr(self.tiktok, "is_room_alive", None)
            if callable(alive_getter):
                return (
                    RoomStatus.LIVE
                    if alive_getter(room_id)
                    else RoomStatus.OFFLINE
                )
        except Exception as ex:  # noqa: BLE001 - status lookup failure is unknown
            logger.warning(
                "Live status check failed; status is unknown: %s",
                redact_urls_in_text(str(ex)),
            )
        return RoomStatus.UNKNOWN

    def followers_mode(self):
        active_recordings = {}  # follower -> Thread

        while True:
            try:
                followers = self.tiktok.get_followers_list(self.sec_uid)

                for follower in followers:
                    if follower in active_recordings:
                        if not active_recordings[follower].is_alive():
                            logger.info(f"Recording of @{follower} finished.")
                            del active_recordings[follower]
                        else:
                            continue

                    try:
                        room_id = self.tiktok.get_room_id_from_user(follower)

                        if not room_id or self._room_status(room_id) is not RoomStatus.LIVE:
                            continue

                        logger.info(f"@{follower} is live. Starting recording...")

                        thread = Thread(
                            target=self.start_recording,
                            args=(follower, room_id),
                            daemon=True,
                        )
                        thread.start()
                        active_recordings[follower] = thread

                        time.sleep(2.5)

                    except TikTokRecorderError as e:
                        logger.error(f"Error while processing @{follower}: {e}")
                        continue

                    except Exception as e:  # noqa: BLE001 - isolate follower failures
                        logger.error(
                            f"Unexpected error processing @{follower}: {e}",
                            exc_info=True,
                        )
                        continue

                print()
                logger.info(
                    f"Waiting {self.automatic_interval} minutes for the next check..."
                )
                time.sleep(self.automatic_interval * TimeOut.ONE_MINUTE)

            except (UserLiveError, LiveNotFound) as ex:
                logger.info(ex)
                logger.info(
                    f"Waiting {self.automatic_interval} seconds before recheck\n"
                )
                time.sleep(self.automatic_interval)

            except ConnectionError:
                logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                time.sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)

            except Exception as ex:  # noqa: BLE001 - keep the follower loop resilient
                logger.error(f"Unexpected error: {ex}", exc_info=True)
                time.sleep(self.automatic_interval)
                continue

    def _build_output_path(
        self, user: str, ext: str = "_flv.mp4", attempt_id: str | None = None
    ) -> str:
        timestamp = time.strftime("%Y.%m.%d_%H-%M-%S", time.localtime())
        attempt_suffix = f"_{attempt_id}" if attempt_id else ""
        filename = f"TK_{user}_{timestamp}{attempt_suffix}{ext}"
        if self.output:
            return str(Path(self.output) / filename)
        return filename

    def _allocate_output_path(self, user: str, ext: str) -> tuple[str, str]:
        lock = getattr(self, "_recording_attempt_lock", None)
        if lock is None:
            lock = self._recording_attempt_lock = Lock()
            self._recording_attempt_number = 0

        with lock:
            self._recording_attempt_number += 1
            attempt_number = self._recording_attempt_number

        while True:
            attempt_id = f"{attempt_number:03d}_{uuid4().hex[:8]}"
            output = self._build_output_path(user, ext, attempt_id=attempt_id)
            if not Path(output).exists():
                break

        try:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            raise TikTokRecorderError(
                f"Unable to prepare recording output directory for {output}: {ex}"
            ) from ex

        return output, attempt_id

    @staticmethod
    def _ffmpeg_headers() -> str:
        return (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.6478.127 Safari/537.36\r\n"
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
            "image/webp,image/apng,application/json,text/plain,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7\r\n"
            "Accept-Encoding: gzip, deflate\r\n"
            "Sec-Ch-Ua: \"Not/A)Brand\";v=\"8\", \"Chromium\";v=\"126\"\r\n"
            "Sec-Ch-Ua-Mobile: ?0\r\n"
            "Sec-Ch-Ua-Platform: \"Windows\"\r\n"
            "Accept-Language: en-US\r\n"
            "Upgrade-Insecure-Requests: 1\r\n"
            "Sec-Fetch-Site: none\r\n"
            "Sec-Fetch-Mode: navigate\r\n"
            "Sec-Fetch-User: ?1\r\n"
            "Sec-Fetch-Dest: document\r\n"
            "Priority: u=0, i\r\n"
            "Referer: https://www.tiktok.com/\r\n"
            "Origin: https://www.tiktok.com/\r\n"
            "Cookie: sessionid_ss=; tt-target-idc=useast2a\r\n"
        )

    def _run_ffmpeg_process(
        self,
        command: list[str],
        *,
        attempt_id: str,
        timeout: float | None,
        segment: str | None = None,
    ) -> FFmpegResult:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError as ex:
            raise TikTokRecorderError(
                "FFmpeg not found. Please ensure ffmpeg is installed and in PATH."
            ) from ex
        except OSError as ex:
            raise TikTokRecorderError(f"Unable to start FFmpeg: {ex}") from ex

        output_lines = []

        def drain_output():
            if process.stdout is None:
                return
            for line in process.stdout:
                output_lines.append(line)
                logger.info(
                    "FFmpeg [attempt=%s segment=%s]: %s",
                    attempt_id,
                    segment or "probe",
                    redact_urls_in_text(line.rstrip()),
                )

        output_reader = Thread(target=drain_output, daemon=True)
        output_reader.start()
        timed_out = False
        interrupted = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
        except KeyboardInterrupt:
            interrupted = True
            process.terminate()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
        finally:
            output_reader.join(timeout=5)
            if process.stdout:
                process.stdout.close()

        logger.info(
            "FFmpeg exited [attempt=%s segment=%s]: code=%s timed_out=%s",
            attempt_id,
            segment or "probe",
            returncode,
            timed_out,
        )
        return FFmpegResult(
            returncode, "".join(output_lines), timed_out, interrupted
        )

    def _probe_stream_candidate(
        self, live_url: str, attempt_id: str, timeout: float = 30
    ) -> FFmpegResult:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-rw_timeout",
            str(int(self.ffmpeg_rw_timeout * 1_000_000)),
            "-headers",
            self._ffmpeg_headers(),
            "-i",
            live_url,
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ]
        return self._run_ffmpeg_process(command, attempt_id=attempt_id, timeout=timeout)

    @staticmethod
    def _http_status_code(output: str) -> int | None:
        match = re.search(
            r"(?:HTTP error|Server returned)\s+(\d{3})", output, re.IGNORECASE
        )
        return int(match.group(1)) if match else None

    @staticmethod
    def _output_write_error(output: str) -> str | None:
        lowered = output.lower()
        if "no space left" in lowered or "disk full" in lowered:
            return "disk space is insufficient"
        if any(
            marker in lowered
            for marker in (
                "permission denied",
                "access is denied",
                "could not open output",
                "error opening output",
                "cannot open output",
            )
        ):
            return "the output file could not be written"
        return None

    def _record_with_ffmpeg(
        self, live_url: str, output: str, attempt_id: str
    ) -> FFmpegResult:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-n",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-rw_timeout",
            str(int(self.ffmpeg_rw_timeout * 1_000_000)),
            "-headers",
            self._ffmpeg_headers(),
            "-i",
            live_url,
            "-c",
            "copy",
        ]

        if self.duration:
            command += ["-t", str(self.duration)]
        command.append(output)

        timeout = self.duration + 30 if self.duration else None
        return self._run_ffmpeg_process(
            command, attempt_id=attempt_id, timeout=timeout, segment=output
        )

    def _record_with_http_stream(
        self, live_url: str, output: str, room_id: str
    ) -> bool:
        """
        Record live stream using the HTTP streaming method.
        """
        buffer_size = 512 * 1024  # 512 KB buffer
        buffer = bytearray()

        logger.info("[PRESS CTRL + C ONCE TO STOP]")
        with open(output, "xb") as out_file:
            stop_recording = False
            interrupted = False
            while not stop_recording:
                try:
                    room_status = self._room_status(room_id)
                    if room_status is RoomStatus.OFFLINE:
                        logger.info("Live status confirms the stream has ended.")
                        break
                    if room_status is RoomStatus.UNKNOWN:
                        logger.warning(
                            "Live status is unknown after stream interruption; "
                            "ending this recording attempt."
                        )
                        break

                    start_time = time.time()
                    for chunk in self.tiktok.download_live_stream(live_url):
                        buffer.extend(chunk)
                        if len(buffer) >= buffer_size:
                            out_file.write(buffer)
                            buffer.clear()

                        elapsed_time = time.time() - start_time
                        if self.duration and elapsed_time >= self.duration:
                            stop_recording = True
                            break

                    if not stop_recording:
                        logger.info("Stream connection ended.")
                        stop_recording = True

                except ConnectionError:
                    logger.error("Stream connection closed during recording.")
                    stop_recording = True

                except (RequestException, HTTPException) as ex:
                    logger.warning(
                        "Stream request failed: %s", redact_urls_in_text(str(ex))
                    )
                    stop_recording = True

                except KeyboardInterrupt:
                    logger.info("Recording stopped by user.")
                    interrupted = True
                    stop_recording = True

                except OSError:
                    raise

                except Exception as ex:  # noqa: BLE001 - retain this partial artifact
                    logger.error(
                        "Unexpected error during recording: %s",
                        redact_urls_in_text(str(ex)),
                        exc_info=True,
                    )
                    stop_recording = True

                finally:
                    if buffer:
                        out_file.write(buffer)
                        buffer.clear()
                    out_file.flush()
        return interrupted

    def _get_stream_candidates(
        self,
        room_id: str,
        user: str,
        *,
        deadline: float,
        stale_urls: set[str],
    ):
        candidate_getter = getattr(self.tiktok, "get_live_stream_candidates", None)
        if callable(candidate_getter):
            raw_candidates = candidate_getter(
                room_id, user=user, deadline=deadline, stale_urls=stale_urls
            )
        else:
            candidate_getter = getattr(self.tiktok, "get_live_url_candidates", None)
            if callable(candidate_getter):
                raw_candidates = candidate_getter(
                    room_id, user=user, deadline=deadline
                )
            else:
                live_url = self.tiktok.get_live_url(
                    room_id, user=user, deadline=deadline
                )
                raw_candidates = [live_url] if live_url else []

        candidates = []
        for candidate in raw_candidates or []:
            if isinstance(candidate, str):
                url = candidate
                is_original = False
            else:
                url = getattr(candidate, "url", None)
                is_original = bool(getattr(candidate, "is_original_quality", False))
            if url:
                candidates.append((url, is_original))
        return candidates

    @staticmethod
    def _candidate_error(output: str, result: FFmpegResult | None = None) -> str:
        if result and result.timed_out:
            return "FFmpeg timed out while validating or recording the candidate"
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if lines:
            return redact_urls_in_text(lines[-1])
        return "FFmpeg exited without a diagnostic message"

    def _log_candidate_failure(
        self,
        *,
        url: str,
        is_original: bool,
        stage: str,
        output: str,
        fallback_reason: str,
        result: FFmpegResult | None = None,
        attempt_id: str | None = None,
        segment: str | None = None,
    ) -> None:
        status_code = self._http_status_code(output)
        logger.warning(
            "Stream candidate failed: quality=%s url=%s attempt=%s segment=%s "
            "stage=%s status=%s error=%s fallback=%s",
            "original" if is_original else "standard",
            redact_url(url),
            attempt_id or "unknown",
            segment or "probe",
            stage,
            status_code if status_code is not None else "unknown",
            self._candidate_error(output, result),
            fallback_reason,
        )

    def start_recording(self, user, room_id):
        """Select, verify, and record candidates until one yields valid video."""
        selection_deadline = time.monotonic() + SELECTION_TIMEOUT_SECONDS
        candidate_urls_tried = set()
        original_attempts = 0
        standard_recording_starts = 0
        stale_urls = set()
        latest_candidates = None
        attempt_number = 0
        max_selection_attempts = 50
        refresh_reason = "initial selection"
        original_fallback_logged = False

        while True:
            if attempt_number >= max_selection_attempts:
                raise TikTokRecorderError(
                    f"Stream selection reached its {max_selection_attempts}-attempt "
                    f"limit for @{user}; failed artifacts were retained."
                )
            if time.monotonic() < selection_deadline:
                logger.info("Refreshing stream candidates (%s).", refresh_reason)
                try:
                    candidates = self._get_stream_candidates(
                        room_id,
                        user,
                        deadline=selection_deadline,
                        stale_urls=stale_urls,
                    )
                except Exception as ex:
                    if time.monotonic() < selection_deadline:
                        raise
                    logger.warning(
                        "Original selection budget exhausted during candidate API "
                        "request: %s",
                        redact_urls_in_text(str(ex)),
                    )
                    if latest_candidates:
                        candidates = latest_candidates
                    else:
                        raise TikTokRecorderError(
                            f"Original selection budget exhausted while fetching "
                            f"candidates for @{user}."
                        ) from ex
                if candidates:
                    latest_candidates = candidates
                elif (
                    time.monotonic() >= selection_deadline and latest_candidates
                ):
                    logger.warning(
                        "Original selection budget exhausted before candidate API "
                        "request; using the most recent candidate list."
                    )
                    candidates = latest_candidates
            elif latest_candidates:
                logger.warning(
                    "Original selection budget exhausted; using the most recent "
                    "candidate list for standard-quality fallback."
                )
                candidates = latest_candidates
            else:
                raise TikTokRecorderError(
                    f"Original selection budget exhausted before candidates were "
                    f"available for @{user}."
                )

            if not candidates:
                raise TikTokRecorderError(
                    f"No stream candidates are available for @{user}."
                )

            selected = None
            for url, is_original in candidates:
                if url in stale_urls or url in candidate_urls_tried:
                    continue
                if is_original:
                    if original_attempts >= 2:
                        continue
                    if time.monotonic() >= selection_deadline:
                        continue
                    selected = (url, is_original)
                    break
                if standard_recording_starts < self.ffmpeg_retries:
                    selected = (url, is_original)
                    break

            if selected is None:
                if original_attempts and not original_fallback_logged:
                    logger.info(
                        "Falling back to standard stream candidates after the "
                        "original-quality candidate limit was reached."
                    )
                    original_fallback_logged = True
                raise TikTokRecorderError(
                    f"No candidate produced a verifiable video segment for @{user}; "
                    "failed artifacts were retained."
                )

            live_url, is_original = selected
            if (
                not is_original
                and original_attempts
                and not original_fallback_logged
            ):
                logger.info(
                    "Falling back to standard stream candidates after original "
                    "quality validation failed or its limit was reached."
                )
                original_fallback_logged = True
            quality = "original" if is_original else "standard"
            if is_original:
                original_attempts += 1
            candidate_urls_tried.add(live_url)

            attempt_number += 1
            attempt_id = f"{attempt_number:03d}"
            logger.info(
                "Validating %s stream candidate url=%s attempt=%s",
                quality,
                redact_url(live_url),
                attempt_id,
            )
            remaining_selection_time = selection_deadline - time.monotonic()
            probe_skipped_for_budget = remaining_selection_time <= 0
            if probe_skipped_for_budget and is_original:
                self._log_candidate_failure(
                    url=live_url,
                    is_original=True,
                    stage="stream probe",
                    output="original selection budget exhausted before FFmpeg probe",
                    fallback_reason="fall back to standard candidates",
                    attempt_id=attempt_id,
                )
                refresh_reason = "original selection budget exhausted"
                continue

            if probe_skipped_for_budget:
                logger.warning(
                    "Original selection budget exhausted; skipping the candidate "
                    "video probe and falling back to a recording attempt."
                )
                probe_result = FFmpegResult(0, "")
            else:
                probe_result = self._probe_stream_candidate(
                    live_url,
                    attempt_id,
                    timeout=remaining_selection_time,
                )

            if not probe_skipped_for_budget and is_original and (
                time.monotonic() >= selection_deadline
            ):
                self._log_candidate_failure(
                    url=live_url,
                    is_original=True,
                    stage="stream probe",
                    output=probe_result.output,
                    fallback_reason="original selection budget exhausted",
                    result=probe_result,
                    attempt_id=attempt_id,
                )
                refresh_reason = "original selection budget exhausted"
                continue

            if probe_result.returncode != 0 or probe_result.timed_out:
                status_code = self._http_status_code(probe_result.output)
                self._log_candidate_failure(
                    url=live_url,
                    is_original=is_original,
                    stage="stream probe",
                    output=probe_result.output,
                    fallback_reason=(
                        "refresh immediately after HTTP 404"
                        if status_code == 404
                        else "try the next refreshed candidate"
                    ),
                    result=probe_result,
                    attempt_id=attempt_id,
                )
                if status_code == 404:
                    stale_urls.add(live_url)
                    refresh_reason = "HTTP 404; stale candidate removed"
                else:
                    refresh_reason = f"{quality} stream probe failed"
                continue

            extension = ".flv" if self.use_ffmpeg else "_flv.mp4"
            output, output_attempt_id = self._allocate_output_path(user, extension)
            logger.info(
                "Starting recording attempt=%s candidate=%s output=%s",
                output_attempt_id,
                redact_url(live_url),
                output,
            )

            try:
                if not is_original:
                    standard_recording_starts += 1
                if self.use_ffmpeg:
                    record_result = self._record_with_ffmpeg(
                        live_url, output, output_attempt_id
                    )
                else:
                    interrupted = self._record_with_http_stream(
                        live_url, output, room_id
                    )
                    record_result = FFmpegResult(0, "", interrupted=interrupted)
            except OSError as ex:
                raise TikTokRecorderError(
                    f"Unable to write recording output {output}; check directory "
                    f"permissions and available disk space: {ex}"
                ) from ex

            status_code = self._http_status_code(record_result.output)
            write_error = self._output_write_error(record_result.output)
            if write_error:
                raise TikTokRecorderError(
                    f"Unable to retain recording output {output}: {write_error}. "
                    f"FFmpeg exited with code {record_result.returncode}."
                )
            if not Path(output).is_file():
                reason = self._candidate_error(record_result.output, record_result)
                self._log_candidate_failure(
                    url=live_url,
                    is_original=is_original,
                    stage="recording",
                    output=record_result.output,
                    fallback_reason=(
                        "refresh immediately after HTTP 404"
                        if status_code == 404
                        else "recording process did not create an output file"
                    ),
                    result=record_result,
                    attempt_id=output_attempt_id,
                    segment=output,
                )
                if status_code == 404:
                    stale_urls.add(live_url)
                    refresh_reason = "HTTP 404; stale candidate removed"
                else:
                    refresh_reason = f"recording failed: {reason}"
                if record_result.interrupted:
                    raise TikTokRecorderError(
                        f"Recording stopped before a file could be retained: {output}"
                    )
                continue

            final_output = output
            try:
                media_info = inspect_video_file(output)
                if not self.use_ffmpeg:
                    converted_output = VideoManagement.convert_flv_to_mp4(
                        output, self.bitrate
                    )
                    if not converted_output:
                        raise MediaValidationError(
                            "FFmpeg conversion did not produce a verified output"
                        )
                    final_output = converted_output
                    media_info = inspect_video_file(final_output)
                    try:
                        Path(output).unlink()
                    except OSError as ex:
                        logger.warning(
                            "Verified converted output %s; could not remove raw "
                            "intermediate %s: %s",
                            final_output,
                            output,
                            ex,
                        )
            except MediaValidationError as ex:
                self._log_candidate_failure(
                    url=live_url,
                    is_original=is_original,
                    stage="media verification",
                    output=str(ex),
                    fallback_reason="retain failed artifact and try the next candidate",
                    attempt_id=output_attempt_id,
                    segment=final_output,
                )
                status_code = status_code or self._http_status_code(str(ex))
                if status_code == 404:
                    stale_urls.add(live_url)
                    refresh_reason = "HTTP 404; stale candidate removed"
                else:
                    refresh_reason = "recording did not contain a decodable video frame"
                if record_result.interrupted:
                    raise TikTokRecorderError(
                        f"Recording stopped; invalid artifact retained at {output}: {ex}"
                    ) from ex
                continue
            except OSError as ex:
                raise TikTokRecorderError(
                    f"Unable to verify or retain recording output {output}: {ex}"
                ) from ex

            room_status = self._room_status(room_id)
            if room_status is RoomStatus.OFFLINE:
                end_reason = "confirmed live ended"
            elif room_status is RoomStatus.LIVE:
                end_reason = "stream connection ended while room remains live"
            else:
                end_reason = "stream connection ended; live status is unknown"

            if record_result.interrupted:
                logger.info("Recording stopped by user; valid segment retained.")
            elif record_result.returncode != 0 or record_result.timed_out:
                logger.warning(
                    "Recording ended abnormally: output=%s exit_code=%s timed_out=%s "
                    "status=%s reason=%s",
                    final_output,
                    record_result.returncode,
                    record_result.timed_out,
                    room_status.value,
                    end_reason,
                )
            else:
                logger.info("Recording connection ended: %s", end_reason)

            audio_info = ", ".join(media_info.audio_codecs) or "none"
            logger.info(
                "Verified video segment: %s | %s %sx%s | audio: %s",
                final_output,
                media_info.video_codec,
                media_info.width,
                media_info.height,
                audio_info,
            )

            if self.use_telegram:
                Telegram().upload(final_output)
            return final_output

    def check_country_blacklisted(self):
        is_blacklisted = self.tiktok.is_country_blacklisted()
        if not is_blacklisted:
            return False

        if self.room_id is None:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED)

        if self.mode == Mode.AUTOMATIC:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_AUTO_MODE)

        elif self.mode == Mode.FOLLOWERS:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_FOLLOWERS_MODE)

        return is_blacklisted
