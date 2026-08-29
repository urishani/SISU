"""Check GitHub for a newer SISU copy, install libraries, and restart."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_DATA_ROOTS = ("cache/", "lists/")
_DATA_FILES = {"field_aliases.json", "config.json"}


@dataclass
class UpdateInfo:
    local: str
    remote: str
    ahead_behind: str
    dirty: bool
    data_files: list[str] = field(default_factory=list)
    code_files: list[str] = field(default_factory=list)


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


def _normalize_git_path(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def is_data_json(path: str) -> bool:
    """Local scan cache / aliases JSON that should not block an update."""
    name = _normalize_git_path(path)
    if not name:
        return False
    if any(name == root.rstrip("/") or name.startswith(root) for root in _DATA_ROOTS):
        return True
    if Path(name).name.lower() in _DATA_FILES:
        return True
    return name.lower().endswith(".json") and name.split("/", 1)[0] in {"cache", "lists"}


def _changed_paths() -> list[str]:
    result = _git("status", "--porcelain", timeout=8)
    if result.returncode != 0:
        return []
    paths: list[str] = []
    for raw in result.stdout.splitlines():
        if len(raw) < 4:
            continue
        body = raw[3:].strip()
        if " -> " in body:
            body = body.split(" -> ", 1)[1]
        if body.startswith('"') and body.endswith('"'):
            body = body[1:-1]
        name = _normalize_git_path(body)
        if name:
            paths.append(name)
    return paths


def _classify_changes(paths: list[str] | None = None) -> tuple[list[str], list[str]]:
    data: list[str] = []
    code: list[str] = []
    for path in paths if paths is not None else _changed_paths():
        if is_data_json(path):
            data.append(path)
        else:
            code.append(path)
    return data, code


def _conflicted_paths() -> list[str]:
    result = _git("diff", "--name-only", "--diff-filter=U", timeout=8)
    return [_normalize_git_path(line) for line in result.stdout.splitlines() if line.strip()]


def _keep_local_data_json(paths: list[str]) -> None:
    for path in paths:
        checkout = _git("checkout", "--theirs", "--", path, timeout=15)
        if checkout.returncode != 0:
            raise UpdateError(
                f"Could not keep the local cache file {path} after the update.\n"
                "Finish the update by hand."
            )
        _git("add", "--", path, timeout=8)


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
    data_files, code_files = _classify_changes()
    return UpdateInfo(
        local=local_hash,
        remote=remote_hash,
        ahead_behind=(status.stdout.splitlines() or [""])[0].strip(),
        dirty=bool(code_files),
        data_files=data_files,
        code_files=code_files,
    )


def apply_update() -> None:
    if not is_git_copy():
        raise UpdateError("Git is not available in this SISU folder, so it cannot update itself.")
    info = check_for_update()
    if info is None:
        return
    data_files, code_files = _classify_changes()
    if code_files:
        listed = "\n".join(f"• {path}" for path in code_files[:12])
        extra = "" if len(code_files) <= 12 else f"\n• … and {len(code_files) - 12} more"
        raise UpdateError(
            "This copy of SISU has local program-file changes, so it cannot update automatically.\n"
            "Stash or copy those files, update this folder by hand, then put them back.\n\n"
            f"{listed}{extra}"
        )
    stashed = False
    stash_message = f"sisu-auto-update {int(time.time())}"
    if data_files:
        stash = _git("stash", "push", "-m", stash_message, "--", *data_files, timeout=30)
        if stash.returncode != 0:
            raise UpdateError(
                stash.stderr.strip()
                or "Could not stash the local JSON cache so the update can download."
            )
        stashed = "No local changes to save" not in (stash.stdout + stash.stderr)
    pull = _git("pull", "--ff-only", timeout=60)
    if pull.returncode != 0:
        if stashed:
            _git("stash", "pop", timeout=30)
        raise UpdateError(pull.stderr.strip() or "Git could not apply the new version.")
    if stashed:
        pop = _git("stash", "pop", timeout=30)
        if pop.returncode != 0:
            conflicts = _conflicted_paths()
            if conflicts and all(is_data_json(path) for path in conflicts):
                _keep_local_data_json(conflicts)
                _git("stash", "drop", timeout=15)
            else:
                names = ", ".join(conflicts) if conflicts else "local files"
                raise UpdateError(
                    "The new version downloaded, but restoring local files needs a manual step.\n"
                    f"Git could not put these back automatically: {names}\n\n"
                    "Your changes are still in git stash. Finish with git stash pop after resolving, "
                    "or update this folder by hand."
                )
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
