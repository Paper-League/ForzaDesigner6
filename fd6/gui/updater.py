"""In-app update check + opt-in self-update against the GitHub Releases API.

Flow:
  1. On launch (frozen builds only), a background QThread queries
     api.github.com/repos/<OWNER>/<REPO>/releases/latest.
  2. If the latest tag's version is newer than the running fd6.__version__, the
     GUI shows a "Update X available — update now?" prompt.
  3. If the user accepts, we download the release's .exe asset to a temp file,
     write a tiny .bat that waits for FD6 to exit, swaps the new exe in, and
     relaunches it — then quit the app so the swap can happen.

Everything is best-effort and fully guarded: no network, a private/missing
repo, a rate-limit, or any parse error simply means "no update offered" and the
app continues normally. Update checks are skipped entirely when running from
source (not frozen), since there's no single exe to replace.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

import fd6

# GitHub repo that publishes FD6 releases.
GITHUB_OWNER = "tokyubevoxelverse"
GITHUB_REPO = "ForzaDesigner6"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"


def _parse_version(text: str) -> tuple[int, ...]:
    """Extract a comparable numeric version tuple from arbitrary tag text.

    Handles tags like 'v0.5.1', '0.5.1', 'Multi-Support-v3456-0.5.1' — we grab
    the LAST dotted-number group so prefixes/build ids don't confuse the compare.
    Returns () when nothing numeric is found (treated as "older than anything").
    """
    matches = re.findall(r"\d+(?:\.\d+)+", text or "")
    if not matches:
        return ()
    return tuple(int(p) for p in matches[-1].split("."))


def _is_newer(latest: str, current: str) -> bool:
    lv, cv = _parse_version(latest), _parse_version(current)
    if not lv:
        return False
    # Pad to equal length for a clean tuple compare (0.5 == 0.5.0).
    n = max(len(lv), len(cv))
    lv += (0,) * (n - len(lv))
    cv += (0,) * (n - len(cv))
    return lv > cv


class UpdateInfo:
    def __init__(self, version: str, tag: str, asset_url: str | None,
                 asset_name: str | None, notes: str) -> None:
        self.version = version
        self.tag = tag
        self.asset_url = asset_url
        self.asset_name = asset_name
        self.notes = notes


class UpdateChecker(QObject):
    """Background GitHub release check. Emits `update_available` only when a
    strictly-newer release with a downloadable .exe asset exists."""

    update_available = Signal(object)  # UpdateInfo
    no_update = Signal()
    failed = Signal(str)

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                RELEASES_API,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "FD6-Updater"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        tag = str(data.get("tag_name") or data.get("name") or "")
        current = fd6.__version__
        if not _is_newer(tag, current):
            self.no_update.emit()
            return
        # Find the first .exe asset to download.
        asset_url = asset_name = None
        for a in data.get("assets", []) or []:
            name = str(a.get("name", ""))
            if name.lower().endswith(".exe"):
                asset_url = a.get("browser_download_url")
                asset_name = name
                break
        info = UpdateInfo(
            version=".".join(str(p) for p in _parse_version(tag)) or tag,
            tag=tag,
            asset_url=asset_url,
            asset_name=asset_name,
            notes=str(data.get("body") or "").strip(),
        )
        self.update_available.emit(info)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_exe_path() -> Path:
    return Path(sys.executable)


def download_and_apply(info: UpdateInfo, progress_cb=None) -> None:
    """Download the new exe and stage a swap-and-relaunch, then exit.

    A small .bat waits for THIS exe to exit (so Windows releases the file lock),
    moves the downloaded exe over it, and relaunches. We then quit the app.
    Raises on failure so the caller can show an error and continue running.
    """
    if not info.asset_url:
        raise RuntimeError("This release has no downloadable .exe asset.")
    exe = current_exe_path()
    tmp_dir = Path(tempfile.gettempdir())
    new_exe = tmp_dir / (info.asset_name or "FD6_update.exe")

    req = urllib.request.Request(info.asset_url, headers={"User-Agent": "FD6-Updater"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        with open(new_exe, "wb") as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                read += len(chunk)
                if progress_cb and total:
                    progress_cb(read, total)

    # Swap script: wait for the old process to release the exe, replace it, relaunch.
    bat = tmp_dir / "fd6_update.bat"
    bat.write_text(
        "@echo off\r\n"
        "echo Updating Forza Designer 6...\r\n"
        ":wait\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        f'tasklist /fi "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul\r\n'
        "if not errorlevel 1 goto wait\r\n"
        f'move /y "{new_exe}" "{exe}" >nul\r\n'
        f'start "" "{exe}"\r\n'
        'del "%~f0"\r\n',
        encoding="ascii",
    )
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
