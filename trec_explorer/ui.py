"""Tkinter desktop interface for the TREC Rulebook."""

from __future__ import annotations

import json
import os
import re
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from tkinter import font as tkfont

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
    def __init__(self, root: tk.Tk, data_path: Path):
        self.root = root
        self.data_path = data_path
        self.library = Library(data_path)
        self.bookmarks = BookmarkStore(_bookmark_path(data_path))

        self.current_section: Section | None = None
        self.current_query = ""
        self.current_rows: dict[str, Section] = {}
        self.search_job: str | None = None
        self.comparison: ComparisonReport | None = None
        self.selected_row: ComparisonRow | None = None
        self.saved_current_uid: str | None = None

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

    # -- window / styles -----------------------------------------------------
    def _configure_root(self) -> None:
        self.root.title("TREC Rulebook — Search & Scenario Compare")
        self.root.configure(bg=BG)
        self.root.minsize(1080, 720)
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

        help_menu = tk.Menu(menubar, tearoff=False, font=FONT_UI)
        help_menu.add_command(label="Quick start", command=self.show_help)
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
        tk.Label(
            snapshot,
            text=f"LIBRARY  ·  {self.library.meta.get('generated_at_display', 'current')}",
            bg=NAVY_DARK, fg="#d8e4e3", font=("Segoe UI", 8, "bold"),
            padx=10, pady=6,
        ).pack()

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
        self.status_left = tk.Label(
            bar,
            text=f"{len(self.library.sections)} sections  ·  {rules} TREC rules  ·  {statutes} statutory sections",
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
        self.kind_badge.configure(
            text="TREC RULE" if section.kind == "Rule" else "TEXAS STATUTE",
            bg=CHIP if section.kind == "Rule" else "#f3e6d1",
            fg=CHIP_TEXT if section.kind == "Rule" else "#7c541f",
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
        original = self.status_left.cget("text")
        self.status_left.configure(text=text, fg=TEAL)
        self.root.after(2200, lambda: self.status_left.configure(text=original, fg=MUTED))

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
        self.compare_status = tk.Label(
            action_row, text="Scenario comparison uses local keyword and issue-lens retrieval — no network call.",
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
        self.compare_notebook.add(self.brief_tab, text="  Comparison brief  ")
        self.compare_notebook.add(self.auth_tab, text="  Authority detail  ")
        self._build_brief_tab()
        self._build_authority_tab()
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
            footer, text="Built from the local library; no scenario text leaves this computer.",
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
            text="Scenario comparison uses local keyword and issue-lens retrieval — no network call."
        )
        self.compare_tree.delete(*self.compare_tree.get_children())
        self._render_brief_empty()
        self._render_authority_empty()

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
            text=f"A: {len(report.a.matches)} authorities  ·  B: {len(report.b.matches)} authorities  ·  local analysis"
        )
        self._build_brief(report)
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
            "Research aid only — not legal advice. Verify the official text before acting. "
            "Official links are available in each authority's detail view.\n",
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
        Path(path).write_text(getattr(self, "brief_markdown", self.brief_plain), encoding="utf-8")
        self._flash_status("Comparison brief exported")

    def _show_compare_help(self) -> None:
        messagebox.showinfo(
            "How comparison works",
            "1. Paste a fact pattern into each box.\n"
            "2. Compare scenarios retrieves the TREC rules and statutes that match "
            "each fact pattern using local keyword ranking, issue vocabulary, and "
            "short issue lenses.\n"
            "3. The brief reports shared issues, unique issues, authorities, and "
            "requirement-style sentences to verify.\n\n"
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
            "shared and unique issues plus the authorities each one raises. Select "
            "an authority to see why it matched each scenario.\n\n"
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
            f"TREC Rulebook\n\n"
            f"{len(self.library.sections)} sections: {rules} TREC rule sections and "
            f"{statutes} statutory sections.\n"
            f"Snapshot: {self.library.meta.get('generated_at_display', 'unknown')}\n"
            f"Data file: {self.data_path}\n\n"
            "Sources: the TREC rules page and the statutes linked from TREC's "
            "Rules and Laws page (Texas Occupations Code chapters 1101 and 1102; "
            "Texas Property Code chapter 221).\n\n"
            + DISCLAIMER,
            parent=self.root,
        )

