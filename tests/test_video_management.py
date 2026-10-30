import io
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from utils.video_management import VideoManagement


def test_conversion_captures_ffmpeg_output_and_does_not_overwrite(monkeypatch, tmp_path, caplog):
    source = tmp_path / "clip_flv.mp4"
    target = tmp_path / "clip.mp4"
    source.write_bytes(b"raw stream")

    class FakeProcess:
        last_command = ()

        def __init__(self, command, **kwargs):
            self.command = command
            FakeProcess.last_command = command
            self.stdout = io.StringIO("converter wrote output\n")
            self.returncode = 0
            Path(command[-1]).write_bytes(b"converted video")

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)

    output = VideoManagement.convert_flv_to_mp4(str(source))

    assert output == str(target)
    assert source.read_bytes() == b"raw stream"
    assert target.read_bytes() == b"converted video"
    assert "-n" in FakeProcess.last_command
    assert "-y" not in FakeProcess.last_command
    assert "converter wrote output" in caplog.text


def test_conversion_surfaces_disk_write_failure(monkeypatch, tmp_path):
    source = tmp_path / "clip_flv.mp4"
    source.write_bytes(b"raw stream")

    class DiskFullProcess:
        def __init__(self, command, **kwargs):
            self.stdout = io.StringIO("No space left on device\n")
            self.returncode = 1

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", DiskFullProcess)

    with pytest.raises(OSError, match="disk space is insufficient"):
        VideoManagement.convert_flv_to_mp4(str(source))

    assert source.read_bytes() == b"raw stream"
