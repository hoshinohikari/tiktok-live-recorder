import time
import subprocess
from http.client import HTTPException
from pathlib import Path
from threading import Thread

from requests import RequestException

from core.tiktok_api import TikTokAPI
from utils.logger_manager import logger
from utils.recorder_config import RecorderConfig
from utils.video_management import VideoManagement
from upload.telegram import Telegram
from utils.custom_exceptions import LiveNotFound, UserLiveError, TikTokRecorderError
from utils.enums import Mode, Error, TimeOut, TikTokError


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

    def _setup(self):
        """Resolve user/room data and validate prerequisites via network calls."""
        self.check_country_blacklisted()

        if self.mode == Mode.FOLLOWERS:
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

            logger.info(f"USERNAME: {self.user}" + ("\n" if not self.room_id else ""))
            if self.room_id:
                logger.info(
                    f"ROOM_ID:  {self.room_id}"
                    + ("\n" if not self.tiktok.is_room_alive(self.room_id) else "")
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
        if not self.tiktok.is_room_alive(self.room_id):
            raise UserLiveError(f"@{self.user}: {TikTokError.USER_NOT_CURRENTLY_LIVE}")

        self.start_recording(self.user, self.room_id)

    def automatic_mode(self):
        while True:
            try:
                self.room_id = self.tiktok.get_room_id_from_user(self.user)
                self.manual_mode()

            except (UserLiveError, LiveNotFound) as ex:
                logger.info(ex)
                logger.info(
                    f"Waiting {self.automatic_interval} seconds before recheck\n"
                )
                time.sleep(self.automatic_interval)

            except ConnectionError:
                logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                time.sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)

            except Exception as ex:
                logger.error(f"Unexpected error: {ex}", exc_info=True)
                continue

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

                        if not room_id or not self.tiktok.is_room_alive(room_id):
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

                    except Exception as e:
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

            except Exception as ex:
                logger.error(f"Unexpected error: {ex}", exc_info=True)
                continue

    def _build_output_path(self, user: str, ext: str = "_flv.mp4") -> str:
        filename = (
            f"TK_{user}_{time.strftime('%Y.%m.%d_%H-%M-%S', time.localtime())}{ext}"
        )
        if self.output:
            return str(Path(self.output) / filename)
        return filename

    def _record_with_ffmpeg(self, live_url: str, output: str, room_id: str) -> None:
        """
        Record live stream using ffmpeg with retry and timeout handling.
        """
        rw_timeout_us = int(self.ffmpeg_rw_timeout * 1_000_000)
        
        headers = (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.127 Safari/537.36\r\n"
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,application/json,text/plain,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7\r\n"
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
            "Origin: https://www.tiktok.com\r\n"
            "Cookie: sessionid_ss=; tt-target-idc=useast2a\r\n"
        )

        base_command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-rw_timeout",
            str(rw_timeout_us),
            "-headers",
            headers,
            "-i",
            live_url,
            "-c",
            "copy",
        ]

        if self.duration:
            base_command += ["-t", str(self.duration)]

        base_command.append(output)

        for attempt in range(1, self.ffmpeg_retries + 1):
            if not self.tiktok.is_room_alive(room_id):
                logger.info("User is no longer live. Stopping recording.")
                return

            logger.info(f"FFmpeg recording attempt {attempt}/{self.ffmpeg_retries}...")

            try:
                timeout = self.duration + 30 if self.duration else None
                result = subprocess.run(
                    base_command,
                    check=False,
                    timeout=timeout,
                )

                if result.returncode == 0:
                    return

                logger.error(
                    f"FFmpeg exited with code {result.returncode}. Retrying..."
                )
            except subprocess.TimeoutExpired:
                logger.error("FFmpeg recording timed out. Retrying...")
            except FileNotFoundError:
                raise TikTokRecorderError(
                    "FFmpeg not found. Please ensure ffmpeg is installed and in PATH."
                )
            except Exception as ex:
                logger.error(f"Unexpected ffmpeg error: {ex}. Retrying...")

            if attempt < self.ffmpeg_retries:
                time.sleep(self.ffmpeg_retry_delay)

        raise TikTokRecorderError("FFmpeg failed after multiple attempts.")

    def _record_with_http_stream(
        self, live_url: str, output: str, room_id: str
    ) -> None:
        """
        Record live stream using the HTTP streaming method.
        """
        buffer_size = 512 * 1024  # 512 KB buffer
        buffer = bytearray()

        logger.info("[PRESS CTRL + C ONCE TO STOP]")
        with open(output, "wb") as out_file:
            stop_recording = False
            while not stop_recording:
                try:
                    if not self.tiktok.is_room_alive(room_id):
                        logger.info("User is no longer live. Stopping recording.")
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

                except ConnectionError:
                    if self.mode == Mode.AUTOMATIC:
                        logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                        time.sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)

                except (RequestException, HTTPException) as ex:
                    logger.warning(f"Network hiccup, retrying: {ex}")
                    time.sleep(2)

                except KeyboardInterrupt:
                    logger.info("Recording stopped by user.")
                    stop_recording = True

                except Exception as ex:
                    logger.error(
                        f"Unexpected error during recording: {ex}",
                        exc_info=True,
                    )
                    stop_recording = True

                finally:
                    if buffer:
                        out_file.write(buffer)
                        buffer.clear()
                    out_file.flush()

    def start_recording(self, user, room_id):
        """
        Start recording live
        """
        live_url = self.tiktok.get_live_url(room_id)
        if not live_url:
            raise LiveNotFound(TikTokError.RETRIEVE_LIVE_URL)

        if self.use_ffmpeg:
            output = self._build_output_path(user, ext=".flv")
        else:
            output = self._build_output_path(user, ext="_flv.mp4")

        if self.duration:
            logger.info(f"Started recording for {self.duration} seconds ")
        else:
            logger.info("Started recording...")

        try:
            if self.use_ffmpeg:
                self._record_with_ffmpeg(live_url, output, room_id)
            else:
                self._record_with_http_stream(live_url, output, room_id)
        except KeyboardInterrupt:
            logger.info("Recording stopped by user.")

        logger.info(f"Recording finished: {output}\n")

        if self.use_ffmpeg:
            final_output = output
        else:
            VideoManagement.convert_flv_to_mp4(output, self.bitrate)
            final_output = output.replace("_flv.mp4", ".mp4")

        if self.use_telegram:
            Telegram().upload(final_output)

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
