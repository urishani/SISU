"""Speak title text with the Windows built-in speech engine."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

_process: subprocess.Popen | None = None


def stop_speech() -> None:
    global _process
    proc = _process
    _process = None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass


def speak_text(text: str, *, hebrew: bool = False) -> bool:
    """Read text aloud with SAPI. Hebrew uses a he-IL voice when Windows has one."""
    global _process
    value = str(text or "").strip()
    if not value:
        return False
    stop_speech()
    folder = Path(tempfile.gettempdir()) / "SISU-share"
    folder.mkdir(parents=True, exist_ok=True)
    body = folder / "sisu_speak.txt"
    script = folder / "sisu_speak.ps1"
    body.write_text(value, encoding="utf-8")

    def ps_str(item: str) -> str:
        return "'" + item.replace("'", "''") + "'"

    culture = "he" if hebrew else "en"
    script.write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "Add-Type -AssemblyName System.Speech",
                "$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer",
                f"$want = {ps_str(culture)}",
                "foreach ($voice in $speak.GetInstalledVoices()) {",
                "  $name = $voice.VoiceInfo.Culture.Name",
                "  if ($name -like ($want + '*')) {",
                "    $speak.SelectVoice($voice.VoiceInfo.Name)",
                "    break",
                "  }",
                "}",
                "$speak.Rate = -1",
                f"$speak.Speak([System.IO.File]::ReadAllText({ps_str(str(body))}, [System.Text.Encoding]::UTF8))",
            ]
        ),
        encoding="utf-8-sig",
    )
    try:
        _process = subprocess.Popen(
            ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        _process = None
        return False
    return True
