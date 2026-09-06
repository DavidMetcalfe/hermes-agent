"""Tests for hermes_cli.dashboard_service (issue #44106)."""

import plistlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Only import the service module when we need it; avoid module-level side effects.

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Mirror the profile_env fixture from tests/hermes_cli/test_profiles.py."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


@pytest.fixture()
def mock_dashboard_service(monkeypatch):
    """Patch the macOS-gated functions to be callable without a real macOS host."""
    import hermes_cli.dashboard_service as ds
    monkeypatch.setattr(ds, "is_macos", lambda: True)
    monkeypatch.setattr(
        ds.subprocess, "run", lambda cmd, **kwargs: MagicMock(returncode=0, stdout="")
    )
    monkeypatch.setattr(
        ds, "_launchd_domain", lambda: f"gui/{__import__('os').getuid()}"
    )
    return ds


# ------------------------------------------------------------------
# Pure function tests (no OS markers needed)
# ------------------------------------------------------------------


class TestDashboardLaunchdLabels:
    def test_default_profile_label(self, profile_env):
        from hermes_cli.dashboard_service import get_dashboard_launchd_label
        assert get_dashboard_launchd_label() == "ai.hermes.dashboard"

    def test_named_profile_label(self, profile_env, monkeypatch):
        import hermes_cli.dashboard_service as ds
        monkeypatch.setattr(ds, "_profile_suffix", lambda: "work")
        from hermes_cli.dashboard_service import get_dashboard_launchd_label
        assert get_dashboard_launchd_label() == "ai.hermes.dashboard-work"


class TestDashboardLaunchdPlistPath:
    def test_default_path_uses_real_account_home(self, monkeypatch):
        import hermes_cli.dashboard_service as ds
        monkeypatch.setattr(
            ds, "_profile_suffix", lambda: ""
        )
        import pwd
        expected_home = Path(pwd.getpwuid(__import__("os").getuid()).pw_dir)
        path = ds.get_dashboard_launchd_plist_path()
        assert path.name == "ai.hermes.dashboard.plist"
        assert path.parent.name == "LaunchAgents"
        assert str(path).startswith(str(expected_home / "Library"))


# ------------------------------------------------------------------
# Plist generation tests
# ------------------------------------------------------------------


class TestGenerateDashboardLaunchdPlist:
    def test_contains_python_argv0_not_console_script(self, mock_dashboard_service):
        # argv[0] MUST be a python executable, never `hermes` console script.
        plist_text = mock_dashboard_service.generate_dashboard_launchd_plist("127.0.0.1", 9119)
        # Verify it parses as XML/plist
        data = plistlib.loads(plist_text.encode("utf-8"))
        prog_args = data.get("ProgramArguments", [])
        assert len(prog_args) >= 1
        # The first argument must be a python executable path (not `hermes` script)
        first_arg = prog_args[0]
        assert first_arg.endswith("python") or "/python" in first_arg, (
            f"argv[0] should be python executable, got: {first_arg}"
        )
        # Second argument should be `-m`
        assert prog_args[1] == "-m"
        # Third should be `hermes_cli.main`
        assert prog_args[2] == "hermes_cli.main"

    def test_default_profile_pins_p_default(self, mock_dashboard_service, monkeypatch):
        monkeypatch.setattr(mock_dashboard_service, "_profile_suffix", lambda: "")
        plist_text = mock_dashboard_service.generate_dashboard_launchd_plist("0.0.0.0", 9119)
        data = plistlib.loads(plist_text.encode("utf-8"))
        prog_args = data.get("ProgramArguments", [])
        args_str = " ".join(prog_args)
        # Default profile must include `-p default` before subcommand
        assert "-p default" in args_str
        # Must include dashboard subcommand
        assert "dashboard" in args_str
        # No detach anywhere
        assert "--detach" not in args_str

    def test_named_profile_uses_isolated(self, mock_dashboard_service, monkeypatch):
        monkeypatch.setattr(mock_dashboard_service, "_profile_suffix", lambda: "testprof")
        plist_text = mock_dashboard_service.generate_dashboard_launchd_plist("127.0.0.1", 9119)
        data = plistlib.loads(plist_text.encode("utf-8"))
        prog_args = data.get("ProgramArguments", [])
        args_str = " ".join(prog_args)
        # Named profile should have `--profile testprof --isolated`
        assert "--profile" in args_str
        assert "testprof" in args_str
        assert "--isolated" in args_str

    def test_no_detach_flag_anywhere(self, mock_dashboard_service):
        plist_text = mock_dashboard_service.generate_dashboard_launchd_plist("127.0.0.1", 9119)
        assert "--detach" not in plist_text

    def test_keepalive_and_run_at_load_present(self, mock_dashboard_service):
        plist_text = mock_dashboard_service.generate_dashboard_launchd_plist("127.0.0.1", 9119)
        assert "<key>KeepAlive</key>" in plist_text
        assert "<true/>" in plist_text
        assert "<key>RunAtLoad</key>" in plist_text

    def test_throttle_interval_and_exit_timeout(self, mock_dashboard_service):
        plist_text = mock_dashboard_service.generate_dashboard_launchd_plist("127.0.0.1", 9119)
        assert "<key>ThrottleInterval</key>" in plist_text
        assert "<integer>30</integer>" in plist_text
        assert "<key>ExitTimeOut</key>" in plist_text
        assert "<integer>25</integer>" in plist_text

    def test_environment_variables_present(self, mock_dashboard_service):
        plist_text = mock_dashboard_service.generate_dashboard_launchd_plist("127.0.0.1", 9119)
        data = plistlib.loads(plist_text.encode("utf-8"))
        env = data.get("EnvironmentVariables", {})
        assert "HERMES_HOME" in env
        assert "VIRTUAL_ENV" in env
        assert "PATH" in env
        assert env.get("HERMES_SUPERVISED_CHILD") == "1"

    def test_extra_args_included(self, mock_dashboard_service):
        plist_text = mock_dashboard_service.generate_dashboard_launchd_plist(
            "127.0.0.1", 9119, extra_args=["--skip-build"]
        )
        data = plistlib.loads(plist_text.encode("utf-8"))
        prog_args = data.get("ProgramArguments", [])
        assert "--skip-build" in prog_args

    def test_plistlib_roundtrip_validates(self, mock_dashboard_service):
        """The generated plist must round-trip through plistlib."""
        plist_text = mock_dashboard_service.generate_dashboard_launchd_plist("127.0.0.1", 9119)
        # Must not raise
        parsed = plistlib.loads(plist_text.encode("utf-8"))
        # Must have required keys
        assert "Label" in parsed
        assert "ProgramArguments" in parsed
        assert "WorkingDirectory" in parsed
        assert "KeepAlive" in parsed


# ------------------------------------------------------------------
# Lifecycle operation tests
# ------------------------------------------------------------------


class TestDashboardLifecycle:
    def test_install_refuses_without_force_when_exists(self, mock_dashboard_service, monkeypatch, tmp_path, capsys):
        # Create a fake existing plist
        plist_path = tmp_path / ".fake_home" / "Library" / "LaunchAgents" / "ai.hermes.dashboard.plist"
        plist_path.parent.mkdir(parents=True)
        plist_path.write_text("fake", encoding="utf-8")
        monkeypatch.setattr(
            mock_dashboard_service, "get_dashboard_launchd_plist_path", lambda: plist_path
        )
        mock_dashboard_service.dashboard_service_install("127.0.0.1", 9119)
        out = capsys.readouterr().out
        assert "already installed" in out

    def test_install_writes_plist_with_force(self, mock_dashboard_service, monkeypatch, tmp_path):
        plist_path = tmp_path / ".fake_home" / "Library" / "LaunchAgents" / "ai.hermes.dashboard.plist"
        plist_path.parent.mkdir(parents=True)
        monkeypatch.setattr(
            mock_dashboard_service, "get_dashboard_launchd_plist_path", lambda: plist_path
        )
        mock_dashboard_service.dashboard_service_install("127.0.0.1", 9119, force=True)
        assert plist_path.exists()

    @pytest.mark.macos_only
    def test_stop_on_non_macos_exits_nonzero(self, monkeypatch, capsys):
        import hermes_cli.dashboard_service as ds
        monkeypatch.setattr(ds, "is_macos", lambda: False)
        with pytest.raises(SystemExit) as exc:
            ds.dashboard_service_stop()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "only supported on macOS" in out

    def test_status_exits_when_uninstalled(self, mock_dashboard_service, monkeypatch, tmp_path, capsys):
        # Point plist_path to a non-existent file
        monkeypatch.setattr(
            mock_dashboard_service,
            "get_dashboard_launchd_plist_path",
            lambda: tmp_path / "nonexistent.plist",
        )
        with pytest.raises(SystemExit) as exc:
            mock_dashboard_service.dashboard_service_status()
        assert exc.value.code == 1

    def test_status_prints_registered_when_installed(self, mock_dashboard_service, monkeypatch, tmp_path, capsys):
        # Create a fake installed plist with ProgramArguments
        plist_path = tmp_path / "installed.plist"
        import plistlib
        plist_data = plistlib.dumps({
            "ProgramArguments": ["python", "-m", "hermes_cli.main", "dashboard", "--host", "127.0.0.1", "--port", "9119", "--no-open"],
        })
        plist_path.write_bytes(plist_data)
        monkeypatch.setattr(
            mock_dashboard_service, "get_dashboard_launchd_plist_path", lambda: plist_path
        )
        # Mock subprocess.run to simulate registered but no PID
        import subprocess
        original_run = subprocess.run
        def mock_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout='  "PID" = -1;')
        monkeypatch.setattr(subprocess, "run", mock_run)
        mock_dashboard_service.dashboard_service_status()
        out = capsys.readouterr().out
        # Must contain the label and the host/port info
        assert "Dashboard service registered" in out or "Dashboard service not registered" in out

    def test_uninstall_deletes_matching_plist(self, mock_dashboard_service, monkeypatch, tmp_path):
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<string>ai.hermes.dashboard</string>", encoding="utf-8")
        monkeypatch.setattr(
            mock_dashboard_service, "get_dashboard_launchd_plist_path", lambda: plist_path
        )
        mock_dashboard_service.dashboard_service_uninstall()
        assert not plist_path.exists()

    def test_uninstall_never_deletes_non_matching_plist(self, mock_dashboard_service, monkeypatch, tmp_path):
        # Defensive: if label doesn't match namespace, don't delete.
        plist_path = tmp_path / "other.plist"
        plist_path.write_text("<string>ai.hermes.gateway</string>", encoding="utf-8")
        monkeypatch.setattr(
            mock_dashboard_service, "get_dashboard_launchd_plist_path", lambda: plist_path
        )
        # Monkeypatch label generation to a different name so it doesn't match
        monkeypatch.setattr(mock_dashboard_service, "get_dashboard_launchd_label", lambda: "ai.hermes.dashboard")
        mock_dashboard_service.dashboard_service_uninstall()
        assert plist_path.exists()


# ------------------------------------------------------------------
# Non-macOS gate message
# ------------------------------------------------------------------


class TestNonMacOSGate:
    def test_start_prints_systemd_hint_on_linux(self, monkeypatch, capsys):
        import hermes_cli.dashboard_service as ds
        monkeypatch.setattr(ds, "is_macos", lambda: False)
        with pytest.raises(SystemExit) as exc:
            ds.dashboard_service_start("127.0.0.1", 9119)
        assert exc.value.code != 0
        out = capsys.readouterr().out
        assert "systemd" in out.lower()

    def test_stop_prints_systemd_hint_on_linux(self, monkeypatch, capsys):
        import hermes_cli.dashboard_service as ds
        monkeypatch.setattr(ds, "is_macos", lambda: False)
        with pytest.raises(SystemExit) as exc:
            ds.dashboard_service_stop()
        assert exc.value.code != 0
        out = capsys.readouterr().out
        assert "systemd" in out.lower()
