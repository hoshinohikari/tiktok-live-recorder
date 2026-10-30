import subprocess
import time
from pathlib import Path

import ffmpeg

from utils.logger_manager import logger
from utils.url_sanitizer import redact_urls_in_text


class VideoManagement:
    @staticmethod
    def wait_for_file_release(file, timeout=10):
        """
        Wait until the file is released (not locked anymore) or timeout is reached.
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                with open(file, "ab"):
                    return True
            except PermissionError:
                time.sleep(0.5)
        return False

    @staticmethod
    def convert_flv_to_mp4(file, bitrate=None, ffmpeg_path=None):
        """
        Convert the video from flv format to mp4 format
        """
        logger.info(f"Converting {file} to MP4 format...")

        if not VideoManagement.wait_for_file_release(file):
            logger.error(
                f"File {file} is still locked after waiting. Skipping conversion."
            )
            return

        try:
            output_args = {"c": "copy"}
            output_file = file.replace("_flv.mp4", ".mp4")

            if bitrate:
                output_args["b:v"] = bitrate
                del output_args["c"]
                output_args["c:v"] = "libx264"
                output_args["c:a"] = "copy"

            command = ffmpeg.compile(
                ffmpeg.input(file).output(output_file, **output_args),
                cmd=ffmpeg_path or "ffmpeg",
            )
            command[1:1] = [
                "-hide_banner",
                "-loglevel",
                "info",
                "-nostats",
                "-n",
            ]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            output_lines = []
            if process.stdout is not None:
                for line in process.stdout:
                    line = line.rstrip()
                    if not line:
                        continue
                    output_lines.append(line)
                    logger.info("FFmpeg conversion output: %s", redact_urls_in_text(line))
            exit_code = process.wait()
        except (ffmpeg.Error, OSError) as ex:
            details = getattr(ex, "stderr", None)
            if isinstance(details, bytes):
                details = details.decode(errors="replace")
            logger.error(
                "ffmpeg conversion failed: %s",
                redact_urls_in_text(str(details or ex)),
            )
            raise

        complete_output = "\n".join(output_lines)
        lower_output = complete_output.lower()
        if "no space left on device" in lower_output or "disk full" in lower_output:
            raise OSError(
                f"disk space is insufficient to convert recording {file}; "
                f"source retained. {redact_urls_in_text(complete_output)}"
            )
        if any(
            marker in lower_output
            for marker in (
                "permission denied",
                "access is denied",
                "could not open output",
                "error opening output",
                "failed to open output",
            )
        ):
            raise OSError(
                f"unable to write converted recording {output_file}; "
                f"source retained. {redact_urls_in_text(complete_output)}"
            )

        if exit_code != 0:
            logger.error(
                "ffmpeg conversion failed with exit code %s: %s",
                exit_code,
                redact_urls_in_text(complete_output),
            )
            return None

        logger.info(f"Finished converting {Path(output_file).resolve()}\n")
        return output_file
