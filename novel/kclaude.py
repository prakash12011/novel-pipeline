"""Text written by Claude through the Claude Code CLI in headless mode (`claude -p`), on the user's Claude subscription
(never the API: ANTHROPIC_API_KEY is removed from the call's environment). Used for the scene lines:
`novel ktrial scenes --writer sonnet`, and in `novel write` after the free Gemini keys (gemini.scene_fallback).

Every call is a fresh, single-turn session that is not saved (--no-session-persistence): nothing to close or delete,
nothing carried from one chapter to the next. Our rules are the whole system prompt (--system-prompt-file), with no
tools, no settings, hooks or MCP servers of the user's own setup (--setting-sources "", --strict-mcp-config), in an empty
folder (no CLAUDE.md). CLAUDE_CODE_OAUTH_TOKEN_HEADLESS in secrets.env (made with `claude setup-token` while logged into
another account) runs the calls as that account; without it, the account Claude Code is logged into.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

MODEL = "claude-sonnet-5-5"     # by full name: the alias `sonnet` of this CLI answered with Sonnet 4.6 (2026-11-18)
EFFORT = "medium"
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN_HEADLESS"


class ClaudeError(RuntimeError):
    """The headless call gave no usable answer (not installed, not logged in, a limit, an error result)."""


def headless_token() -> str:
    from novel.core import secrets
    return os.environ.get(TOKEN_VAR, "").strip() or secrets.load().get(TOKEN_VAR, "").strip()


class ClaudeHeadless:
    """Shaped like episode_translate.Gemini.generate, so the scene writers can use it. `run` is replaced in tests."""

    def __init__(self, model: str = MODEL, effort: str = EFFORT, run=subprocess.run, timeout: float = 1800):
        self.model, self.effort, self.run, self.timeout = model, effort, run, timeout
        self.exe = shutil.which("claude")
        if not self.exe:
            raise ClaudeError("the `claude` command (Claude Code) is not installed or not on PATH")
        self.cost = 0.0                 # what the calls would cost on the API (they are on the subscription)

    def generate(self, system: str, text: str, *, temperature: float = 0.3, max_output_tokens: int = 32000,
                 json_out: bool = False, image: Path | str | None = None):
        """`image`: a picture file, read in place (never copied) by Claude with the Read tool, the only tool then
        enabled, with read access to that file's folder only (picture QA). Without it no tool is enabled."""
        from novel.stages.episode_translate import GenResult
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}    # the subscription, never the API
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output_tokens)
        token = headless_token()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "system.txt"
            sp.write_text(system, encoding="utf-8")
            tools = ["--tools", ""]
            if image is not None:
                image = Path(image).resolve()
                text = f"The picture to look at is the file {image}: read it first.\n\n" + text
                tools = ["--tools", "Read", "--allowedTools", "Read", "--add-dir", str(image.parent)]
            cmd = [self.exe, "-p", "--model", self.model, "--effort", self.effort, "--system-prompt-file", str(sp),
                   *tools, "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
                   "--output-format", "json"]
            try:
                p = self.run(cmd, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             env=env, cwd=tmp, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                raise ClaudeError(f"claude -p gave no answer in {self.timeout:.0f} s") from None
        try:
            d = json.loads(p.stdout)
        except ValueError:
            raise ClaudeError(f"claude -p exit {p.returncode}: {(p.stderr or p.stdout)[:300]}") from None
        if d.get("is_error") or d.get("subtype") not in (None, "success"):
            raise ClaudeError(f"claude -p: {str(d.get('result') or d.get('subtype'))[:300]}")
        u = d.get("usage") or {}
        self.cost += float(d.get("total_cost_usd") or 0)
        used = ",".join(d.get("modelUsage") or {}) or self.model
        return GenResult(text=d.get("result") or "", finish="MAX_TOKENS" if d.get("stop_reason") == "max_tokens"
                         else "STOP", model=f"claude-code:{used}", key=0, seconds=round(time.monotonic() - t0, 1),
                         usage={"promptTokenCount": int(u.get("input_tokens") or 0)
                                + int(u.get("cache_read_input_tokens") or 0)
                                + int(u.get("cache_creation_input_tokens") or 0),
                                "candidatesTokenCount": int(u.get("output_tokens") or 0),
                                "cost": d.get("total_cost_usd")})
