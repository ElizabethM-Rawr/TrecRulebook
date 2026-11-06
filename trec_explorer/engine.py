"""Offline search and scenario-comparison engine for the TREC library."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .lexicon import (
    CONCEPTS,
    ISSUE_LENSES,
    PHRASES,
    STOPWORDS,
    matched_issue_lenses,
)

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*|\d+(?:\.\d+)*")
CITATION_RE = re.compile(
    r"\b(?:22\s*TAC\s*)?(?:§\s*)?(\d{3,4}\.\d+[A-Za-z]?)\b", re.I
)
CHAPTER_RE = re.compile(r"\bchapter\s+(\d{3,4})\b", re.I)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

REQUIREMENT_CUES = (
    " must ", " shall ", " may not ", " is required ", " are required ",
    " is prohibited ", " are prohibited ", " not later than ", " before ",
    " within ", " at least ", " no later than ",
)


@dataclass(frozen=True)
class Section:
    uid: str
    source_id: str
    source_name: str
    kind: str
    citation: str
    citation_short: str
    section: str
    title: str
    body: str
    summary: str
    chapter: str
    chapter_title: str
    subchapter: str
    url: str
    tags: tuple[str, ...]
    word_count: int
    chapter_key: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Section":
        source_id = str(data["source_id"])
        chapter = str(data.get("chapter", ""))
        if source_id == "trec-rules":
            number = re.search(r"(\d+)", chapter)
            chapter_key = f"rule:{number.group(1)}" if number else "rule:other"
        else:
            chapter_key = f"stat:{source_id}:{chapter}"
        return cls(
            uid=str(data["uid"]),
            source_id=source_id,
            source_name=str(data["source_name"]),
            kind=str(data["kind"]),
            citation=str(data["citation"]),
            citation_short=str(data["citation_short"]),
            section=str(data["section"]),
            title=str(data["title"]),
            body=str(data["body"]),
            summary=str(data.get("summary", "")),
            chapter=chapter,
            chapter_title=str(data.get("chapter_title", "")),
            subchapter=str(data.get("subchapter", "")),
            url=str(data["url"]),
            tags=tuple(data.get("tags", [])),
            word_count=int(data.get("word_count", 0)),
            chapter_key=chapter_key,
        )

    @property
    def heading(self) -> str:
        return f"{self.citation_short} {self.title}".strip()


@dataclass
class SearchHit:
    section: Section
    raw_score: float
    score: float  # 0-100, relative to the best hit
    snippet: str
    matched_terms: tuple[str, ...] = ()


@dataclass
class ScenarioMatch:
    section: Section
    score: float  # 0-100 relative to the best match in this scenario
    concepts: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass
class ScenarioReport:
    text: str
    matches: list[ScenarioMatch]
    concepts: list[tuple[str, str, int]]  # key, label, strength
    keywords: list[str]
    word_count: int
    lenses: tuple[tuple[str, str, str], ...] = ()  # id, label, why

    @property
    def match_map(self) -> dict[str, ScenarioMatch]:
        return {match.section.uid: match for match in self.matches}


@dataclass
class ComparisonRow:
    section: Section
    a_score: float
    b_score: float
    combined: float
    a_concepts: tuple[str, ...]
    b_concepts: tuple[str, ...]


@dataclass
class ComparisonReport:
    a: ScenarioReport
    b: ScenarioReport
    shared: list[ComparisonRow] = field(default_factory=list)
    only_a: list[ComparisonRow] = field(default_factory=list)
    only_b: list[ComparisonRow] = field(default_factory=list)
    shared_concepts: list[tuple[str, str]] = field(default_factory=list)
    only_a_concepts: list[tuple[str, str]] = field(default_factory=list)
    only_b_concepts: list[tuple[str, str]] = field(default_factory=list)
    checklist: list[tuple[str, str]] = field(default_factory=list)
    overview: str = ""


@dataclass(frozen=True)
class ChapterOption:
    key: str
    label: str
    count: int

    def __str__(self) -> str:
        return self.label


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def _clean_apostrophes(value: str) -> str:
    return value.replace("’", "'").replace("‘", "'")


def content_tokens(value: str) -> list[str]:
    """Lowercase query terms, preserving order and dropping noise."""
    value = _clean_apostrophes(value.lower())
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(value):
        token = match.group(0).strip("'-")
        if not token or token in STOPWORDS:
            continue
        if len(token) < 2:
            continue
        if token.isdigit() and len(token) < 3:
            continue
        tokens.append(token)
    # de-duplicate, preserving order
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _term_pattern(term: str) -> re.Pattern:
    escaped = re.escape(term.lower().replace("’", "'"))
    if term.replace("-", "").replace(" ", "").isalnum() and len(term) <= 4:
        return re.compile(rf"\b{escaped}\b", re.I)
    return re.compile(escaped, re.I)


def _has_term(haystack: str, term: str) -> bool:
    return bool(_term_pattern(term).search(haystack))


def detect_concepts(text: str) -> list[tuple[str, str, int]]:
    """Return (key, label, strength) for concepts supported by this text."""
    lower = _clean_apostrophes(text.lower())
    found: list[tuple[str, str, int]] = []
    for key, spec in CONCEPTS.items():
        anchors = spec.get("anchors", [])
        if anchors and not any(_has_term(lower, str(a)) for a in anchors):
            continue
        strength = 0
        hits = 0
        for term in spec["terms"]:  # type: ignore[index]
            term = str(term).lower()
            if _has_term(lower, term):
                hits += 1
                # Multi-word phrases are more specific than a single word.
                strength += 2 if " " in term or "-" in term else 1
        if hits and strength >= (1 if anchors else 2):
            found.append((key, str(spec["label"]), strength))
    found.sort(key=lambda item: (-item[2], item[1]))
    return found


def find_citation(value: str) -> str | None:
    match = CITATION_RE.search(value)
    return match.group(1) if match else None


def find_chapter(value: str) -> str | None:
    match = CHAPTER_RE.search(value)
    return match.group(1) if match else None


def highlight_spans(text: str, query: str) -> list[tuple[int, int]]:
    """Find non-overlapping query terms for GUI highlighting."""
    terms: list[str] = []
    for phrase in re.findall(r'"([^"]{2,80})"', query):
        if phrase.strip():
            terms.append(phrase.strip())
    terms.extend(content_tokens(query))
    # Include simple stems so "advertise" highlights "advertising".
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        if len(term) >= 6:
            expanded.append(term[: max(5, len(term) - 2)])
    spans: list[tuple[int, int]] = []
    lower = text.lower()
    for term in dict.fromkeys(expanded):
        pattern = re.compile(re.escape(term.lower()), re.I)
        for match in pattern.finditer(lower):
            spans.append((match.start(), match.end()))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def make_snippet(text: str, query: str, width: int = 260) -> str:
    """A compact snippet around the first query hit."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    spans = highlight_spans(text, query)
    if not spans:
        return text[:width].rsplit(" ", 1)[0] + ("…" if len(text) > width else "")
    start, end = spans[0]
    left = max(0, start - width // 3)
    right = min(len(text), left + width)
    if right - left < width:
        left = max(0, right - width)
    snippet = text[left:right]
    if left > 0:
        snippet = "…" + snippet
    if right < len(text):
        snippet = snippet + "…"
    return snippet


def _fts_quote(term: str) -> str:
    term = term.replace('"', " ").replace("*", " ").strip()
    return '"' + term + '"'


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

class Library:
    def __init__(self, data_path: str | Path) -> None:
        self.data_path = Path(data_path)
        payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        self.meta: dict = payload.get("meta", {})
        self.sections: list[Section] = [
            Section.from_dict(item) for item in payload["sections"]
        ]
        self.by_uid = {section.uid: section for section in self.sections}
        self.by_section = {section.section: section for section in self.sections}
        self._lower_text = {
            section.uid: (
                f"{section.citation} {section.title} {section.body} "
                f"{' '.join(section.tags)}"
            ).lower()
            for section in self.sections
        }
        self._df_cache: dict[str, int] = {}
        self._conn = sqlite3.connect(":memory:")
        self._build_fts()

    def _build_fts(self) -> None:
        self._conn.execute(
            """
            CREATE VIRTUAL TABLE docs USING fts5(
                uid UNINDEXED,
                citation,
                title,
                body,
                tags,
                tokenize='porter unicode61'
            )
            """
        )
        rows = [
            (
                section.uid,
                f"{section.citation} {section.section}",
                section.title,
                section.body,
                " ".join(section.tags),
            )
            for section in self.sections
        ]
        self._conn.executemany(
            "INSERT INTO docs(uid, citation, title, body, tags) VALUES (?, ?, ?, ?, ?)",
            rows,
        )

    # -- metadata / browsing -------------------------------------------------
    def chapters(self) -> list[ChapterOption]:
        grouped: dict[str, list[Section]] = {}
        for section in self.sections:
            grouped.setdefault(section.chapter_key, []).append(section)
        options: list[ChapterOption] = []
        for key, items in grouped.items():
            first = items[0]
            if key.startswith("rule:"):
                number = key.split(":", 1)[1]
                label = f"Ch. {number} — {first.chapter_title}"
            else:
                label = f"{first.source_name} — {first.chapter_title}"
            options.append(ChapterOption(key=key, label=label, count=len(items)))
        options.sort(
            key=lambda option: (
                0 if option.key.startswith("rule:") else 1,
                int(re.search(r"\d+", option.key).group(0)) if re.search(r"\d+", option.key) else 0,
                option.label,
            )
        )
        return options

    def sections_in_chapter(self, chapter_key: str) -> list[Section]:
        return [section for section in self.sections if section.chapter_key == chapter_key]

    def section(self, uid: str) -> Section | None:
        return self.by_uid.get(uid)

    def section_by_number(self, number: str) -> Section | None:
        return self.by_section.get(number)

    def _idf(self, terms: Iterable[str]) -> dict[str, float]:
        total = max(1, len(self.sections))
        out: dict[str, float] = {}
        for term in terms:
            if term not in self._df_cache:
                self._df_cache[term] = sum(
                    1 for text in self._lower_text.values() if term in text
                )
            count = self._df_cache[term]
            out[term] = 1.0 + math.log((total + 1.0) / (count + 1.0))
        return out

    @staticmethod
    def _family(section: Section) -> str:
        title = section.title
        if title.startswith("Standards of Practice"):
            return "Standards of Practice"
        words = title.split()
        return " ".join(words[:3]).lower()

    def _concept_specificity(self, section: Section, key: str) -> float:
        text = self._lower_text[section.uid]
        title = section.title.lower()
        score = 0.0
        for term in CONCEPTS[key]["terms"]:  # type: ignore[index]
            term = str(term)
            if _has_term(text, term):
                score += 1.0
            if _has_term(title, term):
                score += 3.0
        return score

    @staticmethod
    def _lens_title_hits(section: Section, lens: dict) -> int:
        title = section.title.lower()
        return sum(
            1 for term in lens["title_terms"] if str(term) in title
        )

    @staticmethod
    def _scope_applies(
        section: Section, concept_keys: set[str], scenario: str
    ) -> bool:
        """Keep specialist subchapters out of unrelated fact patterns."""
        subchapter = section.subchapter.lower()
        lower = scenario.lower()

        if section.source_id == "pr221" and "timeshare" not in concept_keys:
            return False
        if "real estate inspectors" in subchapter and "inspections" not in concept_keys:
            return False
        if "residential rental locators" in subchapter and not any(
            term in lower for term in ("rent", "lease", "landlord", "tenant")
        ):
            return False
        if "easement or right-of-way" in subchapter and "erw" not in concept_keys:
            return False
        if "education providers" in subchapter and "education" not in concept_keys:
            return False
        title = section.title.lower()
        if (
            "initiation of investigation" in title
            or "consumer complaint processing" in title
            or "schedule of administrative penalties" in title
        ) and "discipline" not in concept_keys:
            return False
        if "timeshare" in subchapter and "timeshare" not in concept_keys:
            return False
        return True

    # -- search --------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        source: str = "all",
        chapter_key: str = "",
        limit: int = 60,
    ) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []

        chapter_number = find_chapter(query)
        if chapter_number:
            for key in (
                f"rule:{chapter_number}",
                f"stat:oc1101:{chapter_number}",
                f"stat:oc1102:{chapter_number}",
                f"stat:pr221:{chapter_number}",
            ):
                if any(section.chapter_key == key for section in self.sections):
                    chapter_key = key
                    break

        chapter_query_terms = {
            term for term in content_tokens(query) if term not in {"chapter", "section"}
        }
        if chapter_number and not chapter_key:
            chapter_query_terms.discard(chapter_number)
        if chapter_key and not chapter_query_terms:
            rows = [
                section
                for section in self.sections_in_chapter(chapter_key)
                if (source == "all" or
                    (source == "rules" and section.kind == "Rule") or
                    (source == "statutes" and section.kind == "Statute"))
            ]
            rows.sort(key=lambda section: section.section)
            return [
                SearchHit(
                    section=section,
                    raw_score=100.0,
                    score=100.0,
                    snippet=section.summary,
                )
                for section in rows[:limit]
            ]

        matched: dict[str, tuple[float, str]] = {}

        # Exact citation lookup gets an immediate, authoritative boost.
        citation = find_citation(query)
        if citation:
            hit = self.section_by_number(citation)
            if hit:
                matched[hit.uid] = (10_000.0, make_snippet(hit.body, query))

        terms, phrases = self._query_terms(query)
        if not terms and not phrases:
            return self._finish_hits(matched, query, source, chapter_key, limit)

        fts_query = self._build_match_query(terms, phrases)
        try:
            cursor = self._conn.execute(
                """
                SELECT uid, bm25(docs, 0.0, 8.0, 4.0, 1.0, 3.0) AS rank,
                       snippet(docs, 3, '⟦', '⟧', ' … ', 28) AS snip
                FROM docs
                WHERE docs MATCH ?
                ORDER BY rank
                LIMIT 600
                """,
                (fts_query,),
            )
            for uid, rank, snip in cursor.fetchall():
                score = max(0.0, -float(rank))
                if uid not in matched or score > matched[uid][0]:
                    matched[uid] = (score, snip or "")
        except sqlite3.OperationalError:
            # Last-resort literal scan, used only for unusual FTS syntax.
            lower_terms = [term.lower() for term in terms]
            for section in self.sections:
                haystack = (
                    f"{section.citation} {section.title} {section.body} "
                    f"{' '.join(section.tags)}"
                ).lower()
                score = sum(haystack.count(term) for term in lower_terms)
                if score:
                    matched[section.uid] = (float(score), make_snippet(section.body, query))

        # Very light reranking: titles and tags are strong signals.
        for uid, (score, snip) in list(matched.items()):
            section = self.by_uid[uid]
            title_lower = section.title.lower()
            tag_lower = " ".join(section.tags).lower()
            title_hits = sum(1 for term in terms if term in title_lower)
            tag_hits = sum(1 for term in terms if term in tag_lower)
            matched[uid] = (
                score * (1 + 0.16 * title_hits + 0.04 * tag_hits),
                snip,
            )

        return self._finish_hits(matched, query, source, chapter_key, limit)

    def _query_terms(self, query: str) -> tuple[list[str], list[str]]:
        phrases = [
            phrase.strip()
            for phrase in re.findall(r'"([^"]{2,90})"', query)
            if phrase.strip()
        ]
        remainder = re.sub(r'"[^"]*"', " ", query)
        terms = content_tokens(remainder)
        # Add known multi-word phrases when they occur naturally.
        lower = _clean_apostrophes(remainder.lower())
        for phrase in PHRASES:
            if phrase in lower and phrase not in phrases:
                phrases.append(phrase)
        return terms[:40], phrases[:10]

    @staticmethod
    def _build_match_query(terms: Iterable[str], phrases: Iterable[str]) -> str:
        parts: list[str] = []
        for phrase in phrases:
            parts.append(_fts_quote(phrase))
        for term in terms:
            parts.append(_fts_quote(term))
        # OR keeps recall high; BM25 and the rerank step order the results.
        return " OR ".join(parts)

    def _finish_hits(
        self,
        matched: dict[str, tuple[float, str]],
        query: str,
        source: str,
        chapter_key: str,
        limit: int,
    ) -> list[SearchHit]:
        rows: list[tuple[Section, float, str]] = []
        for uid, (score, snip) in matched.items():
            section = self.by_uid.get(uid)
            if not section:
                continue
            if source == "rules" and section.kind != "Rule":
                continue
            if source == "statutes" and section.kind != "Statute":
                continue
            if chapter_key and section.chapter_key != chapter_key:
                continue
            rows.append((section, score, snip))

        rows.sort(key=lambda item: (-item[1], item[0].citation_short))
        if not rows:
            return []
        best = rows[0][1] or 1.0
        hits: list[SearchHit] = []
        for section, score, snippet in rows[:limit]:
            relative = 100.0 * score / best
            hits.append(
                SearchHit(
                    section=section,
                    raw_score=score,
                    score=round(min(99.0, max(1.0, relative)), 1),
                    snippet=snippet or make_snippet(section.body, query),
                    matched_terms=tuple(content_tokens(query)),
                )
            )
        return hits

    # -- scenarios -----------------------------------------------------------
    def analyze_scenario(self, text: str, *, limit: int = 12) -> ScenarioReport:
        raw_terms = content_tokens(text)
        concepts = detect_concepts(text)
        lenses = matched_issue_lenses(concepts)
        concept_keys = {key for key, _, _ in concepts[:5]}

        # Expand the query with the vocabulary of the concepts the scenario
        # actually raises.  Original words remain in the query too.
        expansion: list[str] = []
        for key, _, _ in concepts[:4]:
            for term in CONCEPTS[key]["terms"]:  # type: ignore[index]
                term = str(term)
                if term.lower() not in expansion:
                    expansion.append(term.lower())
        expansion = expansion[:22]
        all_terms = list(dict.fromkeys(raw_terms + expansion))[:40]
        phrases = [phrase for phrase in PHRASES if phrase in text.lower()][:8]
        fts_query = self._build_match_query(all_terms, phrases)
        primary_query = self._build_match_query(raw_terms, phrases)

        if not fts_query:
            return ScenarioReport(
                text=text,
                matches=[],
                concepts=concepts,
                keywords=raw_terms,
                word_count=len(text.split()),
            )

        candidates: dict[str, Section] = {}
        expanded_scores: dict[str, float] = {}
        try:
            cursor = self._conn.execute(
                """
                SELECT uid, bm25(docs, 0.0, 8.0, 4.0, 1.0, 3.0) AS rank
                FROM docs
                WHERE docs MATCH ?
                ORDER BY rank
                LIMIT 220
                """,
                (fts_query,),
            )
            for uid, rank in cursor.fetchall():
                section = self.by_uid.get(uid)
                if section:
                    candidates[uid] = section
                    expanded_scores[uid] = max(0.0, -float(rank))
        except sqlite3.OperationalError:
            pass

        primary_scores: dict[str, float] = {}
        if primary_query:
            try:
                cursor = self._conn.execute(
                    """
                    SELECT uid, bm25(docs, 0.0, 8.0, 4.0, 1.0, 3.0) AS rank
                    FROM docs
                    WHERE docs MATCH ?
                    ORDER BY rank
                    LIMIT 220
                    """,
                    (primary_query,),
                )
                for uid, rank in cursor.fetchall():
                    section = self.by_uid.get(uid)
                    if section:
                        candidates[uid] = section
                        primary_scores[uid] = max(0.0, -float(rank))
            except sqlite3.OperationalError:
                primary_scores = {}

        if not candidates:
            # Fallback keyword scoring over the complete corpus.
            for section in self.sections:
                haystack = self._lower_text[section.uid]
                score = sum(
                    1.0 for term in all_terms if _has_term(haystack, term)
                )
                if score:
                    candidates[section.uid] = section
                    expanded_scores[section.uid] = score

        if not candidates:
            return ScenarioReport(
                text=text,
                matches=[],
                concepts=concepts,
                keywords=raw_terms,
                word_count=len(text.split()),
                lenses=tuple(
                    (str(lens["id"]), str(lens["label"]), str(lens["why"]))
                    for lens in lenses
                ),
            )

        max_expanded = max(expanded_scores.values(), default=1.0) or 1.0
        max_primary = max(primary_scores.values(), default=1.0) or 1.0
        idf = self._idf(raw_terms)
        total_idf = sum(idf.get(term, 1.0) for term in raw_terms) or 1.0
        scored: list[tuple[Section, float, tuple[str, ...], tuple[str, ...]]] = []
        for uid, section in candidates.items():
            if not self._scope_applies(section, concept_keys, text):
                continue
            haystack = self._lower_text[uid]
            title_lower = section.title.lower()

            expanded_norm = expanded_scores.get(uid, 0.0) / max_expanded
            primary_norm = primary_scores.get(uid, 0.0) / max_primary
            bm25 = (
                0.78 * primary_norm + 0.22 * expanded_norm
                if primary_scores
                else expanded_norm
            )

            concept_hits: dict[str, int] = {}
            concept_score = 0.0
            for key in concept_keys:
                hits = sum(
                    1
                    for term in CONCEPTS[key]["terms"]  # type: ignore[index]
                    if _has_term(haystack, str(term))
                )
                if hits:
                    concept_hits[key] = hits
                    concept_score += min(3, hits)

            concept_total = max(1.0, 3.0 * max(1, len(concept_keys)))
            concept_fraction = min(1.0, concept_score / concept_total)

            matched_terms: list[str] = []
            matched_weight = 0.0
            title_weight = 0.0
            for term in raw_terms:
                found = _has_term(haystack, term)
                if not found and len(term) >= 6:
                    found = term[:5] in haystack
                if found:
                    matched_terms.append(term)
                    matched_weight += idf.get(term, 1.0)
                title_found = term in title_lower or (
                    len(term) >= 6 and term[:5] in title_lower
                )
                if title_found:
                    title_weight += idf.get(term, 1.0)

            coverage = min(1.0, matched_weight / total_idf)
            title_ratio = min(1.0, title_weight / total_idf)

            bm25_norm = max(0.0, min(1.0, bm25)) ** 0.65
            final = (
                0.42 * bm25_norm
                + 0.27 * concept_fraction
                + 0.19 * coverage
                + 0.12 * title_ratio
            )
            # Direct concept terms in the title are especially meaningful.
            if any(
                _has_term(title_lower, str(term))
                for key in concept_keys
                for term in CONCEPTS[key]["terms"]  # type: ignore[index]
            ):
                final += 0.04

            lens_hits = 0
            hit_lens_labels: list[str] = []
            for lens in lenses:
                hits = self._lens_title_hits(section, lens)
                if hits:
                    lens_hits += hits
                    hit_lens_labels.append(str(lens["label"]))
            if lens_hits:
                final += min(0.30, 0.16 * lens_hits)

            labels = tuple(
                str(CONCEPTS[key]["label"])
                for key in concept_hits
                if key in CONCEPTS
            )
            reasons: list[str] = []
            matched_terms.sort(key=lambda term: -idf.get(term, 1.0))
            if matched_terms:
                reasons.append("Matches scenario terms: " + ", ".join(matched_terms[:6]))
            if labels:
                reasons.append("Touches: " + "; ".join(labels[:3]))
            if title_weight:
                reasons.append("Issue appears in the section title")
            if hit_lens_labels:
                reasons.append("Issue lens: " + "; ".join(hit_lens_labels[:2]))
            if section.kind == "Rule":
                reasons.append("TREC rule (22 TAC)")
            else:
                reasons.append("Statutory authority")
            scored.append((section, final, labels, tuple(reasons)))

        if not scored:
            return ScenarioReport(
                text=text,
                matches=[],
                concepts=concepts,
                keywords=raw_terms,
                word_count=len(text.split()),
                lenses=tuple(
                    (str(lens["id"]), str(lens["label"]), str(lens["why"]))
                    for lens in lenses
                ),
            )
        scored.sort(key=lambda item: (-item[1], item[0].citation_short))
        chosen: list[tuple[Section, float, tuple[str, ...], tuple[str, ...]]] = []
        chosen_uids: set[str] = set()

        # Issue-lens representatives come first, so a decisive short section
        # is not buried under several long keyword-dense sections.
        for lens in lenses:
            ranked = sorted(
                scored,
                key=lambda item: (
                    -self._lens_title_hits(item[0], lens),
                    -item[1],
                    item[0].citation_short,
                ),
            )
            added = 0
            for item in ranked:
                if self._lens_title_hits(item[0], lens) == 0:
                    break
                if item[0].uid in chosen_uids:
                    continue
                chosen.append(item)
                chosen_uids.add(item[0].uid)
                added += 1
                if added >= 2:
                    break

        # First make sure each recognized issue is represented.  This prevents
        # a long Standards-of-Practice section from crowding out a short,
        # decisive statute such as the inspector dual-capacity rule.
        for key, _label, _strength in concepts[:5]:
            ranked = sorted(
                scored,
                key=lambda item: (
                    -self._concept_specificity(item[0], key),
                    -item[1],
                    item[0].citation_short,
                ),
            )
            added = 0
            for item in ranked:
                if item[0].uid in chosen_uids:
                    continue
                chosen.append(item)
                chosen_uids.add(item[0].uid)
                added += 1
                if added >= 2:
                    break

        family_counts: dict[str, int] = {}
        for item in chosen:
            family = self._family(item[0])
            family_counts[family] = family_counts.get(family, 0) + 1
        for item in scored:
            if len(chosen) >= max(limit, 4):
                break
            if item[0].uid in chosen_uids:
                continue
            family = self._family(item[0])
            family_limit = 2 if family == "Standards of Practice" else 3
            if family_counts.get(family, 0) >= family_limit:
                continue
            chosen.append(item)
            chosen_uids.add(item[0].uid)
            family_counts[family] = family_counts.get(family, 0) + 1
        if len(chosen) < max(limit, 4):
            for item in scored:
                if item[0].uid not in chosen_uids:
                    chosen.append(item)
                    chosen_uids.add(item[0].uid)
                if len(chosen) >= max(limit, 4):
                    break

        if len(chosen) > max(limit, 4):
            chosen = chosen[: max(limit, 4)]
        chosen.sort(key=lambda item: (-item[1], item[0].citation_short))
        best = chosen[0][1] or 1.0
        matches = [
            ScenarioMatch(
                section=section,
                score=round(min(99.0, max(5.0, 100.0 * score / best)), 1),
                concepts=labels,
                reasons=reasons,
            )
            for section, score, labels, reasons in chosen
        ]
        return ScenarioReport(
            text=text,
            matches=matches,
            concepts=concepts,
            keywords=raw_terms,
            word_count=len(text.split()),
            lenses=tuple(
                (str(lens["id"]), str(lens["label"]), str(lens["why"]))
                for lens in lenses
            ),
        )

    def compare(self, scenario_a: str, scenario_b: str) -> ComparisonReport:
        a = self.analyze_scenario(scenario_a)
        b = self.analyze_scenario(scenario_b)
        a_map = a.match_map
        b_map = b.match_map

        shared: list[ComparisonRow] = []
        only_a: list[ComparisonRow] = []
        only_b: list[ComparisonRow] = []

        for uid, match in a_map.items():
            if uid in b_map:
                other = b_map[uid]
                shared.append(
                    ComparisonRow(
                        section=match.section,
                        a_score=match.score,
                        b_score=other.score,
                        combined=round((match.score + other.score) / 2, 1),
                        a_concepts=match.concepts,
                        b_concepts=other.concepts,
                    )
                )
            else:
                only_a.append(
                    ComparisonRow(
                        section=match.section,
                        a_score=match.score,
                        b_score=0.0,
                        combined=match.score,
                        a_concepts=match.concepts,
                        b_concepts=(),
                    )
                )
        for uid, match in b_map.items():
            if uid not in a_map:
                only_b.append(
                    ComparisonRow(
                        section=match.section,
                        a_score=0.0,
                        b_score=match.score,
                        combined=match.score,
                        a_concepts=(),
                        b_concepts=match.concepts,
                    )
                )

        shared.sort(key=lambda row: (-row.combined, row.section.citation_short))
        only_a.sort(key=lambda row: (-row.a_score, row.section.citation_short))
        only_b.sort(key=lambda row: (-row.b_score, row.section.citation_short))

        a_labels = {key: label for key, label, _ in a.concepts}
        b_labels = {key: label for key, label, _ in b.concepts}
        shared_keys = set(a_labels) & set(b_labels)
        only_a_keys = set(a_labels) - set(b_labels)
        only_b_keys = set(b_labels) - set(a_labels)

        report = ComparisonReport(
            a=a,
            b=b,
            shared=shared,
            only_a=only_a,
            only_b=only_b,
            shared_concepts=[(key, a_labels[key]) for key in sorted(shared_keys)],
            only_a_concepts=[(key, a_labels[key]) for key in sorted(only_a_keys)],
            only_b_concepts=[(key, b_labels[key]) for key in sorted(only_b_keys)],
        )
        report.checklist = self._build_checklist(report)
        report.overview = self._build_overview(report)
        return report

    def _build_overview(self, report: ComparisonReport) -> str:
        lines: list[str] = []
        if report.shared_concepts:
            lines.append(
                "Shared issues: "
                + ", ".join(label for _, label in report.shared_concepts)
                + "."
            )
        if report.only_a_concepts:
            lines.append(
                "Only scenario A raises: "
                + ", ".join(label for _, label in report.only_a_concepts)
                + "."
            )
        if report.only_b_concepts:
            lines.append(
                "Only scenario B raises: "
                + ", ".join(label for _, label in report.only_b_concepts)
                + "."
            )
        if report.shared:
            lines.append(
                "Authorities retrieved for both scenarios: "
                + "; ".join(
                    f"{row.section.citation_short} {row.section.title}"
                    for row in report.shared[:5]
                )
                + "."
            )
        elif report.a.matches and report.b.matches:
            lines.append(
                "The two scenarios retrieved no common authorities in the top "
                "matches. Compare the unique lists before assuming the same rule "
                "applies."
            )
        if report.only_a:
            lines.append(
                "A-only authority to review: "
                + ", ".join(
                    row.section.citation_short for row in report.only_a[:4]
                )
                + "."
            )
        if report.only_b:
            lines.append(
                "B-only authority to review: "
                + ", ".join(
                    row.section.citation_short for row in report.only_b[:4]
                )
                + "."
            )
        if not lines:
            lines.append(
                "Not enough issue terms were recognized. Add concrete facts "
                "(who did what, when, what was advertised or disclosed, and what "
                "money changed hands) and run the comparison again."
            )
        return "\n\n".join(lines)

    def _build_checklist(self, report: ComparisonReport) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        seen: set[str] = set()
        candidates = (
            report.shared
            + report.only_a[:8]
            + report.only_b[:8]
        )
        for row in candidates:
            for sentence in SENTENCE_RE.split(row.section.body):
                sentence = re.sub(r"\s+", " ", sentence).strip()
                if len(sentence) < 45:
                    continue
                lower = f" {sentence.lower()} "
                if not any(cue in lower for cue in REQUIREMENT_CUES):
                    continue
                key = sentence[:100].lower()
                if key in seen:
                    continue
                seen.add(key)
                if len(sentence) > 310:
                    sentence = sentence[:307].rsplit(" ", 1)[0] + "…"
                rows.append((row.section.citation_short, sentence))
                break
            if len(rows) >= 8:
                break
        return rows
