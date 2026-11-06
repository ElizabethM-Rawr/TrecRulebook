from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trec_explorer import ai, ai_cli  # noqa: E402
from trec_explorer.engine import Library  # noqa: E402

DATA = ROOT / "data" / "trec_library.json"

A = (
    "A sales agent wants to advertise a team name, \"The Smith Group,\" on "
    "social media and on a yard sign. The sponsoring broker has not registered "
    "the team name. The agent also wants to pay a $250 referral fee to an "
    "unlicensed friend who introduced a buyer."
)
B = (
    "A licensed inspector is asked by a real estate broker to inspect a property "
    "that the broker has listed. The inspector wants to advertise a free repair "
    "estimate and then perform repairs on the defects found during the inspection."
)

VALID = {
    "verdict": "A is an advertising and referral problem; B is an inspector conflict problem.",
    "risk_level": "high",
    "confidence": "medium",
    "red_flags": [
        {
            "title": "Unregistered team name",
            "severity": "high",
            "scenario": "A",
            "why_it_matters": "The broker has not registered the advertised team name.",
            "citations": ["§535.154", "22 TAC §535.155"],
            "what_to_check": "Whether the broker registered the name before the advertising.",
        },
        {
            "title": "Fee to an unlicensed person",
            "severity": "high",
            "scenario": "A",
            "why_it_matters": "A referral fee to an unlicensed friend may be a prohibited split.",
            "citations": ["§535.147"],
            "what_to_check": "What the friend did for the fee.",
        },
        {
            "title": "Inspector repair conflict",
            "severity": "medium",
            "scenario": "B",
            "why_it_matters": "The inspector proposes to repair defects found in the same inspection.",
            "citations": ["§1102.303"],
            "what_to_check": "Whether the repair falls within a statutory exception.",
        },
    ],
    "key_differences": ["Only A involves a referral fee."],
    "missing_facts": ["When the advertising ran."],
    "retrieval_notes": ["Team-name registration timing was not retrieved."],
}


@contextlib.contextmanager
def workspace_tempdir():
    """Temp directory inside the repo.

    Windows sandboxes used by the test runner can deny writes into a
    ``mkdtemp`` directory (its restrictive DACL) and chmod on %TEMP%.  A
    normally-created directory under the repo inherits the writable ACL.
    """
    path = ROOT / f".trec-test-{os.getpid()}-{os.urandom(4).hex()}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class _CompletionHandler(BaseHTTPRequestHandler):
    response_text = json.dumps(VALID)
    last_headers: dict = {}
    last_body: dict = {}

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        type(self).last_headers = {
            key.lower(): value for key, value in self.headers.items()
        }
        try:
            type(self).last_body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            type(self).last_body = {}
        if "/unauthorized" in self.path:
            body = b'{"error":"bad key"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "disabled" in self.path:
            body = json.dumps(
                {
                    "error": {
                        "code": 403,
                        "status": "PERMISSION_DENIED",
                        "details": [{"reason": "SERVICE_DISABLED"}],
                    }
                }
            ).encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if ":generateContent" in self.path:
            body = json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": self.response_text}]
                            },
                            "finishReason": "STOP",
                        }
                    ]
                }
            ).encode("utf-8")
        elif "/messages" in self.path:
            body = json.dumps(
                {
                    "type": "message",
                    "content": [{"type": "text", "text": self.response_text}],
                }
            ).encode("utf-8")
        else:
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": self.response_text,
                            }
                        }
                    ]
                }
            ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence test output
        return


class AILayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lib = Library(DATA)
        cls.report = cls.lib.compare(A, B)
        cls.config = ai.AIConfig(max_authorities=14)
        cls.packet = ai.build_packet(cls.report, cls.config, library_meta=cls.lib.meta)

    # -- packet -------------------------------------------------------------
    def test_packet_shape(self) -> None:
        payload = self.packet.payload
        self.assertEqual(payload["schema"], ai.SCHEMA_NAME)
        self.assertEqual(payload["version"], ai.SCHEMA_VERSION)
        self.assertEqual(payload["scenario_a"]["text"], A)
        self.assertEqual(payload["scenario_b"]["text"], B)
        self.assertEqual(len(payload["authorities"]), 14)
        self.assertTrue(payload["retrieved_citations"])
        self.assertTrue(payload["signals"])
        self.assertTrue({"A", "B"} <= {entry["side"] for entry in payload["authorities"]})
        self.assertTrue(all(entry["citation_short"] for entry in payload["authorities"]))
        self.assertTrue(all(entry["body"] for entry in payload["authorities"]))

    def test_packet_prompt_is_self_contained(self) -> None:
        prompt = self.packet.prompt
        self.assertIn("SCENARIO A", prompt)
        self.assertIn("SCENARIO B", prompt)
        self.assertIn("=== AUTHORITIES ===", prompt)
        self.assertIn('"red_flags"', prompt)
        self.assertIn(A, prompt)
        self.assertIn("Do not use outside law", prompt)

    def test_body_truncation_respects_limit(self) -> None:
        packet = ai.build_packet(
            self.report,
            ai.AIConfig(max_authorities=6, authority_chars=600),
            library_meta=self.lib.meta,
        )
        for entry in packet.payload["authorities"]:
            self.assertLessEqual(len(entry["body"]), 600)
            if entry["truncated"]:
                self.assertTrue(entry["body"].endswith("…"))

    def test_packet_json_roundtrip(self) -> None:
        payload = json.loads(self.packet.to_json())
        restored = ai.ComparisonPacket(payload, ai.build_prompt(payload))
        analysis = ai.parse_analysis(json.dumps(VALID), restored)
        self.assertEqual(len(analysis.red_flags), 3)

    def test_authority_selection_is_deduplicated(self) -> None:
        uids = [entry["section"] for entry in self.packet.payload["authorities"]]
        self.assertEqual(len(uids), len(set(uids)))

    # -- signals ------------------------------------------------------------
    def test_signals_include_unique_issues_and_fact_gaps(self) -> None:
        kinds = {signal.kind for signal in ai.build_signals(self.report)}
        self.assertIn("unique_issue", kinds)
        self.assertIn("missing_fact", kinds)
        sides = {signal.side for signal in ai.build_signals(self.report)}
        self.assertTrue({"A", "B"} <= sides)

    def test_missing_facts_detects_common_gaps(self) -> None:
        gaps = ai.missing_facts("A broker did something.", set())
        self.assertTrue(any("date" in gap.lower() for gap in gaps))
        self.assertTrue(any("writing" in gap.lower() for gap in gaps))

        complete = ai.missing_facts(
            "On March 3, 2026, the broker emailed a signed contract and paid a "
            "$500 fee to the seller.",
            {"trust_funds"},
        )
        self.assertEqual(complete, [])

    def test_short_scenario_is_flagged(self) -> None:
        report = self.lib.compare("A broker advertises.", B)
        kinds = {signal.kind for signal in ai.build_signals(report)}
        self.assertIn("thin_scenario", kinds)

    # -- parsing ------------------------------------------------------------
    def test_parse_valid_sorts_and_normalizes(self) -> None:
        analysis = ai.parse_analysis(json.dumps(VALID), self.packet, model="test")
        self.assertEqual(analysis.risk_level, "high")
        self.assertEqual(analysis.confidence, "medium")
        self.assertEqual(analysis.red_flags[0].severity, "high")
        self.assertEqual(analysis.red_flags[0].citations, ("§535.154", "§535.155"))
        self.assertFalse(analysis.citation_warnings)
        self.assertIn("Scenario A", analysis.to_markdown())
        self.assertIn("HIGH", analysis.to_markdown())

    def test_parse_accepts_fenced_json_with_prose(self) -> None:
        text = "Here is the review:\n```json\n" + json.dumps(VALID) + "\n```\nDone."
        analysis = ai.parse_analysis(text, self.packet)
        self.assertEqual(len(analysis.red_flags), 3)

    def test_parse_rejects_non_json(self) -> None:
        with self.assertRaises(ai.AIReviewError) as caught:
            ai.parse_analysis("I could not analyze this.", self.packet)
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_parse_rejects_missing_verdict(self) -> None:
        with self.assertRaises(ai.AIReviewError) as caught:
            ai.parse_analysis('{"red_flags": []}', self.packet)
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_unknown_citation_is_warned_and_removed(self) -> None:
        payload = json.loads(json.dumps(VALID))
        payload["red_flags"][0]["citations"] = ["§999.999"]
        analysis = ai.parse_analysis(json.dumps(payload), self.packet)
        self.assertEqual(analysis.red_flags[0].citations, ())
        self.assertTrue(any("999.999" in warning for warning in analysis.citation_warnings))

    def test_retrieved_but_not_included_citation_is_warned(self) -> None:
        packet = ai.build_packet(
            self.report, ai.AIConfig(max_authorities=4), library_meta=self.lib.meta
        )
        included = {
            entry["citation_short"] for entry in packet.payload["authorities"]
        }
        candidates = [
            citation
            for citation in packet.payload["retrieved_citations"]
            if citation not in included
        ]
        self.assertTrue(candidates, "test needs a retrieved-but-not-included citation")
        payload = json.loads(json.dumps(VALID))
        payload["red_flags"][0]["citations"] = [candidates[0]]
        analysis = ai.parse_analysis(json.dumps(payload), packet)
        self.assertIn(candidates[0], analysis.red_flags[0].citations)
        self.assertTrue(analysis.citation_warnings)

    def test_off_mode_refuses_to_run(self) -> None:
        with self.assertRaises(ai.AIReviewError) as caught:
            ai.run_review(self.packet, ai.AIConfig())
        self.assertEqual(caught.exception.code, "disabled")

    # -- config -------------------------------------------------------------
    def test_config_roundtrip_and_clamps(self) -> None:
        config = ai.AIConfig.from_mapping(
            {
                "mode": "harness",
                "timeout_seconds": 5,
                "max_authorities": 999,
                "authority_chars": "bad",
            }
        )
        self.assertEqual(config.mode, "harness")
        self.assertEqual(config.timeout_seconds, 30)
        self.assertEqual(config.max_authorities, 24)
        self.assertEqual(config.authority_chars, ai.DEFAULT_AUTHORITY_CHARS)
        restored = ai.AIConfig.from_mapping(
            json.loads(json.dumps(config.to_mapping()))
        )
        self.assertEqual(restored, config)

    def test_config_reads_nested_settings(self) -> None:
        config = ai.config_from_settings({"ai_review": {"mode": "api", "api_model": "m"}})
        self.assertEqual(config.mode, "api")
        self.assertEqual(config.api_model, "m")

    def test_config_reads_new_provider_fields(self) -> None:
        config = ai.AIConfig.from_mapping(
            {
                "mode": "anthropic",
                "harness_effort": "high",
                "claude_model": "claude-test",
                "anthropic_api_base": "https://example.test",
                "anthropic_model": "claude-x",
                "anthropic_api_key_env": "MY_ANTHROPIC_KEY",
            }
        )
        self.assertEqual(config.mode, "anthropic")
        self.assertEqual(config.harness_effort, "high")
        self.assertEqual(config.claude_model, "claude-test")
        self.assertEqual(config.anthropic_model, "claude-x")
        restored = ai.AIConfig.from_mapping(
            json.loads(json.dumps(config.to_mapping()))
        )
        self.assertEqual(restored, config)

    def test_default_config_prefers_free_account(self) -> None:
        with mock.patch.object(ai, "account_auth_available", return_value=True):
            self.assertEqual(ai.default_config().mode, "harness")
        with mock.patch.object(ai, "account_auth_available", return_value=False), \
             mock.patch.object(ai, "find_claude_command", return_value=r"C:\claude.cmd"):
            config = ai.default_config()
            self.assertEqual(config.mode, "claude-cli")
            self.assertEqual(config.claude_command, r"C:\claude.cmd")
        with mock.patch.object(ai, "account_auth_available", return_value=False), \
             mock.patch.object(ai, "find_claude_command", return_value=None):
            self.assertEqual(ai.default_config().mode, "off")

    def test_headless_patch_records_effort(self) -> None:
        with workspace_tempdir() as folder:
            with mock.patch.object(ai, "account_auth_available", return_value=True), \
                 mock.patch.object(ai.storage, "app_data_dir", return_value=folder):
                path = ai.headless_account_patch("high")
                self.assertIsNotNone(path)
                self.assertIn(
                    "reasoningEffort: high", path.read_text(encoding="utf-8")
                )
                path = ai.headless_account_patch("off")
                self.assertIn(
                    "reasoningEffort: off", path.read_text(encoding="utf-8")
                )

    # -- harness plumbing ---------------------------------------------------
    def test_find_harness_command_uses_explicit_or_env(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".cmd", delete=False) as handle:
            path = handle.name
        try:
            old = os.environ.get("TREC_DSH_COMMAND")
            os.environ["TREC_DSH_COMMAND"] = path
            try:
                self.assertEqual(ai.find_harness_command(""), path)
                self.assertEqual(ai.find_harness_command(path), path)
            finally:
                if old is None:
                    os.environ.pop("TREC_DSH_COMMAND", None)
                else:
                    os.environ["TREC_DSH_COMMAND"] = old
        finally:
            Path(path).unlink(missing_ok=True)

    def test_harness_argv(self) -> None:
        cmd, env = ai._resolve_harness_argv(r"C:\tools\dsh.cmd")
        self.assertEqual(cmd[:3], ["cmd.exe", "/c", r"C:\tools\dsh.cmd"])
        self.assertIn("--profile", cmd)
        self.assertEqual(cmd[-1], "-")
        self.assertEqual(env, {})
        exe, env = ai._resolve_harness_argv(r"C:\tools\dsh.exe")
        self.assertEqual(exe[0], r"C:\tools\dsh.exe")
        self.assertNotIn("cmd.exe", exe)
        self.assertEqual(env, {})

    def test_patch_insertion_shape(self) -> None:
        patch = Path(r"C:\patch.yml")
        direct = ai._with_patch(["app.exe", "--profile", "headless", "-"], patch)
        self.assertEqual(direct[1:3], ["--patch", str(patch)])
        electron = ai._with_patch(
            ["app.exe", "--expose-internals", "cli.js", "--profile", "headless", "-"],
            patch,
        )
        self.assertEqual(electron[3:5], ["--patch", str(patch)])

    def test_harness_launcher_is_translated_to_electron(self) -> None:
        with workspace_tempdir() as root:
            (root / "resources" / "runtime" / "cli" / "bin").mkdir(parents=True)
            (root / "DeepSeek Harness.exe").write_bytes(b"stub")
            (root / "resources" / "app.asar").write_bytes(b"stub-archive")
            launcher = root / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd"
            launcher.write_text("@echo off\r\n", encoding="utf-8")
            argv, env = ai._resolve_harness_argv(str(launcher))
            self.assertEqual(argv[0], str(root / "DeepSeek Harness.exe"))
            self.assertEqual(argv[1], "--expose-internals")
            self.assertIn("app.asar", argv[2])
            self.assertEqual(env, {"ELECTRON_RUN_AS_NODE": "1"})

    def test_run_process_success(self) -> None:
        code = "import sys; sys.stdout.write(sys.stdin.read().upper())"
        text, _stderr, code_value = ai._run_process(
            [sys.executable, "-c", code], "hello", timeout=15, cancel=None
        )
        self.assertEqual(text, "HELLO")
        self.assertEqual(code_value, 0)

    def test_run_process_timeout(self) -> None:
        code = "import time; time.sleep(5)"
        with self.assertRaises(ai.AIReviewError) as caught:
            ai._run_process(
                [sys.executable, "-c", code], "", timeout=1, cancel=None
            )
        self.assertEqual(caught.exception.code, "harness_timeout")

    def test_run_process_cancel(self) -> None:
        cancel = threading.Event()
        code = "import time; time.sleep(5)"
        threading.Timer(0.4, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(ai.AIReviewError) as caught:
            ai._run_process(
                [sys.executable, "-c", code], "", timeout=20, cancel=cancel
            )
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertLess(time.monotonic() - started, 4)

    # -- Claude Code --------------------------------------------------------
    def test_find_claude_command_uses_env(self) -> None:
        with workspace_tempdir() as folder:
            launcher = folder / "claude.cmd"
            launcher.write_text("@echo off\r\n", encoding="utf-8")
            old = os.environ.get("CLAUDE_CODE_COMMAND")
            os.environ["CLAUDE_CODE_COMMAND"] = str(launcher)
            try:
                self.assertEqual(ai.find_claude_command(""), str(launcher))
            finally:
                if old is None:
                    os.environ.pop("CLAUDE_CODE_COMMAND", None)
                else:
                    os.environ["CLAUDE_CODE_COMMAND"] = old

    def test_claude_argv(self) -> None:
        config = ai.AIConfig(claude_model="claude-test")
        argv = ai._claude_argv(r"C:\tools\claude.cmd", config)
        self.assertEqual(argv[:3], ["cmd.exe", "/c", r"C:\tools\claude.cmd"])
        self.assertIn("-p", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("--model", argv)
        self.assertIn("claude-test", argv)
        self.assertIn("--tools", argv)
        self.assertEqual(argv[-1], "")
        no_tools = ai._claude_argv(
            r"C:\tools\claude.cmd", config, disable_tools=False
        )
        self.assertNotIn("--tools", no_tools)

    def test_parse_claude_output(self) -> None:
        ok = json.dumps({"type": "result", "subtype": "success", "result": "hello"})
        self.assertEqual(ai._parse_claude_output(ok), "hello")
        self.assertEqual(ai._parse_claude_output("plain answer"), "plain answer")
        error = json.dumps({"is_error": True, "result": "bad"})
        with self.assertRaises(ai.AIReviewError) as caught:
            ai._parse_claude_output(error)
        self.assertEqual(caught.exception.code, "claude_failed")

    def test_claude_missing_is_reported(self) -> None:
        with mock.patch.object(ai, "find_claude_command", return_value=None):
            with self.assertRaises(ai.AIReviewError) as caught:
                ai.run_claude_cli("prompt", ai.AIConfig(mode="claude-cli"))
        self.assertEqual(caught.exception.code, "no_claude")

    def test_claude_cli_roundtrip_with_fake_launcher(self) -> None:
        with workspace_tempdir() as folder:
            answer = folder / "answer.py"
            answer.write_text(
                "import sys, json\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'type': 'result', 'subtype': 'success', "
                "'result': json.dumps(" + json.dumps(VALID) + ")}))\n",
                encoding="utf-8",
            )
            launcher = folder / "claude.cmd"
            launcher.write_text(
                f'@echo off\r\n"{sys.executable}" "{answer}" %*\r\n',
                encoding="utf-8",
            )
            config = ai.AIConfig(
                mode="claude-cli",
                claude_command=str(launcher),
                timeout_seconds=30,
            )
            text = ai.run_claude_cli("prompt", config)
            analysis = ai.parse_analysis(text, self.packet)
            self.assertEqual(len(analysis.red_flags), 3)

    # -- Anthropic ----------------------------------------------------------
    def test_anthropic_roundtrip(self) -> None:
        _CompletionHandler.last_headers = {}
        _CompletionHandler.last_body = {}
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CompletionHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            config = ai.AIConfig(
                mode="anthropic",
                anthropic_api_base=f"http://127.0.0.1:{server.server_port}",
                anthropic_model="claude-test",
                anthropic_api_key="sk-ant-test",
                timeout_seconds=10,
            )
            analysis = ai.run_review(self.packet, config)
            self.assertEqual(analysis.model, "claude-test")
            self.assertEqual(len(analysis.red_flags), 3)
            self.assertEqual(
                _CompletionHandler.last_headers.get("x-api-key"), "sk-ant-test"
            )
            self.assertEqual(
                _CompletionHandler.last_body.get("model"), "claude-test"
            )
            self.assertIn(
                "messages", _CompletionHandler.last_body
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_anthropic_missing_key(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            config = ai.AIConfig(mode="anthropic")
            with self.assertRaises(ai.AIReviewError) as caught:
                ai.run_review(self.packet, config)
        self.assertEqual(caught.exception.code, "no_api_key")

    # -- Gemini -------------------------------------------------------------
    def test_gemini_roundtrip(self) -> None:
        _CompletionHandler.last_headers = {}
        _CompletionHandler.last_body = {}
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CompletionHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            config = ai.AIConfig(
                mode="gemini",
                gemini_api_base=f"http://127.0.0.1:{server.server_port}/v1beta",
                gemini_model="gemini-3.5-flash",
                gemini_api_key="g-key",
                timeout_seconds=10,
            )
            analysis = ai.run_review(self.packet, config)
            self.assertEqual(analysis.model, "gemini-3.5-flash")
            self.assertEqual(len(analysis.red_flags), 3)
            self.assertEqual(
                _CompletionHandler.last_headers.get("x-goog-api-key"), "g-key"
            )
            self.assertEqual(
                _CompletionHandler.last_body.get("generationConfig", {}).get(
                    "responseMimeType"
                ),
                "application/json",
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_gemini_disabled_is_explained(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CompletionHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            config = ai.AIConfig(
                mode="gemini",
                gemini_api_base=f"http://127.0.0.1:{server.server_port}/v1beta",
                gemini_model="disabled-model",
                gemini_api_key="g-key",
                timeout_seconds=10,
            )
            with self.assertRaises(ai.AIReviewError) as caught:
                ai.run_gemini("prompt", config)
            self.assertEqual(caught.exception.code, "gemini_disabled")
            self.assertIn("generativelanguage.googleapis.com", caught.exception.detail)
        finally:
            server.shutdown()
            server.server_close()

    def test_gemini_missing_key(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            config = ai.AIConfig(mode="gemini")
            with self.assertRaises(ai.AIReviewError) as caught:
                ai.run_review(self.packet, config)
        self.assertEqual(caught.exception.code, "no_api_key")

    def test_default_config_uses_gemini_key_env(self) -> None:
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g-key"}, clear=False):
            self.assertEqual(ai.default_config().mode, "gemini")

    # -- API ----------------------------------------------------------------
    def test_api_roundtrip(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CompletionHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = ai.AIConfig(
                mode="api",
                api_base_url=f"http://127.0.0.1:{server.server_port}/v1",
                api_model="test-model",
                api_key="test-key",
                timeout_seconds=10,
            )
            content = ai.run_api("prompt", config)
            self.assertIn("verdict", content)
            analysis = ai.run_review(self.packet, config)
            self.assertEqual(analysis.model, "test-model")
            self.assertEqual(len(analysis.red_flags), 3)
        finally:
            server.shutdown()
            server.server_close()

    def test_api_error_is_reported(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CompletionHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            config = ai.AIConfig(
                mode="api",
                api_base_url=f"http://127.0.0.1:{server.server_port}/unauthorized",
                api_key="bad",
                timeout_seconds=10,
            )
            with self.assertRaises(ai.AIReviewError) as caught:
                ai.run_api("prompt", config)
            self.assertEqual(caught.exception.code, "http")
        finally:
            server.shutdown()
            server.server_close()

    # -- CLI ----------------------------------------------------------------
    def test_cli_packet_and_review(self) -> None:
        with workspace_tempdir() as base:
            (base / "a.txt").write_text(A, encoding="utf-8")
            (base / "b.txt").write_text(B, encoding="utf-8")
            packet_path = base / "packet.json"
            answer_path = base / "answer.json"
            review_path = base / "review.md"
            answer_path.write_text(json.dumps(VALID), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = ai_cli.main(
                    [
                        "packet",
                        "--a-file",
                        str(base / "a.txt"),
                        "--b-file",
                        str(base / "b.txt"),
                        "--out",
                        str(packet_path),
                        "--max-authorities",
                        "6",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(packet_path.exists())

            with contextlib.redirect_stdout(io.StringIO()):
                code = ai_cli.main(
                    [
                        "review",
                        "--packet",
                        str(packet_path),
                        "--response",
                        str(answer_path),
                        "--out",
                        str(review_path),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("AI review", review_path.read_text(encoding="utf-8"))

    def test_cli_review_bad_answer(self) -> None:
        with workspace_tempdir() as base:
            packet_path = base / "packet.json"
            packet_path.write_text(self.packet.to_json(), encoding="utf-8")
            bad = base / "bad.json"
            bad.write_text("not json", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                code = ai_cli.main(
                    ["review", "--packet", str(packet_path), "--response", str(bad)]
                )
            self.assertEqual(code, ai_cli.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
