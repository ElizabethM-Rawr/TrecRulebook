"""Optional AI-assisted review for the scenario comparison.

The comparison engine in :mod:`trec_explorer.engine` is deterministic and
offline.  This module adds an *optional* second pass: it packages the two
scenarios and the retrieved authorities, asks a model for a structured review,
validates the answer, and turns it into red flags a person can check.

Transports
----------
``harness``
    Run the local DeepSeek Harness headless CLI (``dsh --profile headless``).
    If the harness has a signed-in account, it is reused; otherwise the run
    uses ``DEEPSEEK_API_KEY`` from the environment or the harness ``.env``.
``api``
    Call an OpenAI-compatible ``/chat/completions`` endpoint (DeepSeek, OpenAI,
    Ollama, LM Studio, or a gateway in front of one).
``manual``
    The UI copies the prepared prompt and applies a JSON response pasted back
    from any chat model.  The app itself makes no network call in this mode.

Nothing here runs unless the user explicitly starts a review.  Scenario text
is only sent when a network/harness transport is chosen.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .engine import (
    ComparisonReport,
    ScenarioReport,
    extract_deadlines,
    extract_penalties,
    find_citation,
)
from . import storage

SCHEMA_NAME = "trec-ai-review-request"
SCHEMA_VERSION = 1

MODES = ("off", "gemini", "harness", "claude-cli", "api", "anthropic")
MODE_LABELS = {
    "off": "Off — local comparison only",
    "gemini": "Google Gemini API (free tier)",
    "harness": "DeepSeek Harness (local account)",
    "claude-cli": "Claude Code (local CLI)",
    "api": "OpenAI-compatible API",
    "anthropic": "Anthropic Claude API",
}
EFFORT_LEVELS = ("off", "low", "high", "max")
RISK_LEVELS = ("high", "medium", "low", "unclear")
CONFIDENCE_LEVELS = ("high", "medium", "low")
SEVERITIES = ("high", "medium", "low")
SIDES = ("A", "B", "both")

DEFAULT_API_BASE = "https://api.deepseek.com/v1"
DEFAULT_API_MODEL = "deepseek-chat"
DEFAULT_API_KEY_ENV = "TREC_AI_API_KEY"
DEFAULT_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_GEMINI_KEY_ENV = "GEMINI_API_KEY"
DEFAULT_ANTHROPIC_BASE = "https://api.anthropic.com"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_HARNESS_EFFORT = "low"
DEFAULT_TIMEOUT = 240
DEFAULT_MAX_AUTHORITIES = 14
DEFAULT_AUTHORITY_CHARS = 2400

# A deterministic pre-check should stay conservative: these expressions only
# answer "does the scenario mention this kind of fact at all?", not whether the
# fact is sufficient.
DATE_RE = re.compile(
    r"\b(?:"
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?"
    r"|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?"
    r"|dec(?:ember)?)\s+\d{1,2}(?:,?\s+\d{4})?"
    r"|(?:last|next|this)\s+(?:week|month|year|spring|summer|fall|winter)"
    r"|\d+\s+(?:calendar\s+)?(?:hours?|days?|weeks?|months?|years?)\s+(?:ago|before|after|from|to|within|late|earlier|later)?"
    r"|within\s+\d+\s+(?:calendar\s+)?(?:hours?|days?|weeks?|months?|years?)"
    r"|deadline|due date|expires?|expiration"
    r")\b",
    re.I,
)
WRITING_RE = re.compile(
    r"\b(?:wrote|written|writing|email|e-mail|text(?:ed|s)?|letter|memo|"
    r"contract|agreement|signed|sign|form|addendum|advertis(?:e|ed|ement|ing)|"
    r"listing|disclosure|notice|document(?:ed|s)?|post(?:ed|ing)?|"
    r"website|social media|billboard|flyer|brochure|handout)\b",
    re.I,
)
MONEY_RE = re.compile(
    r"\b(?:\$\s?\d[\d,.]*|\d[\d,.]*\s*(?:dollars|usd)|commission|fee|"
    r"deposit|earnest|trust money|escrow|payment|paid|pay|rebate|bonus|"
    r"referral fee|option fee|security deposit)\b",
    re.I,
)
LICENSE_RE = re.compile(
    r"\b(?:licen[cs]ed|unlicensed|license|licensure|sponsor(?:ed|ing|ship)?|"
    r"renew(?:al|ed)?|inactive|expired|probationary|exempt)\b",
    re.I,
)
PARTY_RE = re.compile(
    r"\b(?:broker|agent|inspector|buyer|seller|client|customer|developer|"
    r"purchaser|landlord|tenant|owner|principal|consumer)\b",
    re.I,
)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.S | re.I)
_CITATION_NUMBER_RE = re.compile(r"\b(\d{3,4}\.\d+[A-Za-z]?)\b")
_WORDCUT_RE = re.compile(r"\s+")


class AIReviewError(RuntimeError):
    """A review could not be produced or applied.

    ``code`` is stable enough for the UI and CLI to branch on:

    ``disabled``        AI review is turned off.
    ``no_harness``      The DeepSeek Harness CLI could not be found.
    ``no_claude``       The Claude Code CLI could not be found.
    ``harness_failed``  The harness exited non-zero or produced no answer.
    ``claude_failed``   Claude Code exited non-zero or produced no answer.
    ``harness_timeout`` The run exceeded the configured timeout.
    ``http``            The API endpoint returned an error.
    ``no_api_key``      No API key was configured or present in the environment.
    ``gemini_disabled`` The Gemini API is disabled for the key's Cloud project.
    ``invalid_response`` The model answer was not the expected JSON object.
    ``cancelled``       The user cancelled the run.
    """

    def __init__(self, code: str, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AIConfig:
    """User-selected AI review settings.

    ``harness`` and ``claude-cli`` use local CLIs and the account they are
    signed in to.  ``api`` and ``anthropic`` read a key from the config or from
    the environment variable each names.
    """

    mode: str = "off"
    harness_command: str = ""
    harness_effort: str = DEFAULT_HARNESS_EFFORT
    claude_command: str = ""
    claude_model: str = ""
    api_base_url: str = DEFAULT_API_BASE
    api_model: str = DEFAULT_API_MODEL
    api_key_env: str = DEFAULT_API_KEY_ENV
    api_key: str = ""
    gemini_api_base: str = DEFAULT_GEMINI_BASE
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_api_key_env: str = DEFAULT_GEMINI_KEY_ENV
    gemini_api_key: str = ""
    anthropic_api_base: str = DEFAULT_ANTHROPIC_BASE
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    anthropic_api_key_env: str = DEFAULT_ANTHROPIC_KEY_ENV
    anthropic_api_key: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT
    max_authorities: int = DEFAULT_MAX_AUTHORITIES
    authority_chars: int = DEFAULT_AUTHORITY_CHARS

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "AIConfig":
        data = data if isinstance(data, dict) else {}
        mode = str(data.get("mode", "off")).strip().lower()
        if mode not in MODES:
            mode = "off"
        effort = str(
            data.get("harness_effort", DEFAULT_HARNESS_EFFORT)
        ).strip().lower()
        if effort not in EFFORT_LEVELS:
            effort = DEFAULT_HARNESS_EFFORT
        return cls(
            mode=mode,
            harness_command=str(data.get("harness_command", "")).strip(),
            harness_effort=effort,
            claude_command=str(data.get("claude_command", "")).strip(),
            claude_model=str(data.get("claude_model", "")).strip(),
            api_base_url=str(data.get("api_base_url", DEFAULT_API_BASE)).strip()
            or DEFAULT_API_BASE,
            api_model=str(data.get("api_model", DEFAULT_API_MODEL)).strip()
            or DEFAULT_API_MODEL,
            api_key_env=str(data.get("api_key_env", DEFAULT_API_KEY_ENV)).strip()
            or DEFAULT_API_KEY_ENV,
            api_key=str(data.get("api_key", "")),
            gemini_api_base=str(
                data.get("gemini_api_base", DEFAULT_GEMINI_BASE)
            ).strip()
            or DEFAULT_GEMINI_BASE,
            gemini_model=str(
                data.get("gemini_model", DEFAULT_GEMINI_MODEL)
            ).strip()
            or DEFAULT_GEMINI_MODEL,
            gemini_api_key_env=str(
                data.get("gemini_api_key_env", DEFAULT_GEMINI_KEY_ENV)
            ).strip()
            or DEFAULT_GEMINI_KEY_ENV,
            gemini_api_key=str(data.get("gemini_api_key", "")),
            anthropic_api_base=str(
                data.get("anthropic_api_base", DEFAULT_ANTHROPIC_BASE)
            ).strip()
            or DEFAULT_ANTHROPIC_BASE,
            anthropic_model=str(
                data.get("anthropic_model", DEFAULT_ANTHROPIC_MODEL)
            ).strip()
            or DEFAULT_ANTHROPIC_MODEL,
            anthropic_api_key_env=str(
                data.get("anthropic_api_key_env", DEFAULT_ANTHROPIC_KEY_ENV)
            ).strip()
            or DEFAULT_ANTHROPIC_KEY_ENV,
            anthropic_api_key=str(data.get("anthropic_api_key", "")),
            timeout_seconds=_clamp_int(
                data.get("timeout_seconds"), DEFAULT_TIMEOUT, 30, 900
            ),
            max_authorities=_clamp_int(
                data.get("max_authorities"), DEFAULT_MAX_AUTHORITIES, 4, 24
            ),
            authority_chars=_clamp_int(
                data.get("authority_chars"), DEFAULT_AUTHORITY_CHARS, 600, 8000
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "harness_command": self.harness_command,
            "harness_effort": self.harness_effort,
            "claude_command": self.claude_command,
            "claude_model": self.claude_model,
            "api_base_url": self.api_base_url,
            "api_model": self.api_model,
            "api_key_env": self.api_key_env,
            "api_key": self.api_key,
            "gemini_api_base": self.gemini_api_base,
            "gemini_model": self.gemini_model,
            "gemini_api_key_env": self.gemini_api_key_env,
            "gemini_api_key": self.gemini_api_key,
            "anthropic_api_base": self.anthropic_api_base,
            "anthropic_model": self.anthropic_model,
            "anthropic_api_key_env": self.anthropic_api_key_env,
            "anthropic_api_key": self.anthropic_api_key,
            "timeout_seconds": self.timeout_seconds,
            "max_authorities": self.max_authorities,
            "authority_chars": self.authority_chars,
        }

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode, MODE_LABELS["off"])

    @property
    def api_key_value(self) -> str:
        return self.api_key or os.environ.get(self.api_key_env, "")

    @property
    def anthropic_key_value(self) -> str:
        return self.anthropic_api_key or os.environ.get(
            self.anthropic_api_key_env, ""
        )

    @property
    def gemini_key_value(self) -> str:
        return self.gemini_api_key or os.environ.get(self.gemini_api_key_env, "")


def default_config() -> AIConfig:
    """First-run default: prefer the free Gemini key, then a local account."""
    if os.environ.get(DEFAULT_GEMINI_KEY_ENV):
        return AIConfig(mode="gemini")
    if account_auth_available():
        return AIConfig(mode="harness", harness_effort=DEFAULT_HARNESS_EFFORT)
    command = find_claude_command("")
    if command:
        return AIConfig(
            mode="claude-cli",
            claude_command=command,
            harness_effort=DEFAULT_HARNESS_EFFORT,
        )
    return AIConfig()


def _clamp_int(value: object, default: int, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def config_from_settings(settings: dict[str, Any] | None) -> AIConfig:
    """Read the ``ai_review`` block from an app settings mapping."""
    if not isinstance(settings, dict):
        return AIConfig()
    block = settings.get("ai_review")
    return AIConfig.from_mapping(block if isinstance(block, dict) else {})


def config_to_settings(config: AIConfig) -> dict[str, Any]:
    return config.to_mapping()


# ---------------------------------------------------------------------------
# Deterministic pre-check signals
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Signal:
    """One conservative, explainable observation about the comparison."""

    kind: str
    side: str  # "A", "B", or "both"
    label: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "side": self.side,
            "label": self.label,
            "detail": self.detail,
        }


_SIGNAL_PRIORITY = {
    "no_overlap": 0,
    "no_issues": 1,
    "thin_scenario": 2,
    "missing_fact": 3,
    "unique_issue": 4,
    "unique_authority": 5,
    "citation_not_retrieved": 6,
    "concept_not_covered": 7,
    "deadline_one_side": 8,
    "penalty_one_side": 9,
}


def _side_deadlines(scenario: ScenarioReport, limit: int = 4) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for match in scenario.matches[:8]:
        for sentence in extract_deadlines(match.section.body, limit=2):
            rows.append((match.section.citation_short, sentence))
            if len(rows) >= limit:
                return rows
    return rows


def _side_penalties(
    scenario: ScenarioReport, limit: int = 5
) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for match in scenario.matches[:8]:
        for category, sentence in extract_penalties(match.section.body, limit=2):
            rows.append((category, match.section.citation_short, sentence))
            if len(rows) >= limit:
                return rows
    return rows


def missing_facts(text: str, concept_keys: Iterable[str]) -> list[str]:
    """Return fact gaps that commonly change the analysis.

    These are prompts for a human, not findings.  A scenario can omit a fact
    because it is irrelevant; the model is asked to judge that.
    """
    keys = set(concept_keys)
    facts: list[str] = []
    if not DATE_RE.search(text):
        facts.append(
            "No date, deadline, or timing fact — any deadline conclusion is speculative."
        )
    if not WRITING_RE.search(text):
        facts.append(
            "No writing, advertisement, or communication is described — "
            "notice and record requirements cannot be checked."
        )
    if keys & {"trust_funds", "commissions"} and not MONEY_RE.search(text):
        facts.append(
            "The scenario raises money or compensation but does not say what "
            "was paid, who received it, or how it was held."
        )
    if keys & {"licensing", "broker_supervision"} and not LICENSE_RE.search(text):
        facts.append(
            "The scenario does not say whether the person was licensed, "
            "sponsored, exempt, or inactive at the time."
        )
    if not PARTY_RE.search(text):
        facts.append(
            "No parties are identified — who acted for whom is unclear."
        )
    return facts


def build_signals(report: ComparisonReport) -> list[Signal]:
    """Build the deterministic notes that precede (and inform) the AI review."""
    signals: list[Signal] = []
    a_keys = {key for key, _label, _strength in report.a.concepts}
    b_keys = {key for key, _label, _strength in report.b.concepts}

    if report.a.matches and report.b.matches and not report.shared:
        signals.append(
            Signal(
                "no_overlap",
                "both",
                "No shared authority in the top matches",
                "The two scenarios retrieved different authorities. Check the "
                "unique lists before assuming the same rule governs both.",
            )
        )

    for letter, scenario in (("A", report.a), ("B", report.b)):
        if not scenario.matches and not scenario.concepts:
            signals.append(
                Signal(
                    "no_issues",
                    letter,
                    f"Scenario {letter} produced no recognized issue",
                    "Add concrete facts: who did what, when, what was written "
                    "or advertised, and whether money or a license was involved.",
                )
            )
        elif scenario.word_count < 20:
            signals.append(
                Signal(
                    "thin_scenario",
                    letter,
                    f"Scenario {letter} is very short ({scenario.word_count} words)",
                    "A short fact pattern usually omits the fact that decides "
                    "which rule applies. Treat differences cautiously.",
                )
            )

    for letter, labels, keys in (
        ("A", report.only_a_concepts, a_keys - b_keys),
        ("B", report.only_b_concepts, b_keys - a_keys),
    ):
        for _key, label in labels[:2]:
            signals.append(
                Signal(
                    "unique_issue",
                    letter,
                    f"Only scenario {letter} raises: {label}",
                    f"The other scenario does not raise {label.lower()}. Decide "
                    "whether that is a real difference or an omitted fact.",
                )
            )

    for letter, rows in (("A", report.only_a), ("B", report.only_b)):
        for row in rows[:2]:
            signals.append(
                Signal(
                    "unique_authority",
                    letter,
                    f"{row.section.citation_short} retrieved only for {letter}",
                    f"{row.section.title} matched scenario {letter} but not the "
                    "other scenario.",
                )
            )

    for letter, scenario in (("A", report.a), ("B", report.b)):
        citation = find_citation(scenario.text)
        if not citation:
            continue
        found = any(
            match.section.section == citation
            for match in report.a.matches + report.b.matches
        )
        if not found:
            signals.append(
                Signal(
                    "citation_not_retrieved",
                    letter,
                    f"Scenario {letter} cites §{citation}",
                    "That section is not among the retrieved authorities. "
                    "Confirm it applies and read its current text separately.",
                )
            )

    # A concept that fires in the scenario but appears in no retrieved section
    # can mean the library does not cover it, or that retrieval missed it.
    for letter, scenario in (("A", report.a), ("B", report.b)):
        for key, label, _strength in scenario.concepts[:3]:
            covered = any(label in match.concepts for match in scenario.matches)
            if not covered:
                signals.append(
                    Signal(
                        "concept_not_covered",
                        letter,
                        f"{label} was recognized but not retrieved",
                        "No top authority carries this issue label. Check the "
                        "search tab with the same facts before relying on the brief.",
                    )
                )

    a_deadlines = _side_deadlines(report.a)
    b_deadlines = _side_deadlines(report.b)
    if bool(a_deadlines) != bool(b_deadlines):
        side = "A" if a_deadlines else "B"
        other = "B" if side == "A" else "A"
        signals.append(
            Signal(
                "deadline_one_side",
                side,
                f"Deadline language appears only for scenario {side}",
                f"None of the authorities retrieved for scenario {other} carry "
                "a deadline-style sentence. That may be the decisive difference.",
            )
        )

    a_categories = {category for category, _citation, _sentence in _side_penalties(report.a)}
    b_categories = {category for category, _citation, _sentence in _side_penalties(report.b)}
    for category in sorted(a_categories - b_categories):
        signals.append(
            Signal(
                "penalty_one_side",
                "A",
                f"{category} consequence language appears only for A",
                "Read the cited section to see whether the consequence is "
                "available on these facts.",
            )
        )
    for category in sorted(b_categories - a_categories):
        signals.append(
            Signal(
                "penalty_one_side",
                "B",
                f"{category} consequence language appears only for B",
                "Read the cited section to see whether the consequence is "
                "available on these facts.",
            )
        )

    for letter, scenario, keys in (
        ("A", report.a, a_keys),
        ("B", report.b, b_keys),
    ):
        for fact in missing_facts(scenario.text, keys)[:3]:
            signals.append(Signal("missing_fact", letter, f"Fact gap in {letter}", fact))

    deduped: list[Signal] = []
    seen: set[tuple[str, str, str]] = set()
    for signal in signals:
        key = (signal.kind, signal.side, signal.label)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(signal)
    deduped.sort(key=lambda item: (_SIGNAL_PRIORITY.get(item.kind, 99), item.side))
    return deduped[:18]


# ---------------------------------------------------------------------------
# Packet
# ---------------------------------------------------------------------------

@dataclass
class ComparisonPacket:
    """The exact material sent to the model, plus the prompt built from it."""

    payload: dict[str, Any]
    prompt: str

    def to_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, indent=2)

    @property
    def fingerprint(self) -> str:
        import hashlib

        data = json.dumps(self.payload, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(data).hexdigest()


def _short(value: str, limit: int, *, collapse: bool = True) -> str:
    if collapse:
        value = _WORDCUT_RE.sub(" ", value).strip()
    else:
        value = value.strip()
    if len(value) <= limit:
        return value
    cut = value[: limit - 1].rsplit(" ", 1)[0]
    return (cut or value[: limit - 1]) + "…"


def _authority_entry(
    row, side: str, index: int, body_chars: int
) -> dict[str, Any]:
    section = row.section
    body = section.body.strip()
    truncated = len(body) > body_chars
    return {
        "id": f"A{index}",
        "side": side,
        "citation": section.citation,
        "citation_short": section.citation_short,
        "section": section.section,
        "title": section.title,
        "kind": section.kind,
        "url": section.url,
        "body": _short(body, body_chars, collapse=False) if truncated else body,
        "truncated": truncated,
    }


def _select_rows(
    report: ComparisonReport, max_authorities: int
) -> list[tuple[Any, str]]:
    """Shared first, then a balanced A/B sample, with no duplicates."""
    selected: list[tuple[Any, str]] = []
    seen: set[str] = set()

    def add(rows: list[Any], side: str, cap: int) -> None:
        added = 0
        for row in rows:
            if added >= cap or len(selected) >= max_authorities:
                return
            uid = row.section.uid
            if uid in seen:
                continue
            seen.add(uid)
            selected.append((row, side))
            added += 1

    shared_cap = min(len(report.shared), max(1, max_authorities // 2))
    add(report.shared, "both", shared_cap)
    remaining = max_authorities - len(selected)
    a_cap = min(len(report.only_a), (remaining + 1) // 2)
    b_cap = min(len(report.only_b), remaining // 2)
    add(report.only_a, "A", a_cap)
    add(report.only_b, "B", b_cap)

    # Top up if one side was short.
    for rows, side in ((report.only_a, "A"), (report.only_b, "B"), (report.shared, "both")):
        if len(selected) >= max_authorities:
            break
        add(rows, side, max_authorities - len(selected))
    return selected


def build_packet(
    report: ComparisonReport,
    config: AIConfig | None = None,
    *,
    library_meta: dict[str, Any] | None = None,
) -> ComparisonPacket:
    config = config or AIConfig()
    rows = _select_rows(report, config.max_authorities)
    authorities = [
        _authority_entry(row, side, index, config.authority_chars)
        for index, (row, side) in enumerate(rows, start=1)
    ]
    signals = build_signals(report)

    def scenario_payload(scenario: ScenarioReport) -> dict[str, Any]:
        return {
            "text": scenario.text,
            "words": scenario.word_count,
            "concepts": [label for _key, label, _strength in scenario.concepts],
            "top_matches": [
                {
                    "citation_short": match.section.citation_short,
                    "title": match.section.title,
                    "score": match.score,
                }
                for match in scenario.matches[:8]
            ],
        }

    meta = library_meta or {}
    payload: dict[str, Any] = {
        "schema": SCHEMA_NAME,
        "version": SCHEMA_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "library": {
            "snapshot": meta.get("generated_at_display", "current"),
            "sections": meta.get("section_count", 0)
            or sum(int(value) for value in meta.get("counts", {}).values()),
        },
        "scenario_a": scenario_payload(report.a),
        "scenario_b": scenario_payload(report.b),
        "shared_issues": [label for _key, label in report.shared_concepts],
        "only_a_issues": [label for _key, label in report.only_a_concepts],
        "only_b_issues": [label for _key, label in report.only_b_concepts],
        "authorities": authorities,
        "retrieved_citations": sorted(
            {
                row.section.citation_short
                for row in report.shared + report.only_a + report.only_b
            }
        ),
        "signals": [signal.to_dict() for signal in signals],
        "limits": {
            "max_authorities": config.max_authorities,
            "authority_chars": config.authority_chars,
        },
    }
    return ComparisonPacket(payload=payload, prompt=build_prompt(payload))


PROMPT_RULES = """\
You are a careful Texas real estate compliance analyst. You are reviewing two
fact patterns for a research aid. You are not a lawyer and you do not give
legal advice; your job is to flag risk and say what a person must verify.

Compare scenario A and scenario B using ONLY the AUTHORITIES below and the
facts the scenarios state. Do not use tools or inspect the filesystem; the
packet is complete. Do not use outside law, do not assume facts that
are not stated, and do not invent citations.

Return ONE JSON object and nothing else. No markdown fences, no prose before
or after the object. Use exactly this shape:

{
  "verdict": "2-4 sentences comparing the two scenarios and the main risk difference",
  "risk_level": "high | medium | low | unclear",
  "confidence": "high | medium | low",
  "red_flags": [
    {
      "title": "short label",
      "severity": "high | medium | low",
      "scenario": "A | B | both",
      "why_it_matters": "1-2 sentences tied to specific facts and an authority",
      "citations": ["§535.154"],
      "what_to_check": "the concrete fact, document, or deadline a person must confirm"
    }
  ],
  "key_differences": ["the fact or issue that most changes the analysis"],
  "missing_facts": ["a fact that would change the answer if known"],
  "retrieval_notes": ["whether a needed authority seems absent or only tangential"]
}

Rules:
- Give 3-7 red_flags, ordered most serious first. If the facts show no real
  risk, return an empty list and explain why in the verdict.
- severity high = a likely prohibition, license consequence, penalty, or failed
  disclosure on these facts; medium = material compliance ambiguity or missing
  safeguard; low = documentation, recordkeeping, or process risk.
- citations must be exact citation_short values copied from AUTHORITIES. If the
  authority you want is not listed, put that in retrieval_notes instead of
  inventing a citation.
- Prefer the specific section over a general one. Quote the citation, not the
  whole section title.
- missing_facts is only for facts that would change the analysis, not a general
  wish list.
- Do not decide that a license should be suspended, a penalty applies, or a
  contract is void. Describe the risk and what to verify.
"""


def build_prompt(payload: dict[str, Any]) -> str:
    """Render a self-contained prompt from a packet payload."""
    parts: list[str] = [PROMPT_RULES]
    limits = payload.get("limits", {})
    parts.append(
        "\nThe packet was assembled from a local TREC rules/statutes library"
        f" ({payload.get('library', {}).get('snapshot', 'current')} snapshot)."
        f" At most {limits.get('max_authorities', DEFAULT_MAX_AUTHORITIES)}"
        " authorities are included; body text may be truncated.\n"
    )

    for letter in ("A", "B"):
        scenario = payload.get(f"scenario_{letter.lower()}", {})
        parts.append(f"\n=== SCENARIO {letter} ===\n{scenario.get('text', '').strip()}\n")
        if scenario.get("concepts"):
            parts.append("Recognized issues: " + ", ".join(scenario["concepts"]) + "\n")

    shared = payload.get("shared_issues") or []
    only_a = payload.get("only_a_issues") or []
    only_b = payload.get("only_b_issues") or []
    if shared or only_a or only_b:
        parts.append("\n=== ISSUE OVERLAP ===\n")
        if shared:
            parts.append("Shared: " + ", ".join(shared) + "\n")
        if only_a:
            parts.append("A only: " + ", ".join(only_a) + "\n")
        if only_b:
            parts.append("B only: " + ", ".join(only_b) + "\n")

    signals = payload.get("signals") or []
    if signals:
        parts.append("\n=== DETERMINISTIC PRE-CHECK (may be noisy; verify) ===\n")
        for signal in signals:
            parts.append(
                f"- [{signal.get('kind')}/{signal.get('side')}] "
                f"{signal.get('label')}: {signal.get('detail')}\n"
            )

    parts.append("\n=== AUTHORITIES ===\n")
    for authority in payload.get("authorities", []):
        parts.append(
            f"\n[{authority.get('id')}] {authority.get('citation_short')} — "
            f"{authority.get('title')}\n"
            f"side: {authority.get('side')} | type: {authority.get('kind')}\n"
            f"text:\n{authority.get('body', '').strip()}\n"
        )
    parts.append(
        "\n=== OUTPUT ===\n"
        "Return the JSON object now. Use only the citations listed above.\n"
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Analysis model and parsing
# ---------------------------------------------------------------------------

@dataclass
class RedFlag:
    title: str
    severity: str
    scenario: str
    why_it_matters: str
    citations: tuple[str, ...] = ()
    what_to_check: str = ""


@dataclass
class AIAnalysis:
    verdict: str
    risk_level: str
    red_flags: list[RedFlag]
    key_differences: list[str] = field(default_factory=list)
    missing_facts: list[str] = field(default_factory=list)
    retrieval_notes: list[str] = field(default_factory=list)
    citation_warnings: list[str] = field(default_factory=list)
    confidence: str = "low"
    model: str = ""
    mode: str = ""
    created_at: str = ""
    raw_response: str = ""

    @property
    def high_count(self) -> int:
        return sum(1 for flag in self.red_flags if flag.severity == "high")

    def to_markdown(self) -> str:
        lines: list[str] = [
            "## AI review — second opinion",
            "",
            f"**Risk level:** {self.risk_level.upper()}  ·  "
            f"**Confidence:** {self.confidence}  ·  "
            f"**Model:** {self.model or 'unknown'}  ·  "
            f"**Mode:** {MODE_LABELS.get(self.mode, self.mode or 'manual')}",
            "",
            f"> {self.verdict}",
            "",
        ]
        if self.red_flags:
            lines.append(f"### Red flags ({len(self.red_flags)})")
            lines.append("")
            for flag in self.red_flags:
                side = {"A": "Scenario A", "B": "Scenario B"}.get(
                    flag.scenario, "Both scenarios"
                )
                lines.append(f"#### {flag.severity.upper()} — {flag.title}")
                lines.append(f"*{side}*")
                lines.append("")
                if flag.why_it_matters:
                    lines.append(flag.why_it_matters)
                    lines.append("")
                if flag.citations:
                    lines.append("Citations: " + ", ".join(flag.citations))
                if flag.what_to_check:
                    lines.append(f"Check: {flag.what_to_check}")
                lines.append("")
        else:
            lines.extend(["### Red flags", "", "None identified from these facts.", ""])

        for heading, values in (
            ("Key differences", self.key_differences),
            ("Facts that would change the analysis", self.missing_facts),
            ("Retrieval notes", self.retrieval_notes),
            ("Citation check", self.citation_warnings),
        ):
            if not values:
                continue
            lines.append(f"### {heading}")
            lines.append("")
            for value in values:
                lines.append(f"- {value}")
            lines.append("")

        lines.extend(
            [
                "---",
                "",
                "*AI-generated second opinion. It can be wrong, can miss a "
                "relevant authority, and is not legal advice. Verify every "
                "citation against the official text.*",
                "",
            ]
        )
        return "\n".join(lines)

    def to_plain(self) -> str:
        return re.sub(r"[#>*`]", "", self.to_markdown())


def _extract_json_object(text: str) -> dict[str, Any]:
    """Find and decode the first balanced JSON object in ``text``."""
    text = text.strip()
    candidates: list[str] = []
    candidates.extend(match.group(1).strip() for match in _JSON_FENCE_RE.finditer(text))
    candidates.append(text)

    for candidate in candidates:
        start = candidate.find("{")
        while start != -1:
            depth = 0
            in_string = False
            escaped = False
            for index in range(start, len(candidate)):
                char = candidate[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        blob = candidate[start : index + 1]
                        try:
                            value = json.loads(blob)
                        except json.JSONDecodeError:
                            break
                        if isinstance(value, dict):
                            return value
                        break
            start = candidate.find("{", start + 1)
    raise AIReviewError(
        "invalid_response",
        "The AI answer did not contain a valid JSON object.",
        detail=text[:600],
    )


def _as_text(value: Any, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _short(value, limit)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "; ".join(_as_text(item, limit) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_as_text(item, limit)}" for key, item in value.items())
    return str(value)


def _as_text_list(value: Any, limit: int = 40) -> list[str]:
    if isinstance(value, str):
        rows = [value] if value.strip() else []
    elif isinstance(value, list):
        rows = [_as_text(item, 600) for item in value]
    else:
        rows = []
    return [row for row in (_short(item, 600) for item in rows) if row][:limit]


def _normalize_severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in SEVERITIES else "medium"


def _normalize_side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("a", "scenario a"):
        return "A"
    if text in ("b", "scenario b"):
        return "B"
    return "both"


def _normalize_risk(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in RISK_LEVELS else "unclear"


def _normalize_confidence(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in CONFIDENCE_LEVELS else "low"


def _citation_number(value: str) -> str:
    match = _CITATION_NUMBER_RE.search(value.replace("§", " "))
    return match.group(1) if match else value.strip()


def _citation_index(payload: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for authority in payload.get("authorities", []):
        short = str(authority.get("citation_short", "")).strip()
        number = _citation_number(short or str(authority.get("section", "")))
        if number:
            index[number] = short
        full = _citation_number(str(authority.get("citation", "")))
        if full:
            index.setdefault(full, short)
    return index


def _retrieved_index(payload: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for citation in payload.get("retrieved_citations", []):
        short = str(citation).strip()
        number = _citation_number(short)
        if number:
            index[number] = short
    return index


def parse_analysis(
    text: str,
    packet: ComparisonPacket | dict[str, Any],
    *,
    model: str = "",
    mode: str = "manual",
) -> AIAnalysis:
    """Validate a model answer and return a review.

    Citations that do not match a packet authority are kept on the flag but
    reported in ``citation_warnings`` so the UI can mark them.
    """
    payload = packet.payload if isinstance(packet, ComparisonPacket) else packet
    data = _extract_json_object(text)

    verdict = _as_text(data.get("verdict"), 1600).strip()
    if not verdict:
        raise AIReviewError(
            "invalid_response",
            "The AI answer has no verdict.",
            detail=text[:600],
        )

    raw_flags = data.get("red_flags")
    if raw_flags is None:
        raw_flags = []
    if isinstance(raw_flags, dict):
        raw_flags = [raw_flags]
    if not isinstance(raw_flags, list):
        raise AIReviewError(
            "invalid_response",
            "The AI answer's red_flags field is not a list.",
            detail=text[:600],
        )

    flags: list[RedFlag] = []
    for item in raw_flags[:12]:
        if not isinstance(item, dict):
            continue
        title = _as_text(item.get("title"), 160).strip()
        if not title:
            continue
        raw_citations = item.get("citations")
        if isinstance(raw_citations, str):
            raw_citations = [raw_citations]
        if not isinstance(raw_citations, list):
            raw_citations = []
        citations = [
            text
            for text in (_as_text(value, 80).strip() for value in raw_citations)
            if text
        ][:6]
        flags.append(
            RedFlag(
                title=title,
                severity=_normalize_severity(item.get("severity")),
                scenario=_normalize_side(item.get("scenario")),
                why_it_matters=_as_text(item.get("why_it_matters"), 900).strip(),
                citations=tuple(citations),
                what_to_check=_as_text(item.get("what_to_check"), 600).strip(),
            )
        )

    flags.sort(key=lambda flag: SEVERITIES.index(flag.severity))

    index = _citation_index(payload)
    retrieved = _retrieved_index(payload)
    warnings: list[str] = []
    checked: list[RedFlag] = []
    for flag in flags:
        valid: list[str] = []
        for citation in flag.citations:
            number = _citation_number(citation)
            if number in index:
                resolved = index[number]
                if resolved not in valid:
                    valid.append(resolved)
            elif number in retrieved:
                resolved = retrieved[number]
                if resolved not in valid:
                    valid.append(resolved)
                warning = (
                    f"{resolved} (in “{flag.title}”) was retrieved for one of "
                    "the scenarios, but its full text was not included in this "
                    "packet. Read it before relying on the point."
                )
                if warning not in warnings:
                    warnings.append(warning)
            else:
                warning = (
                    f"{citation} (in “{flag.title}”) is not one of the "
                    "authorities in this comparison."
                )
                if warning not in warnings:
                    warnings.append(warning)
        checked.append(
            RedFlag(
                title=flag.title,
                severity=flag.severity,
                scenario=flag.scenario,
                why_it_matters=flag.why_it_matters,
                citations=tuple(valid),
                what_to_check=flag.what_to_check,
            )
        )

    return AIAnalysis(
        verdict=verdict,
        risk_level=_normalize_risk(data.get("risk_level")),
        red_flags=checked,
        key_differences=_as_text_list(data.get("key_differences")),
        missing_facts=_as_text_list(data.get("missing_facts")),
        retrieval_notes=_as_text_list(data.get("retrieval_notes")),
        citation_warnings=warnings,
        confidence=_normalize_confidence(data.get("confidence")),
        model=model,
        mode=mode,
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        raw_response=text,
    )


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

def find_harness_command(explicit: str = "") -> str | None:
    """Locate ``dsh`` (or ``dsh.cmd``) without starting it."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env_value = os.environ.get("TREC_DSH_COMMAND", "")
    if env_value:
        candidates.append(env_value)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(
            str(
                Path(local)
                / "Programs"
                / "DeepSeek Harness"
                / "resources"
                / "runtime"
                / "cli"
                / "bin"
                / "dsh.cmd"
            )
        )
    found = shutil.which("dsh")
    if found:
        candidates.append(found)
    for candidate in candidates:
        try:
            path = Path(candidate).expanduser()
        except (OSError, ValueError):
            continue
        if path.is_file():
            return str(path)
    return None


def _dsh_home() -> Path:
    explicit = os.environ.get("DSH_HOME")
    return Path(explicit).expanduser() if explicit else Path.home() / ".dsh"


def account_auth_available() -> bool:
    """Whether DSH has a signed-in DeepSeek account to reuse.

    The headless profile disables the managed credential store by default.  A
    locally signed-in account can be reused through the documented profile
    patch, which keeps the app key-free for people who use the harness GUI.
    """
    credentials = _dsh_home() / ".credentials.yaml"
    try:
        text = credentials.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "deepseek-account-platform" in text


def headless_account_patch(effort: str = DEFAULT_HARNESS_EFFORT) -> Path | None:
    """Write (once) the overlay that lets headless use the saved account.

    This mirrors the DeepSeek Harness documentation: re-enable the credential
    store, authorization, and the account service, then point the default model
    at the account route.  The file lives in the app-data folder, not in the
    user's DSH profile.
    """
    if not account_auth_available():
        return None
    if effort not in EFFORT_LEVELS:
        effort = DEFAULT_HARNESS_EFFORT
    path = storage.app_data_dir() / "ai_headless_account.patch.yml"
    content = (
        "# Generated by TREC Rulebook for optional AI review.\n"
        "# Lets the one-shot headless agent use the DeepSeek Harness sign-in.\n"
        "- id: credentials\n"
        "  disabled: false\n"
        "- id: authorization\n"
        "  disabled: false\n"
        "- id: deepseek-account\n"
        "  disabled: false\n"
        "- id: agent-default-model\n"
        "  config:\n"
        "    provider: deepseek-account\n"
        "    model: deepseek-flash\n"
        f"    reasoningEffort: {effort}\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
        return path
    except OSError:
        return None


def harness_auth_available() -> bool:
    """Best-effort check for a usable harness credential.

    The headless profile can use a signed-in account (through the generated
    overlay) or an API key from the environment or a harness ``.env``.  This is
    only a hint for the UI; the run itself is the authority.
    """
    if account_auth_available():
        return True
    if os.environ.get("DEEPSEEK_API_KEY"):
        return True
    for env_file in (Path.cwd() / ".env", _dsh_home() / ".env"):
        try:
            if "DEEPSEEK_API_KEY" in env_file.read_text(
                encoding="utf-8", errors="replace"
            ):
                return True
        except OSError:
            continue
    return False


def _with_patch(argv: list[str], patch: Path) -> list[str]:
    """Insert --patch before the app arguments for either launcher shape."""
    if len(argv) > 1 and argv[1] == "--expose-internals":
        index = 3
    elif len(argv) > 2 and Path(argv[0]).name.lower() == "cmd.exe":
        index = 3
    else:
        index = 1
    return argv[:index] + ["--patch", str(patch)] + argv[index:]


def _resolve_harness_argv(command: str) -> tuple[list[str], dict[str, str]]:
    """Return the command and extra environment for one harness run.

    A ``dsh.cmd`` launcher normally starts the Electron binary with
    ``ELECTRON_RUN_AS_NODE=1``.  Calling that binary directly avoids a cmd.exe
    wrapper, so cancelling kills the actual model process instead of leaving a
    child holding the output pipe.
    """
    args = ["--profile", "headless", "--approval-policy", "never", "-"]
    path = Path(command)
    if path.suffix.lower() in (".cmd", ".bat"):
        try:
            root = path.resolve().parents[4]
        except IndexError:
            root = None
        if root is not None:
            exe = root / "DeepSeek Harness.exe"
            asar = root / "resources" / "app.asar"
            cli = (
                asar
                / "dsh"
                / "node_modules"
                / "@deepseek-ai"
                / "dsh-desktop-host"
                / "lib"
                / "cli.js"
            )
            # The cli lives inside app.asar, which Python sees as one archive
            # file; Electron resolves the inner path itself.
            if exe.is_file() and asar.exists():
                return (
                    [str(exe), "--expose-internals", str(cli), *args],
                    {"ELECTRON_RUN_AS_NODE": "1"},
                )
        return ["cmd.exe", "/c", command, *args], {}
    return [command, *args], {}


def _kill_tree(process: subprocess.Popen) -> None:
    """Stop a child and, on Windows, every process it started.

    The dsh launcher is a batch file that starts an Electron/Node process; killing
    only cmd.exe leaves the model call running and its stdout pipe open.
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _run_process(
    argv: list[str],
    stdin_text: str,
    *,
    timeout: int,
    cancel: threading.Event | None,
    env_extra: dict[str, str] | None = None,
    label: str = "AI",
    failure_code: str = "harness_failed",
    missing_code: str = "no_harness",
) -> tuple[str, str, int]:
    """Run one child process, feeding ``stdin_text`` and returning its output.

    stderr is redirected to a temporary file so model reasoning cannot fill a
    pipe and deadlock the child.
    """
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    env.setdefault("NO_COLOR", "1")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    stderr_file = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            env=env,
            creationflags=flags,
        )
    except FileNotFoundError as exc:
        stderr_file.close()
        raise AIReviewError(missing_code, f"Could not start {argv[0]}.", detail=str(exc))
    except OSError as exc:
        stderr_file.close()
        raise AIReviewError(failure_code, f"Could not start {argv[0]}.", detail=str(exc))

    stopped = {"cancel": False}

    def watch() -> None:
        while process.poll() is None:
            if cancel is not None and cancel.is_set():
                stopped["cancel"] = True
                _kill_tree(process)
                return
            time.sleep(0.2)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        stdout, _ = process.communicate(
            input=stdin_text.encode("utf-8"), timeout=timeout
        )
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise AIReviewError(
            "harness_timeout",
            f"The AI run exceeded {timeout} seconds and was stopped.",
        )
    finally:
        stderr_file.seek(0)
        stderr = stderr_file.read().decode("utf-8", "replace")
        stderr_file.close()

    text = stdout.decode("utf-8", "replace").strip()
    if stopped["cancel"]:
        raise AIReviewError("cancelled", "The AI review was cancelled.")
    if process.returncode != 0:
        detail = stderr.strip()
        detail = detail[-1600:] if detail else f"exit code {process.returncode}"
        raise AIReviewError(
            failure_code,
            f"The {label} run did not finish successfully.",
            detail=detail,
        )
    if not text:
        raise AIReviewError(
            failure_code,
            f"The {label} run returned an empty answer.",
            detail=stderr.strip()[-600:],
        )
    return text, stderr, process.returncode


def run_harness(
    prompt: str,
    config: AIConfig,
    *,
    cancel: threading.Event | None = None,
) -> str:
    command = find_harness_command(config.harness_command)
    if not command:
        raise AIReviewError(
            "no_harness",
            "DeepSeek Harness was not found.",
            detail=(
                "Install DeepSeek Harness or set TREC_DSH_COMMAND to the "
                "dsh.cmd launcher, then try again."
            ),
        )
    argv, env_extra = _resolve_harness_argv(command)
    patch = headless_account_patch(config.harness_effort)
    if patch is not None:
        argv = _with_patch(argv, patch)
    try:
        text, _stderr, _code = _run_process(
            argv,
            prompt,
            timeout=config.timeout_seconds,
            cancel=cancel,
            env_extra=env_extra,
            label="local DeepSeek Harness",
            failure_code="harness_failed",
            missing_code="no_harness",
        )
    except AIReviewError as exc:
        if exc.code == "harness_failed" and (
            "MISSING_CREDENTIAL" in exc.detail
            or "ACCOUNT_SIGN_IN_REQUIRED" in exc.detail
        ):
            exc.detail = (
                exc.detail.rstrip()
                + "\n\nDeepSeek Harness has no usable sign-in for its headless "
                "profile. Sign in to DeepSeek Harness and retry, or set "
                "DEEPSEEK_API_KEY in the environment (or in "
                "%USERPROFILE%\\.dsh\\.env) and retry. The API mode in AI "
                "settings is another option."
            )
        raise
    return text


def find_claude_command(explicit: str = "") -> str | None:
    """Locate the Claude Code CLI without starting it."""
    candidates: list[str] = []
    for value in (
        explicit,
        os.environ.get("CLAUDE_CODE_COMMAND", ""),
        os.environ.get("TREC_CLAUDE_COMMAND", ""),
    ):
        if value:
            candidates.append(value)
    found = shutil.which("claude")
    if found:
        candidates.append(found)
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(str(Path(appdata) / "npm" / "claude.cmd"))
        candidates.append(str(Path(appdata) / "npm" / "claude.ps1"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(str(Path(local) / "Programs" / "claude" / "claude.exe"))
    for candidate in candidates:
        try:
            path = Path(candidate).expanduser()
        except (OSError, ValueError):
            continue
        if path.is_file():
            return str(path)
    return None


def _resolve_cli_argv(command: str, args: list[str]) -> list[str]:
    suffix = Path(command).suffix.lower()
    if suffix in (".cmd", ".bat"):
        return ["cmd.exe", "/c", command, *args]
    if suffix == ".ps1":
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            command,
            *args,
        ]
    return [command, *args]


def _claude_argv(
    command: str, config: AIConfig, *, disable_tools: bool = True
) -> list[str]:
    args = ["-p", "--output-format", "json"]
    if config.claude_model:
        args.extend(["--model", config.claude_model])
    if disable_tools:
        # The packet is self-contained; disable built-in tools so the run is a
        # single model answer rather than an agentic file tour.
        args.extend(["--tools", ""])
    return _resolve_cli_argv(command, args)


def _parse_claude_output(text: str) -> str:
    text = text.strip()
    if not text:
        raise AIReviewError(
            "claude_failed", "Claude Code returned an empty answer."
        )
    candidates = [text]
    candidates.extend(
        line.strip()
        for line in reversed(text.splitlines())
        if line.strip().startswith("{")
    )
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("is_error"):
            raise AIReviewError(
                "claude_failed",
                "Claude Code reported an error.",
                detail=str(data)[:1200],
            )
        for key in ("result", "text", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        content = data.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            joined = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
            if joined:
                return joined
    # Some versions print the answer as plain text.
    return text


def run_claude_cli(
    prompt: str,
    config: AIConfig,
    *,
    cancel: threading.Event | None = None,
) -> str:
    command = find_claude_command(config.claude_command)
    if not command:
        raise AIReviewError(
            "no_claude",
            "Claude Code was not found.",
            detail=(
                "Install Claude Code (npm install -g @anthropic-ai/claude-code), "
                "sign in, or set CLAUDE_CODE_COMMAND to its launcher."
            ),
        )
    try:
        text, _stderr, _code = _run_process(
            _claude_argv(command, config),
            prompt,
            timeout=config.timeout_seconds,
            cancel=cancel,
            label="Claude Code",
            failure_code="claude_failed",
            missing_code="no_claude",
        )
    except AIReviewError as exc:
        detail = (exc.detail or "").lower()
        if exc.code == "claude_failed" and "tools" in detail and (
            "unknown" in detail or "invalid" in detail or "unrecognized" in detail
        ):
            # Older Claude Code builds may not know --tools; retry once without it.
            text, _stderr, _code = _run_process(
                _claude_argv(command, config, disable_tools=False),
                prompt,
                timeout=config.timeout_seconds,
                cancel=cancel,
                label="Claude Code",
                failure_code="claude_failed",
                missing_code="no_claude",
            )
        else:
            raise
    return _parse_claude_output(text)


def _anthropic_url(base: str) -> str:
    base = base.strip().rstrip("/")
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: int,
    label: str,
) -> tuple[dict[str, Any], str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:800]
        except OSError:
            pass
        finally:
            try:
                exc.close()
            except OSError:
                pass
        raise AIReviewError(
            "http",
            f"{label} returned HTTP {exc.code}.",
            detail=detail or exc.reason,
        )
    except urllib.error.URLError as exc:
        raise AIReviewError(
            "http",
            f"Could not reach {label}.",
            detail=str(exc.reason),
        )
    except TimeoutError:
        raise AIReviewError(
            "http",
            f"{label} did not answer within {timeout} seconds.",
        )
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AIReviewError(
            "invalid_response",
            f"{label} returned a non-JSON response.",
            detail=f"{exc}; body starts: {body[:400]}",
        )
    if not isinstance(data, dict):
        raise AIReviewError(
            "invalid_response",
            f"{label} returned an unexpected response shape.",
            detail=body[:400],
        )
    return data, body


def run_anthropic(prompt: str, config: AIConfig) -> str:
    key = config.anthropic_key_value
    if not key:
        raise AIReviewError(
            "no_api_key",
            "No Anthropic API key is configured.",
            detail=(
                "Enter a key in AI settings or set the "
                f"{config.anthropic_api_key_env} environment variable."
            ),
        )
    payload: dict[str, Any] = {
        "model": config.anthropic_model,
        "max_tokens": 4000,
        "temperature": 0.2,
        "system": (
            "You return only a single JSON object. You never add markdown "
            "fences or commentary."
        ),
        "messages": [{"role": "user", "content": prompt}],
    }
    data, body = _post_json(
        _anthropic_url(config.anthropic_api_base),
        payload,
        {
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        timeout=config.timeout_seconds,
        label="Anthropic API",
    )
    if data.get("type") == "error":
        raise AIReviewError(
            "http",
            "Anthropic API returned an error.",
            detail=str(data.get("error", data))[:800],
        )
    content = data.get("content")
    if isinstance(content, list):
        text = "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if text:
            return text
    raise AIReviewError(
        "invalid_response",
        "Anthropic API returned no text content.",
        detail=body[:400],
    )


def _gemini_url(base: str, model: str) -> str:
    base = base.strip().rstrip("/")
    if base.endswith("/models"):
        base = base[: -len("/models")]
    model = model.strip()
    if model.startswith("models/"):
        model = model[len("models/") :]
    return f"{base}/models/{model}:generateContent"


def run_gemini(prompt: str, config: AIConfig) -> str:
    key = config.gemini_key_value
    if not key:
        raise AIReviewError(
            "no_api_key",
            "No Gemini API key is configured.",
            detail=(
                "Enter a key in AI settings or set the "
                f"{config.gemini_api_key_env} environment variable. "
                "A free AI Studio key is available at https://aistudio.google.com/apikey."
            ),
        )
    payload: dict[str, Any] = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "You return only a single JSON object. You never add "
                        "markdown fences or commentary."
                    )
                }
            ]
        },
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4000,
            "responseMimeType": "application/json",
        },
    }
    try:
        data, body = _post_json(
            _gemini_url(config.gemini_api_base, config.gemini_model),
            payload,
            {
                "Content-Type": "application/json",
                "x-goog-api-key": key,
            },
            timeout=config.timeout_seconds,
            label="Gemini API",
        )
    except AIReviewError as exc:
        detail = exc.detail or ""
        lower = detail.lower()
        if "service_disabled" in lower or "has not been used in project" in lower:
            project = re.search(r"project=(\d+)", detail)
            direct = (
                "https://console.developers.google.com/apis/api/"
                "generativelanguage.googleapis.com/overview?project="
                + project.group(1)
                if project
                else "https://console.cloud.google.com/apis/library/"
                "generativelanguage.googleapis.com"
            )
            raise AIReviewError(
                "gemini_disabled",
                "Gemini API is not enabled for this Google Cloud project.",
                detail=(
                    "Enable the Gemini API in the project that owns this key, "
                    "wait a minute, and retry:\n"
                    f"{direct}\n\n"
                    "Or create a free Google AI Studio key at "
                    "https://aistudio.google.com/apikey and paste it in AI "
                    "review settings.\n\n"
                    + detail.rstrip()
                ),
            ) from exc
        if "api_key_invalid" in lower or "api key not valid" in lower:
            raise AIReviewError(
                "no_api_key",
                "Gemini rejected the API key.",
                detail=detail,
            ) from exc
        raise

    feedback = data.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise AIReviewError(
            "invalid_response",
            "Gemini blocked the request.",
            detail=str(feedback)[:800],
        )
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise AIReviewError(
            "invalid_response",
            "Gemini returned no candidate answer.",
            detail=str(data.get("error", body))[:800],
        )
    candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    text = ""
    if isinstance(parts, list):
        text = "".join(
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict)
        ).strip()
    if not text:
        raise AIReviewError(
            "invalid_response",
            "Gemini returned no text content.",
            detail=body[:600],
        )
    return text


def _chat_url(base: str) -> str:
    base = base.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _api_request(
    prompt: str,
    config: AIConfig,
    *,
    json_mode: bool,
) -> tuple[str, dict[str, Any]]:
    key = config.api_key_value
    if not key:
        raise AIReviewError(
            "no_api_key",
            "No API key is configured for AI review.",
            detail=(
                f"Enter a key in AI settings or set the "
                f"{config.api_key_env} environment variable."
            ),
        )
    payload: dict[str, Any] = {
        "model": config.api_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You return only a single JSON object. You never add "
                    "markdown fences or commentary."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    data, body = _post_json(
        _chat_url(config.api_base_url),
        payload,
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        timeout=config.timeout_seconds,
        label="AI endpoint",
    )
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIReviewError(
            "invalid_response",
            "The AI endpoint returned an unexpected response shape.",
            detail=f"{exc}; body starts: {body[:400]}",
        )
    if isinstance(content, list):
        # Some OpenAI-compatible servers return content parts instead of a string.
        content = "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise AIReviewError(
            "invalid_response",
            "The AI endpoint returned an empty answer.",
            detail=body[:400],
        )
    return content, data


def run_api(prompt: str, config: AIConfig) -> str:
    try:
        content, _data = _api_request(prompt, config, json_mode=True)
    except AIReviewError as exc:
        # Some OpenAI-compatible servers reject response_format.  Retry once
        # without it rather than making the user change settings.
        if exc.code == "http" and "response_format" in (exc.detail or "").lower():
            content, _data = _api_request(prompt, config, json_mode=False)
        else:
            raise
    return content


def run_review(
    packet: ComparisonPacket,
    config: AIConfig,
    *,
    cancel: threading.Event | None = None,
    progress: Any = None,
) -> AIAnalysis:
    """Run the configured transport and return a validated review."""
    if config.mode == "off":
        raise AIReviewError(
            "disabled",
            "AI review is off. Turn it on in AI settings, or copy the prompt "
            "and paste a response manually.",
        )
    if config.mode == "gemini":
        if progress:
            progress(f"Calling {config.gemini_model}…")
        text = run_gemini(packet.prompt, config)
        model = config.gemini_model
    elif config.mode == "harness":
        if progress:
            progress(
                "Running DeepSeek Harness headless — this can take a minute…"
            )
        text = run_harness(packet.prompt, config, cancel=cancel)
        model = "DeepSeek Harness"
    elif config.mode == "claude-cli":
        if progress:
            progress("Running Claude Code — this can take a minute…")
        text = run_claude_cli(packet.prompt, config, cancel=cancel)
        model = config.claude_model or "Claude Code"
    elif config.mode == "api":
        if progress:
            progress(f"Calling {config.api_model}…")
        text = run_api(packet.prompt, config)
        model = config.api_model
    elif config.mode == "anthropic":
        if progress:
            progress(f"Calling {config.anthropic_model}…")
        text = run_anthropic(packet.prompt, config)
        model = config.anthropic_model
    else:
        raise AIReviewError("disabled", f"Unknown AI review mode: {config.mode}")
    if progress:
        progress("Checking the answer and citations…")
    return parse_analysis(text, packet, model=model, mode=config.mode)


# ---------------------------------------------------------------------------
# Rendering helpers used by the CLI and UI
# ---------------------------------------------------------------------------

def signals_markdown(signals: Iterable[Signal]) -> str:
    rows = list(signals)
    if not rows:
        return ""
    lines = ["### Local pre-check", ""]
    for signal in rows:
        lines.append(f"- **{signal.side} · {signal.label}** — {signal.detail}")
    lines.append("")
    return "\n".join(lines)


def summary_rows(analysis: AIAnalysis) -> list[tuple[str, str, str]]:
    """(severity, title, scenario) rows for compact table display."""
    return [
        (flag.severity, flag.title, flag.scenario) for flag in analysis.red_flags
    ]
