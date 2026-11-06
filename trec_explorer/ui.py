"""Tkinter desktop interface for the TREC Rulebook."""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from tkinter import font as tkfont

from . import __version__, ai, storage, updates
from .engine import (
    ComparisonReport,
    ComparisonRow,
    Library,
    ScenarioReport,
    Section,
    highlight_spans,
)

# ---------------------------------------------------------------------------
# Palette and type
# ---------------------------------------------------------------------------

BG = "#f2eee4"
SURFACE = "#fffdf8"
SURFACE_ALT = "#faf6ec"
INK = "#1d2b30"
MUTED = "#687277"
FAINT = "#8b938f"
NAVY = "#123d49"
NAVY_DARK = "#0c2d36"
TEAL = "#1d6170"
BRICK = "#a6402d"
GOLD = "#c49a3a"
BORDER = "#d8d0bf"
SOFT = "#e9e3d5"
CHIP = "#e5efec"
CHIP_TEXT = "#215b60"
HIT = "#ffe6a1"
WHITE = "#ffffff"

FONT_UI = ("Segoe UI", 10)
FONT_UI_BOLD = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 9)
FONT_SMALL_BOLD = ("Segoe UI", 9, "bold")
FONT_BODY = ("Segoe UI", 11)
FONT_TITLE = ("Georgia", 21, "bold")
FONT_DISPLAY = ("Georgia", 16, "bold")
FONT_SECTION = ("Georgia", 18, "bold")

DISCLAIMER = (
    "This is an educational research aid, not legal advice. The Texas "
    "Secretary of State maintains the official text of agency rules; the Texas "
    "Legislature maintains the official statutes. Verify every citation against "
    "the official source before relying on it."
)

EXAMPLE_A = (
    "A sales agent wants to advertise a team name, \"The Smith Group,\" on "
    "social media and on a yard sign. The sponsoring broker has not registered "
    "the team name. The agent also wants to pay a $250 referral fee to an "
    "unlicensed friend who introduced a buyer."
)

EXAMPLE_B = (
    "A licensed inspector is asked by a real estate broker to inspect a property "
    "that the broker has listed. The inspector wants to advertise a free repair "
    "estimate and then perform repairs on the defects found during the inspection."
)


def _app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "TRECRulesExplorer"
    return Path.home() / ".trec_rules_explorer"


def _bookmark_path(data_path: Path) -> Path:
    """Prefer per-user app data, but fall back to the library folder.

    The fallback keeps bookmarks usable in locked-down or portable setups where
    %APPDATA% is not writable.
    """
    candidates = [
        _app_data_dir() / "bookmarks.json",
        Path.home() / ".trec_rules_explorer" / "bookmarks.json",
    ]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "bookmarks.local.json")
    candidates.append(data_path.parent / "bookmarks.local.json")
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            probe = candidate.parent / ".trec-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return candidates[-1]


# ---------------------------------------------------------------------------
# Small UI helpers
# ---------------------------------------------------------------------------

class PlaceholderEntry(tk.Entry):
    def __init__(self, master, placeholder: str, **kwargs):
        super().__init__(master, **kwargs)
        self.placeholder = placeholder
        self._showing = False
        self.bind("<FocusIn>", self._focus_in, add="+")
        self.bind("<FocusOut>", self._focus_out, add="+")
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        if not self.get():
            self._showing = True
            self.insert(0, self.placeholder)
            self.configure(foreground=FAINT)

    def _focus_in(self, _event=None) -> None:
        if self._showing:
            self.delete(0, "end")
            self.configure(foreground=INK)
            self._showing = False

    def _focus_out(self, _event=None) -> None:
        if not self.get().strip():
            self.delete(0, "end")
            self._show_placeholder()

    def value(self) -> str:
        if self._showing:
            return ""
        return self.get().strip()

    def set_value(self, value: str) -> None:
        self._showing = False
        self.delete(0, "end")
        if value:
            self.insert(0, value)
            self.configure(foreground=INK)
        else:
            self._show_placeholder()


class PlaceholderText(tk.Text):
    def __init__(self, master, placeholder: str, **kwargs):
        super().__init__(master, **kwargs)
        self.placeholder = placeholder
        self._showing = False
        self.bind("<FocusIn>", self._focus_in, add="+")
        self.bind("<FocusOut>", self._focus_out, add="+")
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        if not self.get("1.0", "end").strip():
            self._showing = True
            self.configure(foreground=FAINT)
            self.insert("1.0", self.placeholder)

    def _focus_in(self, _event=None) -> None:
        if self._showing:
            self.delete("1.0", "end")
            self.configure(foreground=INK)
            self._showing = False

    def _focus_out(self, _event=None) -> None:
        if not self.get("1.0", "end").strip():
            self.delete("1.0", "end")
            self._show_placeholder()

    def value(self) -> str:
        if self._showing:
            return ""
        return self.get("1.0", "end").strip()

    def set_value(self, value: str) -> None:
        self._showing = False
        self.delete("1.0", "end")
        if value:
            self.insert("1.0", value)
            self.configure(foreground=INK)
        else:
            self._show_placeholder()


class BookmarkStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            items = payload.get("bookmarks", {})
            if isinstance(items, dict):
                self.data = {
                    str(key): value
                    for key, value in items.items()
                    if isinstance(value, dict)
                }
        except (OSError, ValueError):
            self.data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "bookmarks": self.data}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            # Some OneDrive/antivirus setups block the sibling temp file but
            # allow a direct write.  Do not lose the user's bookmark over it.
            self.path.write_text(text, encoding="utf-8")

    def saved(self, uid: str) -> bool:
        return uid in self.data

    def toggle(self, uid: str) -> bool:
        if uid in self.data:
            del self.data[uid]
            state = False
        else:
            self.data[uid] = {
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "note": "",
            }
            state = True
        self._save()
        return state

    def remove(self, uid: str) -> None:
        if uid in self.data:
            del self.data[uid]
            self._save()

    def set_note(self, uid: str, note: str) -> None:
        if uid not in self.data:
            self.data[uid] = {"saved_at": datetime.now().isoformat(timespec="seconds")}
        self.data[uid]["note"] = note
        self._save()

    def note(self, uid: str) -> str:
        return str(self.data.get(uid, {}).get("note", ""))

    def items(self) -> list[tuple[str, dict]]:
        return sorted(
            self.data.items(),
            key=lambda item: str(item[1].get("saved_at", "")),
            reverse=True,
        )


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class TrecApp:
    def __init__(self, root: tk.Tk, data_path: Path, *, auto_update: bool = True):
        self.root = root
        self.data_path = data_path
        self.library = Library(data_path)
        self.bookmarks = BookmarkStore(_bookmark_path(data_path))
        self.auto_update = auto_update
        self._update_thread: threading.Thread | None = None

        self.current_section: Section | None = None
        self.current_query = ""
        self.current_rows: dict[str, Section] = {}
        self.search_job: str | None = None
        self.comparison: ComparisonReport | None = None
        self.selected_row: ComparisonRow | None = None
        self.saved_current_uid: str | None = None

        settings = storage.read_settings()
        self.ai_config = ai.config_from_settings(settings)
        if "ai_review" not in settings:
            self.ai_config = ai.default_config()
        self.ai_packet: ai.ComparisonPacket | None = None
        self.ai_analysis: ai.AIAnalysis | None = None
        self.ai_thread: threading.Thread | None = None
        self.ai_cancel: threading.Event | None = None
        self.ai_queue: queue.Queue = queue.Queue()
        self.ai_started_at = 0.0
        self.ai_timeout = self.ai_config.timeout_seconds
        self.ai_provider_label = self.ai_config.mode_label

        self._configure_root()
        self._configure_styles()
        self._build_menu()
        self._build_header()
        self._build_notebook()
        self._build_status_bar()
        self._bind_shortcuts()
        self._show_search_empty()
        self._refresh_results(initial=True)
        self._refresh_saved()
        if self.auto_update:
            self.root.after(1200, self._maybe_check_updates)

    # -- window / styles -----------------------------------------------------
    def _configure_root(self) -> None:
        self.root.title("TREC Rulebook — Search & Scenario Compare")
        self.root.configure(bg=BG)
        self.root.minsize(1080, 720)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        width, height = 1320, 860
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 20)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        try:
            self.root.option_add("*tearOff", False)
            self.root.option_add("*Font", FONT_UI)
            self.root.option_add("*Entry.selectBackground", TEAL)
        except tk.TclError:
            pass

        # A simple generated mark: a gold seal with a navy monogram.
        icon = tk.PhotoImage(width=32, height=32)
        icon.put(NAVY, to=(0, 0, 32, 32))
        icon.put(GOLD, to=(4, 4, 28, 28))
        icon.put(NAVY, to=(7, 7, 25, 25))
        icon.put(GOLD, to=(9, 15, 23, 18))
        self.root.iconphoto(True, icon)
        self._icon = icon

    def _on_close(self) -> None:
        if self.ai_cancel is not None:
            self.ai_cancel.set()
        self.root.destroy()

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=INK, font=FONT_UI)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("TLabel", background=BG, foreground=INK, font=FONT_UI)
        style.configure("Card.TLabel", background=SURFACE, foreground=INK, font=FONT_UI)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_SMALL)
        style.configure("CardMuted.TLabel", background=SURFACE, foreground=MUTED, font=FONT_SMALL)
        style.configure("Head.TLabel", background=BG, foreground=INK, font=FONT_DISPLAY)
        style.configure("CardHead.TLabel", background=SURFACE, foreground=INK, font=FONT_DISPLAY)

        style.configure(
            "TButton",
            padding=(12, 7),
            font=FONT_UI,
            background=SURFACE_ALT,
            foreground=INK,
            bordercolor=BORDER,
            lightcolor=SURFACE_ALT,
            darkcolor=SURFACE_ALT,
            focuscolor=TEAL,
        )
        style.map(
            "TButton",
            background=[("active", SOFT), ("disabled", SOFT)],
            foreground=[("disabled", FAINT)],
        )
        style.configure(
            "Accent.TButton",
            padding=(14, 8),
            font=FONT_UI_BOLD,
            background=TEAL,
            foreground=WHITE,
            bordercolor=TEAL,
            lightcolor=TEAL,
            darkcolor=TEAL,
        )
        style.map(
            "Accent.TButton",
            background=[("active", NAVY), ("disabled", "#a9b8b8")],
            foreground=[("disabled", "#f0f0e8")],
        )
        style.configure(
            "Ghost.TButton",
            padding=(9, 6),
            font=FONT_SMALL,
            background=SURFACE,
            foreground=TEAL,
            bordercolor=BORDER,
            lightcolor=SURFACE,
            darkcolor=SURFACE,
        )
        style.map("Ghost.TButton", background=[("active", CHIP)])

        style.configure(
            "AI.TButton",
            padding=(14, 8),
            font=FONT_UI_BOLD,
            background=GOLD,
            foreground=INK,
            bordercolor=GOLD,
            lightcolor=GOLD,
            darkcolor=GOLD,
        )
        style.map(
            "AI.TButton",
            background=[("active", "#a9822c"), ("disabled", "#d8c9a1")],
            foreground=[("disabled", "#6f6753")],
        )

        style.configure(
            "TCombobox",
            padding=(8, 6),
            fieldbackground=SURFACE,
            background=SURFACE,
            foreground=INK,
            arrowsize=14,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", SURFACE)],
            foreground=[("readonly", INK)],
            selectbackground=[("readonly", SURFACE)],
            selectforeground=[("readonly", INK)],
        )

        style.configure(
            "Treeview",
            background=SURFACE,
            fieldbackground=SURFACE,
            foreground=INK,
            rowheight=29,
            font=FONT_UI,
            borderwidth=0,
        )
        style.map(
            "Treeview",
            background=[("selected", TEAL)],
            foreground=[("selected", WHITE)],
        )
        style.configure(
            "Treeview.Heading",
            background=SURFACE_ALT,
            foreground=MUTED,
            font=FONT_SMALL_BOLD,
            relief="flat",
            padding=(8, 6),
        )
        style.map("Treeview.Heading", background=[("active", SOFT)])

        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=BG,
            foreground=MUTED,
            padding=(18, 9),
            font=FONT_UI,
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", SURFACE), ("active", SURFACE_ALT)],
            foreground=[("selected", TEAL), ("active", INK)],
        )
        style.configure("TSeparator", background=BORDER)
        style.configure("Vertical.TScrollbar", background=SOFT, troughcolor=BG, arrowcolor=MUTED)
        style.configure("Horizontal.TScrollbar", background=SOFT, troughcolor=BG, arrowcolor=MUTED)
        style.configure("TPanedwindow", background=BG)
        style.configure("Sash", sashthickness=8, gripcount=0, background=BG)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root, tearoff=False, font=FONT_UI)
        file_menu = tk.Menu(menubar, tearoff=False, font=FONT_UI)
        file_menu.add_command(label="Export comparison…", accelerator="Ctrl+E", command=self.export_comparison)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=False, font=FONT_UI)
        edit_menu.add_command(label="Focus search", accelerator="Ctrl+F", command=self.focus_search)
        edit_menu.add_command(label="Copy citation", command=self.copy_citation)
        edit_menu.add_command(label="Copy section text", command=self.copy_section)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        tools_menu = tk.Menu(menubar, tearoff=False, font=FONT_UI)
        tools_menu.add_command(
            label="Check for rule updates now",
            command=lambda: self._start_update_check(force=True),
        )
        tools_menu.add_command(
            label="Open user library folder",
            command=self._open_library_folder,
        )
        tools_menu.add_command(
            label="Reload updated library",
            command=self._reload_library,
        )
        tools_menu.add_separator()
        ai_menu = tk.Menu(tools_menu, tearoff=False, font=FONT_UI)
        ai_menu.add_command(
            label="Run AI review",
            accelerator="Ctrl+Shift+A",
            command=self.run_ai_review,
        )
        ai_menu.add_command(label="Copy AI prompt", command=self.copy_ai_prompt)
        ai_menu.add_command(
            label="Import AI response…", command=self.import_ai_response
        )
        ai_menu.add_separator()
        ai_menu.add_command(
            label="AI review settings…", command=self.show_ai_settings
        )
        tools_menu.add_cascade(label="AI review", menu=ai_menu)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=False, font=FONT_UI)
        help_menu.add_command(label="Quick start", command=self.show_help)
        help_menu.add_command(label="How AI review works", command=self._show_ai_help)
        help_menu.add_command(label="Official sources", command=self.open_official_sources)
        help_menu.add_command(label="About this library", command=self.show_about)
        help_menu.add_separator()
        help_menu.add_command(label="Disclaimer", command=lambda: messagebox.showinfo("Disclaimer", DISCLAIMER, parent=self.root))
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menubar)

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=NAVY, height=84)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        brand = tk.Frame(header, bg=NAVY)
        brand.pack(side="left", padx=(24, 0), pady=16)

        seal = tk.Canvas(brand, width=48, height=48, bg=NAVY, highlightthickness=0)
        seal.pack(side="left", padx=(0, 13))
        seal.create_oval(2, 2, 46, 46, fill=GOLD, outline="")
        seal.create_oval(7, 7, 41, 41, fill=NAVY, outline="")
        seal.create_text(24, 24, text="TREC", fill=WHITE, font=("Segoe UI", 8, "bold"))

        text_box = tk.Frame(brand, bg=NAVY)
        text_box.pack(side="left")
        tk.Label(
            text_box, text="TREC Rulebook", bg=NAVY, fg=WHITE,
            font=("Georgia", 21, "bold"), anchor="w",
        ).pack(anchor="w")
        tk.Label(
            text_box,
            text="Texas Real Estate Commission rules & statutes  ·  search, compare, cite",
            bg=NAVY, fg="#b9cdd0", font=("Segoe UI", 9), anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        right = tk.Frame(header, bg=NAVY)
        right.pack(side="right", padx=(0, 24), pady=18)

        snapshot = tk.Frame(right, bg=NAVY_DARK, highlightbackground="#2d5c66", highlightthickness=1)
        snapshot.pack(side="left", padx=(0, 14))
        self.snapshot_label = tk.Label(
            snapshot,
            text=f"LIBRARY  ·  {self.library.meta.get('generated_at_display', 'current')}",
            bg=NAVY_DARK, fg="#d8e4e3", font=("Segoe UI", 8, "bold"),
            padx=10, pady=6,
        )
        self.snapshot_label.pack()

        sources = tk.Label(
            right, text="Official sources ↗", bg=NAVY, fg="#e5d19a",
            font=("Segoe UI", 9, "underline"), cursor="hand2",
        )
        sources.pack(side="left", padx=(0, 16))
        sources.bind("<Button-1>", lambda _e: self.open_official_sources())

        help_btn = tk.Label(
            right, text="?", bg=NAVY_DARK, fg=WHITE,
            font=("Segoe UI", 11, "bold"), width=3, pady=2, cursor="hand2",
        )
        help_btn.pack(side="left")
        help_btn.bind("<Button-1>", lambda _e: self.show_help())

        accent = tk.Frame(self.root, bg=GOLD, height=3)
        accent.pack(fill="x")

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=16, pady=(12, 8))

        self.search_tab = tk.Frame(self.notebook, bg=BG)
        self.compare_tab = tk.Frame(self.notebook, bg=BG)
        self.saved_tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.search_tab, text="   Search & Browse   ")
        self.notebook.add(self.compare_tab, text="   Compare Scenarios   ")
        self.notebook.add(self.saved_tab, text="   Saved   ")

        self._build_search_tab()
        self._build_compare_tab()
        self._build_saved_tab()

    def _build_status_bar(self) -> None:
        bar = tk.Frame(self.root, bg="#e9e3d5", height=27)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        counts = self.library.meta.get("counts", {})
        rules = counts.get("trec-rules", 0)
        statutes = sum(v for k, v in counts.items() if k != "trec-rules")
        self._status_base_text = (
            f"{len(self.library.sections)} sections  ·  {rules} TREC rules  ·  "
            f"{statutes} statutory sections"
        )
        self.status_left = tk.Label(
            bar,
            text=self._status_base_text,
            bg="#e9e3d5", fg=MUTED, font=("Segoe UI", 8), anchor="w",
        )
        self.status_left.pack(side="left", padx=18)
        tk.Label(
            bar,
            text="Educational research aid — verify the official text",
            bg="#e9e3d5", fg="#8a7a58", font=("Segoe UI", 8),
        ).pack(side="right", padx=18)

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-f>", lambda _e: self.focus_search())
        self.root.bind("<Control-F>", lambda _e: self.focus_search())
        self.root.bind("<Control-e>", lambda _e: self.export_comparison())
        self.root.bind("<Control-E>", lambda _e: self.export_comparison())
        self.root.bind("<Control-Return>", lambda _e: self.run_comparison())
        self.root.bind("<Control-Shift-A>", lambda _e: self.run_ai_review())
        self.root.bind("<Control-Shift-a>", lambda _e: self.run_ai_review())
        self.root.bind("<F1>", lambda _e: self.show_help())
        self.root.bind("<Control-Key-1>", lambda _e: self.notebook.select(self.search_tab))
        self.root.bind("<Control-Key-2>", lambda _e: self.notebook.select(self.compare_tab))
        self.root.bind("<Control-Key-3>", lambda _e: self.notebook.select(self.saved_tab))

    # ------------------------------------------------------------------
    # Search tab
    # ------------------------------------------------------------------
    def _build_search_tab(self) -> None:
        self.search_tab.grid_rowconfigure(0, weight=1)
        self.search_tab.grid_columnconfigure(0, weight=0, minsize=418)
        self.search_tab.grid_columnconfigure(1, weight=1)

        left = tk.Frame(
            self.search_tab, bg=SURFACE, highlightbackground=BORDER,
            highlightthickness=1, bd=0,
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left.grid_rowconfigure(4, weight=1)
        left.grid_columnconfigure(0, weight=1)

        head = tk.Frame(left, bg=SURFACE)
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        tk.Label(
            head, text="Search the library", bg=SURFACE, fg=INK,
            font=FONT_DISPLAY, anchor="w",
        ).pack(side="left")
        tk.Label(
            head, text="FTS5-ranked", bg=CHIP, fg=CHIP_TEXT,
            font=("Segoe UI", 8, "bold"), padx=7, pady=3,
        ).pack(side="right")

        search_wrap = tk.Frame(left, bg=SOFT, highlightbackground=BORDER, highlightthickness=1)
        search_wrap.grid(row=1, column=0, sticky="ew", padx=18, pady=(2, 10))
        search_wrap.grid_columnconfigure(1, weight=1)
        tk.Label(
            search_wrap, text="⌕", bg=SOFT, fg=TEAL,
            font=("Segoe UI", 16), padx=8,
        ).grid(row=0, column=0)
        self.search_entry = PlaceholderEntry(
            search_wrap,
            "Search a phrase, section number, or issue…",
            bg=SOFT, fg=INK, relief="flat", bd=0, font=("Segoe UI", 11),
            insertbackground=TEAL, highlightthickness=0,
        )
        self.search_entry.grid(row=0, column=1, sticky="ew", ipady=7, pady=1)
        self.search_entry.bind("<KeyRelease>", self._on_search_key)
        self.search_entry.bind("<Return>", lambda _e: self._run_search())
        self.search_entry.bind("<Down>", self._focus_first_result)
        self.search_entry.bind("<Escape>", lambda _e: self.clear_search())
        clear = tk.Label(
            search_wrap, text="×", bg=SOFT, fg=MUTED,
            font=("Segoe UI", 14), padx=8, cursor="hand2",
        )
        clear.grid(row=0, column=2)
        clear.bind("<Button-1>", lambda _e: self.clear_search())

        filters = tk.Frame(left, bg=SURFACE)
        filters.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 6))
        filters.grid_columnconfigure(1, weight=1)

        tk.Label(filters, text="Source", bg=SURFACE, fg=MUTED, font=FONT_SMALL).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.source_var = tk.StringVar(value="All sources")
        self.source_combo = ttk.Combobox(
            filters, textvariable=self.source_var, state="readonly",
            values=("All sources", "TREC Rules", "Statutes"), width=14,
        )
        self.source_combo.grid(row=0, column=1, sticky="ew", pady=(0, 4))
        self.source_combo.bind("<<ComboboxSelected>>", lambda _e: self._run_search())

        tk.Label(filters, text="Chapter", bg=SURFACE, fg=MUTED, font=FONT_SMALL).grid(
            row=1, column=0, sticky="w", padx=(0, 8)
        )
        self.chapters = self.library.chapters()
        self.chapter_var = tk.StringVar(value="All chapters")
        self.chapter_combo = ttk.Combobox(
            filters, textvariable=self.chapter_var, state="readonly",
            values=("All chapters",) + tuple(chapter.label for chapter in self.chapters),
            width=34,
        )
        self.chapter_combo.grid(row=1, column=1, sticky="ew")
        self.chapter_combo.bind("<<ComboboxSelected>>", lambda _e: self._run_search())
        self.chapter_by_label = {chapter.label: chapter.key for chapter in self.chapters}

        quick = tk.Frame(left, bg=SURFACE)
        quick.grid(row=3, column=0, sticky="ew", padx=18, pady=(2, 8))
        tk.Label(quick, text="TRY", bg=SURFACE, fg=FAINT, font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 6))
        for label, query in (
            ("IABS", "IABS notice"),
            ("Trust money", "trust money"),
            ("Team names", "team name advertising"),
            ("Dual capacity", "dual capacity"),
        ):
            chip = tk.Label(
                quick, text=label, bg=CHIP, fg=CHIP_TEXT,
                font=("Segoe UI", 8), padx=7, pady=3, cursor="hand2",
            )
            chip.pack(side="left", padx=(0, 5))
            chip.bind("<Button-1>", lambda _e, q=query: self.quick_search(q))

        count_row = tk.Frame(left, bg=SURFACE)
        count_row.grid(row=4, column=0, sticky="new", padx=18)
        self.result_count = tk.Label(
            count_row, text="", bg=SURFACE, fg=MUTED, font=FONT_SMALL, anchor="w",
        )
        self.result_count.pack(side="left")
        tk.Label(
            count_row, text="click a row to read", bg=SURFACE, fg=FAINT,
            font=("Segoe UI", 8),
        ).pack(side="right")

        tree_wrap = tk.Frame(left, bg=SURFACE)
        tree_wrap.grid(row=5, column=0, sticky="nsew", padx=(12, 8), pady=(4, 12))
        tree_wrap.grid_rowconfigure(0, weight=1)
        tree_wrap.grid_columnconfigure(0, weight=1)
        self.result_tree = ttk.Treeview(
            tree_wrap, columns=("cite", "title"), show="headings",
            selectmode="browse",
        )
        self.result_tree.heading("cite", text="Citation", anchor="w")
        self.result_tree.heading("title", text="Section", anchor="w")
        self.result_tree.column("cite", width=112, minwidth=96, stretch=False, anchor="w")
        self.result_tree.column("title", width=240, minwidth=160, stretch=True, anchor="w")
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.result_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.result_tree.configure(yscrollcommand=scroll.set)
        self.result_tree.tag_configure("rule", foreground="#1c5969")
        self.result_tree.tag_configure("statute", foreground="#75501f")
        self.result_tree.tag_configure("group", font=("Segoe UI", 9, "bold"), foreground=MUTED)
        self.result_tree.bind("<<TreeviewSelect>>", self._on_result_select)
        self.result_tree.bind("<Double-1>", lambda _e: self.focus_search())
        self.result_tree.bind("<Return>", self._activate_result)

        self._build_detail_panel()

    def _build_detail_panel(self) -> None:
        right = tk.Frame(
            self.search_tab, bg=SURFACE, highlightbackground=BORDER,
            highlightthickness=1, bd=0,
        )
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self.detail_empty = tk.Frame(right, bg=SURFACE)
        self.detail_view = tk.Frame(right, bg=SURFACE)
        for frame in (self.detail_empty, self.detail_view):
            frame.grid(row=0, column=0, sticky="nsew")
        self.detail_empty.tkraise()

        empty_inner = tk.Frame(self.detail_empty, bg=SURFACE)
        empty_inner.place(relx=0.5, rely=0.43, anchor="center")
        tk.Label(
            empty_inner, text="§", bg=SURFACE, fg=GOLD,
            font=("Georgia", 58, "bold"),
        ).pack()
        tk.Label(
            empty_inner, text="Search or choose a section", bg=SURFACE, fg=INK,
            font=("Georgia", 20, "bold"),
        ).pack(pady=(8, 8))
        tk.Label(
            empty_inner,
            text=(
                "Full text, precise citations, official links, and bookmarks.\n"
                "Try a phrase, a section number such as 535.155, or browse a chapter."
            ),
            bg=SURFACE, fg=MUTED, font=FONT_UI, justify="center",
        ).pack()
        empty_buttons = tk.Frame(empty_inner, bg=SURFACE)
        empty_buttons.pack(pady=(18, 0))
        for label, query in (
            ("IABS notice", "IABS notice"),
            ("Trust money", "trust money"),
            ("Dual capacity", "dual capacity"),
        ):
            btn = ttk.Button(
                empty_buttons, text=label, style="Ghost.TButton",
                command=lambda q=query: self.quick_search(q),
            )
            btn.pack(side="left", padx=4)

        # Detail view
        self.detail_view.grid_rowconfigure(3, weight=1)
        self.detail_view.grid_columnconfigure(0, weight=1)

        top = tk.Frame(self.detail_view, bg=SURFACE)
        top.grid(row=0, column=0, sticky="ew", padx=26, pady=(20, 0))
        top.grid_columnconfigure(0, weight=1)

        self.kind_badge = tk.Label(
            top, text="", bg=CHIP, fg=CHIP_TEXT,
            font=("Segoe UI", 8, "bold"), padx=8, pady=3,
        )
        self.kind_badge.grid(row=0, column=0, sticky="w")
        self.chapter_label = tk.Label(
            top, text="", bg=SURFACE, fg=FAINT, font=("Segoe UI", 8),
            anchor="e",
        )
        self.chapter_label.grid(row=0, column=1, sticky="e")

        self.detail_citation = tk.Label(
            self.detail_view, text="", bg=SURFACE, fg=TEAL,
            font=("Consolas", 10, "bold"), anchor="w",
        )
        self.detail_citation.grid(row=1, column=0, sticky="ew", padx=26, pady=(8, 2))

        self.detail_title = tk.Label(
            self.detail_view, text="", bg=SURFACE, fg=INK,
            font=FONT_SECTION, anchor="w", justify="left", wraplength=760,
        )
        self.detail_title.grid(row=2, column=0, sticky="ew", padx=26, pady=(0, 8))
        self.detail_title.bind("<Configure>", self._resize_detail_title)

        body_wrap = tk.Frame(self.detail_view, bg=SURFACE)
        body_wrap.grid(row=3, column=0, sticky="nsew", padx=22, pady=(0, 0))
        body_wrap.grid_rowconfigure(1, weight=1)
        body_wrap.grid_columnconfigure(0, weight=1)

        self.tag_row = tk.Frame(body_wrap, bg=SURFACE)
        self.tag_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        text_wrap = tk.Frame(body_wrap, bg=SURFACE)
        text_wrap.grid(row=1, column=0, sticky="nsew")
        text_wrap.grid_rowconfigure(0, weight=1)
        text_wrap.grid_columnconfigure(0, weight=1)
        self.detail_text = tk.Text(
            text_wrap, wrap="word", bg=SURFACE, fg=INK, bd=0,
            relief="flat", font=FONT_BODY, padx=6, pady=2,
            insertbackground=TEAL, highlightthickness=0,
            spacing1=2, spacing3=6, cursor="arrow",
        )
        self.detail_text.grid(row=0, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(text_wrap, orient="vertical", command=self.detail_text.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.detail_text.configure(yscrollcommand=detail_scroll.set)
        self.detail_text.tag_configure("body", font=FONT_BODY, foreground=INK, spacing1=2, spacing3=6)
        self.detail_text.tag_configure(
            "hit", background=HIT, foreground=INK,
        )
        self.detail_text.tag_configure(
            "source_link", foreground=TEAL, underline=True, font=FONT_SMALL,
        )
        self.detail_text.tag_configure("rule_note", foreground=MUTED, font=FONT_SMALL)
        self.detail_text.tag_bind("source_link", "<Button-1>", self._open_current_source)
        self.detail_text.tag_bind("source_link", "<Enter>", lambda _e: self.detail_text.configure(cursor="hand2"))
        self.detail_text.tag_bind("source_link", "<Leave>", lambda _e: self.detail_text.configure(cursor="arrow"))

        actions = tk.Frame(self.detail_view, bg=SURFACE_ALT)
        actions.grid(row=4, column=0, sticky="ew")
        actions.grid_columnconfigure(3, weight=1)
        self.copy_cite_btn = ttk.Button(actions, text="Copy citation", style="Ghost.TButton", command=self.copy_citation)
        self.copy_cite_btn.grid(row=0, column=0, padx=(18, 6), pady=10)
        ttk.Button(actions, text="Copy text", style="Ghost.TButton", command=self.copy_section).grid(
            row=0, column=1, padx=6, pady=10
        )
        self.save_btn = ttk.Button(actions, text="☆ Save", style="Ghost.TButton", command=self.toggle_bookmark)
        self.save_btn.grid(row=0, column=2, padx=6, pady=10)
        ttk.Button(actions, text="Open official source ↗", style="Accent.TButton", command=self.open_current_source).grid(
            row=0, column=4, padx=(6, 18), pady=10
        )

    def _resize_detail_title(self, event) -> None:
        self.detail_title.configure(wraplength=max(420, event.width - 12))

    # -- search behavior -----------------------------------------------------
    def _on_search_key(self, event=None) -> None:
        if event is not None and event.keysym in ("Return", "Escape", "Up", "Down"):
            return
        if self.search_job:
            self.root.after_cancel(self.search_job)
        self.search_job = self.root.after(220, self._run_search)

    def _selected_source(self) -> str:
        return {
            "All sources": "all",
            "TREC Rules": "rules",
            "Statutes": "statutes",
        }.get(self.source_var.get(), "all")

    def _selected_chapter(self) -> str:
        label = self.chapter_var.get()
        return self.chapter_by_label.get(label, "")

    def quick_search(self, query: str) -> None:
        self.notebook.select(self.search_tab)
        self.source_var.set("All sources")
        self.chapter_var.set("All chapters")
        self.search_entry.set_value(query)
        self.search_entry.focus_set()
        self._run_search()

    def clear_search(self) -> None:
        self.search_entry.set_value("")
        self.current_query = ""
        self._run_search()

    def _run_search(self) -> None:
        self.search_job = None
        query = self.search_entry.value()
        self.current_query = query
        source = self._selected_source()
        chapter = self._selected_chapter()
        self.result_tree.delete(*self.result_tree.get_children())
        self.current_rows.clear()

        if query:
            hits = self.library.search(query, source=source, chapter_key=chapter, limit=220)
            sections = [hit.section for hit in hits]
            score_map = {hit.section.uid: hit.score for hit in hits}
            self.result_count.configure(
                text=f"{len(sections)} result{'s' if len(sections) != 1 else ''} for “{query}”"
            )
        else:
            sections = [
                section for section in self.library.sections
                if (source == "all" or
                    (source == "rules" and section.kind == "Rule") or
                    (source == "statutes" and section.kind == "Statute"))
                and (not chapter or section.chapter_key == chapter)
            ]
            sections.sort(key=lambda section: (
                0 if section.kind == "Rule" else 1,
                section.source_id,
                section.section,
            ))
            score_map = {}
            if chapter:
                label = self.chapter_var.get()
                self.result_count.configure(text=f"{len(sections)} sections in {label}")
            else:
                self.result_count.configure(text=f"All {len(sections)} sections")

        for section in sections:
            self._insert_result_row(section, score_map.get(section.uid))

        if sections:
            first = sections[0].uid
            if query or chapter:
                self.result_tree.selection_set(first)
                self.result_tree.focus(first)
                self._show_section(sections[0], query=query)
        else:
            self.current_section = None
            self._show_search_empty()
            if query:
                self.result_count.configure(text=f"No results for “{query}”")
            else:
                self.result_count.configure(text="No sections match these filters")

    def _insert_result_row(self, section: Section, score: float | None) -> None:
        star = "★ " if self.bookmarks.saved(section.uid) else ""
        tag = "rule" if section.kind == "Rule" else "statute"
        self.result_tree.insert(
            "", "end", iid=section.uid,
            values=(section.citation_short, star + section.title),
            tags=(tag,),
        )
        self.current_rows[section.uid] = section

    def _on_result_select(self, _event=None) -> None:
        selection = self.result_tree.selection()
        if not selection:
            return
        uid = selection[0]
        section = self.current_rows.get(uid) or self.library.section(uid)
        if section:
            self._show_section(section, query=self.current_query)

    def _focus_first_result(self, _event=None) -> str:
        children = self.result_tree.get_children()
        if children:
            self.result_tree.focus_set()
            self.result_tree.selection_set(children[0])
            self.result_tree.focus(children[0])
            self.result_tree.see(children[0])
        return "break"

    def _activate_result(self, _event=None) -> str:
        self._on_result_select()
        return "break"

    def _show_search_empty(self) -> None:
        self.detail_empty.tkraise()
        self.current_section = None

    def _show_section(self, section: Section, query: str = "") -> None:
        self.current_section = section
        self.detail_view.tkraise()
        if section.kind == "Rule":
            badge_text, badge_bg, badge_fg = "TREC RULE", CHIP, CHIP_TEXT
        elif section.source_id == "oc53":
            badge_text, badge_bg, badge_fg = "RELATED STATUTE", "#f1e4e0", "#7c3f2f"
        else:
            badge_text, badge_bg, badge_fg = "TEXAS STATUTE", "#f3e6d1", "#7c541f"
        self.kind_badge.configure(
            text=badge_text,
            bg=badge_bg,
            fg=badge_fg,
        )
        chapter = section.chapter
        if section.kind == "Rule" and chapter:
            chapter = re.sub(r"^Chapter\s+(\d+).*", r"Chapter \1", chapter)
        self.chapter_label.configure(text=chapter)
        self.detail_citation.configure(text=section.citation)
        self.detail_title.configure(text=section.title or "(untitled section)")

        for child in self.tag_row.winfo_children():
            child.destroy()
        for tag in section.tags[:3]:
            chip = tk.Label(
                self.tag_row, text=tag, bg=CHIP if section.kind == "Rule" else "#f3e6d1",
                fg=CHIP_TEXT if section.kind == "Rule" else "#7c541f",
                font=("Segoe UI", 8), padx=7, pady=3,
            )
            chip.pack(side="left", padx=(0, 5))
        if len(section.tags) > 3:
            tk.Label(
                self.tag_row, text=f"+{len(section.tags) - 3} more",
                bg=SURFACE_ALT, fg=MUTED, font=("Segoe UI", 8), padx=5, pady=3,
            ).pack(side="left", padx=(0, 5))
        word_label = tk.Label(
            self.tag_row, text=f"{section.word_count:,} words", bg=SURFACE,
            fg=FAINT, font=("Segoe UI", 8),
        )
        word_label.pack(side="right")

        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", section.body, "body")
        self.detail_text.insert("end", "\n\n")
        self.detail_text.insert("end", "Official source: " + section.url, "source_link")
        self.detail_text.insert("end", "\n")
        related = self.library.related_sections(section.uid)[:6]
        if related:
            self.detail_text.insert("end", "\nCross-referenced authorities (check context):\n", "source_link")
            for item in related:
                self.detail_text.insert(
                    "end",
                    f"  {item.citation_short} — {item.title}\n",
                    "source_link",
                )
        self.detail_text.configure(state="disabled")
        self.detail_text.yview_moveto(0)

        for start, end in highlight_spans(section.body, query):
            self.detail_text.configure(state="normal")
            self.detail_text.tag_add("hit", f"1.0+{start}c", f"1.0+{end}c")
            self.detail_text.configure(state="disabled")

        saved = self.bookmarks.saved(section.uid)
        self.save_btn.configure(text="★ Saved" if saved else "☆ Save")
        self.copy_cite_btn.configure(state="normal")
        self._resize_detail_title(type("E", (), {"width": self.detail_title.winfo_width()})())

    def focus_search(self) -> None:
        self.notebook.select(self.search_tab)
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    # -- detail actions ------------------------------------------------------
    def copy_citation(self) -> None:
        if not self.current_section:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.current_section.citation)
        self._flash_status(f"Copied {self.current_section.citation_short}")

    def copy_section(self) -> None:
        if not self.current_section:
            return
        section = self.current_section
        self.root.clipboard_clear()
        self.root.clipboard_append(
            f"{section.citation} — {section.title}\n\n{section.body}\n\n{section.url}"
        )
        self._flash_status("Copied section text and citation")

    def open_current_source(self) -> None:
        if self.current_section:
            webbrowser.open(self.current_section.url)

    def _open_current_source(self, _event=None) -> None:
        self.open_current_source()

    def toggle_bookmark(self) -> None:
        if not self.current_section:
            return
        try:
            state = self.bookmarks.toggle(self.current_section.uid)
        except OSError as exc:
            messagebox.showerror(
                "Could not save bookmark",
                f"The bookmark file could not be written:\n{exc}\n\n"
                "Move the application to a writable folder or set APPDATA.",
                parent=self.root,
            )
            return
        self.save_btn.configure(text="★ Saved" if state else "☆ Save")
        self._flash_status("Saved to your bookmarks" if state else "Removed from bookmarks")
        self._refresh_saved()
        # Update the star in the visible result row without rebuilding.
        if self.result_tree.exists(self.current_section.uid):
            values = list(self.result_tree.item(self.current_section.uid, "values"))
            if len(values) > 1:
                title = values[1].lstrip("★ ").strip()
                values[1] = ("★ " if state else "") + title
                self.result_tree.item(self.current_section.uid, values=values)

    def _flash_status(self, text: str) -> None:
        self.status_left.configure(text=text, fg=TEAL)
        self.root.after(
            2200,
            lambda: self.status_left.configure(
                text=self._status_base_text, fg=MUTED
            ),
        )

    # -- source updates ------------------------------------------------------
    def _maybe_check_updates(self) -> None:
        if updates.is_due():
            self._start_update_check()

    def _start_update_check(self, *, force: bool = False) -> None:
        if self._update_thread and self._update_thread.is_alive():
            self._flash_status("An update check is already running")
            return
        if not force and not updates.is_due():
            return
        self._set_status("Checking for newer rule and statute text…", TEAL)
        self._update_thread = threading.Thread(
            target=self._update_worker,
            kwargs={"force": force},
            daemon=True,
        )
        self._update_thread.start()

    def _update_worker(self, *, force: bool) -> None:
        try:
            payload, changed = updates.fetch_update(
                self.library.meta,
                progress=lambda message: self._thread_status(message),
            )
            if changed and payload is not None:
                target = updates.stage_payload(payload)
                updates.mark_checked(result="updated")
                self._schedule_update_callback(
                    lambda: self._update_finished("updated", target, None, force)
                )
            else:
                updates.mark_checked(result="current")
                self._schedule_update_callback(
                    lambda: self._update_finished("current", None, None, force)
                )
        except Exception as exc:  # network, parse, or local write failure
            try:
                updates.mark_error(str(exc))
            except OSError:
                pass
            self._schedule_update_callback(
                lambda: self._update_finished("error", None, str(exc), force)
            )

    def _schedule_update_callback(self, callback) -> None:
        try:
            self.root.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    def _thread_status(self, message: str) -> None:
        try:
            self.root.after(0, lambda: self._set_status(message, TEAL))
        except tk.TclError:
            pass

    def _set_status(self, text: str, color: str = TEAL) -> None:
        try:
            if not self.status_left.winfo_exists():
                return
            self.status_left.configure(text=text, fg=color)
        except tk.TclError:
            pass

    def _update_finished(
        self,
        result: str,
        target: Path | None,
        error: str | None,
        force: bool,
    ) -> None:
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        self._update_thread = None
        if result == "updated":
            self._set_status(
                "Updated library downloaded — use Tools → Reload updated library",
                TEAL,
            )
            if force:
                messagebox.showinfo(
                    "Library updated",
                    "A newer version of the public rules or statutes was "
                    "downloaded.\n\n"
                    "Use Tools → Reload updated library to apply it now, or "
                    "it will be used automatically next time you start the app.",
                    parent=self.root,
                )
        elif result == "current":
            self._set_status("Library is current · checked just now", MUTED)
            self.root.after(
                5000,
                lambda: self._set_status(self._status_base_text, MUTED),
            )
        else:
            self._set_status("Update check unavailable — using the local library", BRICK)
            if force:
                messagebox.showwarning(
                    "Could not check for updates",
                    "The app could not download or parse the latest sources.\n\n"
                    f"{error}\n\n"
                    "Your current library is unchanged.",
                    parent=self.root,
                )
            else:
                self.root.after(
                    5000,
                    lambda: self._set_status(self._status_base_text, MUTED),
                )

    def _open_library_folder(self) -> None:
        folder = storage.app_data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        webbrowser.open(folder.as_uri())

    def _reload_library(self) -> None:
        target = storage.user_library_path()
        if not target.exists():
            messagebox.showinfo(
                "No downloaded update",
                "No updated library has been downloaded yet. Use "
                "Tools → Check for rule updates now.",
                parent=self.root,
            )
            return
        try:
            new_library = Library(target)
        except Exception as exc:
            messagebox.showerror(
                "Could not reload library",
                f"The downloaded library could not be opened:\n{exc}",
                parent=self.root,
            )
            return
        self.library = new_library
        self.data_path = target
        self.chapters = self.library.chapters()
        self.chapter_combo.configure(
            values=("All chapters",)
            + tuple(chapter.label for chapter in self.chapters)
        )
        self.chapter_var.set("All chapters")
        self.chapter_by_label = {chapter.label: chapter.key for chapter in self.chapters}
        self.snapshot_label.configure(
            text=f"LIBRARY  ·  {self.library.meta.get('generated_at_display', 'current')}"
        )
        counts = self.library.meta.get("counts", {})
        rules = counts.get("trec-rules", 0)
        statutes = sum(v for k, v in counts.items() if k != "trec-rules")
        self._status_base_text = (
            f"{len(self.library.sections)} sections  ·  {rules} TREC rules  ·  "
            f"{statutes} statutory sections"
        )
        self.status_left.configure(text=self._status_base_text, fg=MUTED)
        self.current_section = None
        self.current_query = ""
        self.comparison = None
        self.selected_row = None
        self.search_entry.set_value("")
        self._show_search_empty()
        self._render_comparison_empty()
        self._render_brief_empty()
        self._render_authority_empty()
        self._refresh_results(initial=True)
        self._refresh_saved()
        self._flash_status("Updated library loaded")

    def _refresh_results(self, initial: bool = False) -> None:
        self._run_search()

    # ------------------------------------------------------------------
    # Compare tab
    # ------------------------------------------------------------------
    def _build_compare_tab(self) -> None:
        self.compare_tab.grid_rowconfigure(1, weight=1)
        self.compare_tab.grid_columnconfigure(0, weight=1)

        input_card = tk.Frame(
            self.compare_tab, bg=SURFACE, highlightbackground=BORDER,
            highlightthickness=1, bd=0,
        )
        input_card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        input_card.grid_columnconfigure(0, weight=1)

        top = tk.Frame(input_card, bg=SURFACE)
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 6))
        top.grid_columnconfigure(0, weight=1)
        title_box = tk.Frame(top, bg=SURFACE)
        title_box.grid(row=0, column=0, sticky="w")
        tk.Label(
            title_box, text="Compare two scenarios", bg=SURFACE, fg=INK,
            font=FONT_DISPLAY, anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="Paste two fact patterns. The library retrieves the authorities each one raises, then shows what overlaps and what differs.",
            bg=SURFACE, fg=MUTED, font=FONT_SMALL, anchor="w",
        ).pack(anchor="w", pady=(2, 0))
        actions_top = tk.Frame(top, bg=SURFACE)
        actions_top.grid(row=0, column=1, sticky="e")
        ttk.Button(
            actions_top, text="Load example pair", style="Ghost.TButton",
            command=self._load_example_pair,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            actions_top, text="How it works", style="Ghost.TButton",
            command=self._show_compare_help,
        ).pack(side="left")

        scenarios = tk.Frame(input_card, bg=SURFACE)
        scenarios.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 4))
        scenarios.grid_columnconfigure(0, weight=1, uniform="scenario")
        scenarios.grid_columnconfigure(1, weight=1, uniform="scenario")

        self.scenario_a = self._build_scenario_input(
            scenarios, 0, "SCENARIO A", "e.g., a sales agent advertises a team name without broker registration…",
            "A", BRICK,
        )
        self.scenario_b = self._build_scenario_input(
            scenarios, 1, "SCENARIO B", "e.g., an inspector is asked to inspect a property listed by a broker…",
            "B", TEAL,
        )

        action_row = tk.Frame(input_card, bg=SURFACE)
        action_row.grid(row=2, column=0, sticky="ew", padx=20, pady=(8, 14))
        action_row.grid_columnconfigure(4, weight=1)
        self.compare_btn = ttk.Button(
            action_row, text="Compare scenarios", style="Accent.TButton",
            command=self.run_comparison,
        )
        self.compare_btn.grid(row=0, column=0, sticky="w")
        ttk.Button(
            action_row, text="Swap A ↔ B", style="Ghost.TButton",
            command=self._swap_scenarios,
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            action_row, text="Clear both", style="Ghost.TButton",
            command=self._clear_scenarios,
        ).grid(row=0, column=2, padx=(6, 0))
        self.ai_btn = ttk.Button(
            action_row, text="AI review", style="AI.TButton",
            command=self.run_ai_review,
        )
        self.ai_btn.grid(row=0, column=3, padx=(10, 0))
        self.compare_status = tk.Label(
            action_row,
            text="Local retrieval. AI review is optional and runs only when you ask.",
            bg=SURFACE, fg=FAINT, font=FONT_SMALL, anchor="w",
        )
        self.compare_status.grid(row=0, column=4, sticky="w", padx=(16, 0))

        body = ttk.Panedwindow(self.compare_tab, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew")
        self.compare_paned = body

        left = tk.Frame(body, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        body.add(left, weight=0)
        left.grid_rowconfigure(2, weight=1)
        left.grid_columnconfigure(0, weight=1)

        left_head = tk.Frame(left, bg=SURFACE)
        left_head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 2))
        left_head.grid_columnconfigure(0, weight=1)
        tk.Label(
            left_head, text="Authorities", bg=SURFACE, fg=INK,
            font=FONT_DISPLAY, anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.compare_count = tk.Label(
            left_head, text="No comparison yet", bg=SURFACE, fg=MUTED,
            font=FONT_SMALL, anchor="e",
        )
        self.compare_count.grid(row=0, column=1, sticky="e")
        legend = tk.Frame(left, bg=SURFACE)
        legend.grid(row=1, column=0, sticky="ew", padx=16, pady=(2, 6))
        for text, color in (("Shared", TEAL), ("Only A", BRICK), ("Only B", "#7a541c")):
            tk.Label(
                legend, text="● " + text, bg=SURFACE, fg=color,
                font=("Segoe UI", 8),
            ).pack(side="left", padx=(0, 10))
        tk.Label(
            legend, text="scores are relative", bg=SURFACE, fg=FAINT,
            font=("Segoe UI", 8),
        ).pack(side="right")

        tree_wrap = tk.Frame(left, bg=SURFACE)
        tree_wrap.grid(row=2, column=0, sticky="nsew", padx=(10, 8), pady=(0, 10))
        tree_wrap.grid_rowconfigure(0, weight=1)
        tree_wrap.grid_columnconfigure(0, weight=1)
        self.compare_tree = ttk.Treeview(
            tree_wrap, columns=("a", "b"), show="tree headings", selectmode="browse",
        )
        self.compare_tree.heading("#0", text="Authority", anchor="w")
        self.compare_tree.heading("a", text="A", anchor="center")
        self.compare_tree.heading("b", text="B", anchor="center")
        self.compare_tree.column("#0", width=380, minwidth=250, stretch=True)
        self.compare_tree.column("a", width=42, minwidth=36, stretch=False, anchor="center")
        self.compare_tree.column("b", width=42, minwidth=36, stretch=False, anchor="center")
        self.compare_tree.grid(row=0, column=0, sticky="nsew")
        cscroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.compare_tree.yview)
        cscroll.grid(row=0, column=1, sticky="ns")
        self.compare_tree.configure(yscrollcommand=cscroll.set)
        self.compare_tree.tag_configure("group", font=("Segoe UI", 9, "bold"), foreground=MUTED)
        self.compare_tree.tag_configure("shared", foreground="#174f59")
        self.compare_tree.tag_configure("onlya", foreground="#8d3b2a")
        self.compare_tree.tag_configure("onlyb", foreground="#76501c")
        self.compare_tree.bind("<<TreeviewSelect>>", self._on_compare_select)

        right = tk.Frame(body, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        body.add(right, weight=1)
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        self.compare_notebook = ttk.Notebook(right)
        self.compare_notebook.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)

        self.brief_tab = tk.Frame(self.compare_notebook, bg=SURFACE)
        self.auth_tab = tk.Frame(self.compare_notebook, bg=SURFACE)
        self.ai_tab = tk.Frame(self.compare_notebook, bg=SURFACE)
        self.compare_notebook.add(self.brief_tab, text="  Comparison brief  ")
        self.compare_notebook.add(self.auth_tab, text="  Authority detail  ")
        self.compare_notebook.add(self.ai_tab, text="  AI review  ")
        self._build_brief_tab()
        self._build_authority_tab()
        self._build_ai_tab()
        self._render_comparison_empty()
        self.root.after_idle(self._set_compare_sash)

    def _set_compare_sash(self) -> None:
        try:
            self.compare_paned.sashpos(0, 500)
        except tk.TclError:
            pass

    def _build_scenario_input(
        self, parent: tk.Frame, column: int, title: str, placeholder: str,
        letter: str, accent: str,
    ) -> PlaceholderText:
        card = tk.Frame(
            parent, bg=SURFACE_ALT, highlightbackground=BORDER, highlightthickness=1,
        )
        card.grid(row=0, column=column, sticky="nsew", padx=(0, 10) if column == 0 else (10, 0))
        card.grid_columnconfigure(0, weight=1)
        head = tk.Frame(card, bg=SURFACE_ALT)
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
        head.grid_columnconfigure(1, weight=1)
        tk.Label(
            head, text=letter, bg=accent, fg=WHITE, font=("Segoe UI", 8, "bold"),
            width=3, pady=2,
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        tk.Label(
            head, text=title, bg=SURFACE_ALT, fg=INK, font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=1, sticky="w")
        clear = tk.Label(
            head, text="clear", bg=SURFACE_ALT, fg=MUTED, font=("Segoe UI", 8, "underline"),
            cursor="hand2",
        )
        clear.grid(row=0, column=2, sticky="e")
        text = PlaceholderText(
            card, placeholder, height=5, wrap="word", bg=SURFACE, fg=INK,
            relief="flat", bd=0, padx=10, pady=8, font=FONT_UI,
            insertbackground=accent, highlightthickness=0, spacing1=1, spacing3=3,
        )
        text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(2, 4))
        footer = tk.Frame(card, bg=SURFACE_ALT)
        footer.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 7))
        count_label = tk.Label(
            footer, text="0 words", bg=SURFACE_ALT, fg=FAINT, font=("Segoe UI", 8),
        )
        count_label.pack(side="left")
        tk.Label(
            footer, text="Add who, what, when, and money", bg=SURFACE_ALT,
            fg=FAINT, font=("Segoe UI", 8),
        ).pack(side="right")
        text.bind("<KeyRelease>", lambda _e, t=text, c=count_label: self._update_scenario_count(t, c))
        text.bind("<<Paste>>", lambda _e, t=text, c=count_label: self.root.after(20, lambda: self._update_scenario_count(t, c)))
        clear.bind("<Button-1>", lambda _e, t=text, c=count_label: self._clear_one_scenario(t, c))
        text._count_label = count_label
        text._title = title
        return text

    def _update_scenario_count(self, text: PlaceholderText, label: tk.Label) -> None:
        value = text.value()
        label.configure(text=f"{len(value.split()):,} words")

    def _clear_one_scenario(self, text: PlaceholderText, label: tk.Label) -> None:
        text.set_value("")
        label.configure(text="0 words")

    def _build_brief_tab(self) -> None:
        wrap = tk.Frame(self.brief_tab, bg=SURFACE)
        wrap.pack(fill="both", expand=True, padx=18, pady=(12, 8))
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        self.brief_text = tk.Text(
            wrap, wrap="word", bg=SURFACE, fg=INK, bd=0, relief="flat",
            font=("Segoe UI", 10), padx=4, pady=2, highlightthickness=0,
            spacing1=2, spacing3=5, cursor="arrow",
        )
        self.brief_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.brief_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.brief_text.configure(yscrollcommand=scroll.set)
        self.brief_text.tag_configure("kicker", foreground=GOLD, font=("Segoe UI", 8, "bold"))
        self.brief_text.tag_configure("h1", foreground=INK, font=("Georgia", 20, "bold"), spacing1=4, spacing3=8)
        self.brief_text.tag_configure("h2", foreground=TEAL, font=("Segoe UI", 9, "bold"), spacing1=14, spacing3=5)
        self.brief_text.tag_configure("body", foreground=INK, font=("Segoe UI", 10), spacing3=4)
        self.brief_text.tag_configure("bullet", foreground=INK, font=("Segoe UI", 10), lmargin1=16, lmargin2=30, spacing3=4)
        self.brief_text.tag_configure("cite", foreground=TEAL, font=("Consolas", 9, "bold"))
        self.brief_text.tag_configure("muted", foreground=MUTED, font=("Segoe UI", 9))
        self.brief_text.tag_configure("warn", foreground="#7b5a2a", background="#f7edda", font=("Segoe UI", 9))
        self.brief_text.tag_configure("divider", foreground=BORDER)

        footer = tk.Frame(self.brief_tab, bg=SURFACE_ALT)
        footer.pack(fill="x", side="bottom")
        ttk.Button(footer, text="Copy brief", style="Ghost.TButton", command=self.copy_brief).pack(
            side="left", padx=(18, 6), pady=8
        )
        ttk.Button(footer, text="Export .md…", style="Ghost.TButton", command=self.export_comparison).pack(
            side="left", padx=6, pady=8
        )
        tk.Label(
            footer, text="Built locally. AI review, when used, is disclosed in its tab.",
            bg=SURFACE_ALT, fg=FAINT, font=("Segoe UI", 8),
        ).pack(side="right", padx=18)

    def _build_authority_tab(self) -> None:
        self.auth_tab.grid_rowconfigure(0, weight=1)
        self.auth_tab.grid_columnconfigure(0, weight=1)
        self.auth_container = tk.Frame(self.auth_tab, bg=SURFACE)
        self.auth_container.grid(row=0, column=0, sticky="nsew")
        self.auth_container.grid_rowconfigure(0, weight=1)
        self.auth_container.grid_columnconfigure(0, weight=1)
        self._render_authority_empty()

    def _build_ai_tab(self) -> None:
        self.ai_tab.grid_rowconfigure(2, weight=1)
        self.ai_tab.grid_columnconfigure(0, weight=1)

        head = tk.Frame(self.ai_tab, bg=SURFACE)
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 2))
        head.grid_columnconfigure(0, weight=1)
        title_box = tk.Frame(head, bg=SURFACE)
        title_box.grid(row=0, column=0, sticky="w")
        tk.Label(
            title_box, text="AI review", bg=SURFACE, fg=INK,
            font=FONT_DISPLAY, anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="An optional second opinion: severity-ranked red flags, key "
                 "differences, and the facts that would change the analysis.",
            bg=SURFACE, fg=MUTED, font=FONT_SMALL, anchor="w",
        ).pack(anchor="w", pady=(2, 0))
        head_actions = tk.Frame(head, bg=SURFACE)
        head_actions.grid(row=0, column=1, sticky="e")
        ttk.Button(
            head_actions, text="Settings…", style="Ghost.TButton",
            command=self.show_ai_settings,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            head_actions, text="How it works", style="Ghost.TButton",
            command=self._show_ai_help,
        ).pack(side="left")

        bar = tk.Frame(self.ai_tab, bg=SURFACE)
        bar.grid(row=1, column=0, sticky="ew", padx=18, pady=(6, 6))
        self.ai_run_btn = ttk.Button(
            bar, text="Run AI review", style="AI.TButton",
            command=self.run_ai_review,
        )
        self.ai_run_btn.pack(side="left")
        self.ai_cancel_btn = ttk.Button(
            bar, text="Cancel", style="Ghost.TButton",
            command=self._cancel_ai, state="disabled",
        )
        self.ai_cancel_btn.pack(side="left", padx=(8, 0))
        self.ai_copy_prompt_btn = ttk.Button(
            bar, text="Copy AI prompt", style="Ghost.TButton",
            command=self.copy_ai_prompt,
        )
        self.ai_copy_prompt_btn.pack(side="left", padx=(8, 0))
        self.ai_import_btn = ttk.Button(
            bar, text="Import AI response…", style="Ghost.TButton",
            command=self.import_ai_response,
        )
        self.ai_import_btn.pack(side="left", padx=(6, 0))
        self.ai_status = tk.Label(
            bar, text="", bg=SURFACE, fg=FAINT, font=FONT_SMALL, anchor="w",
        )
        self.ai_status.pack(side="left", padx=(16, 0))

        wrap = tk.Frame(self.ai_tab, bg=SURFACE)
        wrap.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 8))
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        self.ai_text = tk.Text(
            wrap, wrap="word", bg=SURFACE, fg=INK, bd=0, relief="flat",
            font=("Segoe UI", 10), padx=4, pady=2, highlightthickness=0,
            spacing1=2, spacing3=5, cursor="arrow",
        )
        self.ai_text.grid(row=0, column=0, sticky="nsew")
        ai_scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.ai_text.yview)
        ai_scroll.grid(row=0, column=1, sticky="ns")
        self.ai_text.configure(yscrollcommand=ai_scroll.set)
        self.ai_text.tag_configure("kicker", foreground=GOLD, font=("Segoe UI", 8, "bold"))
        self.ai_text.tag_configure("h1", foreground=INK, font=("Georgia", 20, "bold"), spacing1=4, spacing3=8)
        self.ai_text.tag_configure("h2", foreground=TEAL, font=("Segoe UI", 9, "bold"), spacing1=14, spacing3=5)
        self.ai_text.tag_configure("body", foreground=INK, font=("Segoe UI", 10), spacing3=4)
        self.ai_text.tag_configure("bullet", foreground=INK, font=("Segoe UI", 10), lmargin1=16, lmargin2=30, spacing3=4)
        self.ai_text.tag_configure("cite", foreground=TEAL, font=("Consolas", 9, "bold"))
        self.ai_text.tag_configure("muted", foreground=MUTED, font=("Segoe UI", 9))
        self.ai_text.tag_configure("warn", foreground="#7b5a2a", background="#f7edda", font=("Segoe UI", 9))
        self.ai_text.tag_configure("divider", foreground=BORDER)
        self.ai_text.tag_configure(
            "verdict", foreground=INK, background=CHIP, font=("Segoe UI", 10),
            lmargin1=14, lmargin2=14, rmargin=14, spacing1=8, spacing3=8,
        )
        self.ai_text.tag_configure("flagtitle", foreground=INK, font=("Segoe UI", 10, "bold"), spacing3=2)
        self.ai_text.tag_configure("sev-high", foreground=BRICK, font=("Segoe UI", 9, "bold"))
        self.ai_text.tag_configure("sev-medium", foreground="#8a5a1f", font=("Segoe UI", 9, "bold"))
        self.ai_text.tag_configure("sev-low", foreground=MUTED, font=("Segoe UI", 9, "bold"))

        footer = tk.Frame(self.ai_tab, bg=SURFACE_ALT)
        footer.grid(row=3, column=0, sticky="ew")
        ttk.Button(
            footer, text="Copy review", style="Ghost.TButton",
            command=self.copy_ai_review,
        ).pack(side="left", padx=(18, 6), pady=8)
        ttk.Button(
            footer, text="Export review .md…", style="Ghost.TButton",
            command=self.export_ai_review,
        ).pack(side="left", padx=6, pady=8)
        self.ai_privacy = tk.Label(
            footer, text="", bg=SURFACE_ALT, fg=FAINT, font=("Segoe UI", 8),
        )
        self.ai_privacy.pack(side="right", padx=18)

    # -- AI review -----------------------------------------------------------
    def _ai_mode_status(self) -> tuple[str, str]:
        config = self.ai_config
        if config.mode == "off":
            return "Off — local comparison only", FAINT
        if config.mode == "gemini":
            if config.gemini_key_value:
                return f"Ready · {config.gemini_model} (free tier)", TEAL
            return (
                f"Gemini key missing — set {config.gemini_api_key_env}",
                BRICK,
            )
        if config.mode == "harness":
            command = ai.find_harness_command(config.harness_command)
            if not command:
                return "Harness not found — set its path in Settings", BRICK
            if ai.harness_auth_available():
                return "Ready · DeepSeek Harness account (free)", TEAL
            return "Harness found — sign in or set DEEPSEEK_API_KEY", BRICK
        if config.mode == "claude-cli":
            command = ai.find_claude_command(config.claude_command)
            if not command:
                return "Claude Code not found — set its path in Settings", BRICK
            return "Ready · Claude Code", TEAL
        if config.mode == "anthropic":
            if config.anthropic_key_value:
                return f"Ready · {config.anthropic_model}", TEAL
            return (
                f"Anthropic key missing — set {config.anthropic_api_key_env}",
                BRICK,
            )
        if config.api_key_value:
            return f"Ready · {config.api_model}", TEAL
        return f"API key missing — set {config.api_key_env}", BRICK

    def _set_ai_status(self, text: str, color: str = FAINT) -> None:
        try:
            if self.ai_status.winfo_exists():
                self.ai_status.configure(text=text, fg=color)
        except tk.TclError:
            pass

    def _ai_privacy_text(self) -> str:
        if self.ai_config.mode == "gemini":
            return (
                "Sends the scenarios and authority text to "
                f"{self.ai_config.gemini_api_base} (Google Gemini)."
            )
        if self.ai_config.mode == "harness":
            return "DeepSeek Harness is local; its model provider receives the scenarios and authority text."
        if self.ai_config.mode == "claude-cli":
            return "Claude Code is local; the Anthropic account it uses receives the scenarios and authority text."
        if self.ai_config.mode == "anthropic":
            return f"Sends the scenarios and authority text to {self.ai_config.anthropic_api_base}."
        if self.ai_config.mode == "api":
            return f"Sends the scenarios and authority text to {self.ai_config.api_base_url}."
        return "Off by default. Nothing leaves this computer unless you copy the prompt yourself."

    def _ai_write(self, text: str, tag: str = "body") -> None:
        self.ai_text.insert("end", text, tag)

    def _render_ai_empty(self) -> None:
        self.ai_analysis = None
        self.ai_packet = None
        self.ai_text.configure(state="normal")
        self.ai_text.delete("1.0", "end")
        self._ai_write("AI REVIEW\n", "kicker")
        self._ai_write("A second opinion on the comparison\n", "h1")
        self._ai_write(
            "The comparison brief is built locally from the library. AI review "
            "is an optional extra pass: it asks a model to read the same two "
            "scenarios and the retrieved authorities, then returns red flags, "
            "key differences, and the facts that would change the analysis.\n\n",
            "body",
        )
        self._ai_write(
            "Run a comparison first, then choose Run AI review. You can also "
            "copy the prepared prompt into any AI chat and import its JSON answer.\n",
            "muted",
        )
        self.ai_text.configure(state="disabled")
        self.ai_privacy.configure(text=self._ai_privacy_text())
        status, color = self._ai_mode_status()
        self._set_ai_status(status, color)

    def _render_ai_idle(self, report: ComparisonReport | None) -> None:
        self.ai_analysis = None
        self.ai_text.configure(state="normal")
        self.ai_text.delete("1.0", "end")
        self._ai_write("AI REVIEW\n", "kicker")
        self._ai_write("Optional second opinion\n", "h1")
        self._ai_write(
            "The local brief is deterministic. AI review adds judgment: which "
            "differences are likely to matter, what facts are missing, and "
            "whether the citations hold up.\n",
            "body",
        )
        signals = (
            self.ai_packet.payload.get("signals", []) if self.ai_packet else []
        )
        self._ai_write("\nLOCAL PRE-CHECK\n", "h2")
        if signals:
            for signal in signals[:12]:
                self._ai_write(
                    f"• {signal.get('side')} · {signal.get('label')} — "
                    f"{signal.get('detail')}\n",
                    "bullet",
                )
        else:
            self._ai_write("No pre-check notices.\n", "muted")

        self._ai_write("\nHOW TO USE\n", "h2")
        self._ai_write(
            "1. Run AI review uses the provider in Settings: Google Gemini "
            "(free tier) by default, or the free local DeepSeek Harness / "
            "Claude Code account, or another API.\n"
            "2. Copy AI prompt puts the full prompt on the clipboard; paste it "
            "into any AI chat, then use Import AI response with the JSON it returns.\n"
            "3. Scores are still relative, and the model can be wrong. Verify "
            "every citation against the official text.\n",
            "body",
        )
        self.ai_text.configure(state="disabled")
        self.ai_privacy.configure(text=self._ai_privacy_text())
        status, color = self._ai_mode_status()
        self._set_ai_status(status, color)

    def _render_ai_analysis(self, analysis: ai.AIAnalysis) -> None:
        self.ai_text.configure(state="normal")
        self.ai_text.delete("1.0", "end")
        self._ai_write("AI REVIEW\n", "kicker")
        self._ai_write("Second opinion on the comparison\n", "h1")
        self._ai_write(
            f"{analysis.risk_level.upper()} risk  ·  {analysis.confidence} confidence  "
            f"·  {analysis.model or 'model unknown'}  ·  "
            f"{ai.MODE_LABELS.get(analysis.mode, analysis.mode or 'manual')}\n",
            "muted",
        )
        self._ai_write("─" * 72 + "\n", "divider")
        self._ai_write(analysis.verdict + "\n\n", "verdict")

        self._ai_write(f"\nRED FLAGS ({len(analysis.red_flags)})\n", "h2")
        if not analysis.red_flags:
            self._ai_write("None identified from these facts.\n", "body")
        for flag in analysis.red_flags:
            side = {"A": "Scenario A", "B": "Scenario B"}.get(
                flag.scenario, "Both scenarios"
            )
            self._ai_write(f"{flag.severity.upper()}  ", f"sev-{flag.severity}")
            self._ai_write(f"{flag.title}\n", "flagtitle")
            self._ai_write(f"{side}\n", "muted")
            if flag.why_it_matters:
                self._ai_write(flag.why_it_matters + "\n", "body")
            if flag.citations:
                self._ai_write("Citations: " + ", ".join(flag.citations) + "\n", "cite")
            if flag.what_to_check:
                self._ai_write("Check: " + flag.what_to_check + "\n", "muted")
            self._ai_write("\n", "body")

        for heading, values in (
            ("KEY DIFFERENCES", analysis.key_differences),
            ("FACTS THAT WOULD CHANGE THE ANALYSIS", analysis.missing_facts),
            ("RETRIEVAL NOTES", analysis.retrieval_notes),
            ("CITATION CHECK", analysis.citation_warnings),
        ):
            if not values:
                continue
            self._ai_write(f"\n{heading}\n", "h2")
            for value in values:
                tag = "warn" if heading == "CITATION CHECK" else "bullet"
                self._ai_write("• " + value + "\n", tag)

        self._ai_write("\n" + "─" * 72 + "\n", "divider")
        self._ai_write(
            "AI-generated second opinion. It can be wrong, can miss a relevant "
            "authority, and is not legal advice. Confirm every citation against "
            "the official source.\n",
            "warn",
        )
        self.ai_text.configure(state="disabled")
        self.ai_text.yview_moveto(0)
        self.ai_privacy.configure(text=self._ai_privacy_text())

    def _render_ai_error(self, error: ai.AIReviewError) -> None:
        self.ai_text.configure(state="normal")
        self.ai_text.delete("1.0", "end")
        self._ai_write("AI REVIEW\n", "kicker")
        self._ai_write("The review did not run\n", "h1")
        self._ai_write(f"[{error.code}] {error}\n\n", "warn")
        if error.detail:
            self._ai_write(error.detail + "\n\n", "muted")
        self._ai_write(
            "The local comparison is still available in the brief. You can also "
            "copy the prompt and import an answer from another AI chat.\n",
            "body",
        )
        self.ai_text.configure(state="disabled")

    def _ai_ready(self) -> bool:
        if not self.comparison:
            messagebox.showinfo(
                "Compare first",
                "Run a scenario comparison before asking for an AI review.",
                parent=self.root,
            )
            return False
        if self.ai_config.mode == "off":
            if messagebox.askyesno(
                "AI review is off",
                "AI review is off. Open AI review settings to enable DeepSeek "
                "Harness (local) or an OpenAI-compatible API?\n\n"
                "You can also use Copy AI prompt and Import AI response without "
                "enabling anything.",
                parent=self.root,
            ):
                self.show_ai_settings()
            return False
        if (
            self.ai_config.mode == "harness"
            and not ai.find_harness_command(self.ai_config.harness_command)
        ):
            messagebox.showerror(
                "DeepSeek Harness not found",
                "The dsh launcher was not found. Install DeepSeek Harness, or "
                "set the path to dsh.cmd in AI review settings (or the "
                "TREC_DSH_COMMAND environment variable).",
                parent=self.root,
            )
            return False
        if self.ai_config.mode == "gemini" and not self.ai_config.gemini_key_value:
            messagebox.showerror(
                "Gemini API key missing",
                "Enter a Gemini API key in AI review settings, or set the "
                f"{self.ai_config.gemini_api_key_env} environment variable. "
                "A free key is available at https://aistudio.google.com/apikey.",
                parent=self.root,
            )
            return False
        if self.ai_config.mode == "harness" and not ai.harness_auth_available():
            if not messagebox.askyesno(
                "No harness sign-in found",
                "DeepSeek Harness is installed, but this app could not find a "
                "signed-in account, DEEPSEEK_API_KEY, or a harness .env file. "
                "The run may fail.\n\nTry it anyway?",
                parent=self.root,
            ):
                return False
        if (
            self.ai_config.mode == "claude-cli"
            and not ai.find_claude_command(self.ai_config.claude_command)
        ):
            messagebox.showerror(
                "Claude Code not found",
                "The Claude Code CLI was not found. Install it with "
                "`npm install -g @anthropic-ai/claude-code`, sign in, or set "
                "CLAUDE_CODE_COMMAND to the launcher.",
                parent=self.root,
            )
            return False
        if self.ai_config.mode == "anthropic" and not self.ai_config.anthropic_key_value:
            messagebox.showerror(
                "Anthropic API key missing",
                "Enter an Anthropic API key in AI review settings, or set the "
                f"{self.ai_config.anthropic_api_key_env} environment variable.",
                parent=self.root,
            )
            return False
        if self.ai_config.mode == "api" and not self.ai_config.api_key_value:
            messagebox.showerror(
                "API key missing",
                "Enter an API key in AI review settings, or set the "
                f"{self.ai_config.api_key_env} environment variable.",
                parent=self.root,
            )
            return False
        return True

    def run_ai_review(self) -> None:
        if self.ai_thread and self.ai_thread.is_alive():
            return
        if not self._ai_ready():
            return
        self.ai_packet = ai.build_packet(
            self.comparison, self.ai_config, library_meta=self.library.meta
        )
        self.ai_cancel = threading.Event()
        self.ai_queue = queue.Queue()
        self.ai_started_at = time.monotonic()
        self.ai_timeout = self.ai_config.timeout_seconds
        self.ai_provider_label = self.ai_config.mode_label
        self.ai_run_btn.configure(state="disabled")
        self.ai_cancel_btn.configure(state="normal")
        self._set_ai_status("Starting the review…", TEAL)
        self.notebook.select(self.compare_tab)
        self.compare_notebook.select(self.ai_tab)
        self.ai_thread = threading.Thread(
            target=self._ai_worker,
            args=(self.ai_packet, self.ai_config, self.ai_cancel),
            daemon=True,
        )
        self.ai_thread.start()
        self.root.after(60, self._poll_ai)
        self.root.after(1000, self._ai_heartbeat)

    def _ai_heartbeat(self) -> None:
        if self.ai_thread is None:
            return
        if self.ai_cancel is not None and self.ai_cancel.is_set():
            self._set_ai_status("Cancelling…", MUTED)
        else:
            elapsed = int(time.monotonic() - self.ai_started_at)
            self._set_ai_status(
                f"{self.ai_provider_label} · {elapsed}s elapsed "
                f"(limit {self.ai_timeout}s) · Cancel to stop",
                TEAL,
            )
        try:
            self.root.after(1000, self._ai_heartbeat)
        except tk.TclError:
            pass

    def _ai_worker(self, packet, config, cancel) -> None:
        """Background worker; it only touches the queue, never Tk."""
        def progress(message: str) -> None:
            self.ai_queue.put(("progress", message, packet))

        try:
            analysis = ai.run_review(
                packet, config, cancel=cancel, progress=progress
            )
        except ai.AIReviewError as exc:
            self.ai_queue.put(("error", exc, packet))
        except Exception as exc:  # pragma: no cover - defensive worker path
            self.ai_queue.put(
                ("error", ai.AIReviewError("harness_failed", str(exc)), packet)
            )
        else:
            self.ai_queue.put(("done", analysis, packet))

    def _ai_reset_run(self) -> None:
        self.ai_thread = None
        self.ai_cancel = None
        self.ai_run_btn.configure(state="normal")
        self.ai_cancel_btn.configure(state="disabled")

    def _poll_ai(self) -> None:
        """Drain worker messages on the Tk main thread."""
        try:
            while True:
                kind, payload, packet = self.ai_queue.get_nowait()
                if kind == "progress":
                    self._set_ai_status(str(payload), TEAL)
                elif kind == "done":
                    self._ai_finished(payload, None, packet)
                elif kind == "error":
                    self._ai_finished(None, payload, packet)
        except queue.Empty:
            pass
        if self.ai_thread is not None:
            try:
                self.root.after(60, self._poll_ai)
            except tk.TclError:
                pass

    @staticmethod
    def _flag_count(count: int) -> str:
        return f"{count} red flag" + ("" if count == 1 else "s")

    def _ai_finished(
        self,
        analysis: ai.AIAnalysis | None,
        error: ai.AIReviewError | None,
        packet: ai.ComparisonPacket | None = None,
    ) -> None:
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        if packet is not None and packet is not self.ai_packet:
            # The comparison changed while the worker was running.
            self._ai_reset_run()
            return
        self.ai_thread = None
        self.ai_cancel = None
        self.ai_run_btn.configure(state="normal")
        self.ai_cancel_btn.configure(state="disabled")
        if error is not None:
            if error.code == "cancelled":
                self._set_ai_status("Review cancelled", MUTED)
                return
            self._set_ai_status("AI review failed", BRICK)
            self._render_ai_error(error)
            messagebox.showerror(
                "AI review failed",
                f"{error}\n\n{error.detail}".strip()[:1200],
                parent=self.root,
            )
            return
        assert analysis is not None
        self.ai_analysis = analysis
        self._render_ai_analysis(analysis)
        self.compare_notebook.select(self.ai_tab)
        self._set_ai_status(
            f"Review ready · {analysis.risk_level.upper()} risk · "
            f"{self._flag_count(len(analysis.red_flags))}",
            TEAL,
        )
        self._flash_status("AI review ready")

    def _cancel_ai(self) -> None:
        if self.ai_cancel is not None:
            self.ai_cancel.set()
            self._set_ai_status("Cancelling…", MUTED)

    def copy_ai_prompt(self) -> None:
        if not self.comparison:
            messagebox.showinfo(
                "Compare first",
                "Run a scenario comparison before copying an AI prompt.",
                parent=self.root,
            )
            return
        packet = ai.build_packet(
            self.comparison, self.ai_config, library_meta=self.library.meta
        )
        self.ai_packet = packet
        self.root.clipboard_clear()
        self.root.clipboard_append(packet.prompt)
        self._flash_status("AI prompt copied — paste it into your AI chat")
        self._set_ai_status(
            "Prompt copied. Paste the response with Import AI response…", TEAL
        )

    def import_ai_response(self) -> None:
        if not self.comparison and not self.ai_packet:
            messagebox.showinfo(
                "Compare first",
                "Run a scenario comparison before importing an AI response.",
                parent=self.root,
            )
            return
        packet = ai.build_packet(
            self.comparison, self.ai_config, library_meta=self.library.meta
        ) if self.comparison else self.ai_packet
        if packet is None:
            messagebox.showinfo(
                "Compare first",
                "Run a scenario comparison before importing an AI response.",
                parent=self.root,
            )
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Import AI response")
        dialog.configure(bg=BG)
        dialog.geometry("820x640")
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(
            dialog, text="Paste the JSON response", bg=BG, fg=INK,
            font=FONT_DISPLAY, anchor="w",
        ).pack(fill="x", padx=18, pady=(14, 2))
        tk.Label(
            dialog,
            text="The response must be the JSON object described in the prompt. "
                 "Citations that are not in the comparison are flagged.",
            bg=BG, fg=MUTED, font=FONT_SMALL, anchor="w",
        ).pack(fill="x", padx=18, pady=(0, 8))

        wrap = tk.Frame(dialog, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        text = tk.Text(
            wrap, wrap="word", bg=SURFACE, fg=INK, bd=0, relief="flat",
            font=("Consolas", 9), padx=10, pady=8, highlightthickness=0,
        )
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)

        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill="x", padx=18, pady=(0, 14))

        def load_file() -> None:
            path = filedialog.askopenfilename(
                parent=dialog,
                title="Load AI response",
                filetypes=(("JSON", "*.json"), ("Text", "*.txt"), ("All files", "*.*")),
            )
            if not path:
                return
            try:
                text.delete("1.0", "end")
                text.insert("1.0", Path(path).read_text(encoding="utf-8"))
            except OSError as exc:
                messagebox.showerror("Could not read file", str(exc), parent=dialog)

        def apply() -> None:
            value = text.get("1.0", "end").strip()
            if not value:
                messagebox.showinfo(
                    "Nothing to import", "Paste the AI response first.", parent=dialog
                )
                return
            try:
                analysis = ai.parse_analysis(value, packet)
            except ai.AIReviewError as exc:
                messagebox.showerror(
                    "Could not read the response",
                    f"{exc}\n\n{exc.detail}".strip()[:1200],
                    parent=dialog,
                )
                return
            self.ai_analysis = analysis
            self._render_ai_analysis(analysis)
            self.compare_notebook.select(self.ai_tab)
            self._set_ai_status(
                f"Imported review · {analysis.risk_level.upper()} risk · "
                f"{self._flag_count(len(analysis.red_flags))}",
                TEAL,
            )
            dialog.destroy()
            self._flash_status("AI response imported")

        ttk.Button(buttons, text="Load file…", style="Ghost.TButton", command=load_file).pack(
            side="left"
        )
        ttk.Button(buttons, text="Cancel", style="Ghost.TButton", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Apply", style="Accent.TButton", command=apply).pack(
            side="right", padx=(0, 8)
        )
        text.focus_set()

    def copy_ai_review(self) -> None:
        if not self.ai_analysis:
            messagebox.showinfo(
                "No AI review yet", "Run or import an AI review first.", parent=self.root
            )
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.ai_analysis.to_markdown())
        self._flash_status("AI review copied")

    def export_ai_review(self) -> None:
        if not self.ai_analysis:
            messagebox.showinfo(
                "No AI review yet", "Run or import an AI review first.", parent=self.root
            )
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export AI review",
            defaultextension=".md",
            initialfile=f"trec-ai-review-{datetime.now().strftime('%Y%m%d')}.md",
            filetypes=(("Markdown", "*.md"), ("Text", "*.txt"), ("All files", "*.*")),
        )
        if not path:
            return
        Path(path).write_text(self.ai_analysis.to_markdown(), encoding="utf-8")
        self._flash_status("AI review exported")

    def show_ai_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("AI review settings")
        dialog.configure(bg=BG)
        dialog.geometry("760x680")
        dialog.transient(self.root)
        dialog.grab_set()

        body = tk.Frame(dialog, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=(14, 8))
        body.grid_columnconfigure(0, weight=1)

        tk.Label(
            body, text="AI review settings", bg=BG, fg=INK, font=FONT_DISPLAY,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            body,
            text="Default is the free local account. Paid API keys are optional. "
                 "Local search and comparison never call a model.",
            bg=BG, fg=MUTED, font=FONT_SMALL, anchor="w", justify="left",
            wraplength=720,
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))

        mode_var = tk.StringVar(value=ai.MODE_LABELS[self.ai_config.mode])
        mode_row = tk.Frame(body, bg=BG)
        mode_row.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        mode_row.grid_columnconfigure(1, weight=1)
        tk.Label(
            mode_row, text="Provider", bg=BG, fg=INK, font=FONT_SMALL_BOLD,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Combobox(
            mode_row,
            values=list(ai.MODE_LABELS.values()),
            textvariable=mode_var,
            state="readonly",
        ).grid(row=0, column=1, sticky="ew")

        tabs = ttk.Notebook(body)
        tabs.grid(row=3, column=0, sticky="nsew", pady=(0, 8))
        body.grid_rowconfigure(3, weight=1)
        local_tab = tk.Frame(tabs, bg=BG)
        gemini_tab = tk.Frame(tabs, bg=BG)
        api_tab = tk.Frame(tabs, bg=BG)
        tabs.add(local_tab, text="  Local CLIs  ")
        tabs.add(gemini_tab, text="  Gemini  ")
        tabs.add(api_tab, text="  Other APIs  ")
        for tab in (local_tab, gemini_tab, api_tab):
            tab.grid_columnconfigure(1, weight=1)

        def section(parent, text_value: str, row: int) -> int:
            tk.Label(
                parent, text=text_value, bg=BG, fg=TEAL,
                font=("Segoe UI", 8, "bold"), anchor="w",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 4))
            return row + 1

        def field_label(parent, text_value: str, row: int) -> int:
            tk.Label(
                parent, text=text_value, bg=BG, fg=INK, font=FONT_SMALL,
                anchor="w",
            ).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=(0, 6))
            return row

        def field_entry(
            parent, var: tk.StringVar, row: int, *, show: str = ""
        ) -> tk.Entry:
            entry = tk.Entry(
                parent, textvariable=var, bg=SURFACE, fg=INK, relief="flat",
                highlightthickness=1, highlightbackground=BORDER,
                highlightcolor=TEAL, font=FONT_UI, show=show,
            )
            entry.grid(row=row, column=1, sticky="ew", pady=(0, 6), ipady=4)
            return entry

        def labeled_entry(
            parent, text_value: str, var: tk.StringVar, row: int, *, show: str = ""
        ) -> int:
            field_label(parent, text_value, row)
            field_entry(parent, var, row, show=show)
            return row + 1

        # ---- local CLIs -------------------------------------------------
        row = 0
        row = section(local_tab, "DEEPSEEK HARNESS (FREE LOCAL ACCOUNT)", row)
        harness_var = tk.StringVar(value=self.ai_config.harness_command)
        field_label(local_tab, "dsh launcher", row)
        harness_row = tk.Frame(local_tab, bg=BG)
        harness_row.grid(row=row, column=1, sticky="ew", pady=(0, 4))
        harness_row.grid_columnconfigure(0, weight=1)
        tk.Entry(
            harness_row, textvariable=harness_var, bg=SURFACE, fg=INK,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=TEAL, font=FONT_UI,
        ).grid(row=0, column=0, sticky="ew", ipady=4)
        harness_status = tk.Label(
            local_tab, text="", bg=BG, fg=FAINT, font=("Segoe UI", 8),
            anchor="w", justify="left", wraplength=560,
        )
        harness_status.grid(row=row + 1, column=1, sticky="w", pady=(0, 6))

        def detect_harness(auto: bool = False) -> None:
            current = harness_var.get().strip()
            found = ai.find_harness_command("" if auto else current)
            if found:
                if auto or not current:
                    harness_var.set(found)
                harness_status.configure(text="Found: " + found, fg=TEAL)
            else:
                harness_status.configure(
                    text="Not found. Install DeepSeek Harness or set TREC_DSH_COMMAND.",
                    fg=BRICK,
                )

        ttk.Button(
            harness_row, text="Auto-detect", style="Ghost.TButton",
            command=lambda: detect_harness(True),
        ).grid(row=0, column=1, padx=(8, 0))
        row += 2

        effort_var = tk.StringVar(value=self.ai_config.harness_effort)
        field_label(local_tab, "Review effort", row)
        ttk.Combobox(
            local_tab, values=list(ai.EFFORT_LEVELS), textvariable=effort_var,
            state="readonly",
        ).grid(row=row, column=1, sticky="ew", pady=(0, 2))
        row += 1
        tk.Label(
            local_tab,
            text="low is the cheap default; high/max spend more tokens for deeper reasoning.",
            bg=BG, fg=FAINT, font=("Segoe UI", 8), anchor="w",
        ).grid(row=row, column=1, sticky="w", pady=(0, 8))
        row += 1

        row = section(local_tab, "CLAUDE CODE (LOCAL CLI)", row)
        claude_var = tk.StringVar(value=self.ai_config.claude_command)
        field_label(local_tab, "claude launcher", row)
        claude_row = tk.Frame(local_tab, bg=BG)
        claude_row.grid(row=row, column=1, sticky="ew", pady=(0, 4))
        claude_row.grid_columnconfigure(0, weight=1)
        tk.Entry(
            claude_row, textvariable=claude_var, bg=SURFACE, fg=INK,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=TEAL, font=FONT_UI,
        ).grid(row=0, column=0, sticky="ew", ipady=4)
        claude_status = tk.Label(
            local_tab, text="", bg=BG, fg=FAINT, font=("Segoe UI", 8),
            anchor="w", justify="left", wraplength=560,
        )
        claude_status.grid(row=row + 1, column=1, sticky="w", pady=(0, 6))

        def detect_claude(auto: bool = False) -> None:
            current = claude_var.get().strip()
            found = ai.find_claude_command("" if auto else current)
            if found:
                if auto or not current:
                    claude_var.set(found)
                claude_status.configure(text="Found: " + found, fg=TEAL)
            else:
                claude_status.configure(
                    text="Not found. Install Claude Code or set CLAUDE_CODE_COMMAND.",
                    fg=BRICK,
                )

        ttk.Button(
            claude_row, text="Auto-detect", style="Ghost.TButton",
            command=lambda: detect_claude(True),
        ).grid(row=0, column=1, padx=(8, 0))
        row += 2

        claude_model_var = tk.StringVar(value=self.ai_config.claude_model)
        row = labeled_entry(
            local_tab, "Model (optional)", claude_model_var, row
        )
        tk.Label(
            local_tab,
            text="Leave blank to use the model Claude Code is signed in to.",
            bg=BG, fg=FAINT, font=("Segoe UI", 8), anchor="w",
        ).grid(row=row, column=1, sticky="w", pady=(0, 8))
        row += 1

        # ---- Gemini -----------------------------------------------------
        row = 0
        row = section(gemini_tab, "GOOGLE GEMINI (FREE TIER)", row)
        gemini_base_var = tk.StringVar(value=self.ai_config.gemini_api_base)
        gemini_model_var = tk.StringVar(value=self.ai_config.gemini_model)
        gemini_key_env_var = tk.StringVar(
            value=self.ai_config.gemini_api_key_env
        )
        gemini_key_var = tk.StringVar(value=self.ai_config.gemini_api_key)
        row = labeled_entry(gemini_tab, "Base URL", gemini_base_var, row)
        row = labeled_entry(gemini_tab, "Model", gemini_model_var, row)
        row = labeled_entry(gemini_tab, "Key env var", gemini_key_env_var, row)
        row = labeled_entry(gemini_tab, "API key", gemini_key_var, row, show="*")
        gemini_note = tk.Label(
            gemini_tab,
            text="The free tier uses the Gemini API. If a run says "
                 "SERVICE_DISABLED, enable the API for the key's Google Cloud "
                 "project, or create a free key at https://aistudio.google.com/apikey.",
            bg="#f7edda", fg="#6d5528", font=("Segoe UI", 8), anchor="w",
            justify="left", wraplength=600, padx=8, pady=6,
        )
        gemini_note.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        row += 1

        # ---- APIs -------------------------------------------------------
        row = 0
        row = section(api_tab, "OPENAI-COMPATIBLE (DEEPSEEK / OPENAI / OLLAMA)", row)
        api_base_var = tk.StringVar(value=self.ai_config.api_base_url)
        api_model_var = tk.StringVar(value=self.ai_config.api_model)
        api_key_env_var = tk.StringVar(value=self.ai_config.api_key_env)
        api_key_var = tk.StringVar(value=self.ai_config.api_key)
        row = labeled_entry(api_tab, "Base URL", api_base_var, row)
        row = labeled_entry(api_tab, "Model", api_model_var, row)
        row = labeled_entry(api_tab, "Key env var", api_key_env_var, row)
        row = labeled_entry(api_tab, "API key", api_key_var, row, show="*")

        row = section(api_tab, "ANTHROPIC CLAUDE API", row)
        anthropic_base_var = tk.StringVar(value=self.ai_config.anthropic_api_base)
        anthropic_model_var = tk.StringVar(value=self.ai_config.anthropic_model)
        anthropic_key_env_var = tk.StringVar(
            value=self.ai_config.anthropic_api_key_env
        )
        anthropic_key_var = tk.StringVar(value=self.ai_config.anthropic_api_key)
        row = labeled_entry(api_tab, "Base URL", anthropic_base_var, row)
        row = labeled_entry(api_tab, "Model", anthropic_model_var, row)
        row = labeled_entry(api_tab, "Key env var", anthropic_key_env_var, row)
        row = labeled_entry(api_tab, "API key", anthropic_key_var, row, show="*")
        tk.Label(
            api_tab,
            text="Leave a key blank to read its environment variable. A typed "
                 "key is stored in settings.json in plain text.",
            bg=BG, fg=FAINT, font=("Segoe UI", 8), anchor="w", justify="left",
            wraplength=620,
        ).grid(row=row, column=1, sticky="w", pady=(0, 8))
        row += 1

        timeout_var = tk.StringVar(value=str(self.ai_config.timeout_seconds))
        row = labeled_entry(api_tab, "Timeout (seconds)", timeout_var, row)

        note = tk.Label(
            body,
            text="Privacy: a local CLI runs on this computer, but the model "
                 "provider it uses still receives the scenarios and authority "
                 "text. API modes send the same material to the endpoint above.",
            bg="#f7edda", fg="#6d5528", font=("Segoe UI", 8), anchor="w",
            justify="left", wraplength=720, padx=10, pady=8,
        )
        note.grid(row=4, column=0, sticky="ew", pady=(4, 4))

        def save() -> None:
            mode = next(
                (key for key, label in ai.MODE_LABELS.items() if label == mode_var.get()),
                "off",
            )
            try:
                timeout = int(timeout_var.get())
            except ValueError:
                timeout = ai.DEFAULT_TIMEOUT
            config = ai.AIConfig.from_mapping(
                {
                    "mode": mode,
                    "harness_command": harness_var.get().strip(),
                    "harness_effort": effort_var.get().strip(),
                    "claude_command": claude_var.get().strip(),
                    "claude_model": claude_model_var.get().strip(),
                    "api_base_url": api_base_var.get().strip(),
                    "api_model": api_model_var.get().strip(),
                    "api_key_env": api_key_env_var.get().strip(),
                    "api_key": api_key_var.get(),
                    "gemini_api_base": gemini_base_var.get().strip(),
                    "gemini_model": gemini_model_var.get().strip(),
                    "gemini_api_key_env": gemini_key_env_var.get().strip(),
                    "gemini_api_key": gemini_key_var.get(),
                    "anthropic_api_base": anthropic_base_var.get().strip(),
                    "anthropic_model": anthropic_model_var.get().strip(),
                    "anthropic_api_key_env": anthropic_key_env_var.get().strip(),
                    "anthropic_api_key": anthropic_key_var.get(),
                    "timeout_seconds": timeout,
                    "max_authorities": self.ai_config.max_authorities,
                    "authority_chars": self.ai_config.authority_chars,
                }
            )
            settings = storage.read_settings()
            settings["ai_review"] = ai.config_to_settings(config)
            storage.write_settings(settings)
            self.ai_config = config
            if self.comparison:
                self.ai_packet = ai.build_packet(
                    self.comparison, config, library_meta=self.library.meta
                )
                self._render_ai_idle(self.comparison)
            else:
                self._render_ai_empty()
            dialog.destroy()
            self._flash_status("AI review settings saved")

        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill="x", padx=20, pady=(0, 14))
        ttk.Button(buttons, text="Cancel", style="Ghost.TButton", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Save settings", style="Accent.TButton", command=save).pack(
            side="right", padx=(0, 8)
        )
        detect_harness()
        detect_claude()

    def _show_ai_help(self) -> None:
        messagebox.showinfo(
            "How AI review works",
            "AI review is optional; once configured it runs only when you ask.\n\n"
            "It sends both scenarios and the retrieved authority text to the "
            "provider you choose:\n"
            "• Google Gemini API (free tier) uses a Gemini key and defaults to "
            "gemini-3.5-flash. If the run says SERVICE_DISABLED, enable the "
            "Gemini API for the key's Cloud project or use a free AI Studio key.\n"
            "• DeepSeek Harness (local account) runs the installed headless "
            "agent. If you are signed in to the harness, it reuses that account "
            "through a small overlay; otherwise it needs DEEPSEEK_API_KEY. This "
            "is the default and uses the free local account.\n"
            "• Claude Code runs the installed `claude` CLI in print mode. It "
            "reuses whatever account Claude Code is signed in to.\n"
            "• OpenAI-compatible API calls the endpoint, model, and key in "
            "settings (DeepSeek, OpenAI, Ollama, LM Studio, or a gateway).\n"
            "• Anthropic Claude API calls the Messages API with an Anthropic key.\n"
            "• Manual: Copy AI prompt, paste it into any chat, then Import AI "
            "response with the JSON answer.\n\n"
            "The answer is checked before display: a citation that is not one of "
            "the comparison's authorities is flagged, and the review is labelled "
            "as AI-generated. It can still be wrong or miss a relevant section. "
            "It is not legal advice; verify the official text.\n\n"
            "Local search and comparison never make a network call.",
            parent=self.root,
        )

    def _render_authority_empty(self) -> None:
        for child in self.auth_container.winfo_children():
            child.destroy()
        inner = tk.Frame(self.auth_container, bg=SURFACE)
        inner.place(relx=0.5, rely=0.42, anchor="center")
        tk.Label(inner, text="↔", bg=SURFACE, fg=GOLD, font=("Georgia", 40, "bold")).pack()
        tk.Label(
            inner, text="Select an authority", bg=SURFACE, fg=INK,
            font=("Georgia", 17, "bold"),
        ).pack(pady=(4, 6))
        tk.Label(
            inner, text="See why each scenario matched it, side by side.", bg=SURFACE,
            fg=MUTED, font=FONT_UI,
        ).pack()

    def _render_comparison_empty(self) -> None:
        self.compare_count.configure(text="No comparison yet")
        self.compare_status.configure(
            text="Local retrieval. AI review is optional and runs only when you ask."
        )
        self.compare_tree.delete(*self.compare_tree.get_children())
        self._render_brief_empty()
        self._render_authority_empty()
        self._render_ai_empty()

    def _render_brief_empty(self) -> None:
        self.brief_text.configure(state="normal")
        self.brief_text.delete("1.0", "end")
        self.brief_text.insert("end", "SCENARIO COMPARISON\n", "kicker")
        self.brief_text.insert("end", "Two fact patterns, one research trail\n", "h1")
        self.brief_text.insert(
            "end",
            "Paste a scenario into each box above, then choose Compare scenarios. "
            "The brief will show shared issues, unique issues, the authorities each "
            "scenario raises, and requirements worth checking.\n\n",
            "body",
        )
        self.brief_text.insert("end", "Try the example pair →\n", "muted")
        self.brief_text.configure(state="disabled")
        self.brief_plain = ""
        self.brief_markdown = ""

    # -- scenario actions ----------------------------------------------------
    def _update_scenario_counts(self) -> None:
        for text in (self.scenario_a, self.scenario_b):
            label = getattr(text, "_count_label", None)
            if label:
                self._update_scenario_count(text, label)

    def _load_example_pair(self) -> None:
        self.scenario_a.set_value(EXAMPLE_A)
        self.scenario_b.set_value(EXAMPLE_B)
        self._update_scenario_counts()
        self.compare_status.configure(text="Example pair loaded. Choose Compare scenarios.")
        self.run_comparison()

    def _clear_scenarios(self) -> None:
        if self.ai_cancel is not None:
            self.ai_cancel.set()
        self.scenario_a.set_value("")
        self.scenario_b.set_value("")
        self._update_scenario_counts()
        self.comparison = None
        self.selected_row = None
        self._render_comparison_empty()

    def _swap_scenarios(self) -> None:
        a = self.scenario_a.value()
        b = self.scenario_b.value()
        self.scenario_a.set_value(b)
        self.scenario_b.set_value(a)
        self._update_scenario_counts()

    def run_comparison(self) -> None:
        if self.ai_cancel is not None:
            self.ai_cancel.set()
        self._update_scenario_counts()
        a = self.scenario_a.value().strip()
        b = self.scenario_b.value().strip()
        if len(a) < 8 or len(b) < 8:
            messagebox.showinfo(
                "Two scenarios needed",
                "Paste a fact pattern into both Scenario A and Scenario B.",
                parent=self.root,
            )
            return
        self.compare_btn.configure(state="disabled")
        self.compare_status.configure(text="Reading both scenarios…")
        self.root.update_idletasks()
        try:
            report = self.library.compare(a, b)
        except Exception as exc:  # pragma: no cover - defensive UI path
            self.compare_btn.configure(state="normal")
            self.compare_status.configure(text="Comparison failed.")
            messagebox.showerror("Comparison failed", str(exc), parent=self.root)
            return
        self.comparison = report
        self.compare_btn.configure(state="normal")
        self._render_comparison(report)
        self.notebook.select(self.compare_tab)
        self.compare_notebook.select(self.brief_tab)

    def _render_comparison(self, report: ComparisonReport) -> None:
        self.compare_tree.delete(*self.compare_tree.get_children())
        self.comparison_rows: dict[str, ComparisonRow] = {}
        self._add_comparison_group("Shared", report.shared, "shared", "group-shared")
        self._add_comparison_group("Only in A", report.only_a, "onlya", "group-onlya")
        self._add_comparison_group("Only in B", report.only_b, "onlyb", "group-onlyb")
        self.compare_count.configure(
            text=f"{len(report.shared)} shared · {len(report.only_a)} A-only · {len(report.only_b)} B-only"
        )
        self.compare_status.configure(
            text=f"A: {len(report.a.matches)} authorities  ·  B: {len(report.b.matches)} authorities  ·  local analysis  ·  AI optional"
        )
        self.ai_analysis = None
        self.ai_packet = ai.build_packet(
            report, self.ai_config, library_meta=self.library.meta
        )
        self._build_brief(report)
        self._render_ai_idle(report)
        self.compare_notebook.select(self.brief_tab)
        self._render_authority_empty()
        self.root.after_idle(self._set_compare_sash)

    def _add_comparison_group(
        self, label: str, rows: list[ComparisonRow], tag: str, iid: str,
    ) -> None:
        parent = self.compare_tree.insert(
            "", "end", iid=iid, text=f"{label}  ({len(rows)})", open=True,
            values=("", ""), tags=("group",),
        )
        if not rows:
            self.compare_tree.insert(
                parent, "end", iid=f"{iid}:empty", text="No authorities retrieved",
                values=("", ""), tags=("group",),
            )
            return
        for row in rows:
            row_iid = f"{iid}:{row.section.uid}"
            self.comparison_rows[row_iid] = row
            self.compare_tree.insert(
                parent, "end", iid=row_iid,
                text=f"{row.section.citation_short}  {row.section.title}",
                values=(
                    f"{row.a_score:.0f}" if row.a_score else "—",
                    f"{row.b_score:.0f}" if row.b_score else "—",
                ),
                tags=(tag,),
            )

    def _on_compare_select(self, _event=None) -> None:
        selection = self.compare_tree.selection()
        if not selection:
            return
        iid = selection[0]
        if iid in ("group-shared", "group-onlya", "group-onlyb"):
            return
        row = getattr(self, "comparison_rows", {}).get(iid)
        if row:
            self._show_authority_row(row)
            self.compare_notebook.select(self.auth_tab)

    def _show_authority_row(self, row: ComparisonRow | None) -> None:
        if not row or not self.comparison:
            self._render_authority_empty()
            return
        self.selected_row = row
        for child in self.auth_container.winfo_children():
            child.destroy()
        self.auth_container.grid_rowconfigure(0, weight=1)
        self.auth_container.grid_columnconfigure(0, weight=1)

        outer = tk.Frame(self.auth_container, bg=SURFACE)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.grid_rowconfigure(2, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        head = tk.Frame(outer, bg=SURFACE)
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 2))
        head.grid_columnconfigure(0, weight=1)
        tk.Label(
            head, text=row.section.citation, bg=SURFACE, fg=TEAL,
            font=("Consolas", 10, "bold"), anchor="w",
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            head, text="RULE" if row.section.kind == "Rule" else "STATUTE",
            bg=CHIP if row.section.kind == "Rule" else "#f3e6d1",
            fg=CHIP_TEXT if row.section.kind == "Rule" else "#7c541f",
            font=("Segoe UI", 8, "bold"), padx=7, pady=2,
        ).grid(row=0, column=1, sticky="e", padx=(0, 8))
        ttk.Button(
            head, text="Open official source ↗", style="Ghost.TButton",
            command=lambda: webbrowser.open(row.section.url),
        ).grid(row=0, column=2, sticky="e")

        tk.Label(
            outer, text=row.section.title, bg=SURFACE, fg=INK,
            font=("Georgia", 16, "bold"), anchor="w", justify="left",
            wraplength=720,
        ).grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 8))

        content = tk.Frame(outer, bg=SURFACE)
        content.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 8))
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1, uniform="auth")
        content.grid_columnconfigure(1, weight=1, uniform="auth")

        sides = tk.Frame(content, bg=SURFACE)
        sides.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        sides.grid_columnconfigure(0, weight=1)
        sides.grid_rowconfigure(0, weight=1, uniform="side")
        sides.grid_rowconfigure(1, weight=1, uniform="side")

        a_match = self.comparison.a.match_map.get(row.section.uid)
        b_match = self.comparison.b.match_map.get(row.section.uid)
        self._build_side_panel(sides, 0, 0, "SCENARIO A", BRICK, row.a_score, a_match)
        self._build_side_panel(sides, 1, 0, "SCENARIO B", TEAL, row.b_score, b_match)

        body = tk.Frame(content, bg=SURFACE)
        body.grid(row=0, column=1, sticky="nsew")
        body.grid_rowconfigure(1, weight=1)
        body.grid_columnconfigure(0, weight=1)
        tk.Label(
            body, text="FULL TEXT", bg=SURFACE, fg=FAINT,
            font=("Segoe UI", 8, "bold"), anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        text_wrap = tk.Frame(body, bg=SURFACE_ALT, highlightbackground=BORDER, highlightthickness=1)
        text_wrap.grid(row=1, column=0, sticky="nsew")
        text_wrap.grid_rowconfigure(0, weight=1)
        text_wrap.grid_columnconfigure(0, weight=1)
        txt = tk.Text(
            text_wrap, wrap="word", bg=SURFACE_ALT, fg=INK, bd=0, relief="flat",
            font=("Segoe UI", 10), padx=12, pady=8, highlightthickness=0,
            spacing1=1, spacing3=4, cursor="arrow",
        )
        txt.grid(row=0, column=0, sticky="nsew")
        txt_scroll = ttk.Scrollbar(text_wrap, orient="vertical", command=txt.yview)
        txt_scroll.grid(row=0, column=1, sticky="ns")
        txt.configure(yscrollcommand=txt_scroll.set)
        txt.insert("1.0", row.section.body)
        txt.configure(state="disabled")

        footer = tk.Frame(outer, bg=SURFACE_ALT)
        footer.grid(row=3, column=0, sticky="ew")
        ttk.Button(
            footer, text="Copy citation", style="Ghost.TButton",
            command=lambda: self._copy_text(row.section.citation),
        ).pack(side="left", padx=(18, 6), pady=8)
        ttk.Button(
            footer, text="Copy text", style="Ghost.TButton",
            command=lambda: self._copy_text(
                f"{row.section.citation} — {row.section.title}\n\n{row.section.body}\n\n{row.section.url}"
            ),
        ).pack(side="left", padx=6, pady=8)
        tk.Label(
            footer, text=f"{row.section.word_count:,} words", bg=SURFACE_ALT,
            fg=FAINT, font=("Segoe UI", 8),
        ).pack(side="right", padx=18)

    def _build_side_panel(
        self, parent, row: int, column: int, label: str, color: str,
        score: float, match,
    ) -> None:
        frame = tk.Frame(
            parent, bg=SURFACE_ALT, highlightbackground=BORDER, highlightthickness=1,
        )
        frame.grid(
            row=row, column=column, sticky="nsew",
            pady=(0, 8) if row == 0 else (8, 0),
        )
        frame.grid_columnconfigure(0, weight=1)
        top = tk.Frame(frame, bg=SURFACE_ALT)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))
        tk.Label(
            top, text=label, bg=SURFACE_ALT, fg=color,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left")
        tk.Label(
            top, text=f"match {score:.0f}" if score else "not retrieved",
            bg=SURFACE_ALT, fg=MUTED, font=("Segoe UI", 8),
        ).pack(side="right")
        body = tk.Frame(frame, bg=SURFACE_ALT)
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        if match:
            for reason in match.reasons[:1]:
                tk.Label(
                    body, text="• " + reason, bg=SURFACE_ALT, fg=INK,
                    font=("Segoe UI", 8), anchor="w", justify="left",
                    wraplength=280,
                ).pack(fill="x", pady=(0, 2))
            if match.concepts:
                tk.Label(
                    body, text=" · ".join(match.concepts[:3]), bg=SURFACE_ALT,
                    fg=CHIP_TEXT, font=("Segoe UI", 8), anchor="w", justify="left",
                    wraplength=280,
                ).pack(fill="x", pady=(4, 0))
        else:
            tk.Label(
                body, text="No match in this scenario's retrieved authorities.",
                bg=SURFACE_ALT, fg=FAINT, font=("Segoe UI", 8), anchor="w",
                justify="left", wraplength=280,
            ).pack(fill="x")

    # -- comparison brief ----------------------------------------------------
    def _build_brief(self, report: ComparisonReport) -> None:
        self.brief_text.configure(state="normal")
        self.brief_text.delete("1.0", "end")
        plain: list[str] = []
        md: list[str] = []

        def add(text: str, tag: str = "body") -> None:
            self.brief_text.insert("end", text, tag)
            plain.append(text)

        add("SCENARIO COMPARISON\n", "kicker")
        add("Scenario comparison brief\n", "h1")
        add(
            "Shared issues, unique issues, and authorities to verify.\n",
            "muted",
        )
        add(
            f"Generated {datetime.now().strftime('%B %d, %Y at %I:%M %p')}  ·  "
            f"Library snapshot {self.library.meta.get('generated_at_display', 'current')}\n",
            "muted",
        )
        add("─" * 72 + "\n", "divider")
        md.extend(
            [
                "# TREC Scenario Comparison",
                "",
                f"*Generated {datetime.now().strftime('%B %d, %Y')} · "
                f"Library snapshot {self.library.meta.get('generated_at_display', 'current')}*",
                "",
            ]
        )

        for letter, scenario in (("A", report.a), ("B", report.b)):
            add(f"SCENARIO {letter}  ·  {len(scenario.keywords)} issue terms  ·  {scenario.word_count} words\n", "h2")
            add(self._compact_text(scenario.text, 420) + "\n", "body")
            if scenario.lenses:
                for lens_id, lens_label, why in scenario.lenses:
                    add(f"Issue lens: {lens_label}\n", "bullet")
                    add(f"  {why}\n", "muted")
                    md.append(f"**Scenario {letter} lens — {lens_label}:** {why}")
                md.append("")
            elif scenario.concepts:
                labels = ", ".join(label for _, label, _ in scenario.concepts[:4])
                add(f"Recognized issues: {labels}\n", "muted")
                md.append(f"**Scenario {letter} recognized issues:** {labels}")
                md.append("")

        add("\nWHAT THE SCENARIOS SHARE\n", "h2")
        add(self._clean_overview(report.overview) + "\n", "body")
        md.extend(["## What the scenarios share", "", report.overview, ""])

        if report.shared_concepts:
            add("Shared issue lenses: " + ", ".join(label for _, label in report.shared_concepts) + "\n", "bullet")
            md.append("**Shared issue lenses:** " + ", ".join(label for _, label in report.shared_concepts))
            md.append("")
        if report.only_a_concepts:
            add("Only A raises: " + ", ".join(label for _, label in report.only_a_concepts) + "\n", "bullet")
            md.append("**Only A raises:** " + ", ".join(label for _, label in report.only_a_concepts))
            md.append("")
        if report.only_b_concepts:
            add("Only B raises: " + ", ".join(label for _, label in report.only_b_concepts) + "\n", "bullet")
            md.append("**Only B raises:** " + ", ".join(label for _, label in report.only_b_concepts))
            md.append("")

        add("\nAUTHORITIES TO REVIEW\n", "h2")
        self._add_brief_rows(add, md, "Shared", report.shared)
        self._add_brief_rows(add, md, "Only in A", report.only_a)
        self._add_brief_rows(add, md, "Only in B", report.only_b)

        add("\nDEADLINES & TRIGGERS\n", "h2")
        if report.deadlines:
            add(
                "Extracted time language. These are sentences to verify, not a "
                "computed deadline. Confirm the triggering event and the counting rule.\n",
                "muted",
            )
            md.append("## Deadlines & triggers")
            md.append(
                "*Extracted time language, not a computed deadline. Confirm the "
                "triggering event and the counting rule.*"
            )
            md.append("")
            for citation, sentence in report.deadlines:
                add(f"{citation}  ", "cite")
                add(sentence + "\n", "bullet")
                md.append(f"- **{citation}** — {sentence}")
        else:
            add("No deadline-style sentences extracted from the retrieved authorities.\n", "body")
            md.append("## Deadlines & triggers")
            md.append("No deadline-style sentences extracted.")
        md.append("")

        add("\nPOSSIBLE CONSEQUENCES\n", "h2")
        if report.penalties:
            add(
                "Extracted consequence language, grouped for review. This does "
                "not decide which consequence applies to these facts.\n",
                "muted",
            )
            md.append("## Possible consequences")
            md.append(
                "*Extracted consequence language, not a finding that a penalty applies.*"
            )
            md.append("")
            for category, citation, sentence in report.penalties:
                add(f"{category}  ·  {citation}\n", "cite")
                add(sentence + "\n", "bullet")
                md.append(f"- **{category}** · **{citation}** — {sentence}")
        else:
            add("No penalty or consequence sentences extracted from the retrieved authorities.\n", "body")
            md.append("## Possible consequences")
            md.append("No penalty or consequence sentences extracted.")
        md.append("")

        add("\nFACTS TO CONFIRM\n", "h2")
        for fact in report.facts_to_confirm:
            add("• " + fact + "\n", "bullet")
            md.append(f"- {fact}")
        md.append("")

        add("\nREQUIREMENTS WORTH CHECKING\n", "h2")
        if report.checklist:
            for citation, sentence in report.checklist:
                add(f"{citation}  ", "cite")
                add(sentence + "\n", "bullet")
                md.append(f"- **{citation}** — {sentence}")
        else:
            add(
                "No requirement-style sentences were extracted from the retrieved sections. "
                "Read the authorities above and confirm the operative language.\n",
                "body",
            )
            md.append("No requirement-style sentences were extracted.")
        md.append("")

        add("\n" + "─" * 72 + "\n", "divider")
        add(
            "Research aid only — not legal advice. Deadlines and consequences are "
            "extracted language, not legal conclusions. Verify the official text "
            "before acting. Official links are available in each authority's detail view.\n",
            "warn",
        )
        md.extend(
            [
                "",
                "---",
                "",
                "*Research aid only — not legal advice. Verify the official text before acting.*",
                "",
            ]
        )
        self.brief_text.configure(state="disabled")
        self.brief_text.yview_moveto(0)
        self.brief_plain = "".join(plain)
        self.brief_markdown = "\n".join(md).strip() + "\n"

    def _add_brief_rows(self, add, md, label: str, rows: list[ComparisonRow]) -> None:
        add(f"{label}  ({len(rows)})\n", "body")
        if not rows:
            add("  No authorities retrieved.\n", "muted")
            md.append(f"**{label}:** none retrieved")
            md.append("")
            return
        for row in rows[:10]:
            add("  •  ", "bullet")
            add(row.section.citation_short + "  ", "cite")
            add(row.section.title + "\n", "bullet")
            md.append(
                f"- **{row.section.citation_short}** — {row.section.title} "
                f"(A {row.a_score:.0f} / B {row.b_score:.0f})"
            )
        if len(rows) > 10:
            add(f"  … and {len(rows) - 10} more in the authority list.\n", "muted")
        md.append("")

    @staticmethod
    def _compact_text(value: str, limit: int) -> str:
        value = re.sub(r"\s+", " ", value).strip()
        if len(value) <= limit:
            return value
        return value[: limit - 1].rsplit(" ", 1)[0] + "…"

    @staticmethod
    def _clean_overview(value: str) -> str:
        return re.sub(r"\n{2,}", "\n", value).strip()

    def copy_brief(self) -> None:
        if not getattr(self, "brief_plain", ""):
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.brief_plain)
        self._flash_status("Comparison brief copied")

    def _copy_text(self, value: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self._flash_status("Copied to clipboard")

    def export_comparison(self) -> None:
        if not self.comparison:
            messagebox.showinfo(
                "Nothing to export", "Compare two scenarios first.", parent=self.root,
            )
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export comparison brief",
            defaultextension=".md",
            initialfile=f"trec-comparison-{datetime.now().strftime('%Y%m%d')}.md",
            filetypes=(("Markdown", "*.md"), ("Text", "*.txt"), ("All files", "*.*")),
        )
        if not path:
            return
        markdown = getattr(self, "brief_markdown", self.brief_plain)
        if self.ai_analysis is not None:
            markdown = markdown.rstrip() + "\n\n" + self.ai_analysis.to_markdown()
        Path(path).write_text(markdown, encoding="utf-8")
        self._flash_status("Comparison brief exported")

    def _show_compare_help(self) -> None:
        messagebox.showinfo(
            "How comparison works",
            "1. Paste a fact pattern into each box.\n"
            "2. Compare scenarios retrieves the TREC rules and statutes that match "
            "each fact pattern using local keyword ranking, issue vocabulary, and "
            "short issue lenses.\n"
            "3. The brief reports shared issues, unique issues, authorities, and "
            "requirement-style sentences to verify.\n"
            "4. The AI review tab is an optional second opinion. It is off by "
            "default; use Settings or copy the prompt for any AI chat.\n\n"
            "Scores are relative to the best match in that scenario, not a probability "
            "or a legal conclusion. Read the full text and verify the official source.",
            parent=self.root,
        )

    # ------------------------------------------------------------------
    # Saved tab
    # ------------------------------------------------------------------
    def _build_saved_tab(self) -> None:
        self.saved_tab.grid_rowconfigure(0, weight=1)
        self.saved_tab.grid_columnconfigure(0, weight=0, minsize=430)
        self.saved_tab.grid_columnconfigure(1, weight=1)

        left = tk.Frame(
            self.saved_tab, bg=SURFACE, highlightbackground=BORDER,
            highlightthickness=1,
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left.grid_rowconfigure(2, weight=1)
        left.grid_columnconfigure(0, weight=1)

        head = tk.Frame(left, bg=SURFACE)
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 2))
        tk.Label(head, text="Saved authorities", bg=SURFACE, fg=INK, font=FONT_DISPLAY).pack(side="left")
        self.saved_count = tk.Label(head, text="", bg=SURFACE, fg=MUTED, font=FONT_SMALL)
        self.saved_count.pack(side="right")
        tk.Label(
            left, text="Bookmarks and your own notes stay on this computer.",
            bg=SURFACE, fg=MUTED, font=FONT_SMALL, anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 8))

        tree_wrap = tk.Frame(left, bg=SURFACE)
        tree_wrap.grid(row=2, column=0, sticky="nsew", padx=(10, 8), pady=(0, 10))
        tree_wrap.grid_rowconfigure(0, weight=1)
        tree_wrap.grid_columnconfigure(0, weight=1)
        self.saved_tree = ttk.Treeview(
            tree_wrap, columns=("cite", "title"), show="headings", selectmode="browse",
        )
        self.saved_tree.heading("cite", text="Citation", anchor="w")
        self.saved_tree.heading("title", text="Section", anchor="w")
        self.saved_tree.column("cite", width=108, minwidth=94, stretch=False)
        self.saved_tree.column("title", width=280, minwidth=170, stretch=True)
        self.saved_tree.grid(row=0, column=0, sticky="nsew")
        ss = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.saved_tree.yview)
        ss.grid(row=0, column=1, sticky="ns")
        self.saved_tree.configure(yscrollcommand=ss.set)
        self.saved_tree.bind("<<TreeviewSelect>>", self._on_saved_select)

        right = tk.Frame(
            self.saved_tab, bg=SURFACE, highlightbackground=BORDER,
            highlightthickness=1,
        )
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(2, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self.saved_kicker = tk.Label(
            right, text="YOUR NOTE", bg=SURFACE, fg=GOLD,
            font=("Segoe UI", 8, "bold"), anchor="w",
        )
        self.saved_kicker.grid(row=0, column=0, sticky="ew", padx=22, pady=(20, 2))
        self.saved_title = tk.Label(
            right, text="Select a saved authority", bg=SURFACE, fg=INK,
            font=("Georgia", 18, "bold"), anchor="w", justify="left", wraplength=700,
        )
        self.saved_title.grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 10))
        note_wrap = tk.Frame(right, bg=SURFACE)
        note_wrap.grid(row=2, column=0, sticky="nsew", padx=22, pady=(0, 10))
        note_wrap.grid_rowconfigure(0, weight=1)
        note_wrap.grid_columnconfigure(0, weight=1)
        self.saved_note = tk.Text(
            note_wrap, wrap="word", bg=SURFACE_ALT, fg=INK, bd=0, relief="flat",
            font=("Segoe UI", 10), padx=12, pady=10, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=TEAL, spacing1=2, spacing3=3,
        )
        self.saved_note.grid(row=0, column=0, sticky="nsew")
        note_scroll = ttk.Scrollbar(note_wrap, orient="vertical", command=self.saved_note.yview)
        note_scroll.grid(row=0, column=1, sticky="ns")
        self.saved_note.configure(yscrollcommand=note_scroll.set)
        self.saved_note.insert(
            "1.0",
            "Add your own summary, questions, or follow-up. Notes are saved locally.",
        )
        self.saved_note.configure(foreground=FAINT)
        self.saved_note.bind("<FocusIn>", self._note_focus_in)
        self.saved_note.bind("<FocusOut>", self._note_focus_out)

        footer = tk.Frame(right, bg=SURFACE_ALT)
        footer.grid(row=3, column=0, sticky="ew")
        self.save_note_btn = ttk.Button(
            footer, text="Save note", style="Accent.TButton", command=self._save_note,
        )
        self.save_note_btn.pack(side="left", padx=(18, 6), pady=10)
        ttk.Button(
            footer, text="Open section", style="Ghost.TButton", command=self._open_saved,
        ).pack(side="left", padx=6, pady=10)
        ttk.Button(
            footer, text="Remove bookmark", style="Ghost.TButton", command=self._remove_saved,
        ).pack(side="left", padx=6, pady=10)

        self.saved_empty = tk.Label(
            right,
            text="Save a section from Search & Browse to build your own research set.\n"
                 "Each bookmark can carry a note.",
            bg=SURFACE, fg=FAINT, font=FONT_UI, justify="center",
        )
        self.saved_empty.grid(row=4, column=0, pady=(0, 18))

    def _refresh_saved(self) -> None:
        if not hasattr(self, "saved_tree"):
            return
        self.saved_tree.delete(*self.saved_tree.get_children())
        items = self.bookmarks.items()
        self.saved_count.configure(text=f"{len(items)} saved")
        for uid, data in items:
            section = self.library.section(uid)
            if not section:
                continue
            self.saved_tree.insert(
                "", "end", iid=uid,
                values=(section.citation_short, section.title),
            )
        if items:
            self.saved_empty.grid_remove()
        else:
            self.saved_empty.grid()

    def _on_saved_select(self, _event=None) -> None:
        selection = self.saved_tree.selection()
        if not selection:
            return
        uid = selection[0]
        section = self.library.section(uid)
        if not section:
            return
        self.saved_current_uid = uid
        self.saved_title.configure(text=f"{section.citation_short}  {section.title}")
        self.saved_kicker.configure(text="YOUR NOTE  ·  " + section.citation)
        note = self.bookmarks.note(uid)
        self.saved_note.delete("1.0", "end")
        if note:
            self.saved_note.configure(foreground=INK)
            self.saved_note.insert("1.0", note)
        else:
            self.saved_note.configure(foreground=FAINT)
            self.saved_note.insert(
                "1.0", "Add your own summary, questions, or follow-up."
            )

    def _note_focus_in(self, _event=None) -> None:
        if self.saved_note.cget("foreground") == FAINT:
            self.saved_note.delete("1.0", "end")
            self.saved_note.configure(foreground=INK)

    def _note_focus_out(self, _event=None) -> None:
        if not self.saved_note.get("1.0", "end").strip():
            self.saved_note.configure(foreground=FAINT)
            self.saved_note.insert(
                "1.0", "Add your own summary, questions, or follow-up."
            )

    def _save_note(self) -> None:
        if not self.saved_current_uid:
            return
        note = self.saved_note.get("1.0", "end").strip()
        if note.startswith("Add your own summary"):
            note = ""
        try:
            self.bookmarks.set_note(self.saved_current_uid, note)
        except OSError as exc:
            messagebox.showerror(
                "Could not save note", f"The note file could not be written:\n{exc}",
                parent=self.root,
            )
            return
        self._flash_status("Note saved")

    def _open_saved(self) -> None:
        if not self.saved_current_uid:
            return
        section = self.library.section(self.saved_current_uid)
        if not section:
            return
        self.notebook.select(self.search_tab)
        self.source_var.set("All sources")
        self.chapter_var.set("All chapters")
        self.search_entry.set_value(section.citation_short)
        self._run_search()

    def _remove_saved(self) -> None:
        if not self.saved_current_uid:
            return
        section = self.library.section(self.saved_current_uid)
        if section and not messagebox.askyesno(
            "Remove bookmark", f"Remove {section.citation_short} from saved authorities?",
            parent=self.root,
        ):
            return
        self.bookmarks.remove(self.saved_current_uid)
        self.saved_current_uid = None
        self.saved_title.configure(text="Select a saved authority")
        self.saved_note.delete("1.0", "end")
        self._refresh_saved()
        self._flash_status("Bookmark removed")

    # ------------------------------------------------------------------
    # Help / sources / misc
    # ------------------------------------------------------------------
    def show_help(self) -> None:
        messagebox.showinfo(
            "Quick start",
            "SEARCH & BROWSE\n"
            "Type keywords, a phrase in quotes, or a section number. Filter by "
            "source or chapter. Select a row to read the full text; use the buttons "
            "to copy a citation, open the official source, or save it.\n\n"
            "COMPARE SCENARIOS\n"
            "Paste two fact patterns and choose Compare scenarios. The brief shows "
            "shared and unique issues plus the authorities each one raises, extracted "
            "deadline and consequence language, and facts to confirm. Select an "
            "authority to see why it matched each scenario.\n\n"
            "UPDATES\n"
            "The app checks the public sources when online, at most once a day. "
            "Use Tools to check now, open the downloaded-library folder, or reload "
            "a staged update without restarting.\n\n"
            "SAVED\n"
            "Bookmarks and your notes are stored locally. Open one to jump back to "
            "the section.\n\n"
            "SHORTCUTS\n"
            "Ctrl+F search  ·  Ctrl+Enter compare  ·  Ctrl+E export  ·  F1 help",
            parent=self.root,
        )

    def open_official_sources(self) -> None:
        webbrowser.open("https://www.trec.texas.gov/agency-information/rules-and-laws/trec-rules")
        webbrowser.open("https://www.trec.texas.gov/rules-and-laws")

    def show_about(self) -> None:
        counts = self.library.meta.get("counts", {})
        rules = counts.get("trec-rules", 0)
        statutes = sum(v for k, v in counts.items() if k != "trec-rules")
        messagebox.showinfo(
            "About this library",
            f"TREC Rulebook v{__version__}\n\n"
            f"{len(self.library.sections)} sections: {rules} TREC rule sections and "
            f"{statutes} statutory sections.\n"
            f"Snapshot: {self.library.meta.get('generated_at_display', 'unknown')}\n"
            f"Data file: {self.data_path}\n\n"
            "Sources: the TREC rules page; Texas Occupations Code chapters 1101, "
            "1102, 1105, and 53; and Texas Property Code chapter 221. Chapter 53 "
            "is included as related licensing law.\n\n"
            "The app can stage a newer library from the public sources when online.\n\n"
            + DISCLAIMER,
            parent=self.root,
        )

