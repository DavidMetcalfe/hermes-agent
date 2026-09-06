"""Dashboard launchd service core for macOS lifecycle management (issue #44106).

WHY first-class macOS LaunchAgent lifecycle: the web dashboard needs the same
supervised restart / install / status path the gateway already has on macOS,
rather than being managed only by the Linux-only systemd branch.

No `--detach`: the dashboard already runs foreground (`cmd_dashboard`) — detach
would exit before launchd could supervise it, and KeepAlive would respawn the
launcher, not the server (the PR #40636 defect).
"""

import os
import plistlib
import subprocess
import sys
import urllib.request
from pathlib import Path

from hermes_cli.gateway import (
    _launchctl_bootstrap,
    _launchd_degrade_or_raise,
    _launchd_domain,
    _profile_suffix,
    _profile_arg,
    get_python_path,
    is_macos,
    PROJECT_ROOT,
    _service_venv_dir,
    _build_service_path_dirs,
    _append_node_dir_for_service,
    _stable_service_working_dir,
)
from hermes_constants import get_hermes_home


def get_dashboard_launchd_label() -> str:
    """LaunchAgent label for the dashboard, scoped per profile."""
    suffix = _profile_suffix()
    return f"ai.hermes.dashboard-{suffix}" if suffix else "ai.hermes.dashboard"


def get_dashboard_launchd_plist_path() -> Path:
    """`~/Library/LaunchAgents/<label>.plist` under the real account home."""
    import pwd
    suffix = _profile_suffix()
    name = f"ai.hermes.dashboard-{suffix}" if suffix else "ai.hermes.dashboard"
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / "Library" / "LaunchAgents" / f"{name}.plist"


def generate_dashboard_launchd_plist(
    host: str, port: int, *, extra_args: list[str] | None = None
) -> str:
    """Generate launchd plist XML for the dashboard service.

    Profile policy (per controller design):
    - Default profile: pins `-p default` before subcommand so sticky active_profile
      can't reroute the supervised server.
    - Named profile: `--profile <name> --isolated` so the service owns a dedicated
      per-profile server.
    """
    working_dir = _stable_service_working_dir()
    hermes_home = str(get_hermes_home().resolve())
    log_dir = get_hermes_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    label = get_dashboard_launchd_label()
    venv_dir = _service_venv_dir()

    priority_dirs = _build_service_path_dirs()
    _append_node_dir_for_service(priority_dirs)
    sane_path = ":".join(dict.fromkeys(priority_dirs + [p for p in os.environ.get("PATH", "").split(":") if p]))

    profile_parts: list[str] = []
    suffix = _profile_suffix()
    if suffix:
        profile_parts.extend(["--profile", suffix, "--isolated"])
    else:
        profile_parts.extend(["-p", "default"])

    args: list[str] = [
        get_python_path(),
        "-m", "hermes_cli.main",
        *profile_parts,
        "dashboard",
        "--host", host,
        "--port", str(port),
        "--no-open",
        *(extra_args or []),
    ]

    prog_args_xml = "\n        ".join(
        f"<string>{part}</string>" for part in args
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        {prog_args_xml}
    </array>

    <key>WorkingDirectory</key>
    <string>{working_dir}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{sane_path}</string>
        <key>VIRTUAL_ENV</key>
        <string>{venv_dir}</string>
        <key>HERMES_HOME</key>
        <string>{hermes_home}</string>
        <key>HERMES_SUPERVISED_CHILD</key>
        <string>1</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <!-- ThrottleInterval: 30s minimum between respawns prevents crash-loop storms. -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <!-- ExitTimeOut: 25s graceful-drain headroom before SIGTERM -> SIGKILL. -->
    <key>ExitTimeOut</key>
    <integer>25</integer>

    <key>StandardOutPath</key>
    <string>{log_dir}/dashboard.log</string>

    <key>StandardErrorPath</key>
    <string>{log_dir}/dashboard.error.log</string>
</dict>
</plist>
"""


# ------------------------------------------------------------------
# Service lifecycle (macOS-gated; Linux prints a clear hint and exits)
# ------------------------------------------------------------------


def dashboard_service_install(host: str, port: int, extra_args: list[str] | None = None, *, force: bool = False) -> None:
    """Write the dashboard LaunchAgent plist and bootstrap it."""
    if not is_macos():
        print("Dashboard launchd service is only supported on macOS; on Linux use a systemd unit.")
        sys.exit(1)
    plist_path = get_dashboard_launchd_plist_path()
    if plist_path.exists() and not force:
        print(f"Dashboard service already installed at: {plist_path}")
        print("Use --force to reinstall.")
        return
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(generate_dashboard_launchd_plist(host, port, extra_args=extra_args), encoding="utf-8")
    label = get_dashboard_launchd_label()
    domain = _launchd_domain()
    # Boot out any stale registration before bootstrap (same pattern as gateway).
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=False, timeout=30)
    try:
        _launchctl_bootstrap(domain, plist_path, label, timeout=30)
    except subprocess.CalledProcessError as e:
        _launchd_degrade_or_raise(e, "launchctl bootstrap")
        return
    print(f"Dashboard service installed at: {plist_path}")


def dashboard_service_start(host: str, port: int, extra_args: list[str] | None = None) -> None:
    if not is_macos():
        print("Dashboard launchd service is only supported on macOS; on Linux use a systemd unit.")
        sys.exit(1)
    plist_path = get_dashboard_launchd_plist_path()
    label = get_dashboard_launchd_label()
    domain = _launchd_domain()
    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True, timeout=30)
    except subprocess.CalledProcessError as e:
        if e.returncode == 5:  # EIO = already registered; recover.
            subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=False, timeout=30)
            subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True, timeout=30)
        else:
            raise
    try:
        subprocess.run(["launchctl", "kickstart", f"{domain}/{label}"], check=True, timeout=30)
    except subprocess.CalledProcessError:
        print(f"Failed to kickstart {domain}/{label}; try: launchctl kickstart -k {domain}/{label}")
        raise
    print("Dashboard service started.")


def dashboard_service_stop() -> None:
    if not is_macos():
        print("Dashboard launchd service is only supported on macOS; on Linux use a systemd unit.")
        sys.exit(1)
    label = get_dashboard_launchd_label()
    domain = _launchd_domain()
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=False, timeout=30)
    print("Dashboard service stopped.")


def dashboard_service_restart(host: str, port: int, extra_args: list[str] | None = None) -> None:
    if not is_macos():
        print("Dashboard launchd service is only supported on macOS; on Linux use a systemd unit.")
        sys.exit(1)
    label = get_dashboard_launchd_label()
    domain = _launchd_domain()
    try:
        subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=True, timeout=90)
        print("Dashboard service restarted.")
    except subprocess.CalledProcessError:
        print(f"Failed to restart; try: launchctl kickstart -k {domain}/{label}")
        raise


def dashboard_service_uninstall() -> None:
    if not is_macos():
        print("Dashboard launchd service is only supported on macOS; on Linux use a systemd unit.")
        sys.exit(1)
    plist_path = get_dashboard_launchd_plist_path()
    label = get_dashboard_launchd_label()
    domain = _launchd_domain()
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=False, timeout=30)
    if plist_path.exists():
        # Only delete if the label matches our namespace (defensive).
        plist_text = plist_path.read_text(encoding="utf-8")
        if label in plist_text:
            plist_path.unlink()
            print(f"Removed {plist_path}")
    print("Dashboard service uninstalled.")


def dashboard_service_status() -> None:
    """Print launchd state + HTTP /api/status probe result."""
    plist_path = get_dashboard_launchd_plist_path()
    label = get_dashboard_launchd_label()

    # Launchd registration / PID from plist ProgramArguments (source of truth)
    host: str | None = None
    port: int | None = None
    if plist_path.exists():
        try:
            plist_data = plistlib.loads(plist_path.read_bytes())
            prog_args = plist_data.get("ProgramArguments", [])
            # Find --host and --port from ProgramArguments array
            for i, arg in enumerate(prog_args):
                if arg == "--host" and i + 1 < len(prog_args):
                    host = prog_args[i + 1]
                elif arg == "--port" and i + 1 < len(prog_args):
                    try:
                        port = int(prog_args[i + 1])
                    except ValueError:
                        pass
        except Exception as exc:
            print(f"Could not read installed plist: {exc}")
    else:
        print("Dashboard service is not installed (no plist found).")
        sys.exit(1)

    domain = _launchd_domain()
    launchd_registered = False
    launchd_pid = None
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=10,
        )
        launchd_registered = result.returncode == 0
        # Parse PID from output like `"PID" = 1234;`
        for line in result.stdout.splitlines():
            if '"PID"' in line and '=' in line:
                try:
                    pid_str = line.split('=', 1)[1].strip().rstrip(';').strip('"')
                    pid_val = int(pid_str)
                    if pid_val > 0:
                        launchd_pid = pid_val
                except ValueError:
                    pass
    except Exception:
        pass

    if launchd_registered and launchd_pid is not None:
        print(f"Dashboard service registered with launchd: {label}")
        print(f"Supervising PID: {launchd_pid}")
    elif launchd_registered:
        print(f"Dashboard service registered with launchd: {label} (not running)")
    else:
        print(f"Dashboard service not registered with launchd: {label}")

    # HTTP probe to configured host:port (read from plist, the source of truth)
    if host is not None and port is not None:
        url = f"http://{host}:{port}/api/status"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                print(f"Dashboard HTTP up ({resp.status}) at {url}")
        except urllib.error.HTTPError as exc:
            # Any HTTP response (including 401) = server is up.
            print(f"Dashboard HTTP responding ({exc.code}) at {url}")
        except Exception as exc:
            print(f"Dashboard HTTP down (connection error): {exc}")
    else:
        print("Could not determine host/port from installed plist for HTTP probe.")


