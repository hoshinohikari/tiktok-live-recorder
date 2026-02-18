import argparse
import getpass
import shutil
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SystemdInstallConfig:
    service_name: str
    description: str
    python_bin: str
    main_script: Path
    working_directory: Path
    recorder_args: list[str]
    user_service: bool
    run_as_user: Optional[str]
    run_as_group: Optional[str]
    log_runner_script: Path
    log_file: Path
    restart_sec: int
    enable_now: bool
    dry_run: bool


def build_parser() -> argparse.ArgumentParser:
    current_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Install TikTok Live Recorder as a systemd service."
    )
    parser.add_argument(
        "--service-name",
        default="tiktok-live-recorder",
        help="Service name (without .service suffix).",
    )
    parser.add_argument(
        "--description",
        default="TikTok Live Recorder",
        help="Description shown in systemd.",
    )
    parser.add_argument(
        "--python-bin",
        default=sys.executable,
        help="Python executable used by systemd.",
    )
    parser.add_argument(
        "--main-script",
        type=Path,
        default=current_dir / "main.py",
        help="Path to main.py.",
    )
    parser.add_argument(
        "--working-directory",
        type=Path,
        default=current_dir,
        help="Working directory for the service process.",
    )
    parser.add_argument(
        "--user-service",
        action="store_true",
        help="Install as a user service in ~/.config/systemd/user.",
    )
    parser.add_argument(
        "--run-as-user",
        default=None,
        help="Set systemd User=... (for system services).",
    )
    parser.add_argument(
        "--run-as-group",
        default=None,
        help="Set systemd Group=... (for system services).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Base log file path. Daily files become <name>-YYYY-MM-DD<ext>. Default base: <working-directory>/logs/<service>.log",
    )
    parser.add_argument(
        "--restart-sec",
        type=int,
        default=5,
        help="Restart delay in seconds. Default: 5.",
    )
    parser.add_argument(
        "--enable-now",
        action="store_true",
        help="Run 'systemctl enable --now' instead of only enable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print service content and commands, without writing files.",
    )
    parser.add_argument(
        "--example",
        action="store_true",
        help="Print an example command and exit.",
    )
    return parser


def normalize_service_name(service_name: str) -> str:
    return service_name if service_name.endswith(".service") else f"{service_name}.service"


def normalize_recorder_args(recorder_args: list[str]) -> list[str]:
    args = [arg for arg in recorder_args if arg != "--"]
    if "-no-update-check" not in args:
        args.append("-no-update-check")
    return args


def default_log_file(working_directory: Path, service_name: str) -> Path:
    normalized = normalize_service_name(service_name)
    log_name = normalized.replace(".service", ".log")
    return working_directory / "logs" / log_name


def build_exec_start(config: SystemdInstallConfig) -> str:
    log_extension = config.log_file.suffix or ".log"
    command = [
        config.python_bin,
        str(config.log_runner_script),
        "--log-dir",
        str(config.log_file.parent),
        "--log-prefix",
        config.log_file.stem,
        "--log-extension",
        log_extension,
        "--",
        config.python_bin,
        str(config.main_script),
        *config.recorder_args,
    ]
    return shlex.join(command)


def build_service_content(config: SystemdInstallConfig, service_name: str) -> str:
    wanted_by = "default.target" if config.user_service else "multi-user.target"
    lines = [
        "[Unit]",
        f"Description={config.description}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={config.working_directory}",
        f"ExecStart={build_exec_start(config)}",
        "Restart=on-failure",
        f"RestartSec={config.restart_sec}",
        "Environment=PYTHONUNBUFFERED=1",
    ]

    if config.run_as_user:
        lines.append(f"User={config.run_as_user}")
    if config.run_as_group:
        lines.append(f"Group={config.run_as_group}")

    lines.extend(
        [
            "",
            "[Install]",
            f"WantedBy={wanted_by}",
            "",
            f"# Unit file: {service_name}",
        ]
    )
    return "\n".join(lines) + "\n"


def get_service_path(service_name: str, user_service: bool) -> Path:
    if user_service:
        return Path.home() / ".config" / "systemd" / "user" / service_name
    return Path("/etc/systemd/system") / service_name


def run_systemctl(user_service: bool, *args: str) -> None:
    if shutil.which("systemctl") is None:
        raise FileNotFoundError("systemctl command not found.")
    command = ["systemctl"]
    if user_service:
        command.append("--user")
    command.extend(args)
    subprocess.run(command, check=True)


def install_service(config: SystemdInstallConfig) -> None:
    service_name = normalize_service_name(config.service_name)
    service_path = get_service_path(service_name, config.user_service)
    service_content = build_service_content(config, service_name)

    if config.dry_run:
        print(f"[dry-run] Service file path: {service_path}")
        print(f"[dry-run] Log file base path: {config.log_file}")
        print(f"[dry-run] Daily logs path: {daily_log_pattern(config.log_file)}")
        print(service_content)
        print("[dry-run] Commands:")
        prefix = "systemctl --user" if config.user_service else "systemctl"
        print(f"  {prefix} daemon-reload")
        if config.enable_now:
            print(f"  {prefix} enable --now {service_name}")
        else:
            print(f"  {prefix} enable {service_name}")
        return

    service_path.parent.mkdir(parents=True, exist_ok=True)
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(service_content, encoding="utf-8")

    run_systemctl(config.user_service, "daemon-reload")
    if config.enable_now:
        run_systemctl(config.user_service, "enable", "--now", service_name)
    else:
        run_systemctl(config.user_service, "enable", service_name)

    print(f"Installed service: {service_path}")
    print("Done.")
    if config.user_service:
        print(f"Manage with: systemctl --user status {service_name}")
    else:
        print(f"Manage with: systemctl status {service_name}")
    print(f"Daily logs: {daily_log_pattern(config.log_file)}")


def build_config(args: argparse.Namespace, recorder_args: list[str]) -> SystemdInstallConfig:
    service_name = args.service_name.strip()
    if not service_name:
        raise ValueError("service-name cannot be empty.")

    if args.restart_sec < 0:
        raise ValueError("restart-sec must be >= 0.")

    if args.user_service:
        run_as_user = None
        run_as_group = None
    else:
        run_as_user = args.run_as_user
        run_as_group = args.run_as_group

    main_script = args.main_script.expanduser().resolve()
    working_directory = args.working_directory.expanduser().resolve()
    log_runner_script = Path(__file__).resolve().parent / "daily_log_runner.py"
    if args.log_file:
        candidate_log_file = args.log_file.expanduser()
        if not candidate_log_file.is_absolute():
            candidate_log_file = working_directory / candidate_log_file
        log_file = candidate_log_file.resolve()
    else:
        log_file = default_log_file(working_directory, service_name)

    if not main_script.is_file():
        raise ValueError(f"main script not found: {main_script}")
    if not log_runner_script.is_file():
        raise ValueError(f"log runner script not found: {log_runner_script}")
    if not working_directory.is_dir():
        raise ValueError(f"working directory not found: {working_directory}")
    if log_file.exists() and log_file.is_dir():
        raise ValueError(f"log file path is a directory: {log_file}")

    return SystemdInstallConfig(
        service_name=service_name,
        description=args.description.strip() or "TikTok Live Recorder",
        python_bin=str(Path(args.python_bin).expanduser()),
        main_script=main_script,
        working_directory=working_directory,
        recorder_args=normalize_recorder_args(recorder_args),
        user_service=args.user_service,
        run_as_user=run_as_user,
        run_as_group=run_as_group,
        log_runner_script=log_runner_script,
        log_file=log_file,
        restart_sec=args.restart_sec,
        enable_now=args.enable_now,
        dry_run=args.dry_run,
    )


def daily_log_pattern(log_file: Path) -> str:
    extension = log_file.suffix or ".log"
    return str(log_file.parent / f"{log_file.stem}-YYYY-MM-DD{extension}")


def print_example() -> None:
    current_user = getpass.getuser()
    print("Example (user service):")
    print(
        "python3 systemd_installer.py --user-service --enable-now "
        "--service-name tiktok-auto "
        "-- -user someuser -mode automatic -automatic_interval 30"
    )
    print()
    print("Example (system service):")
    print(
        "sudo python3 systemd_installer.py --enable-now "
        f"--run-as-user {current_user} --service-name tiktok-auto "
        "-- -user someuser -mode automatic -automatic_interval 30"
    )


def main() -> int:
    parser = build_parser()
    args, recorder_args = parser.parse_known_args()

    if args.example:
        print_example()
        return 0

    try:
        config = build_config(args, recorder_args)
        install_service(config)
        return 0
    except subprocess.CalledProcessError as ex:
        print(f"systemctl command failed: {ex}")
        return 1
    except PermissionError as ex:
        print(f"Permission denied: {ex}")
        print("Try again with sudo for system services, or use --user-service.")
        return 1
    except Exception as ex:
        print(f"Error: {ex}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
