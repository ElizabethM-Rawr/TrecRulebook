"""Command-line workflow for optional AI comparison review.

This is the bridge used by humans and external agents:

    # 1. Build a request packet from two scenario files
    python -m trec_explorer.ai_cli packet --a-file a.txt --b-file b.txt \
        --out packet.json --prompt-out prompt.txt

    # 2. A model (or an agent) answers with the JSON schema in the prompt
    python -m trec_explorer.ai_cli review --packet packet.json \
        --response answer.json --out review.md

    # 3. Or let the configured transport run it directly
    python -m trec_explorer.ai_cli run --packet packet.json --mode harness \
        --out review.md

The packet is a plain JSON document.  An agent that can read files can work
from it without this CLI; the CLI validates the answer and checks citations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ai, storage
from .engine import Library

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PROVIDER = 3


def _read_text(path: str | None, inline: str | None, label: str) -> str:
    if inline is not None:
        return inline
    if not path:
        raise SystemExit(f"error: provide --{label}-file or --{label}-text")
    return Path(path).expanduser().read_text(encoding="utf-8")


def _default_data_path() -> Path:
    return storage.resource_root() / "data" / "trec_library.json"


def _load_packet(path: str) -> ai.ComparisonPacket:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != ai.SCHEMA_NAME:
        raise SystemExit(
            f"error: {path} is not a {ai.SCHEMA_NAME} packet (version "
            f"{ai.SCHEMA_VERSION})."
        )
    return ai.ComparisonPacket(payload=payload, prompt=ai.build_prompt(payload))


def _config_from_args(args: argparse.Namespace) -> ai.AIConfig:
    settings = storage.read_settings()
    if args.mode:
        base = ai.AIConfig.from_mapping(settings.get("ai_review"))
        return ai.AIConfig(
            mode=args.mode,
            harness_command=args.harness_command or base.harness_command,
            harness_effort=args.harness_effort or base.harness_effort,
            claude_command=args.claude_command or base.claude_command,
            claude_model=args.claude_model or base.claude_model,
            api_base_url=args.base_url or base.api_base_url,
            api_model=args.model or base.api_model,
            api_key_env=args.api_key_env or base.api_key_env,
            api_key=base.api_key,
            gemini_api_base=args.gemini_base or base.gemini_api_base,
            gemini_model=args.gemini_model or base.gemini_model,
            gemini_api_key_env=args.gemini_key_env or base.gemini_api_key_env,
            gemini_api_key=base.gemini_api_key,
            anthropic_api_base=args.anthropic_base or base.anthropic_api_base,
            anthropic_model=args.anthropic_model or base.anthropic_model,
            anthropic_api_key_env=(
                args.anthropic_key_env or base.anthropic_api_key_env
            ),
            anthropic_api_key=base.anthropic_api_key,
            timeout_seconds=args.timeout or base.timeout_seconds,
            max_authorities=base.max_authorities,
            authority_chars=base.authority_chars,
        )
    if "ai_review" in settings:
        return ai.config_from_settings(settings)
    return ai.default_config()


def _print_summary(analysis: ai.AIAnalysis) -> None:
    print(f"risk: {analysis.risk_level}  confidence: {analysis.confidence}")
    print(f"red flags: {len(analysis.red_flags)}")
    for flag in analysis.red_flags:
        print(f"  [{flag.severity.upper():6}] ({flag.scenario}) {flag.title}")
        if flag.citations:
            print("           " + ", ".join(flag.citations))
    if analysis.citation_warnings:
        print("citation warnings:")
        for warning in analysis.citation_warnings:
            print(f"  - {warning}")


def _write_or_print(markdown: str, out: str | None) -> None:
    if out:
        path = Path(out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        print(f"wrote {path}")
    else:
        print(markdown)


def cmd_packet(args: argparse.Namespace) -> int:
    a = _read_text(args.a_file, args.a_text, "a")
    b = _read_text(args.b_file, args.b_text, "b")
    data_path = Path(args.data).expanduser() if args.data else _default_data_path()
    if not data_path.exists():
        print(f"error: library not found at {data_path}", file=sys.stderr)
        return EXIT_USAGE
    library = Library(data_path)
    config = ai.AIConfig(max_authorities=args.max_authorities)
    report = library.compare(a, b)
    packet = ai.build_packet(report, config, library_meta=library.meta)
    if args.out:
        path = Path(args.out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(packet.to_json(), encoding="utf-8")
        print(f"wrote {path} ({len(packet.payload['authorities'])} authorities)")
    if args.prompt_out:
        path = Path(args.prompt_out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(packet.prompt, encoding="utf-8")
        print(f"wrote {path} ({len(packet.prompt):,} chars)")
    if not args.out and not args.prompt_out:
        print(packet.to_json())
    if args.print_prompt:
        print(packet.prompt)
    return EXIT_OK


def cmd_review(args: argparse.Namespace) -> int:
    packet = _load_packet(args.packet)
    response = Path(args.response).expanduser().read_text(encoding="utf-8")
    try:
        analysis = ai.parse_analysis(
            response, packet, model=args.model or "external", mode="manual"
        )
    except ai.AIReviewError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        if exc.detail:
            print(exc.detail, file=sys.stderr)
        return EXIT_USAGE
    _print_summary(analysis)
    if args.json:
        payload = {
            "verdict": analysis.verdict,
            "risk_level": analysis.risk_level,
            "confidence": analysis.confidence,
            "red_flags": [
                {
                    "title": flag.title,
                    "severity": flag.severity,
                    "scenario": flag.scenario,
                    "why_it_matters": flag.why_it_matters,
                    "citations": list(flag.citations),
                    "what_to_check": flag.what_to_check,
                }
                for flag in analysis.red_flags
            ],
            "key_differences": analysis.key_differences,
            "missing_facts": analysis.missing_facts,
            "retrieval_notes": analysis.retrieval_notes,
            "citation_warnings": analysis.citation_warnings,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.out:
        _write_or_print(analysis.to_markdown(), args.out)
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    packet = _load_packet(args.packet)
    config = _config_from_args(args)
    try:
        analysis = ai.run_review(
            packet, config, progress=lambda text: print(text, file=sys.stderr)
        )
    except ai.AIReviewError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        if exc.detail:
            print(exc.detail, file=sys.stderr)
        return EXIT_PROVIDER
    _print_summary(analysis)
    if args.json:
        print(json.dumps(analysis.raw_response, ensure_ascii=False))
    _write_or_print(analysis.to_markdown(), args.out)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trec_explorer.ai_cli",
        description="Build, answer, and validate optional AI comparison reviews.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    packet = sub.add_parser("packet", help="build a review request from two scenarios")
    packet.add_argument("--a-file")
    packet.add_argument("--a-text")
    packet.add_argument("--b-file")
    packet.add_argument("--b-text")
    packet.add_argument("--data", help="path to trec_library.json")
    packet.add_argument("--out", help="write the packet JSON here")
    packet.add_argument("--prompt-out", help="write the prompt text here")
    packet.add_argument("--print-prompt", action="store_true")
    packet.add_argument("--max-authorities", type=int, default=ai.DEFAULT_MAX_AUTHORITIES)
    packet.set_defaults(func=cmd_packet)

    review = sub.add_parser("review", help="validate an external JSON answer")
    review.add_argument("--packet", required=True)
    review.add_argument("--response", required=True)
    review.add_argument("--out", help="write the rendered review markdown here")
    review.add_argument("--json", action="store_true", help="print normalized JSON")
    review.add_argument("--model", help="label for the model that answered")
    review.set_defaults(func=cmd_review)

    run = sub.add_parser("run", help="run the configured AI transport")
    run.add_argument("--packet", required=True)
    run.add_argument(
        "--mode",
        choices=["gemini", "harness", "claude-cli", "api", "anthropic"],
    )
    run.add_argument("--harness-command")
    run.add_argument("--harness-effort", choices=list(ai.EFFORT_LEVELS))
    run.add_argument("--claude-command")
    run.add_argument("--claude-model")
    run.add_argument("--gemini-base")
    run.add_argument("--gemini-model")
    run.add_argument("--gemini-key-env")
    run.add_argument("--base-url")
    run.add_argument("--model")
    run.add_argument("--api-key-env")
    run.add_argument("--anthropic-base")
    run.add_argument("--anthropic-model")
    run.add_argument("--anthropic-key-env")
    run.add_argument("--timeout", type=int)
    run.add_argument("--out", help="write the rendered review markdown here")
    run.add_argument("--json", action="store_true", help="print the raw answer")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
