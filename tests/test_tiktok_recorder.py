import io
import json
import shutil
import subprocess
import sys
import time as real_time
from http.server import (
    BaseHTTPRequestHandler,
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
)
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
import requests
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests import Session as CurlSession
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import JSONDecodeError as RequestsJSONDecodeError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.tiktok_api import LiveStreamCandidate, TikTokAPI
from core.tiktok_recorder import FFmpegResult, TikTokRecorder
from http_utils.http_client import HttpClient
from utils.custom_exceptions import TikTokRecorderError
from utils.enums import Mode, RoomStatus
from utils.recorder_config import RecorderConfig


@pytest.fixture
def local_media_server(tmp_path):
    class QuietHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tmp_path), **kwargs)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def local_ffmpeg():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("local FFmpeg and ffprobe are required for this media test")
    return ffmpeg


class FakeTikTokAPI:
    def __init__(self, blacklisted=True):
        self.blacklisted = blacklisted
        self.calls = []

    def is_country_blacklisted(self):
        self.calls.append("is_country_blacklisted")
        return self.blacklisted

    def get_room_id_from_user(self, user):
        self.calls.append(f"get_room_id_from_user:{user}")
        return "1234567890"

    def get_user_from_room_id(self, room_id):
        self.calls.append(f"get_user_from_room_id:{room_id}")
        return "creator"

    def get_sec_uid(self):
        self.calls.append("get_sec_uid")
        return "sec_uid"

    def is_room_alive(self, room_id):
        self.calls.append(f"is_room_alive:{room_id}")
        return True


@pytest.mark.parametrize(
    "error",
    [
        RequestsConnectionError("connection reset"),
        CurlConnectionError("connection reset"),
        RequestsJSONDecodeError("invalid JSON", "", 0),
    ],
)
def test_automatic_mode_backs_off_after_request_failure(monkeypatch, error):
    class StopLoop(BaseException):
        pass

    class FailingAPI:
        calls = 0

        def get_room_id_from_user(self, user):
            self.calls += 1
            if self.calls > 1:
                raise StopLoop
            raise error

    sleeps = []

    def stop_after_sleep(seconds):
        sleeps.append(seconds)
        raise StopLoop

    recorder = object.__new__(TikTokRecorder)
    recorder.user = "creator"
    recorder.tiktok = FailingAPI()
    monkeypatch.setattr("core.tiktok_recorder.time.sleep", stop_after_sleep)

    with pytest.raises(StopLoop):
        recorder.automatic_mode()

    assert sleeps == [5]


def test_automatic_mode_uses_capped_failure_backoff(monkeypatch):
    class StopLoop(BaseException):
        pass

    class LiveAPI:
        def get_room_id_from_user(self, user):
            return "1234567890"

        def get_room_status(self, room_id):
            from utils.enums import RoomStatus

            return RoomStatus.LIVE

    recorder = object.__new__(TikTokRecorder)
    recorder.user = "creator"
    recorder.tiktok = LiveAPI()
    recorder.automatic_interval = 5

    def fail_recording(*args):
        raise TikTokRecorderError("candidate round failed")

    monkeypatch.setattr(recorder, "start_recording", fail_recording)
    sleeps = []

    def stop_after_schedule(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 5:
            raise StopLoop

    monkeypatch.setattr("core.tiktok_recorder.time.sleep", stop_after_schedule)

    with pytest.raises(StopLoop):
        recorder.automatic_mode()

    assert sleeps == [5, 10, 20, 30, 30]


def test_automatic_mode_waits_when_room_status_is_unknown(monkeypatch):
    class StopLoop(BaseException):
        pass

    class UnknownAPI:
        def get_room_id_from_user(self, user):
            return "1234567890"

        def get_room_status(self, room_id):
            from utils.enums import RoomStatus

            return RoomStatus.UNKNOWN

    recorder = object.__new__(TikTokRecorder)
    recorder.user = "creator"
    recorder.tiktok = UnknownAPI()
    recorder.automatic_interval = 5
    recorder.start_recording = lambda *args: pytest.fail(
        "unknown room status must not start a recording"
    )
    sleeps = []

    def stop_after_sleep(seconds):
        sleeps.append(seconds)
        raise StopLoop

    monkeypatch.setattr("core.tiktok_recorder.time.sleep", stop_after_sleep)

    with pytest.raises(StopLoop):
        recorder.automatic_mode()

    assert sleeps == [5]


def test_manual_mode_reports_unknown_status_without_claiming_offline():
    class UnknownAPI:
        def get_room_status(self, room_id):
            from utils.enums import RoomStatus

            return RoomStatus.UNKNOWN

    recorder = object.__new__(TikTokRecorder)
    recorder.tiktok = UnknownAPI()
    recorder.room_id = "1234567890"
    recorder.user = "creator"
    recorder.start_recording = lambda *args: pytest.fail(
        "unknown status must not start a recording"
    )

    with pytest.raises(TikTokRecorderError, match="status is unknown"):
        recorder.manual_mode()


def test_setup_resolves_room_id_before_country_check_for_manual_user():
    recorder = TikTokRecorder(
        RecorderConfig(mode=Mode.MANUAL, user="creator", cookies={})
    )
    fake_api = FakeTikTokAPI(blacklisted=True)
    recorder.tiktok = fake_api

    recorder._setup()

    assert recorder.room_id == "1234567890"
    assert fake_api.calls == [
        "get_room_id_from_user:creator",
        "is_country_blacklisted",
        "is_room_alive:1234567890",
    ]


def test_setup_keeps_followers_country_check_before_sec_uid():
    recorder = TikTokRecorder(RecorderConfig(mode=Mode.FOLLOWERS, cookies={}))
    fake_api = FakeTikTokAPI(blacklisted=True)
    recorder.tiktok = fake_api

    with pytest.raises(TikTokRecorderError, match="Captcha required"):
        recorder._setup()

    assert fake_api.calls == ["is_country_blacklisted"]


def test_setup_keeps_automatic_mode_blocked_after_room_resolution():
    recorder = TikTokRecorder(
        RecorderConfig(mode=Mode.AUTOMATIC, user="creator", cookies={})
    )
    fake_api = FakeTikTokAPI(blacklisted=True)
    recorder.tiktok = fake_api

    with pytest.raises(TikTokRecorderError, match="Automatic mode is available"):
        recorder._setup()

    assert recorder.room_id == "1234567890"
    assert fake_api.calls == [
        "get_room_id_from_user:creator",
        "is_country_blacklisted",
    ]


def test_setup_keeps_manual_room_id_allowed_when_country_check_is_blocked():
    recorder = TikTokRecorder(
        RecorderConfig(mode=Mode.MANUAL, room_id="1234567890", cookies={})
    )
    fake_api = FakeTikTokAPI(blacklisted=True)
    recorder.tiktok = fake_api

    recorder._setup()

    assert recorder.room_id == "1234567890"
    assert fake_api.calls == [
        "get_user_from_room_id:1234567890",
        "is_country_blacklisted",
        "is_room_alive:1234567890",
    ]


def test_invalid_recording_artifact_is_kept_without_upload(
    monkeypatch, tmp_path, caplog
):
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
            use_telegram=True,
        )
    )

    class RecordingAPI:
        def get_live_url(self, room_id, user=None, deadline=None):
            return "https://cdn.example/live.flv?sign=secret"

        def is_room_alive(self, room_id):
            return True

    recorder.tiktok = RecordingAPI()
    uploads = []
    monkeypatch.setattr(
        "core.tiktok_recorder.Telegram",
        lambda: SimpleNamespace(upload=uploads.append),
    )

    def fake_run(command, **kwargs):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="invalid media")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.command = command
            self.stdout = io.StringIO("candidate decoded\n")
            self.returncode = 0
            if "-c" in command and command[-1] != "-":
                Path(command[-1]).write_bytes(b"not a media file")

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("core.tiktok_recorder.subprocess.run", fake_run)
    monkeypatch.setattr("core.tiktok_recorder.subprocess.Popen", FakeProcess)

    with pytest.raises(TikTokRecorderError):
        recorder.start_recording("creator", "1234567890")

    artifacts = list(tmp_path.glob("TK_creator_*.flv"))
    assert len(artifacts) == 1
    assert all(path.read_bytes() == b"not a media file" for path in artifacts)
    assert uploads == []
    assert "ffprobe rejected the recording: invalid media" in caplog.text
    assert any(str(path) in caplog.text for path in artifacts)


@pytest.mark.parametrize("timeout_tool", ["ffprobe", "ffmpeg"])
def test_media_validation_timeout_keeps_artifact_and_logs_path(
    monkeypatch, tmp_path, caplog, timeout_tool
):
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
            use_telegram=True,
        )
    )

    class RecordingAPI:
        def get_live_url(self, room_id, user=None, deadline=None):
            return "https://cdn.example/live.flv"

    recorder.tiktok = RecordingAPI()
    uploads = []
    monkeypatch.setattr(
        "core.tiktok_recorder.Telegram",
        lambda: SimpleNamespace(upload=uploads.append),
    )

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.stdout = io.StringIO("candidate decoded\n")
            self.returncode = 0
            if "-c" in command and command[-1] != "-":
                Path(command[-1]).write_bytes(b"recorded artifact")

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def media_command(command, **kwargs):
        if command[0] == "ffprobe" and timeout_tool == "ffprobe":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "h264",
                                "width": 64,
                                "height": 48,
                            }
                        ]
                    }
                ),
                stderr="",
            )
        if timeout_tool == "ffmpeg":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("core.tiktok_recorder.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("core.tiktok_recorder.subprocess.run", media_command)

    with pytest.raises(TikTokRecorderError):
        recorder.start_recording("creator", "1234567890")

    artifacts = list(tmp_path.glob("TK_creator_*.flv"))
    assert len(artifacts) == 1
    assert all(path.read_bytes() == b"recorded artifact" for path in artifacts)
    assert uploads == []
    expected_reason = (
        "ffprobe timed out"
        if timeout_tool == "ffprobe"
        else "Video frame decoding timed out"
    )
    assert expected_reason in caplog.text
    assert any(str(path) in caplog.text for path in artifacts)


def test_ordinary_recording_budget_is_global_across_refreshed_candidates(
    monkeypatch, tmp_path
):
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
        )
    )

    class RecordingAPI:
        calls = 0

        def get_live_url(self, room_id, user=None, deadline=None):
            self.calls += 1
            if self.calls > 4:
                return None
            return f"https://cdn.example/standard-{self.calls}.flv"

    api = RecordingAPI()
    recorder.tiktok = api
    recording_urls = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.stdout = io.StringIO("candidate decoded\n")
            self.returncode = 0
            if "-c" in command and command[-1] != "-":
                recording_urls.append(command[command.index("-i") + 1])
                Path(command[-1]).write_bytes(b"not a media file")

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def reject_artifact(command, **kwargs):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="invalid media"
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("core.tiktok_recorder.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("core.tiktok_recorder.subprocess.run", reject_artifact)

    with pytest.raises(TikTokRecorderError):
        recorder.start_recording("creator", "1234567890")

    assert recording_urls == [
        "https://cdn.example/standard-1.flv",
        "https://cdn.example/standard-2.flv",
        "https://cdn.example/standard-3.flv",
    ]
    assert len(list(tmp_path.glob("TK_creator_*.flv"))) == 3


def test_failed_standard_candidate_advances_through_stable_ordered_candidates(
    monkeypatch, tmp_path, caplog
):
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
        )
    )

    class RecordingAPI:
        def get_live_stream_candidates(
            self, room_id, user=None, deadline=None, stale_urls=None
        ):
            return [
                LiveStreamCandidate(f"https://cdn.example/standard-{index}.flv")
                for index in range(1, 5)
            ]

    recorder.tiktok = RecordingAPI()
    recording_urls = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.stdout = io.StringIO("candidate decoded\n")
            self.returncode = 0
            if "-c" in command and command[-1] != "-":
                recording_urls.append(command[command.index("-i") + 1])
                Path(command[-1]).write_bytes(b"invalid media")

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def reject_artifact(command, **kwargs):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="invalid media"
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("core.tiktok_recorder.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("core.tiktok_recorder.subprocess.run", reject_artifact)

    with pytest.raises(TikTokRecorderError):
        recorder.start_recording("creator", "1234567890")

    assert recording_urls == [
        "https://cdn.example/standard-1.flv",
        "https://cdn.example/standard-2.flv",
        "https://cdn.example/standard-3.flv",
    ]
    assert len(list(tmp_path.glob("TK_creator_*.flv"))) == 3
    assert "https://cdn.example/standard-4.flv" not in caplog.text


def test_original_selection_deadline_covers_candidate_lookup_and_probe(
    monkeypatch, tmp_path, caplog
):
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
        )
    )

    class FakeClock:
        now = 0.0

        def monotonic(self):
            return self.now

    clock = FakeClock()
    monkeypatch.setattr("core.tiktok_recorder.time.monotonic", clock.monotonic)

    class RecordingAPI:
        def __init__(self):
            self.deadlines = []

        def get_live_stream_candidates(
            self, room_id, user=None, deadline=None, stale_urls=None
        ):
            self.deadlines.append(deadline)
            clock.now = 29.99
            return [
                LiveStreamCandidate("https://cdn.example/original.flv", True),
                LiveStreamCandidate("https://cdn.example/standard.flv"),
            ]

    api = RecordingAPI()
    recorder.tiktok = api
    original_probe_urls = []
    original_probe_timeouts = []
    recording_urls = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.command = command
            self.stdout = io.StringIO("candidate decoded\n")
            self.returncode = 0
            input_url = command[command.index("-i") + 1]
            if "-frames:v" in command:
                original_probe_urls.append(input_url)
            elif "-c" in command and command[-1] != "-":
                recording_urls.append(input_url)
                Path(command[-1]).write_bytes(b"valid video")

        def wait(self, timeout=None):
            if "-frames:v" in self.command and self.returncode == 0:
                original_probe_timeouts.append(timeout)
                clock.now += timeout
                raise subprocess.TimeoutExpired(self.command, timeout)
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def inspect_artifact(command, **kwargs):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "h264",
                                "width": 640,
                                "height": 360,
                            }
                        ]
                    }
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("core.tiktok_recorder.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("core.tiktok_recorder.subprocess.run", inspect_artifact)

    output = recorder.start_recording("creator", "1234567890")

    assert api.deadlines == [30]
    assert original_probe_urls == ["https://cdn.example/original.flv"]
    assert original_probe_timeouts[0] == pytest.approx(0.01)
    assert recording_urls == ["https://cdn.example/standard.flv"]
    assert Path(output).read_bytes() == b"valid video"
    assert "budget exhausted" in caplog.text.lower()


def test_start_recording_falls_back_from_unusable_original_candidate(
    monkeypatch, tmp_path, caplog
):
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
            use_telegram=True,
        )
    )

    class RecordingAPI:
        def get_live_stream_candidates(
            self, room_id, user=None, deadline=None, stale_urls=None
        ):
            return [
                LiveStreamCandidate(
                    "https://cdn.example/original.flv?sign=original-secret", True
                ),
                LiveStreamCandidate("https://cdn.example/standard.flv?token=std-secret"),
            ]

        def get_room_status(self, room_id):
            from utils.enums import RoomStatus

            return RoomStatus.LIVE

    recorder.tiktok = RecordingAPI()
    uploads = []
    monkeypatch.setattr(
        "core.tiktok_recorder.Telegram",
        lambda: SimpleNamespace(upload=uploads.append),
    )
    process_inputs = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.command = command
            self.stdout = io.StringIO("decoded one frame\n")
            self.returncode = 0
            input_url = command[command.index("-i") + 1]
            process_inputs.append(input_url)
            if "-c" in command and command[-1] != "-":
                assert "-n" in command
                assert "-y" not in command
                contents = b"unusable original" if "original.flv" in input_url else b"valid standard"
                Path(command[-1]).write_bytes(contents)

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def fake_run(command, **kwargs):
        media_path = Path(
            command[-1] if command[0] == "ffprobe" else command[command.index("-i") + 1]
        )
        is_valid = media_path.read_bytes() == b"valid standard"
        if command[0] == "ffprobe":
            streams = {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360},
                    {"codec_type": "audio", "codec_name": "aac"},
                ]
            }
            return subprocess.CompletedProcess(
                command,
                0 if is_valid else 1,
                stdout=json.dumps(streams) if is_valid else "",
                stderr="" if is_valid else "invalid data",
            )
        return subprocess.CompletedProcess(command, 0 if is_valid else 1, stdout="", stderr="")

    monkeypatch.setattr("core.tiktok_recorder.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("core.tiktok_recorder.subprocess.run", fake_run)

    recorder.start_recording("creator", "1234567890")

    artifacts = list(tmp_path.glob("TK_creator_*.flv"))
    assert len(artifacts) == 2
    assert {path.read_bytes() for path in artifacts} == {
        b"unusable original",
        b"valid standard",
    }
    assert len(uploads) == 1
    assert Path(uploads[0]).read_bytes() == b"valid standard"
    assert process_inputs.count("https://cdn.example/original.flv?sign=original-secret") >= 1
    assert process_inputs.count("https://cdn.example/standard.flv?token=std-secret") >= 1
    assert "original-secret" not in caplog.text
    assert "std-secret" not in caplog.text
    assert "decoded one frame" in caplog.text
    assert "FFmpeg exited [attempt=" in caplog.text


def test_recording_refreshes_candidates_after_http_404_without_retrying_stale_url(
    monkeypatch, tmp_path, caplog
):
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
            use_telegram=True,
        )
    )

    class RecordingAPI:
        calls = 0

        def get_live_stream_candidates(
            self, room_id, user=None, deadline=None, stale_urls=None
        ):
            self.calls += 1
            candidates = [
                LiveStreamCandidate("https://cdn.example/stale.flv?token=private")
            ]
            if self.calls > 1:
                candidates.append(LiveStreamCandidate("https://cdn.example/fresh.flv"))
            return candidates

        def get_room_status(self, room_id):
            from utils.enums import RoomStatus

            return RoomStatus.LIVE

    api = RecordingAPI()
    recorder.tiktok = api
    uploads = []
    monkeypatch.setattr(
        "core.tiktok_recorder.Telegram",
        lambda: SimpleNamespace(upload=uploads.append),
    )
    process_inputs = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            input_url = command[command.index("-i") + 1]
            process_inputs.append(input_url)
            self.returncode = 0
            if "stale.flv" in input_url:
                self.stdout = io.StringIO("HTTP error 404 Not Found\n")
                self.returncode = 1
            else:
                self.stdout = io.StringIO("decoded one frame\n")
                if "-c" in command and command[-1] != "-":
                    Path(command[-1]).write_bytes(b"valid video")

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def fake_run(command, **kwargs):
        media_path = Path(
            command[-1] if command[0] == "ffprobe" else command[command.index("-i") + 1]
        )
        valid = media_path.read_bytes() == b"valid video"
        if command[0] == "ffprobe":
            streams = {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360}
                ]
            }
            return subprocess.CompletedProcess(
                command,
                0 if valid else 1,
                stdout=json.dumps(streams) if valid else "",
                stderr="" if valid else "invalid data",
            )
        return subprocess.CompletedProcess(command, 0 if valid else 1, stdout="", stderr="")

    monkeypatch.setattr("core.tiktok_recorder.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("core.tiktok_recorder.subprocess.run", fake_run)

    recorder.start_recording("creator", "1234567890")

    assert api.calls == 2
    assert process_inputs.count("https://cdn.example/stale.flv?token=private") == 1
    assert "https://cdn.example/fresh.flv" in process_inputs
    assert len(uploads) == 1
    assert Path(uploads[0]).read_bytes() == b"valid video"
    assert "private" not in caplog.text
    assert "status=404" in caplog.text


def test_recorder_404_is_excluded_across_candidate_refreshes(
    monkeypatch, tmp_path, caplog, local_media_server
):
    ffmpeg = local_ffmpeg()
    media_path = tmp_path / "fresh.flv"
    generated_media = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x48:r=5:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-f",
            "flv",
            str(media_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert generated_media.returncode == 0, generated_media.stderr

    stale_original_url = f"{local_media_server}/missing-original.flv?sign=old"
    fresh_original_url = f"{local_media_server}/fresh.flv?sign=fresh"
    missing_standard_url = f"{local_media_server}/missing.flv"

    def live_response(original_url, standard_url):
        ao_url = f"{original_url}&only_audio=1"
        sdk_data = {
            "ao": {"main": {"flv": ao_url}},
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

    candidate_responses = [
        live_response(stale_original_url, missing_standard_url),
        live_response(stale_original_url, missing_standard_url),
        live_response(fresh_original_url, missing_standard_url),
    ]
    candidate_requests = []

    class CandidateResponse:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

        def iter_content(self, chunk_size):
            body = json.dumps(self.payload).encode()
            for offset in range(0, len(body), chunk_size):
                yield body[offset : offset + chunk_size]

        def close(self):
            pass

    class CandidateHTTP:
        def get(self, url, **kwargs):
            candidate_requests.append((url, kwargs))
            return CandidateResponse(candidate_responses.pop(0))

    original_probe_urls = []

    class UnexpectedOriginalByteProbe:
        def get(self, url, **kwargs):
            original_probe_urls.append(url)
            raise AssertionError("original candidates are validated by recorder FFmpeg")

    api = TikTokAPI(proxy=None, cookies={})
    api.http_client = CandidateHTTP()
    api._http_client_stream = UnexpectedOriginalByteProbe()
    api.get_room_status = lambda room_id: RoomStatus.LIVE
    get_live_stream_candidates = api.get_live_stream_candidates
    stale_url_sets = []
    stale_url_snapshots = []

    def track_candidate_refresh(room_id, user=None, deadline=None, stale_urls=None):
        stale_url_sets.append(stale_urls)
        stale_url_snapshots.append(set(stale_urls) if stale_urls is not None else None)
        return get_live_stream_candidates(
            room_id, user=user, deadline=deadline, stale_urls=stale_urls
        )

    api.get_live_stream_candidates = track_candidate_refresh
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
            use_telegram=True,
        )
    )
    recorder.tiktok.http_client.close()
    recorder.tiktok._http_client_stream.close()
    recorder.tiktok = api
    uploads = []
    monkeypatch.setattr(
        "core.tiktok_recorder.Telegram",
        lambda: SimpleNamespace(upload=uploads.append),
    )

    output = recorder.start_recording("creator", "1234567890")

    assert output == uploads[0]
    assert Path(output).is_file()
    assert len(candidate_requests) == 3
    assert stale_url_sets[0] is stale_url_sets[1]
    assert stale_url_snapshots == [
        set(),
        {stale_original_url},
        {stale_original_url, missing_standard_url},
    ]
    assert original_probe_urls == []
    assert "sign=old" not in caplog.text
    assert "sign=fresh" not in caplog.text


def test_probe_skipped_fallback_still_requires_final_video_validation(
    monkeypatch, tmp_path, caplog, local_media_server
):
    ffmpeg = local_ffmpeg()
    audio_path = tmp_path / "audio-only.flv"
    generated_media = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=1",
            "-c:a",
            "aac",
            "-f",
            "flv",
            str(audio_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert generated_media.returncode == 0, generated_media.stderr

    class FakeClock:
        now = 0.0

        def monotonic(self):
            return self.now

    clock = FakeClock()
    fake_time = SimpleNamespace(
        monotonic=clock.monotonic,
        time=real_time.time,
        sleep=real_time.sleep,
        strftime=real_time.strftime,
        localtime=real_time.localtime,
    )
    monkeypatch.setattr("core.tiktok_recorder.time", fake_time)

    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
            use_telegram=True,
        )
    )

    class RecordingAPI:
        def get_live_stream_candidates(
            self, room_id, user=None, deadline=None, stale_urls=None
        ):
            clock.now = deadline
            return [LiveStreamCandidate(f"{local_media_server}/audio-only.flv")]

    recorder.tiktok = RecordingAPI()
    uploads = []
    monkeypatch.setattr(
        "core.tiktok_recorder.Telegram",
        lambda: SimpleNamespace(upload=uploads.append),
    )

    with pytest.raises(TikTokRecorderError, match="No candidate produced"):
        recorder.start_recording("creator", "1234567890")

    artifacts = list(tmp_path.glob("TK_creator_*.flv"))
    assert len(artifacts) == 1
    assert artifacts[0].stat().st_size > 0
    assert uploads == []
    assert "skipping the candidate video probe" in caplog.text
    assert "stage=media verification" in caplog.text


def test_nonzero_ffmpeg_exit_keeps_and_uploads_verified_video_with_abnormal_log(
    monkeypatch, tmp_path, caplog
):
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
            use_telegram=True,
        )
    )

    class RecordingAPI:
        def get_live_url_candidates(self, room_id, user=None, deadline=None):
            return ["https://cdn.example/standard.flv"]

        def get_room_status(self, room_id):
            from utils.enums import RoomStatus

            return RoomStatus.UNKNOWN

    recorder.tiktok = RecordingAPI()
    uploads = []
    monkeypatch.setattr(
        "core.tiktok_recorder.Telegram",
        lambda: SimpleNamespace(upload=uploads.append),
    )

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.command = command
            self.stdout = io.StringIO("connection reset after recording\n")
            self.returncode = 0
            if "-c" in command and command[-1] != "-":
                Path(command[-1]).write_bytes(b"valid video")
                self.returncode = 7

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def fake_run(command, **kwargs):
        media_path = Path(
            command[-1] if command[0] == "ffprobe" else command[command.index("-i") + 1]
        )
        valid = media_path.read_bytes() == b"valid video"
        if command[0] == "ffprobe":
            streams = {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 320, "height": 240}
                ]
            }
            return subprocess.CompletedProcess(
                command,
                0 if valid else 1,
                stdout=json.dumps(streams) if valid else "",
                stderr="" if valid else "invalid data",
            )
        return subprocess.CompletedProcess(command, 0 if valid else 1, stdout="", stderr="")

    monkeypatch.setattr("core.tiktok_recorder.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("core.tiktok_recorder.subprocess.run", fake_run)

    output = recorder.start_recording("creator", "1234567890")

    assert Path(output).read_bytes() == b"valid video"
    assert uploads == [output]
    assert "ended abnormally" in caplog.text
    assert "exit_code=7" in caplog.text
    assert "status=unknown" in caplog.text
    assert "h264 320x240" in caplog.text


def test_unwritable_output_reports_insufficient_disk_space(monkeypatch, tmp_path):
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
        )
    )

    class RecordingAPI:
        def get_live_stream_candidates(
            self, room_id, user=None, deadline=None, stale_urls=None
        ):
            return [LiveStreamCandidate("https://cdn.example/standard.flv")]

    recorder.tiktok = RecordingAPI()

    class FailingDiskProcess:
        def __init__(self, command, **kwargs):
            self.stdout = io.StringIO(
                "Could not write header: No space left on device\n"
                if "-c" in command
                else "decoded one frame\n"
            )
            self.returncode = 1 if "-c" in command else 0

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("core.tiktok_recorder.subprocess.Popen", FailingDiskProcess)

    with pytest.raises(TikTokRecorderError, match="disk space is insufficient"):
        recorder.start_recording("creator", "1234567890")

    assert list(tmp_path.iterdir()) == []


def test_original_quality_is_limited_to_two_refreshed_candidate_attempts(
    monkeypatch, tmp_path
):
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
        )
    )

    class RecordingAPI:
        calls = 0

        def get_live_stream_candidates(
            self, room_id, user=None, deadline=None, stale_urls=None
        ):
            self.calls += 1
            return [
                LiveStreamCandidate(f"https://cdn.example/original-{self.calls}.flv", True),
                LiveStreamCandidate("https://cdn.example/standard.flv"),
            ]

        def get_room_status(self, room_id):
            from utils.enums import RoomStatus

            return RoomStatus.LIVE

    api = RecordingAPI()
    recorder.tiktok = api
    process_inputs = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            input_url = command[command.index("-i") + 1]
            process_inputs.append(input_url)
            if "original-" in input_url:
                self.stdout = io.StringIO("Could not find video stream\n")
                self.returncode = 1
            else:
                self.stdout = io.StringIO("decoded one frame\n")
                self.returncode = 0
                if "-c" in command:
                    Path(command[-1]).write_bytes(b"valid video")

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def fake_run(command, **kwargs):
        media_path = Path(
            command[-1] if command[0] == "ffprobe" else command[command.index("-i") + 1]
        )
        valid = media_path.read_bytes() == b"valid video"
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0 if valid else 1,
                stdout=json.dumps(
                    {
                        "streams": [
                            {"codec_type": "video", "codec_name": "h264", "width": 320, "height": 240}
                        ]
                    }
                )
                if valid
                else "",
                stderr="" if valid else "invalid data",
            )
        return subprocess.CompletedProcess(command, 0 if valid else 1, stdout="", stderr="")

    monkeypatch.setattr("core.tiktok_recorder.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("core.tiktok_recorder.subprocess.run", fake_run)

    recorder.start_recording("creator", "1234567890")

    assert api.calls == 3
    assert [url for url in process_inputs if "original-" in url] == [
        "https://cdn.example/original-1.flv",
        "https://cdn.example/original-2.flv",
    ]


def test_original_quality_falls_back_when_thirty_second_budget_expires(
    monkeypatch, tmp_path, caplog
):
    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
        )
    )

    class RecordingAPI:
        calls = 0

        def get_live_stream_candidates(
            self, room_id, user=None, deadline=None, stale_urls=None
        ):
            self.calls += 1
            return [
                LiveStreamCandidate(f"https://cdn.example/original-{self.calls}.flv", True),
                LiveStreamCandidate("https://cdn.example/standard.flv"),
            ]

        def get_room_status(self, room_id):
            from utils.enums import RoomStatus

            return RoomStatus.LIVE

    api = RecordingAPI()
    recorder.tiktok = api
    process_inputs = []

    class FakeClock:
        now = 0.0

        def monotonic(self):
            return self.now

    clock = FakeClock()
    monkeypatch.setattr("core.tiktok_recorder.time.monotonic", clock.monotonic)

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.command = command
            input_url = command[command.index("-i") + 1]
            process_inputs.append(input_url)
            if "original-1" in input_url:
                self.stdout = io.StringIO("Could not find video stream\n")
                self.returncode = 1
            else:
                self.stdout = io.StringIO("decoded one frame\n")
                self.returncode = 0
                if "-c" in command:
                    Path(command[-1]).write_bytes(b"valid video")

        def wait(self, timeout=None):
            if "-frames:v" in self.command and "original-1" in " ".join(self.command):
                clock.now = 31
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def fake_run(command, **kwargs):
        media_path = Path(
            command[-1] if command[0] == "ffprobe" else command[command.index("-i") + 1]
        )
        valid = media_path.read_bytes() == b"valid video"
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0 if valid else 1,
                stdout=json.dumps(
                    {
                        "streams": [
                            {"codec_type": "video", "codec_name": "h264", "width": 320, "height": 240}
                        ]
                    }
                )
                if valid
                else "",
                stderr="" if valid else "invalid data",
            )
        return subprocess.CompletedProcess(command, 0 if valid else 1, stdout="", stderr="")

    monkeypatch.setattr("core.tiktok_recorder.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("core.tiktok_recorder.subprocess.run", fake_run)

    recorder.start_recording("creator", "1234567890")

    assert api.calls == 1
    assert "https://cdn.example/original-1.flv" in process_inputs
    assert "https://cdn.example/original-2.flv" not in process_inputs
    assert process_inputs.count("https://cdn.example/standard.flv") == 1
    assert "budget exhausted" in caplog.text.lower()
    assert "Falling back to standard stream candidates" in caplog.text


@pytest.mark.parametrize("slow_endpoint", ["candidate_api", "page_fallback"])
@pytest.mark.parametrize(
    "session_factory",
    [
        pytest.param(requests.Session, id="requests"),
        pytest.param(CurlSession, id="curl_cffi"),
    ],
)
def test_slow_trickle_selection_falls_back_without_a_late_probe(
    monkeypatch,
    tmp_path,
    caplog,
    local_media_server,
    slow_endpoint,
    session_factory,
):
    ffmpeg = local_ffmpeg()
    standard_media = tmp_path / "standard.flv"
    generated_media = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x48:r=5:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-f",
            "flv",
            str(standard_media),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert generated_media.returncode == 0, generated_media.stderr

    class FakeSelectionClock:
        now = 0.0

        def monotonic(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    clock = FakeSelectionClock()
    monkeypatch.setattr(
        "core.tiktok_api.TikTokAPI.MAX_SELECTION_HTTP_TIMEOUT_SECONDS", 0.05
    )
    fake_time = SimpleNamespace(
        monotonic=clock.monotonic,
        time=real_time.time,
        sleep=real_time.sleep,
        strftime=real_time.strftime,
        localtime=real_time.localtime,
    )
    monkeypatch.setattr("core.tiktok_recorder.time", fake_time)
    monkeypatch.setattr("core.tiktok_api.time", fake_time)

    original_url = f"{local_media_server}/missing-original.flv?sign=old"
    standard_url = f"{local_media_server}/standard.flv"

    def live_response():
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

    trickle_started_event = Event()
    trickle_completed_event = Event()
    trickle_bytes = []
    api_request_count = [0]

    class SlowSelectionHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, body, content_type="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def _trickle(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            trickle_started_event.set()
            try:
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    trickle_bytes.append(byte)
                    clock.advance(10)
                    real_time.sleep(0.005)
                trickle_completed_event.set()
            except OSError:
                pass

        def do_GET(self):
            if self.path.startswith("/webcast/room/info/"):
                api_request_count[0] += 1
                if api_request_count[0] == 1:
                    self._send(json.dumps(live_response()).encode())
                elif slow_endpoint == "page_fallback":
                    self._send(b'{"status_code":4003110}')
                else:
                    payload = live_response()
                    payload["padding"] = "x" * 200
                    self._trickle(json.dumps(payload).encode(), "application/json")
                return

            if self.path == "/@creator/live" and slow_endpoint == "page_fallback":
                self._trickle(b"<html>" + b"x" * 200 + b"</html>", "text/html")
                return

            self.send_error(404)

    class SlowSelectionServer(ThreadingHTTPServer):
        daemon_threads = True

    slow_server = SlowSelectionServer(("127.0.0.1", 0), SlowSelectionHandler)
    slow_server_thread = Thread(target=slow_server.serve_forever, daemon=True)
    slow_server_thread.start()
    host, port = slow_server.server_address
    local_api_url = f"http://{host}:{port}"

    api = TikTokAPI.__new__(TikTokAPI)
    api.WEBCAST_URL = local_api_url
    api.BASE_URL = local_api_url
    api.http_client = session_factory()
    if hasattr(api.http_client, "trust_env"):
        api.http_client.trust_env = False
    api._http_client_stream = requests.Session()
    api._http_client_stream.trust_env = False
    api.get_room_status = lambda room_id: RoomStatus.LIVE

    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
            use_telegram=True,
        )
    )
    recorder.tiktok.http_client.close()
    recorder.tiktok._http_client_stream.close()
    recorder.tiktok = api
    probe_calls = []

    def probe_candidate(url, attempt_id, timeout):
        probe_calls.append((url, clock.now, timeout))
        return FFmpegResult(1, "HTTP error 404", False, False)

    monkeypatch.setattr(recorder, "_probe_stream_candidate", probe_candidate)
    record_calls = []
    record_with_ffmpeg = recorder._record_with_ffmpeg

    def track_record_start(url, output, attempt_id):
        record_calls.append((url, clock.now, real_time.monotonic()))
        return record_with_ffmpeg(url, output, attempt_id)

    monkeypatch.setattr(recorder, "_record_with_ffmpeg", track_record_start)
    uploads = []
    monkeypatch.setattr(
        "core.tiktok_recorder.Telegram",
        lambda: SimpleNamespace(upload=uploads.append),
    )
    wall_started = real_time.monotonic()

    try:
        output = recorder.start_recording("creator", "1234567890")
    finally:
        api.http_client.close()
        api._http_client_stream.close()
        slow_server.shutdown()
        slow_server.server_close()
        slow_server_thread.join(timeout=5)

    assert Path(output).is_file()
    assert uploads == [output]
    assert record_calls
    assert record_calls[0][2] - wall_started < 0.8
    assert len(probe_calls) == 1
    assert probe_calls[0][:2] == (original_url, 0.0)
    assert probe_calls[0][2] == pytest.approx(30.0)
    assert [call[0] for call in record_calls] == [standard_url]
    assert clock.now >= 30
    assert trickle_started_event.is_set()
    assert trickle_bytes
    assert not trickle_completed_event.is_set()
    assert "Stream candidate failed: quality=original" in caplog.text
    assert "budget exhausted" in caplog.text.lower()


def test_default_curl_slow_headers_expire_selection_before_fallback(
    monkeypatch, tmp_path, local_media_server
):
    ffmpeg = local_ffmpeg()
    standard_media = tmp_path / "standard.flv"
    generated_media = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x48:r=5:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-f",
            "flv",
            str(standard_media),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert generated_media.returncode == 0, generated_media.stderr

    monkeypatch.setattr("core.tiktok_recorder.SELECTION_TIMEOUT_SECONDS", 0.25)
    monkeypatch.delenv("TIKTOK_HTTP_BACKEND", raising=False)
    monkeypatch.setattr("http_utils.http_client.is_termux", lambda: False)
    api_client = HttpClient(proxy=None, cookies={})
    assert type(api_client.req).__module__.startswith("curl_cffi.")

    original_url = "http://127.0.0.1/original.flv?sign=old"
    standard_url = f"{local_media_server}/standard.flv"
    payload = {
        "data": {
            "status": 2,
            "stream_url": {
                "live_core_sdk_data": {
                    "pull_data": {
                        "stream_data": json.dumps(
                            {
                                "data": {
                                    "ao": {
                                        "main": {
                                            "flv": f"{original_url}&only_audio=1"
                                        }
                                    },
                                    "hd": {"main": {"flv": standard_url}},
                                }
                            }
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
    headers_started = Event()
    headers_completed = Event()
    handler_finished = Event()
    api_request_count = [0]

    class SlowHeadersHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            api_request_count[0] += 1
            if api_request_count[0] == 1:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                return

            headers_started.set()
            response_headers = (
                b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n"
                b"Connection: close\r\n\r\n"
            )
            try:
                for byte in response_headers:
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

    api = TikTokAPI.__new__(TikTokAPI)
    api.WEBCAST_URL = f"http://127.0.0.1:{server.server_address[1]}"
    api.http_client = api_client.req
    api._http_client_stream = api_client.req_stream
    api.get_room_status = lambda room_id: RoomStatus.LIVE

    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            output=str(tmp_path),
            cookies={},
            use_ffmpeg=True,
            use_telegram=True,
        )
    )
    recorder.tiktok.http_client.close()
    recorder.tiktok._http_client_stream.close()
    recorder.tiktok = api
    probe_calls = []
    monkeypatch.setattr(
        recorder,
        "_probe_stream_candidate",
        lambda url, attempt_id, timeout: (
            probe_calls.append((url, real_time.monotonic()))
            or FFmpegResult(1, "HTTP error 404", False, False)
        ),
    )
    record_calls = []
    record_with_ffmpeg = recorder._record_with_ffmpeg

    def track_record_start(url, output, attempt_id):
        record_calls.append((url, real_time.monotonic()))
        return record_with_ffmpeg(url, output, attempt_id)

    monkeypatch.setattr(recorder, "_record_with_ffmpeg", track_record_start)
    uploads = []
    monkeypatch.setattr(
        "core.tiktok_recorder.Telegram",
        lambda: SimpleNamespace(upload=uploads.append),
    )

    selection_started = real_time.monotonic()
    try:
        output = recorder.start_recording("creator", "1234567890")
    finally:
        api_client.req.close()
        api_client.req_stream.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        handler_finished.wait(timeout=2)

    assert Path(output).is_file()
    assert uploads == [output]
    assert api_request_count == [2]
    assert headers_started.is_set()
    assert not headers_completed.is_set()
    assert len(probe_calls) == 1
    assert probe_calls[0][0] == original_url
    assert record_calls[0][0] == standard_url
    assert record_calls[0][1] - selection_started < 0.8
