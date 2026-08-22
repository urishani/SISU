"""Check GitHub for a newer SISU copy, install libraries, and restart."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class UpdateInfo:
    local: str
    remote: str
    ahead_behind: str
    dirty: bool


@dataclass
class AppVersion:
    number: int
    date: str
    commit: str = ""

    def label(self) -> str:
        if self.number and self.date:
            return f"version {self.number}, {self.date}"
        if self.number:
            return f"version {self.number}"
        if self.date:
            return f"version {self.date}"
        return ""


class UpdateError(Exception):
    pass


def _git(*args: str, timeout: int = 40) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(APP_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        creationflags=CREATE_NO_WINDOW,
    )


def _python_for_pip() -> str:
    exe = Path(sys.executable)
    name = exe.name.lower()
    if name == "pythonw.exe":
        alt = exe.with_name("python.exe")
        if alt.exists():
            return str(alt)
    if name == "pyw.exe":
        alt = exe.with_name("py.exe")
        if alt.exists():
            return str(alt)
    return sys.executable


def is_git_copy() -> bool:
    try:
        result = _git("rev-parse", "--is-inside-work-tree", timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def read_app_version() -> AppVersion:
    """Version is the Git commit count on this copy, with the date of HEAD."""
    if not is_git_copy():
        return AppVersion(number=0, date="")
    try:
        count = _git("rev-list", "--count", "HEAD", timeout=8)
        stamped = _git("log", "-1", "--format=%cs", timeout=8)
        commit = _git("rev-parse", "--short", "HEAD", timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return AppVersion(number=0, date="")
    number = 0
    if count.returncode == 0:
        try:
            number = int((count.stdout or "").strip())
        except ValueError:
            number = 0
    date = stamped.stdout.strip() if stamped.returncode == 0 else ""
    return AppVersion(
        number=number,
        date=date,
        commit=commit.stdout.strip() if commit.returncode == 0 else "",
    )


def check_for_update() -> UpdateInfo | None:
    if not is_git_copy():
        return None
    fetch = _git("fetch", "--prune", timeout=45)
    if fetch.returncode != 0:
        raise UpdateError(fetch.stderr.strip() or "Could not reach GitHub to check for updates.")
    local = _git("rev-parse", "HEAD", timeout=8)
    remote = _git("rev-parse", "@{u}", timeout=8)
    if local.returncode != 0:
        raise UpdateError("Could not read the current SISU version.")
    if remote.returncode != 0:
        return None
    local_hash = local.stdout.strip()
    remote_hash = remote.stdout.strip()
    if local_hash == remote_hash:
        return None
    status = _git("status", "-sb", timeout=8)
    dirty = _git("status", "--porcelain", timeout=8)
    return UpdateInfo(
        local=local_hash,
        remote=remote_hash,
        ahead_behind=(status.stdout.splitlines() or [""])[0].strip(),
        dirty=bool(dirty.stdout.strip()),
    )


def apply_update() -> None:
    if not is_git_copy():
        raise UpdateError("Git is not available in this SISU folder, so it cannot update itself.")
    info = check_for_update()
    if info is None:
        return
    if info.dirty:
        raise UpdateError(
            "This copy of SISU has local file changes, so it cannot update automatically.\n"
            "Update this folder by hand, or wait until local changes are cleared."
        )
    pull = _git("pull", "--ff-only")
    if pull.returncode != 0:
        raise UpdateError(pull.stderr.strip() or "Git could not apply the new version.")
    pip = subprocess.run(
        [_python_for_pip(), "-m", "pip", "install", "-r", str(APP_DIR / "requirements.txt")],
        cwd=str(APP_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        creationflags=CREATE_NO_WINDOW,
    )
    if pip.returncode != 0:
        raise UpdateError(pip.stderr.strip() or "The new version downloaded, but extra libraries could not be installed.")


def restart_process() -> None:
    script = str(APP_DIR / "app.py")
    subprocess.Popen([sys.executable, script], cwd=str(APP_DIR), close_fds=True)
