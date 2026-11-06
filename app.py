#!/usr/bin/env python3
"""Launch the TREC Rulebook desktop application.

Example:
    python app.py
    python app.py --data data/trec_library.json

The application is offline after installation; it uses only Python's standard
library plus Tkinter.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

ROOT = Path(__file__).resolve().parent
# PyInstaller one-file builds unpack bundled data under sys._MEIPASS.  The
# explicit lookup keeps both source runs and frozen builds working.
if getattr(sys, "frozen", False):
    ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
sys.path.insert(0, str(ROOT))

from trec_explorer.ui import TrecApp  # noqa: E402


def enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TREC Rulebook desktop app")
    parser.add_argument(
        "--data",
        type=Path,
        default=ROOT / "data" / "trec_library.json",
        help="Path to trec_library.json",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Build the window, update once, and exit (for installation checks).",
    )
    args = parser.parse_args(argv)

    data_path = args.data.expanduser().resolve()
    if not data_path.exists():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Library not found",
            "The TREC library was not found at:\n"
            f"{data_path}\n\n"
            "Run this once from the project folder:\n"
            "    python tools/build_library.py",
            parent=root,
        )
        root.destroy()
        return 2

    enable_dpi_awareness()
    root = tk.Tk()
    try:
        TrecApp(root, data_path)
    except Exception as exc:  # pragma: no cover - startup failure UI path
        root.withdraw()
        messagebox.showerror("Could not start TREC Rulebook", str(exc), parent=root)
        root.destroy()
        return 1

    if args.smoke_test:
        root.update_idletasks()
        root.update()
        root.after(250, root.destroy)
        root.mainloop()
        print("TREC Rulebook smoke test: OK")
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
