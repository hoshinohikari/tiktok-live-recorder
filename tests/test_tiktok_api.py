import json
import sys
import time as real_time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import RequestException as CurlRequestException
from requests.exceptions import ConnectionError as RequestsConnectionError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.tiktok_api import TikTokAPI
from http_utils.http_client import HttpClient
from utils.custom_exceptions import TikRecUnavailableError, UserLiveError
from utils.enums import RoomStatus


class FakeResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def json(self):
        return self._data

    def iter_content(self, chunk_size):
        body = json.dumps(self._data).encode()
        for offset in range(0, len(body), chunk_size):
            yield body[offset : offset + chunk_size]

    def close(self):
        pass

    @property
    def text(self):
        return json.dumps(self._data)

    def raise_for_status(self):
        pass


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []
        self.request_kwargs = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        self.request_kwargs.append(kwargs)
        return FakeResponse(self.responses.pop(0))


def build_api(*responses):
    api = TikTokAPI.__new__(TikTokAPI)
    api.WEBCAST_URL = "https://webcast.tiktok.com"
    api.http_client = FakeHttpClient(list(responses))
    return api


def test_default_curl_backend_total_timeout_bounds_slow_response_headers(
    monkeypatch,
):
    monkeypatch.delenv("TIKTOK_HTTP_BACKEND", raising=False)
    monkeypatch.setattr("http_utils.http_client.is_termux", lambda: False)
    client = HttpClient(proxy=None, cookies={})
    api = TikTokAPI.__new__(TikTokAPI)
    api.http_client = client.req
    assert type(api.http_client).__module__.startswith("curl_cffi.")

    headers_started = Event()
    headers_completed = Event()
    handler_finished = Event()

    class SlowHeadersHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            headers_started.set()
            response = (
                b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n"
                b"Connection: close\r\n\r\n"
            )
            try:
                for byte in response:
                    self.connection.sendall(bytes([byte]))
                    real_time.sleep(0.02)
                headers_completed.set()
            except OSError:
                pass
            finally:
                handler_finished.set()

    class SlowHeadersServer(ThreadingHTTPServer):
        daemon_threads = True

    server = SlowHeadersServer(("127.0.0.1", 0), SlowHeadersHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    started = real_time.monotonic()
    try:
        with pytest.raises(CurlRequestException, match="timed out"):
            api._get_selection_response(
                f"http://127.0.0.1:{server.server_address[1]}/", timeout=0.25
            )
        elapsed = real_time.monotonic() - started
        assert elapsed < 0.8
        assert headers_started.is_set()
        assert not headers_completed.is_set()
    finally:
        client.req.close()
        client.req_stream.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        handler_finished.wait(timeout=2)


def test_is_room_alive_rejects_fake_check_alive_positive():
    api = build_api(
        {"data": [{"alive": True, "room_id": 123}], "status_code": 0},
        {"data": {"message": "Request params error"}, "status_code": 10011},
    )

    assert api.is_room_alive("123") is False


def test_is_room_alive_accepts_confirmed_stream_room():
    api = build_api(
        {"data": [{"alive": True, "room_id": 123}], "status_code": 0},
        {
            "data": {
                "status": 2,
                "stream_url": {
                    "live_core_sdk_data": {"pull_data": {"stream_data": '{"data": {}}'}}
                },
            },
            "status_code": 0,
        },
    )

    assert api.is_room_alive("123") is True


def test_live_checks_use_a_timeout_for_both_requests():
    api = build_api(
        {"data": [{"alive": True}]},
        {"data": {"status": 2, "stream_url": {"flv_pull_url": {"HD1": "url"}}}},
    )

    assert api.is_room_alive("123") is True
    assert len(api.http_client.request_kwargs) == 2
    assert all(kwargs.get("timeout") for kwargs in api.http_client.request_kwargs)


def test_room_lookup_uses_a_timeout_for_signing_and_user_requests():
    api = build_api(
        {"signed_path": "/@creator/live"},
        {"data": {"user": {"roomId": "123"}}},
    )
    api.BASE_URL = "https://www.tiktok.com"
    api.TIKREC_API = "https://tikrec.com"

    assert api.get_room_id_from_user("creator") == "123"
    assert len(api.http_client.request_kwargs) == 2
    assert all(kwargs.get("timeout") for kwargs in api.http_client.request_kwargs)


def test_room_lookup_fallback_uses_a_timeout(monkeypatch):
    api = build_api({"data": {"room_info": {"id": "123"}}})
    api.EULER_API = "https://tiktok.eulerstream.com"

    def unavailable(_user):
        raise TikRecUnavailableError("signing service unavailable")

    monkeypatch.setattr(api, "_tikrec_get_room_id_signed_url", unavailable)

    assert api.get_room_id_from_user("creator") == "123"
    assert api.http_client.request_kwargs[0].get("timeout")


def test_is_room_alive_keeps_restricted_live_as_alive():
    api = build_api(
        {"data": [{"alive": True, "room_id": 123}], "status_code": 0},
        {"data": {}, "status_code": 4003110},
    )

    assert api.is_room_alive("123") is True


def test_is_room_alive_skips_room_info_when_check_alive_is_false():
    api = build_api({"data": [{"alive": False, "room_id": 123}], "status_code": 0})

    assert api.is_room_alive("123") is False
    assert len(api.http_client.urls) == 1


def test_room_status_reports_unknown_when_status_request_fails():
    api = build_api()

    def fail_request(url, **kwargs):
        raise RequestsConnectionError("connection reset")

    api.http_client.get = fail_request

    assert api.get_room_status("123") is RoomStatus.UNKNOWN


def test_room_status_distinguishes_confirmed_offline_from_live():
    offline_api = build_api({"data": [{"alive": False}]})
    live_api = build_api(
        {"data": [{"alive": True}]},
        {"data": {"status": 2, "stream_url": {"flv_pull_url": {"HD1": "url"}}}},
    )

    assert offline_api.get_room_status("123") is RoomStatus.OFFLINE
    assert live_api.get_room_status("123") is RoomStatus.LIVE


def test_room_status_does_not_confirm_live_from_empty_stream_urls():
    api = build_api(
        {"data": [{"alive": True}]},
        {
            "data": {
                "status": 2,
                "stream_url": {"flv_pull_url": {"HD1": ""}},
            }
        },
    )

    assert api.get_room_status("123") is RoomStatus.UNKNOWN


def test_is_room_alive_rejects_null_check_alive_data():
    api = build_api({"data": None, "status_code": 0})

    assert api.is_room_alive("123") is False
    assert len(api.http_client.urls) == 1


def test_is_room_alive_rejects_null_room_info_data():
    api = build_api(
        {"data": [{"alive": True, "room_id": 123}], "status_code": 0},
        {"data": None, "status_code": 0},
    )

    assert api.is_room_alive("123") is False


def test_is_room_alive_rejects_ended_room_with_stale_stream_urls():
    api = build_api(
        {"data": [{"alive": True, "room_id": 123}], "status_code": 0},
        {
            "data": {
                "status": 4,
                "finish_time": 1784118433,
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {"stream_data": '{"data": {}}'}
                    },
                    "flv_pull_url": {"HD1": "https://example.com/stale.flv"},
                },
            },
            "status_code": 0,
        },
    )

    assert api.is_room_alive("123") is False


def test_get_live_url_rejects_ended_room_with_stale_stream_urls():
    api = build_api(
        {
            "data": {
                "status": 4,
                "finish_time": 1784118433,
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {"stream_data": '{"data": {}}'}
                    },
                    "flv_pull_url": {"HD1": "https://example.com/stale.flv"},
                },
            },
            "status_code": 0,
        },
    )

    with pytest.raises(UserLiveError, match="not hosting a live stream"):
        api.get_live_url("123", user="creator")


def test_get_live_url_candidates_returns_ordered_unique_streams():
    api = build_api(
        {
            "data": {
                "status": 2,
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {
                            "stream_data": (
                                '{"data": {'
                                '"hd": {"main": {"flv": "https://cdn/hd.flv"}},'
                                '"ld": {"main": {"flv": "https://cdn/ld.flv"}},'
                                '"ao": {"main": {"flv": "https://cdn/audio.flv"}}'
                                "}}"
                            ),
                            "options": {
                                "qualities": [
                                    {"sdk_key": "hd", "level": 3},
                                    {"sdk_key": "ld", "level": 1},
                                ]
                            },
                        }
                    },
                    "flv_pull_url": {
                        "HD1": "https://cdn/hd.flv",
                        "SD1": "https://cdn/sd.flv",
                    },
                },
            },
            "status_code": 0,
        },
    )

    assert api.get_live_url_candidates("123", user="creator") == [
        "https://cdn/hd.flv",
        "https://cdn/ld.flv",
        "https://cdn/audio.flv",
        "https://cdn/sd.flv",
    ]


def test_stream_candidates_identify_original_quality_source():
    api = build_api(
        {
            "data": {
                "status": 2,
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {
                            "stream_data": (
                                '{"data":{"ao":{"main":{"flv":'
                                '"https://cdn/original.flv?expire=1&sign=s&only_audio=1"}},'
                                '"hd":{"main":{"flv":"https://cdn/hd.flv"}}}}'
                            ),
                            "options": {
                                "qualities": [
                                    {"sdk_key": "hd", "level": 3},
                                    {"sdk_key": "ao", "level": 0},
                                ]
                            },
                        }
                    }
                },
            },
            "status_code": 0,
        }
    )

    candidates = api.get_live_stream_candidates("123", user="creator")

    assert api.http_client.request_kwargs[0]["timeout"] == (5, 1)
    assert api.http_client.request_kwargs[0]["stream"] is True
    assert [(item.url, item.is_original_quality) for item in candidates] == [
        ("https://cdn/original.flv?expire=1&sign=s", True),
        ("https://cdn/hd.flv", False),
    ]


def test_stale_original_quality_url_is_excluded_from_candidate_refresh():
    def live_response(original_url, standard_url):
        sdk_data = {
            "ao": {"main": {"flv": f"{original_url}&only_audio=1"}},
            "hd": {"main": {"flv": standard_url}},
        }
        return {
            "data": {
                "status": 2,
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {
                            "stream_data": json.dumps({"data": sdk_data}),
                            "options": {
                                "qualities": [
                                    {"sdk_key": "hd", "level": 3},
                                    {"sdk_key": "ao", "level": 0},
                                ]
                            },
                        }
                    }
                },
            },
            "status_code": 0,
        }

    original_url = "https://cdn.example/original-first.flv?sign=old"
    standard_url = "https://cdn.example/standard-first.flv"
    api = build_api(live_response(original_url, standard_url))

    candidates = api.get_live_stream_candidates(
        "123", user="creator", stale_urls={original_url}
    )

    assert api.http_client.urls == [
        "https://webcast.tiktok.com/webcast/room/info/?aid=1988&room_id=123"
    ]
    assert [candidate.url for candidate in candidates] == [standard_url]


def test_candidate_deadline_and_stale_original_are_applied_to_streaming_response(
    monkeypatch, caplog
):
    def live_response(original_url, standard_url):
        sdk_data = {
            "ao": {"main": {"flv": f"{original_url}&only_audio=1"}},
            "hd": {"main": {"flv": standard_url}},
        }
        return {
            "data": {
                "status": 2,
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {
                            "stream_data": json.dumps({"data": sdk_data}),
                            "options": {
                                "qualities": [
                                    {"sdk_key": "hd", "level": 3},
                                    {"sdk_key": "ao", "level": 0},
                                ]
                            },
                        }
                    }
                },
            },
            "status_code": 0,
        }

    original_url = "https://cdn.example/original-first.flv?sign=old"
    standard_url = "https://cdn.example/standard-first.flv"
    api = build_api(
        live_response(original_url, standard_url),
        live_response(original_url, standard_url),
    )

    class FakeClock:
        now = 6.0

        def monotonic(self):
            return self.now

    clock = FakeClock()
    monkeypatch.setattr(
        "core.tiktok_api.time", SimpleNamespace(monotonic=clock.monotonic)
    )

    candidates = api.get_live_stream_candidates("123", deadline=10)
    assert [candidate.url for candidate in candidates] == [original_url, standard_url]

    clock.now = 8
    refreshed_candidates = api.get_live_stream_candidates(
        "123", deadline=10, stale_urls={original_url}
    )

    assert [candidate.url for candidate in refreshed_candidates] == [standard_url]
    assert [kwargs["timeout"] for kwargs in api.http_client.request_kwargs] == [
        (4, 1),
        (2, 1),
    ]
    assert all(kwargs["stream"] is True for kwargs in api.http_client.request_kwargs)
    assert "sign=old" not in caplog.text
