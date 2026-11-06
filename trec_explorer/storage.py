"""Local paths and atomic JSON helpers shared by the app and updater."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "TRECRulesExplorer"
    return Path.home() / ".trec_rules_explorer"


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def user_library_path() -> Path:
    return app_data_dir() / "trec_library.json"


def _meta_timestamp(path: Path) -> datetime | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = str(payload.get("meta", {}).get("generated_at", ""))
        return datetime.fromisoformat(raw) if raw else None
    except (OSError, ValueError):
        return None


def choose_library_path(bundled: Path) -> Path:
    """Prefer a newer auto-updated library, otherwise use the bundled one."""
    updated = user_library_path()
    if not updated.exists():
        return bundled
    bundled_time = _meta_timestamp(bundled)
    updated_time = _meta_timestamp(updated)
    if updated_time is None:
        return bundled
    if bundled_time is None:
        return updated
    if updated_time.tzinfo is None and bundled_time.tzinfo is not None:
        updated_time = updated_time.replace(tzinfo=bundled_time.tzinfo)
    elif bundled_time.tzinfo is None and updated_time.tzinfo is not None:
        bundled_time = bundled_time.replace(tzinfo=updated_time.tzinfo)
    return updated if updated_time >= bundled_time else bundled


def read_settings() -> dict:
    try:
        payload = json.loads(settings_path().read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(values: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(values)
    payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    tmp = path.with_suffix(".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        path.write_text(text, encoding="utf-8")


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        path.write_text(text, encoding="utf-8")


def resource_root() -> Path:
    """Where bundled read-only data lives (source run or PyInstaller)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent
