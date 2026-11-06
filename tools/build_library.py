#!/usr/bin/env python3
"""Build the offline TREC research library.

Fetches the public TREC rules page and the statutes TREC links to, normalizes
the text, and writes ``data/trec_library.json``.  Standard library only.

Usage:
    python tools/build_library.py
    python tools/build_library.py --cache-dir .cache   # reuse downloads
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trec_explorer.lexicon import CONCEPTS  # noqa: E402

RULES_URL = "https://www.trec.texas.gov/agency-information/rules-and-laws/trec-rules"
STATUTES = [
    {
        "id": "oc1101",
        "code": "OC",
        "chapter": "1101",
        "citation_name": "Tex. Occ. Code",
        "title": "Real Estate License Act (TRELA)",
        "url": "https://statutes.capitol.texas.gov/Docs/OC/htm/OC.1101.htm",
        "fetch_url": "https://tcss.legis.texas.gov/resources/OC/htm/OC.1101.htm",
    },
    {
        "id": "oc1102",
        "code": "OC",
        "chapter": "1102",
        "citation_name": "Tex. Occ. Code",
        "title": "Real Estate Inspectors",
        "url": "https://statutes.capitol.texas.gov/Docs/OC/htm/OC.1102.htm",
        "fetch_url": "https://tcss.legis.texas.gov/resources/OC/htm/OC.1102.htm",
    },
    {
        "id": "pr221",
        "code": "PR",
        "chapter": "221",
        "citation_name": "Tex. Prop. Code",
        "title": "Timeshare Plans",
        "url": "https://statutes.capitol.texas.gov/Docs/PR/htm/PR.221.htm",
        "fetch_url": "https://tcss.legis.texas.gov/resources/PR/htm/PR.221.htm",
    },
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36 TREcRulesExplorer/1.0"
)

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def normalize(value: str) -> str:
    """Collapse whitespace while keeping the text readable."""
    value = html.unescape(value)
    value = value.replace("\xa0", " ").replace("\u200b", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def title_case_heading(value: str) -> str:
    """Turn an all-caps statutory heading into readable title case."""
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    if not value:
        return value
    letters = [c for c in value if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.75:
        return value[:1].upper() + value[1:]

    small = {
        "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
        "of", "on", "or", "the", "to", "with",
    }
    words = value.lower().split()
    out: list[str] = []
    for index, word in enumerate(words):
        if word in small and index not in (0, len(words) - 1):
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def short_summary(text: str, limit: int = 250) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for sentence in sentences:
        if not out:
            out = sentence
        elif len(out) + 1 + len(sentence) <= limit:
            out += " " + sentence
        else:
            break
    if len(out) < 40:
        out = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return out


def infer_tags(title: str, body: str) -> list[str]:
    text = (title + "\n" + body).lower()
    tags: list[tuple[int, str]] = []
    for spec in CONCEPTS.values():
        hits = 0
        for term in spec["terms"]:  # type: ignore[index]
            term = str(term)
            if len(term) <= 4 and term.isalpha():
                found = re.search(rf"\b{re.escape(term)}\b", text)
            else:
                found = re.search(re.escape(term), text)
            if found:
                hits += 1
        if hits >= 2:
            tags.append((hits, str(spec["label"])))
    tags.sort(key=lambda item: (-item[0], item[1]))
    return [label for _, label in tags[:6]]


def make_record(
    *,
    uid: str,
    source_id: str,
    source_name: str,
    kind: str,
    citation: str,
    citation_short: str,
    section: str,
    title: str,
    body: str,
    url: str,
    chapter: str = "",
    chapter_title: str = "",
    subchapter: str = "",
) -> dict:
    body = normalize(body)
    return {
        "uid": uid,
        "source_id": source_id,
        "source_name": source_name,
        "kind": kind,
        "citation": citation,
        "citation_short": citation_short,
        "section": section,
        "title": title.strip(),
        "body": body,
        "summary": short_summary(body),
        "chapter": chapter,
        "chapter_title": chapter_title,
        "subchapter": subchapter,
        "url": url,
        "tags": infer_tags(title, body),
        "word_count": len(body.split()),
    }


# ---------------------------------------------------------------------------
# TREC rules parser
# ---------------------------------------------------------------------------

class RuleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chapter = ""
        self.subchapter = ""
        self.current: dict | None = None
        self.sections: list[dict] = []
        self.heading_tag: str | None = None
        self.heading_buf: list[str] = []
        self.blocks: list[dict] = []
        self.lists: list[dict] = []
        self.skip_anchor = 0
        self.done = False

    # -- heading / section state -------------------------------------------
    def _flush_blocks(self) -> None:
        while self.blocks:
            self._finish_block(self.blocks[-1])

    def _finish_block(self, block: dict) -> None:
        if block not in self.blocks:
            return
        self.blocks.remove(block)
        text = normalize("".join(block["buf"]))
        if not text or text.lower() == "return to top":
            return
        if self.current is None:
            return
        prefix = block.get("indent", "") + block.get("marker", "")
        line = f"{prefix} {text}".strip() if prefix else text
        self.current["_lines"].append(line)

    def _finish_section(self) -> None:
        if not self.current:
            return
        body = normalize("\n".join(self.current.pop("_lines", [])))
        if not body:
            self.current = None
            return
        num = self.current["section"]
        self.current.update(
            body=body,
            summary=short_summary(body),
            tags=infer_tags(self.current["title"], body),
            word_count=len(body.split()),
        )
        self.sections.append(self.current)
        self.current = None

    @staticmethod
    def _list_type(attrs: dict) -> str:
        cls = (attrs.get("class") or "").lower()
        if "lower-alpha" in cls:
            return "a"
        if "upper-alpha" in cls:
            return "A"
        if "lower-roman" in cls:
            return "i"
        if "upper-roman" in cls:
            return "I"
        return "1"

    @staticmethod
    def _marker(kind: str, count: int) -> str:
        if kind == "1":
            return f"{count}."
        if kind in ("a", "A"):
            if count <= 26:
                char = chr((96 if kind == "a" else 64) + count)
                return f"({char})"
            return f"({count})"
        if kind in ("i", "I"):
            romans = [
                "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
                "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii",
                "xix", "xx",
            ]
            value = romans[count - 1] if count <= len(romans) else str(count)
            return f"({value.upper() if kind == 'I' else value})"
        return "•"

    def _process_heading(self, tag: str, raw: str) -> None:
        text = normalize(raw)
        if not text:
            return
        if tag == "h3":
            if text.lower() == "share":
                self.done = True
                self._finish_section()
                return
            if text.lower().startswith("chapter "):
                self._finish_section()
                self.chapter = text
                self.subchapter = ""
            return

        # h4
        if text.lower().startswith("subchapter"):
            self._finish_section()
            self.subchapter = text
            return
        match = re.match(r"^§\s*([0-9]+\.[0-9]+)\s*(.*)$", text)
        if not match:
            return
        self._finish_section()
        section_number = match.group(1)
        title = match.group(2).strip().rstrip(".")
        self.current = {
            "uid": f"rule-{section_number}",
            "source_id": "trec-rules",
            "source_name": "TREC Rules",
            "kind": "Rule",
            "citation": f"22 TAC §{section_number}",
            "citation_short": f"§{section_number}",
            "section": section_number,
            "title": title,
            "chapter": self.chapter,
            "chapter_title": re.sub(r"^Chapter\s+\d+\s*", "", self.chapter),
            "subchapter": self.subchapter,
            "url": (
                "https://www.trec.texas.gov/agency-information/rules-and-laws/"
                f"trec-rules#section{section_number}"
            ),
            "_lines": [],
        }

    # -- HTML callbacks -----------------------------------------------------
    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        attrs = {key: (value or "") for key, value in attrs_list}
        if tag in ("h3", "h4"):
            self._flush_blocks()
            self.heading_tag = tag
            self.heading_buf = []
            return
        if self.heading_tag:
            return
        if tag in ("ol", "ul"):
            if self.current is not None:
                self.lists.append({"type": "1" if tag == "ol" else "bullet", "count": 0})
                if tag == "ol":
                    self.lists[-1]["type"] = self._list_type(attrs)
            return
        if tag == "li":
            if self.current is not None and self.lists:
                context = self.lists[-1]
                context["count"] += 1
                marker = self._marker(context["type"], context["count"])
                indent = "  " * (len(self.lists) - 1)
                self.blocks.append(
                    {"tag": "li", "buf": [], "marker": marker, "indent": indent}
                )
            return
        if tag == "p":
            if self.current is not None:
                if self.blocks and self.blocks[-1]["tag"] == "p":
                    self._finish_block(self.blocks[-1])
                self.blocks.append({"tag": "p", "buf": [], "marker": "", "indent": ""})
            return
        if tag == "br":
            if self.heading_tag:
                self.heading_buf.append(" ")
            elif self.blocks:
                self.blocks[-1]["buf"].append("\n")
            return
        if tag == "a":
            href = attrs.get("href", "")
            classes = attrs.get("class", "")
            if href == "#top" or "link-align-right" in classes:
                self.skip_anchor += 1
            return

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            if self.heading_tag:
                self.heading_buf.append(" ")
            elif self.blocks:
                self.blocks[-1]["buf"].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.done:
            return
        if tag in ("h3", "h4") and self.heading_tag == tag:
            self._process_heading(tag, "".join(self.heading_buf))
            self.heading_tag = None
            self.heading_buf = []
            return
        if self.heading_tag:
            return
        if tag in ("p", "li") and self.blocks:
            # Close the nearest matching block.  HTML from this page is tidy,
            # but p/li can be implicitly closed by the source markup.
            for index in range(len(self.blocks) - 1, -1, -1):
                if self.blocks[index]["tag"] == tag:
                    self._finish_block(self.blocks[index])
                    break
            return
        if tag in ("ol", "ul") and self.lists:
            self.lists.pop()
            return
        if tag == "a" and self.skip_anchor:
            self.skip_anchor -= 1

    def handle_data(self, data: str) -> None:
        if self.done:
            return
        if self.heading_tag:
            self.heading_buf.append(data)
        elif self.blocks and not self.skip_anchor:
            self.blocks[-1]["buf"].append(data)

    def close(self) -> None:  # noqa: A003 - HTMLParser API
        super().close()
        self._finish_section()


# ---------------------------------------------------------------------------
# Statute parser
# ---------------------------------------------------------------------------

class StatuteParser(HTMLParser):
    def __init__(self, config: dict) -> None:
        super().__init__(convert_charrefs=True)
        self.config = config
        self.current: dict | None = None
        self.sections: list[dict] = []
        self.blocks: list[dict] = []
        self.subchapter = ""
        self.chapter_title = ""
        self.last_number = -1.0

    def _finish_section(self) -> None:
        if not self.current:
            return
        body = normalize("\n\n".join(self.current.pop("_lines", [])))
        if not body:
            self.current = None
            return
        self.current.update(
            body=body,
            summary=short_summary(body),
            tags=infer_tags(self.current["title"], body),
            word_count=len(body.split()),
        )
        self.sections.append(self.current)
        self.current = None

    def _split_heading(self, rest: str) -> tuple[str, str]:
        match = re.match(r"^(.{2,140}?)\.\s+(.*)$", rest, re.S)
        if match:
            candidate, tail = match.group(1), match.group(2)
            letters = [c for c in candidate if c.isalpha()]
            if letters and sum(c.isupper() for c in letters) / len(letters) >= 0.7:
                return title_case_heading(candidate), tail
        return title_case_heading(rest), ""

    def _process_block(self, raw: str) -> None:
        text = normalize(raw)
        if not text:
            return
        if text.upper().startswith("SUBCHAPTER"):
            self._finish_section()
            self.subchapter = text
            return
        if text.upper().startswith("CHAPTER "):
            self.chapter_title = text
            return
        if re.match(
            r"^(Added|Amended|Renumbered|Transferred|Repealed|Expired)\s+by\b",
            text,
            re.I,
        ):
            return

        match = re.match(r"^Sec\.\s*(\d+\.\d+[A-Za-z]?)\.\s*(.*)$", text, re.S)
        if match:
            number_text = match.group(1)
            try:
                numeric = float(".".join(number_text.replace(".", " ").split()[:2]))
            except ValueError:
                numeric = -1.0
            # A paragraph beginning with a cross-reference to an earlier
            # section is body text, not a new section heading.
            if self.current is not None and numeric <= self.last_number:
                self.current["_lines"].append(text)
                return

            self._finish_section()
            title, body = self._split_heading(match.group(2))
            self.last_number = numeric
            self.current = {
                "uid": f"stat-{number_text}",
                "source_id": self.config["id"],
                "source_name": (
                    f"{self.config['citation_name']} ch. {self.config['chapter']}"
                ),
                "kind": "Statute",
                "citation": (
                    f"{self.config['citation_name']} §{number_text}"
                ),
                "citation_short": f"§{number_text}",
                "section": number_text,
                "title": title,
                "chapter": self.config["chapter"],
                "chapter_title": self.config["title"],
                "subchapter": self.subchapter,
                "url": f"{self.config['url']}#{number_text}",
                "_lines": [body] if body else [],
            }
            return

        if self.current is not None:
            self.current["_lines"].append(text)

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        if tag == "p":
            if self.blocks and self.blocks[-1]["tag"] == "p":
                self._finish_block(self.blocks[-1])
            self.blocks.append({"tag": "p", "buf": []})
        elif tag == "br" and self.blocks:
            self.blocks[-1]["buf"].append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and self.blocks:
            self.blocks[-1]["buf"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self.blocks:
            self._finish_block(self.blocks[-1])

    def _finish_block(self, block: dict) -> None:
        if block in self.blocks:
            self.blocks.remove(block)
        self._process_block("".join(block["buf"]))

    def handle_data(self, data: str) -> None:
        if self.blocks:
            self.blocks[-1]["buf"].append(data)

    def close(self) -> None:  # noqa: A003 - HTMLParser API
        super().close()
        while self.blocks:
            self._finish_block(self.blocks[-1])
        self._finish_section()


# ---------------------------------------------------------------------------
# Fetch / build
# ---------------------------------------------------------------------------

def fetch(url: str, cache_dir: Path | None, name: str) -> str:
    cache_path = cache_dir / name if cache_dir else None
    if cache_path and cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read()
            text = raw.decode("utf-8-sig", errors="replace")
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(text, encoding="utf-8")
            return text
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not download {url}: {last_error}")


def parse_rules(html_text: str) -> list[dict]:
    parser = RuleParser()
    parser.feed(html_text)
    parser.close()
    return parser.sections


def parse_statute(html_text: str, config: dict) -> list[dict]:
    parser = StatuteParser(config)
    parser.feed(html_text)
    parser.close()
    return parser.sections


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory for downloaded HTML (reused when present).",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "trec_library.json",
        help="Output JSON path.",
    )
    args = ap.parse_args(list(argv) if argv is not None else None)

    cache_dir = args.cache_dir
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    print("Fetching TREC rules ...")
    rules_html = fetch(RULES_URL, cache_dir, "trec-rules.html")
    rules = parse_rules(rules_html)
    print(f"  parsed {len(rules)} rule sections")

    sections = list(rules)
    sources = [
        {
            "id": "trec-rules",
            "name": "TREC Rules",
            "kind": "Rule",
            "url": RULES_URL,
            "description": "Texas Administrative Code, Title 22, Part 23",
        }
    ]
    for config in STATUTES:
        print(f"Fetching {config['citation_name']} ch. {config['chapter']} ...")
        html_text = fetch(config["fetch_url"], cache_dir, f"{config['id']}.html")
        parsed = parse_statute(html_text, config)
        print(f"  parsed {len(parsed)} sections")
        sections.extend(parsed)
        sources.append(
            {
                "id": config["id"],
                "name": f"{config['citation_name']} ch. {config['chapter']}",
                "kind": "Statute",
                "url": config["url"],
                "description": config["title"],
            }
        )

    if len(rules) < 150:
        raise SystemExit(
            f"Refusing to write an incomplete library: only {len(rules)} rules parsed."
        )
    if not sections:
        raise SystemExit("No sections parsed.")

    # Keep source order (rules in page order, statutes numeric) but ensure UIDs
    # are unique and stable.
    seen: set[str] = set()
    for record in sections:
        uid = record["uid"]
        if uid in seen:
            raise SystemExit(f"Duplicate record id: {uid}")
        seen.add(uid)

    generated = datetime.now(timezone.utc).astimezone()
    counts: dict[str, int] = {}
    for record in sections:
        counts[record["source_id"]] = counts.get(record["source_id"], 0) + 1

    payload = {
        "meta": {
            "schema_version": 1,
            "generated_at": generated.isoformat(timespec="seconds"),
            "generated_at_display": generated.strftime("%B %d, %Y"),
            "rules_url": RULES_URL,
            "sources": sources,
            "counts": counts,
            "total_sections": len(sections),
            "disclaimer": (
                "Educational research aid only, not legal advice. The Texas "
                "Secretary of State maintains the official text of agency rules, "
                "and the Texas Legislature maintains the official statutes. "
                "Verify every citation against the official source before acting."
            ),
        },
        "sections": sections,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {args.output} ({args.output.stat().st_size:,} bytes)")
    print(f"Counts: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
