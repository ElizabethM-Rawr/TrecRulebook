"""Theme palette tests."""

from __future__ import annotations

import unittest

from trec_explorer import ui


class ThemeTests(unittest.TestCase):
    def tearDown(self) -> None:
        ui.apply_theme(ui.LIGHT_THEME)

    def test_normalize_theme(self) -> None:
        self.assertEqual(ui.normalize_theme("dark"), ui.DARK_THEME)
        self.assertEqual(ui.normalize_theme("DARK"), ui.DARK_THEME)
        self.assertEqual(ui.normalize_theme(" light "), ui.LIGHT_THEME)
        self.assertEqual(ui.normalize_theme(""), ui.LIGHT_THEME)
        self.assertEqual(ui.normalize_theme(None), ui.LIGHT_THEME)

    def test_palettes_match(self) -> None:
        self.assertEqual(set(ui.LIGHT_PALETTE), set(ui.DARK_PALETTE))

    def test_apply_theme_swaps_globals(self) -> None:
        ui.apply_theme(ui.DARK_THEME)
        self.assertEqual(ui.BG, ui.DARK_PALETTE["BG"])
        self.assertEqual(ui.INK, ui.DARK_PALETTE["INK"])
        self.assertEqual(ui.STATUS_BG, ui.DARK_PALETTE["STATUS_BG"])

        ui.apply_theme(ui.LIGHT_THEME)
        self.assertEqual(ui.BG, ui.LIGHT_PALETTE["BG"])
        self.assertEqual(ui.INK, ui.LIGHT_PALETTE["INK"])


if __name__ == "__main__":
    unittest.main()
