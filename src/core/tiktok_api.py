import html
import json
import re
import time
from dataclasses import dataclass

from http_utils.http_client import HttpClient
from utils.custom_exceptions import (
    LiveNotFound,
    TikRecUnavailableError,
    TikTokRecorderError,
    UserLiveError,
)
from utils.enums import RoomStatus, StatusCode, TikTokError
from utils.logger_manager import logger
from utils.url_sanitizer import redact_url, redact_urls_in_text


@dataclass(frozen=True)
class LiveStreamCandidate:
    url: str
    is_original_quality: bool = False


class TikTokAPI:
    REQUEST_TIMEOUT = (5, 20)  # connect and read timeouts in seconds
    MAX_SELECTION_HTTP_TIMEOUT_SECONDS = 5.0

    def __init__(self, proxy, cookies):
        self.BASE_URL = "https://www.tiktok.com"
        self.WEBCAST_URL = "https://webcast.tiktok.com"
        self.API_URL = "https://www.tiktok.com/api-live/user/room/"
        self.EULER_API = "https://tiktok.eulerstream.com"
        self.TIKREC_API = "https://tikrec.com"

        self.http_client = HttpClient(proxy, cookies).req
        self._http_client_stream = HttpClient(proxy, cookies).req_stream

    def _is_authenticated(self) -> bool:
        response = self.http_client.get(f"{self.BASE_URL}/foryou")
        response.raise_for_status()

        content = response.text
        return "login-title" not in content

    def is_country_blacklisted(self) -> bool:
        """
        Checks if the user is in a blacklisted country that requires login
        """
        response = self.http_client.get(f"{self.BASE_URL}/live", allow_redirects=False)

        return response.status_code == StatusCode.REDIRECT

    def is_room_alive(self, room_id: str) -> bool:
        """
        Checking whether the user is live.
        """
        return self._query_room_status(room_id) is RoomStatus.LIVE

    def get_room_status(self, room_id: str) -> RoomStatus:
        """Return live, confirmed offline, or unknown when status is unavailable."""
        try:
            return self._query_room_status(room_id)
        except Exception as ex:  # noqa: BLE001 - failed queries mean unknown status
            logger.warning(
                "Live status check failed; status is unknown: %s",
                redact_urls_in_text(str(ex)),
            )
            return RoomStatus.UNKNOWN

    def _query_room_status(self, room_id: str) -> RoomStatus:
        if not room_id:
            raise UserLiveError(TikTokError.USER_NOT_CURRENTLY_LIVE)

        alive_data = self.http_client.get(
            f"{self.WEBCAST_URL}/webcast/room/check_alive/"
            f"?aid=1988&region=CH&room_ids={room_id}&user_is_login=true",
            timeout=self.REQUEST_TIMEOUT,
        ).json()

        data_list = alive_data.get("data")
        if (
            not isinstance(data_list, list)
            or not data_list
            or not isinstance(data_list[0], dict)
        ):
            return RoomStatus.UNKNOWN

        alive_value = data_list[0].get("alive")
        if alive_value is False:
            return RoomStatus.OFFLINE
        if alive_value is not True:
            return RoomStatus.UNKNOWN

        room_info = self.http_client.get(
            f"{self.WEBCAST_URL}/webcast/room/info/?aid=1988&room_id={room_id}",
            timeout=self.REQUEST_TIMEOUT,
        ).json()

        status_code = room_info.get("status_code", 0)
        if status_code == 4003110:
            return RoomStatus.LIVE

        if status_code != 0:
            return RoomStatus.UNKNOWN

        room_data = room_info.get("data")
        if not isinstance(room_data, dict):
            return RoomStatus.UNKNOWN

        room_status = room_data.get("status")
        if room_status is not None and str(room_status) != "2":
            return RoomStatus.OFFLINE

        stream_url = room_data.get("stream_url") or {}
        if not isinstance(stream_url, dict):
            return RoomStatus.UNKNOWN

        sdk_data = stream_url.get("live_core_sdk_data") or {}
        if not isinstance(sdk_data, dict):
            return RoomStatus.UNKNOWN
        pull_data = sdk_data.get("pull_data") or {}
        if not isinstance(pull_data, dict):
            return RoomStatus.UNKNOWN
        sdk_stream_data = (
            pull_data.get("stream_data")
        )

        def has_stream_url(value):
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, dict):
                return any(has_stream_url(item) for item in value.values())
            return False

        if (
            sdk_stream_data
            or has_stream_url(stream_url.get("flv_pull_url"))
            or has_stream_url(stream_url.get("hls_pull_url"))
            or has_stream_url(stream_url.get("hls_pull_url_map"))
            or has_stream_url(stream_url.get("rtmp_pull_url"))
        ):
            return RoomStatus.LIVE
        return RoomStatus.UNKNOWN

    def get_sec_uid(self):
        """
        Returns the sec_uid of the authenticated user.
        """
        response = self.http_client.get(f"{self.BASE_URL}/foryou")

        sec_uid = re.search('"secUid":"(.*?)",', response.text)
        if sec_uid:
            sec_uid = sec_uid.group(1)

        return sec_uid

    def get_user_from_room_id(self, room_id) -> str:
        """
        Given a room_id, I get the username
        """
        data = self.http_client.get(
            f"{self.WEBCAST_URL}/webcast/room/info/?aid=1988&room_id={room_id}",
            timeout=5,
        ).json()

        if "Follow the creator to watch their LIVE" in json.dumps(data):
            raise UserLiveError(TikTokError.ACCOUNT_PRIVATE_FOLLOW)

        if "This account is private" in data:
            raise UserLiveError(TikTokError.ACCOUNT_PRIVATE)

        display_id = data.get("data", {}).get("owner", {}).get("display_id")
        if display_id is None:
            raise TikTokRecorderError(TikTokError.USERNAME_ERROR)

        return display_id

    def get_room_and_user_from_url(self, live_url: str):
        """
        Given a url, get user and room_id.
        """
        response = self.http_client.get(live_url, allow_redirects=False)
        content = response.text

        if response.status_code == StatusCode.REDIRECT:
            raise UserLiveError(TikTokError.COUNTRY_BLACKLISTED)

        if response.status_code == StatusCode.MOVED:  # MOBILE URL
            matches = re.findall("com/@(.*?)/live", content)
            if len(matches) < 1:
                raise LiveNotFound(TikTokError.INVALID_TIKTOK_LIVE_URL)

            user = matches[0]

        # https://www.tiktok.com/@<username>/live
        match = re.match(r"https?://(?:www\.)?tiktok\.com/@([^/]+)/live", live_url)
        if match:
            user = match.group(1)

        room_id = self.get_room_id_from_user(user)

        return user, room_id

    def _old_get_room_id_from_user(self, user: str) -> str:
        params = {"uniqueId": user, "giftInfo": "false"}

        response = self.http_client.get(
            f"{self.EULER_API}/webcast/room_info",
            params=params,
            headers={"x-api-key": ""},
            timeout=self.REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            raise UserLiveError(TikTokError.ROOM_ID_ERROR)

        data = response.json()

        room_id = data.get("data", {}).get("room_info", {}).get("id")
        if not room_id:
            raise UserLiveError(TikTokError.ROOM_ID_ERROR)

        return room_id

    def _tikrec_get_room_id_signed_url(self, user: str) -> str:
        try:
            response = self.http_client.get(
                f"{self.TIKREC_API}/tiktok/room/api/sign",
                params={"unique_id": user},
                timeout=self.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except Exception as e:  # Translate signing failures to a domain error.
            raise TikRecUnavailableError(
                f"tikrec signing service is unreachable: {e}"
            ) from e

        try:
            data = response.json()
        except ValueError as e:
            raise TikRecUnavailableError(
                "tikrec signing service returned an invalid response "
                "(expected JSON, got something else — the service may be down)."
            ) from e

        signed_path = data.get("signed_path")
        if not signed_path:
            raise TikRecUnavailableError(
                "tikrec signing service did not return a signed_path "
                "(the service may be down or overloaded)."
            )

        return f"{self.BASE_URL}{signed_path}"

    def get_room_id_from_user(self, user: str) -> str | None:
        """Given a username, get the room_id."""
        try:
            signed_url = self._tikrec_get_room_id_signed_url(user)
        except TikRecUnavailableError as e:
            logger.warning(
                f"[!] tikrec is unavailable ({e}). "
                "Falling back to unsigned API — recording continues but may be less reliable."
            )
            return self._old_get_room_id_from_user(user)

        response = self.http_client.get(signed_url, timeout=self.REQUEST_TIMEOUT)
        content = response.text

        if not content or "Please wait" in content:
            raise UserLiveError(TikTokError.WAF_BLOCKED)

        data = response.json()
        return (data.get("data") or {}).get("user", {}).get("roomId")

    def get_followers_list(self, sec_uid) -> list:
        """
        Returns all followers for the authenticated user by paginating
        """
        followers = []
        cursor = 0
        has_more = True

        ms_token = self.http_client.get(
            f"{self.BASE_URL}/api/user/list/?"
            "WebIdLastTime=1747672102&aid=1988&app_language=it-IT&app_name=tiktok_web&"
            "browser_language=it-IT&browser_name=Mozilla&browser_online=true&"
            "browser_platform=Linux%20x86_64&"
            "browser_version=5.0%20%28X11%3B%20Linux%20x86_64%29%20AppleWebKit%2F537.36%20%28KHTML%2C%20like%20Gecko%29%20Chrome%2F140.0.0.0%20Safari%2F537.36&"
            "channel=tiktok_web&cookie_enabled=true&count=5&data_collection_enabled=true&"
            "device_id=7506194516308166166&device_platform=web_pc&focus_state=true&"
            "from_page=user&history_len=3&is_fullscreen=false&is_page_visible=true&"
            "maxCursor=0&minCursor=0&odinId=7246312836442604570&os=linux&priority_region=IT&"
            "referer=&region=IT&root_referer=https%3A%2F%2Fwww.tiktok.com%2Flive&scene=21&"
            "screen_height=1080&screen_width=1920&tz_name=Europe%2FRome&user_is_login=true&"
            "verifyFp=verify_mh4yf0uq_rdjp1Xwt_OoTk_4Jrf_AS8H_sp31opbnJFre&webcast_language=it-IT&"
            "msToken=GphHoLvRR4QxA5AWVwDkrs3AbumoK5H8toE8LVHtj6cce3ToGdXhMfvDWzOXG-0GXUWoaGVHrwGNA4k_NnjuFFnHgv2S5eMjsvtkAhwMPa13xLmvP7tumx0KreFjPwTNnOj-BvAkPdO5Zrev3hoFBD9lHVo=&X-Bogus=&X-Gnarly="
        ).cookies["msToken"]

        while has_more:
            url = (
                "https://www.tiktok.com/api/user/list/?"
                "WebIdLastTime=1747672102&aid=1988&app_language=it-IT&app_name=tiktok_web"
                "&browser_language=it-IT&browser_name=Mozilla&browser_online=true"
                "&browser_platform=Linux%20x86_64&browser_version=5.0%20%28X11%3B%20Linux%20x86_64%29%20AppleWebKit%2F537.36%20%28KHTML%2C%20like%20Gecko%29%20Chrome%2F140.0.0.0%20Safari%2F537.36&channel=tiktok_web&"
                "cookie_enabled=true&count=5&data_collection_enabled=true&device_id=7506194516308166166"
                "&device_platform=web_pc&focus_state=true&from_page=user&history_len=3&"
                f"is_fullscreen=false&is_page_visible=true&maxCursor={cursor}&minCursor={cursor}&"
                "odinId=7246312836442604570&os=linux&priority_region=IT&referer=&"
                "region=IT&scene=21&screen_height=1080&screen_width=1920"
                "&tz_name=Europe%2FRome&user_is_login=true&"
                f"secUid={sec_uid}&verifyFp=verify_mh4yf0uq_rdjp1Xwt_OoTk_4Jrf_AS8H_sp31opbnJFre&"
                f"webcast_language=it-IT&msToken={ms_token}&X-Bogus=&X-Gnarly="
            )

            response = self.http_client.get(url)

            if response.status_code != StatusCode.OK:
                raise TikTokRecorderError("Failed to retrieve followers list.")

            if not response.content:
                raise TikTokRecorderError("Empty response from TikTok followers API.")

            data = response.json()
            user_list = data.get("userList", [])

            for user in user_list:
                username = user.get("user", {}).get("uniqueId")
                if username:
                    followers.append(username)

            has_more = data.get("hasMore", False)
            new_cursor = data.get("minCursor", 0)

            if new_cursor == cursor:
                break

            cursor = new_cursor

        if not followers:
            raise TikTokRecorderError("Followers list is empty.")

        return followers

    def _derive_original_quality_url(
        self, sdk_data: dict, *, stale_urls: set[str] | None = None
    ) -> str | None:
        """Derive the original-quality URL; the recorder validates its media."""
        ao_flv: str = sdk_data.get("ao", {}).get("main", {}).get("flv", "")
        if not ao_flv or "only_audio=1" not in ao_flv:
            return None

        # Strip the audio-only marker to reveal the original stream URL.
        original_url = (
            ao_flv
            .replace("&only_audio=1", "")
            .replace("?only_audio=1&", "?")
            .replace("?only_audio=1", "")
        )

        if stale_urls is not None and original_url in stale_urls:
            logger.debug(
                "Skipping previously failed original-quality URL: %s",
                redact_url(original_url),
            )
            return None

        logger.debug("Derived original-quality candidate: %s", redact_url(original_url))
        return original_url

    def _get_selection_response(self, url: str, timeout: float):
        """Open a selection response with the strongest backend timeout available."""
        if type(self.http_client).__module__.startswith("curl_cffi."):
            # curl_cffi's scalar timeout is a total CURLOPT_TIMEOUT_MS when the
            # response is buffered. Streaming changes it to a low-speed timeout.
            return self.http_client.get(url, timeout=timeout)

        request_timeout = (
            min(timeout, self.MAX_SELECTION_HTTP_TIMEOUT_SECONDS),
            min(timeout, 1),
        )
        return self.http_client.get(url, stream=True, timeout=request_timeout)

    @staticmethod
    def _read_response_body(
        response, *, deadline: float | None = None
    ) -> bytes | None:
        """Read a streamed response while checking the selection deadline.

        Requests' read timeout is an idle timeout between bytes, not a total
        transfer limit. Its bodies are consumed one byte at a time and checked
        against the deadline; a one-second read timeout bounds a stalled read.
        curl_cffi responses are already fully buffered by a request with a total
        timeout. Requests can still spend longer than the budget parsing
        slow-trickled response headers before returning the Response object.
        """
        body = bytearray()
        chunk_size = 1 if deadline is not None else 64 * 1024
        try:
            if type(response).__module__.startswith("curl_cffi."):
                body = response.content
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                return body

            chunks = response.iter_content(chunk_size=chunk_size)

            if deadline is not None and time.monotonic() >= deadline:
                return None

            for chunk in chunks:
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                if chunk:
                    body.extend(chunk)

            if deadline is not None and time.monotonic() >= deadline:
                return None
            return bytes(body)
        finally:
            response.close()

    def _get_stream_url_from_page(
        self, user: str, *, deadline: float | None = None
    ) -> str | None:
        """
        Fallback: fetch the live page HTML and extract the stream URL directly.
        Used when the webcast API returns status code 4003110 (WAF/access restriction).
        """
        try:
            live_page_url = f"{self.BASE_URL}/@{user}/live"
            timeout = self.MAX_SELECTION_HTTP_TIMEOUT_SECONDS
            if deadline is not None:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    logger.warning(
                        "Original selection budget exhausted before live-page fallback request."
                    )
                    return None
                timeout = min(timeout, self.MAX_SELECTION_HTTP_TIMEOUT_SECONDS)
            response = self._get_selection_response(live_page_url, timeout)
            content_bytes = self._read_response_body(response, deadline=deadline)
            if content_bytes is None:
                logger.warning(
                    "Original selection budget exhausted while reading the "
                    "live-page fallback response."
                )
                return None

            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    "Original selection budget exhausted before parsing the "
                    "live-page fallback response."
                )
                return None

            content = content_bytes.decode(
                getattr(response, "encoding", None) or "utf-8", errors="replace"
            )

            flv_matches = re.findall(r'https?://[^\s"\'<>]+\.flv[^\s"\'<>]*', content)
            if flv_matches:
                # Prefer original (_or4) or SD quality
                for url in flv_matches:
                    url = html.unescape(url.rstrip("\\"))
                    if "_or4" in url or "_sd" in url:
                        logger.info(
                            "Found stream URL from page: %s...",
                            redact_url(url)[:80],
                        )
                        return url
                return html.unescape(flv_matches[0].rstrip("\\"))

            hls_matches = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', content)
            if hls_matches:
                return html.unescape(hls_matches[0].rstrip("\\"))

            return None
        except Exception as e:  # noqa: BLE001 - page parsing/network failures are optional
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    "Original selection budget exhausted during live-page fallback request."
                )
            logger.warning(
                "Failed to extract stream URL from page: %s",
                redact_urls_in_text(str(e)),
            )
            return None

    def _add_live_url_candidate(self, candidates: list[str], url: str | None) -> None:
        if url and url not in candidates:
            candidates.append(url)

    def get_live_stream_candidates(
        self,
        room_id: str,
        user: str | None = None,
        *,
        deadline: float | None = None,
        stale_urls: set[str] | None = None,
    ) -> list[LiveStreamCandidate]:
        """
        Return candidate CDN URLs (flv or m3u8) for the streaming.
        If the API returns status code 4003110 and a username is provided,
        falls back to scraping the live page directly.

        The recorder shares ``stale_urls`` across refreshes so original-quality
        URLs that return HTTP 404 are skipped for the rest of the recording round.
        """
        return self._get_live_stream_candidates(
            room_id, user, deadline=deadline, stale_urls=stale_urls
        )

    def _get_live_stream_candidates(
        self,
        room_id: str,
        user: str | None,
        *,
        deadline: float | None = None,
        stale_urls: set[str] | None = None,
    ) -> list[LiveStreamCandidate]:
        if stale_urls is None:
            stale_urls = set()

        timeout = self.MAX_SELECTION_HTTP_TIMEOUT_SECONDS
        if deadline is not None:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                logger.warning(
                    "Original selection budget exhausted before candidate API request."
                )
                return []
            timeout = min(timeout, self.MAX_SELECTION_HTTP_TIMEOUT_SECONDS)

        response = self._get_selection_response(
            f"{self.WEBCAST_URL}/webcast/room/info/?aid=1988&room_id={room_id}",
            timeout,
        )
        response_body = self._read_response_body(response, deadline=deadline)
        if response_body is None:
            logger.warning(
                "Original selection budget exhausted while reading the candidate "
                "API response."
            )
            return []

        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(
                "Original selection budget exhausted before parsing the candidate "
                "API response."
            )
            return []

        data = json.loads(response_body)

        if "This account is private" in data:
            raise UserLiveError(TikTokError.ACCOUNT_PRIVATE)

        status_code = data.get("status_code", 0)

        if status_code == 4003110:
            if user:
                logger.info(
                    "API blocked by WAF (4003110). Trying fallback: extract stream URL from live page..."
                )
                fallback_url = self._get_stream_url_from_page(
                    user, deadline=deadline
                )
                if fallback_url:
                    return [LiveStreamCandidate(fallback_url)]

            raise UserLiveError(TikTokError.LIVE_RESTRICTION)

        room_data = data.get("data") or {}
        room_status = room_data.get("status")
        if room_status is not None and str(room_status) != "2":
            raise UserLiveError(TikTokError.USER_NOT_CURRENTLY_LIVE)

        stream_url = room_data.get("stream_url") or {}
        if not isinstance(stream_url, dict):
            return []

        sdk_data_str = (
            stream_url.get("live_core_sdk_data", {})
            .get("pull_data", {})
            .get("stream_data")
        )
        candidates = []
        if not sdk_data_str:
            logger.warning(
                "No SDK stream data found. Falling back to legacy URLs. Consider contacting the developer to update the code."
            )
            flv_pull_url = stream_url.get("flv_pull_url", {})
            for key in ("FULL_HD1", "HD1", "SD2", "SD1"):
                self._add_live_url_candidate(candidates, flv_pull_url.get(key))
            self._add_live_url_candidate(candidates, stream_url.get("hls_pull_url"))
            self._add_live_url_candidate(candidates, stream_url.get("rtmp_pull_url"))
            return [LiveStreamCandidate(url) for url in candidates]

        # Extract stream options
        sdk_data = json.loads(sdk_data_str).get("data", {})
        qualities = (
            stream_url.get("live_core_sdk_data", {})
            .get("pull_data", {})
            .get("options", {})
            .get("qualities", [])
        )
        if not qualities:
            logger.warning("No qualities found in the stream data. Returning None.")
            return []
        level_map = {q["sdk_key"]: q["level"] for q in qualities}

        # Local feature: probe the hidden original-quality stream first.
        original_url = self._derive_original_quality_url(
            sdk_data, stale_urls=stale_urls
        )
        if original_url:
            self._add_live_url_candidate(candidates, original_url)

        ordered_sdk_keys = sorted(
            sdk_data.keys(), key=lambda key: level_map.get(key, -1), reverse=True
        )
        for sdk_key in ordered_sdk_keys:
            entry = sdk_data[sdk_key]
            stream_main = entry.get("main", {})
            flv_url = stream_main.get("flv")
            if not (flv_url and "only_audio=1" in flv_url):
                self._add_live_url_candidate(candidates, flv_url)
            self._add_live_url_candidate(
                candidates, stream_main.get("hls") or stream_main.get("m3u8")
            )

        flv_pull_url = stream_url.get("flv_pull_url", {})
        for key in ("FULL_HD1", "HD1", "SD2", "SD1"):
            self._add_live_url_candidate(candidates, flv_pull_url.get(key))
        self._add_live_url_candidate(candidates, stream_url.get("hls_pull_url"))
        self._add_live_url_candidate(candidates, stream_url.get("rtmp_pull_url"))

        resolved_candidates = [
            LiveStreamCandidate(
                url=url,
                is_original_quality=bool(original_url and url == original_url),
            )
            for url in candidates
        ]

        return resolved_candidates

    def get_live_urls(
        self,
        room_id: str,
        user: str | None = None,
        *,
        deadline: float | None = None,
    ) -> list[str]:
        """Return candidate URLs while preserving the legacy string-list API."""
        return [
            candidate.url
            for candidate in self.get_live_stream_candidates(
                room_id, user=user, deadline=deadline
            )
        ]

    def get_live_url(
        self,
        room_id: str,
        user: str | None = None,
        *,
        deadline: float | None = None,
    ) -> str | None:
        """Return the first candidate CDN URL for the streaming."""
        live_urls = self.get_live_urls(room_id, user=user, deadline=deadline)
        if live_urls:
            return live_urls[0]
        return None

    def get_live_url_candidates(
        self,
        room_id: str,
        user: str | None = None,
        *,
        deadline: float | None = None,
    ) -> list[str]:
        """Return candidate CDN URLs for the streaming."""
        return self.get_live_urls(room_id, user=user, deadline=deadline)

    def download_live_stream(self, live_url: str):
        """Generator that returns the live stream for a given room_id."""
        stream = self._http_client_stream.get(live_url, stream=True)
        for chunk in stream.iter_content(chunk_size=4096):
            if chunk:
                yield chunk
