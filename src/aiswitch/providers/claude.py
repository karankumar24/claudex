"""
Claude Code CLI provider.

Wraps the `claude` command-line tool installed via `npm i -g @anthropic-ai/claude-code`.

New session:    claude -p "<prompt>" --output-format json
Resume session: claude -r <session_id> -p "<prompt>" --output-format json

Output format (--output-format json) returns a single JSON object:
  {
    "type": "result",
    "result": "<assistant response text>",
    "session_id": "...",
    "is_error": false,
    "cost_usd": 0.012,
    ...
  }
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

from .base import BaseProvider, ProviderResult
from ..models import ErrorClass

# ── Error pattern matching ────────────────────────────────────────────────────

# Strings that indicate the monthly usage plan is exhausted (case-insensitive match)
_QUOTA_PATTERNS = [
    "usage limit reached",
    "claude.ai/settings/limits",
    "you've reached your",
    "monthly limit",
]

# Strings that indicate authentication problems
_AUTH_PATTERNS = [
    "not authenticated",
    "authentication required",
    "invalid api key",
    "please run: claude login",
    "log in to",
    "unauthorized",
]

# Strings indicating a transient rate limit (will retry)
_RATE_LIMIT_PATTERNS = [
    "rate limit",
    "too many requests",
    "overloaded",
]


class ClaudeProvider(BaseProvider):
    name = "claude"

    def run(
        self,
        prompt: str,
        session_id: Optional[str],
        config: dict,
    ) -> ProviderResult:
        """
        Build and execute the `claude` command, then parse the JSON result.
        """
        cmd = ["claude"]

        # Resume an existing conversation if we have a session ID
        if session_id:
            cmd.extend(["-r", session_id])

        # Core flags: non-interactive prompt with JSON output
        cmd.extend(["-p", prompt, "--output-format", "json"])

        # Optional tool allowlist from config
        for tool in config.get("claude", {}).get("allowed_tools", []):
            cmd.extend(["--allowedTools", tool])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5-minute hard limit per turn
            )
        except subprocess.TimeoutExpired:
            return ProviderResult(
                success=False,
                error_class=ErrorClass.OTHER_ERROR,
                error_message="Claude CLI timed out after 5 minutes.",
            )
        except FileNotFoundError:
            return ProviderResult(
                success=False,
                error_class=ErrorClass.OTHER_ERROR,
                error_message=(
                    "'claude' command not found. "
                    "Install with: npm i -g @anthropic-ai/claude-code"
                ),
            )

        raw = (proc.stdout or "") + (proc.stderr or "")
        return self._parse(proc, raw)

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse(
        self, proc: subprocess.CompletedProcess, raw: str
    ) -> ProviderResult:
        stdout = (proc.stdout or "").strip()

        # Try JSON parse first (the happy path with --output-format json)
        parsed: Optional[dict] = None
        if stdout:
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                pass

        if parsed is not None:
            is_error = parsed.get("is_error", False)
            text = parsed.get("result", "")
            session_id = parsed.get("session_id")

            if not is_error and text:
                return ProviderResult(
                    success=True,
                    text=text,
                    session_id=session_id,
                    raw_output=raw,
                )

            # is_error=True inside the JSON envelope
            error_msg = text or raw
            return ProviderResult(
                success=False,
                session_id=session_id,
                error_class=self._classify(error_msg, proc.returncode),
                error_message=error_msg[:800],
                raw_output=raw,
            )

        # No valid JSON — fall back to text-based classification
        if proc.returncode == 0 and stdout:
            # Plain-text success (shouldn't happen with --output-format json, but be safe)
            return ProviderResult(success=True, text=stdout, raw_output=raw)

        return ProviderResult(
            success=False,
            error_class=self._classify(raw, proc.returncode),
            error_message=(raw[:800] if raw else "Unknown error from Claude CLI"),
            raw_output=raw,
        )

    def _classify(self, text: str, exit_code: int) -> ErrorClass:
        lower = text.lower()

        if any(p in lower for p in _QUOTA_PATTERNS):
            return ErrorClass.QUOTA_EXHAUSTED

        if any(p in lower for p in _AUTH_PATTERNS):
            return ErrorClass.AUTH_REQUIRED

        if any(p in lower for p in _RATE_LIMIT_PATTERNS):
            return ErrorClass.TRANSIENT_RATE_LIMIT

        return ErrorClass.OTHER_ERROR
