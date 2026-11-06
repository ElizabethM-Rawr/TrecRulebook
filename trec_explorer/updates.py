"""Background source refresh for the desktop app.

The updater downloads the same public pages as ``tools/build_library.py``,
rebuilds the JSON payload, and writes it to the user's writable app-data
folder.  It never overwrites the bundled library.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import ingest, storage

CHECK_INTERVAL = timedelta(hours=24)


def is_due(settings: dict | None = None, *, force: bool = False) -> bool:
    if force:
        return True
    settings = settings if settings is not None else storage.read_settings()
    raw = settings.get("last_update_check")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    now = datetime.now(last.tzinfo) if last.tzinfo else datetime.now()
    return now - last >= CHECK_INTERVAL


def mark_checked(**extra) -> None:
    settings = storage.read_settings()
    settings["last_update_check"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    settings.pop("last_update_error", None)
    settings.update(extra)
    storage.write_settings(settings)


def mark_error(message: str) -> None:
    """Record a failed attempt without suppressing the next startup retry."""
    settings = storage.read_settings()
    settings["last_update_error"] = message[:240]
    storage.write_settings(settings)


def source_hashes(payload: dict) -> dict[str, str]:
    hashes = payload.get("meta", {}).get("source_hashes", {})
    return {str(key): str(value) for key, value in hashes.items()}


def fetch_update(
    current_meta: dict,
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict | None, bool]:
    """Fetch and parse current sources.

    Returns ``(payload, changed)``.  A payload is returned only when the
    source bytes differ from the active library, so a normal startup does not
    rewrite the user cache.  Network and parse errors propagate to the caller.
    """
    payload = ingest.build_payload(progress=progress)
    old_hashes = source_hashes({"meta": current_meta})
    new_hashes = source_hashes(payload)
    changed = not old_hashes or any(
        new_hashes.get(key) != old_hashes.get(key) for key in new_hashes
    )
    return (payload if changed else None), changed


def stage_payload(payload: dict) -> Path:
    target = storage.user_library_path()
    storage.write_json_atomic(target, payload)
    return target
