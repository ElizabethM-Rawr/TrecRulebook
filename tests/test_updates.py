from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trec_explorer import storage, updates  # noqa: E402


def payload(hashes: dict[str, str]) -> dict:
    return {"meta": {"source_hashes": hashes}, "sections": [{"uid": "x"}]}


class UpdateTests(unittest.TestCase):
    def test_due_uses_24_hour_window(self) -> None:
        self.assertTrue(updates.is_due({}))
        self.assertFalse(
            updates.is_due({"last_update_check": "2099-01-01T00:00:00+00:00"})
        )
        self.assertTrue(
            updates.is_due({"last_update_check": "2020-01-01T00:00:00+00:00"})
        )

    def test_changed_source_hash_returns_payload(self) -> None:
        new = payload({"rules": "new"})
        with patch.object(updates.ingest, "build_payload", return_value=new):
            result, changed = updates.fetch_update(
                {"source_hashes": {"rules": "old"}}
            )
        self.assertTrue(changed)
        self.assertIs(result, new)

    def test_same_source_hash_returns_no_payload(self) -> None:
        new = payload({"rules": "same"})
        with patch.object(updates.ingest, "build_payload", return_value=new):
            result, changed = updates.fetch_update(
                {"source_hashes": {"rules": "same"}}
            )
        self.assertFalse(changed)
        self.assertIsNone(result)

    def test_newer_user_library_wins(self) -> None:
        bundled = Path("bundled.json")
        user = Path("user.json")
        with (
            patch.object(storage, "user_library_path", return_value=user),
            patch.object(storage.Path, "exists", return_value=True),
            patch.object(
                storage,
                "_meta_timestamp",
                side_effect=[
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    datetime(2026, 6, 1, tzinfo=timezone.utc),
                ],
            ),
        ):
            self.assertEqual(storage.choose_library_path(bundled), user)


if __name__ == "__main__":
    unittest.main(verbosity=2)
