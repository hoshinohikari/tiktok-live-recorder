import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

MEDIA_TOOL_TIMEOUT_SECONDS = 30


class MediaValidationError(ValueError):
    """Raised when a recording cannot be verified as playable video."""


@dataclass(frozen=True)
class VideoMediaInfo:
    video_codec: str
    width: int
    height: int
    audio_codecs: tuple[str, ...]


def inspect_video_file(
    path: str | Path,
    *,
    ffprobe_executable: str = "ffprobe",
    ffmpeg_executable: str = "ffmpeg",
) -> VideoMediaInfo:
    """Require a video stream and a successfully decoded video frame."""
    media_path = Path(path)
    if not media_path.is_file():
        raise MediaValidationError(f"Recording file does not exist: {media_path}")

    try:
        probe = subprocess.run(
            [
                ffprobe_executable,
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(media_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=MEDIA_TOOL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as ex:
        raise MediaValidationError(
            f"ffprobe timed out after {MEDIA_TOOL_TIMEOUT_SECONDS} seconds for {media_path}"
        ) from ex
    except OSError as ex:
        raise MediaValidationError(f"Unable to run ffprobe: {ex}") from ex

    if probe.returncode != 0:
        detail = (probe.stderr or "").strip()
        raise MediaValidationError(
            f"ffprobe rejected the recording{': ' + detail if detail else ''}"
        )

    try:
        metadata = json.loads(probe.stdout)
        streams = metadata.get("streams") if isinstance(metadata, dict) else None
    except (json.JSONDecodeError, AttributeError) as ex:
        raise MediaValidationError("ffprobe returned invalid stream metadata") from ex
    if not isinstance(streams, list):
        raise MediaValidationError("ffprobe returned invalid stream metadata")

    video_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if not video_streams:
        raise MediaValidationError("Recording contains no video stream")

    video = video_streams[0]
    codec = video.get("codec_name")
    width = video.get("width")
    height = video.get("height")
    if not codec or not isinstance(width, int) or not isinstance(height, int):
        raise MediaValidationError("Video stream metadata is incomplete")

    try:
        decoded = subprocess.run(
            [
                ffmpeg_executable,
                "-hide_banner",
                "-v",
                "error",
                "-nostats",
                "-xerror",
                "-i",
                str(media_path),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=MEDIA_TOOL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as ex:
        raise MediaValidationError(
            "Video frame decoding timed out after "
            f"{MEDIA_TOOL_TIMEOUT_SECONDS} seconds for {media_path}"
        ) from ex
    except OSError as ex:
        raise MediaValidationError(f"Unable to run ffmpeg to decode a frame: {ex}") from ex

    if decoded.returncode != 0:
        detail = (decoded.stderr or "").strip()
        raise MediaValidationError(
            f"Video frame could not be decoded{': ' + detail if detail else ''}"
        )

    audio_codecs = tuple(
        stream["codec_name"]
        for stream in streams
        if isinstance(stream, dict)
        and stream.get("codec_type") == "audio"
        and stream.get("codec_name")
    )
    return VideoMediaInfo(codec, width, height, audio_codecs)
