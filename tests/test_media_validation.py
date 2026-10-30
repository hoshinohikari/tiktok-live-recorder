import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from utils.media_validation import (
    MediaValidationError,
    inspect_video_file,
)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="local FFmpeg tools are required for the media verification check",
)
def test_inspect_video_file_requires_and_reports_decodable_local_video(tmp_path):
    media_path = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x48:d=0.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=0.2",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(media_path),
        ],
        check=True,
        capture_output=True,
    )

    media = inspect_video_file(media_path)

    assert (media.video_codec, media.width, media.height) == ("mpeg4", 64, 48)
    assert media.audio_codecs == ("aac",)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="local FFmpeg tools are required for the media verification check",
)
def test_inspect_video_file_rejects_pure_audio_media(tmp_path):
    media_path = tmp_path / "audio-only.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=0.2",
            "-c:a",
            "aac",
            str(media_path),
        ],
        check=True,
        capture_output=True,
    )

    with pytest.raises(MediaValidationError, match="no video stream"):
        inspect_video_file(media_path)


def test_inspect_video_file_rejects_ffprobe_timeout_with_artifact_path(
    monkeypatch, tmp_path
):
    media_path = tmp_path / "ffprobe-timeout.mp4"
    media_path.write_bytes(b"recorded artifact")

    def timeout_ffprobe(command, **kwargs):
        if command[0] != "ffprobe":
            pytest.fail("frame decoding should not start after an ffprobe timeout")
        assert kwargs.get("timeout") is not None
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("utils.media_validation.subprocess.run", timeout_ffprobe)

    with pytest.raises(MediaValidationError, match="ffprobe timed out") as error:
        inspect_video_file(media_path)

    assert str(media_path) in str(error.value)


def test_inspect_video_file_rejects_frame_decode_timeout_with_artifact_path(
    monkeypatch, tmp_path
):
    media_path = tmp_path / "decode-timeout.mp4"
    media_path.write_bytes(b"recorded artifact")

    def timeout_decode(command, **kwargs):
        assert kwargs.get("timeout") is not None
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
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("utils.media_validation.subprocess.run", timeout_decode)

    with pytest.raises(
        MediaValidationError, match="Video frame decoding timed out"
    ) as error:
        inspect_video_file(media_path)

    assert str(media_path) in str(error.value)
