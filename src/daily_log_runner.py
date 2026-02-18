import argparse
import datetime
import subprocess
import sys
from pathlib import Path
from typing import Optional, TextIO


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a command and write combined stdout/stderr to daily log files."
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        required=True,
        help="Directory where daily logs will be written.",
    )
    parser.add_argument(
        "--log-prefix",
        required=True,
        help="File prefix for generated daily logs.",
    )
    parser.add_argument(
        "--log-extension",
        default=".log",
        help="File extension for generated logs. Default: .log",
    )
    parser.add_argument(
        "--echo",
        action="store_true",
        help="Also print logs to this wrapper's stdout.",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run. Pass command args after --",
    )
    return parser


def normalize_extension(extension: str) -> str:
    if not extension:
        return ".log"
    return extension if extension.startswith(".") else f".{extension}"


def normalize_command(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


def daily_log_path(log_dir: Path, log_prefix: str, log_extension: str) -> Path:
    day = datetime.date.today().isoformat()
    return log_dir / f"{log_prefix}-{day}{log_extension}"


def run_with_daily_logs(
    command: list[str],
    log_dir: Path,
    log_prefix: str,
    log_extension: str,
    echo: bool = False,
) -> int:
    log_dir.mkdir(parents=True, exist_ok=True)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    current_log_path: Optional[Path] = None
    current_log_file: Optional[TextIO] = None

    try:
        if process.stdout is None:
            raise RuntimeError("Failed to capture child process output.")

        for line in process.stdout:
            target_path = daily_log_path(log_dir, log_prefix, log_extension)
            if target_path != current_log_path:
                if current_log_file:
                    current_log_file.close()
                current_log_file = target_path.open("a", encoding="utf-8")
                current_log_path = target_path

            current_log_file.write(line)
            current_log_file.flush()

            if echo:
                sys.stdout.write(line)
                sys.stdout.flush()

        return process.wait()
    except KeyboardInterrupt:
        if process.poll() is None:
            process.terminate()
            process.wait()
        return 130
    finally:
        if process.stdout:
            process.stdout.close()
        if current_log_file:
            current_log_file.close()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    command = normalize_command(args.command)
    if not command:
        parser.error("Missing command. Add command arguments after --")

    extension = normalize_extension(args.log_extension)

    try:
        return run_with_daily_logs(
            command=command,
            log_dir=args.log_dir.expanduser().resolve(),
            log_prefix=args.log_prefix,
            log_extension=extension,
            echo=args.echo,
        )
    except FileNotFoundError as ex:
        print(f"Command not found: {ex}")
        return 1
    except Exception as ex:
        print(f"Error: {ex}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
