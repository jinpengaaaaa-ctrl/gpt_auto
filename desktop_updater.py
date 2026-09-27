"""GitHub Releases updater used by the Windows desktop console."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

REPOSITORY = "jpwcreate/gpt_auto"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
VERSION_FILE = Path(__file__).resolve().with_name("VERSION")


def current_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip().lstrip("v")
    except OSError:
        return "0.0.0"


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for item in value.lstrip("v").split("."):
        digits = "".join(char for char in item if char.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts or [0])


def latest_release(timeout: float = 5.0) -> dict[str, Any] | None:
    request = urllib.request.Request(
        API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "GPT-Automation-Desktop"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    tag = str(payload.get("tag_name", ""))
    if not tag or _version_tuple(tag) <= _version_tuple(current_version()):
        return None
    assets = payload.get("assets") or []
    installer = next(
        (
            asset
            for asset in assets
            if str(asset.get("name", "")).lower().endswith((".exe", ".msi"))
        ),
        None,
    )
    if not installer:
        return None
    return {
        "version": tag.lstrip("v"),
        "name": payload.get("name") or tag,
        "body": payload.get("body") or "",
        "asset_name": installer.get("name", ""),
        "asset_url": installer.get("browser_download_url", ""),
    }


def install_release(release: dict[str, Any], *, silent: bool = False) -> Path:
    """Download an official release asset and launch the installer.

    The host is restricted to GitHub release domains so a compromised API
    response cannot turn the update button into an arbitrary download.
    """
    url = str(release.get("asset_url", ""))
    if not url.startswith(("https://github.com/", "https://objects.githubusercontent.com/")):
        raise ValueError("更新地址不是受信任的 GitHub Releases 地址")
    target_dir = Path(tempfile.gettempdir()) / "gpt-automation-update"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / Path(str(release.get("asset_name", "update.exe"))).name
    request = urllib.request.Request(url, headers={"User-Agent": "GPT-Automation-Desktop"})
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            output.write(block)
    args = [str(target)]
    if target.suffix.lower() == ".exe" and silent:
        args.append("/VERYSILENT")
    subprocess.Popen(args, close_fds=True)
    return target


def check_and_install(*, silent: bool = False) -> dict[str, Any] | None:
    release = latest_release()
    if release is None:
        return None
    install_release(release, silent=silent)
    return release

