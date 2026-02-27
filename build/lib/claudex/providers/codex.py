"""
Codex CLI provider.

Wraps the `codex` command-line tool (OpenAI Codex CLI).

New session:    codex exec --json "<prompt>"
Resume session: codex exec resume <session_id> --json "<prompt>"

Output is a stream of newline-delimited JSON (JSONL) events.
We parse the stream robustly, picking out the key event types:

  thread.started   → captures the session/thread ID
  item.completed   → if item.type == "agent_message", extracts the text
  error            → classifies the failure

Example JSONL stream (abbreviated):
  {"type":"thread.started","thread_id":"thread_abc123"}
  {"type":"item.completed","item":{"type":"agent_message","content":[{"type":"output_text","text":"Hello!"}]}}
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

from .base import BaseProvider, ProviderResult
from ..models import ErrorClass


class CodexProvider(BaseProvider):
    name = "codex"

    def run(
        self,
        prompt: str,
        session_id: Optional[str],
        config: dict,
    ) -> ProviderResult:
        """
        Build and execute the `codex exec` command, then parse the JSONL output.
        """
        codex_cfg = config.get("codex", {})
        cmd = ["codex", "exec"]

        model = codex_cfg.get("model")
        if model:
            cmd.extend(["--model", model])

        # Valid codex exec modes:
        #   --sandbox {read-only|workspace-write|danger-full-access}
        #   --full-auto
        #   --dangerously-bypass-approvals-and-sandbox
        sandbox = codex_cfg.get("sandbox", "read-only")
        if sandbox in {"read-only", "workspace-write", "danger-full-access"}:
            cmd.extend(["--sandbox", sandbox])
        elif sandbox == "full-auto":
            cmd.append("--full-auto")
        elif sandbox == "dangerously-bypass-approvals-and-sandbox":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            # Invalid values fall back to read-only for safety.
            cmd.extend(["--sandbox", "read-only"])

        if session_id:
            # Resume an existing session
            cmd.extend(["resume", session_id])

        # --json flag tells codex to emit JSONL events
        cmd.append("--json")
        cmd.append(prompt)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return ProviderResult(
                success=False,
                error_class=ErrorClass.OTHER_ERROR,
                error_message="Codex CLI timed out after 5 minutes.",
            )
        except FileNotFoundError:
            return ProviderResult(
                success=False,
                error_class=ErrorClass.OTHER_ERROR,
                error_message=(
                    "'codex' command not found. "
                    "Install with: npm i -g @openai/codex"
                ),
            )

        return self._parse_jsonl(proc)

    # ── JSONL parsing ─────────────────────────────────────────────────────────

    def _parse_jsonl(self, proc: subprocess.CompletedProcess) -> ProviderResult:
        """
        Walk the JSONL event stream and extract:
          - thread_id from thread.started
          - assistant text from the last item.completed with type==agent_message
          - error info from error events
        Non-JSON lines are silently skipped (codex may emit progress lines).
        """
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        raw = stdout + stderr

        thread_id: Optional[str] = None
        assistant_text: Optional[str] = None
        last_error: Optional[dict] = None

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # Not a JSON event line — skip

            event_type = event.get("type", "")

            if event_type == "thread.started":
                # Capture whichever id field codex uses (field name varies by version)
                thread_id = (
                    event.get("thread_id")
                    or event.get("id")
                    or event.get("session_id")
                )

            elif event_type == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    # content is a list of blocks; concatenate all text blocks
                    parts: list[str] = []
                    for block in item.get("content", []):
                        if isinstance(block, dict):
                            # Different field names across codex versions
                            text = (
                                block.get("text")
                                or block.get("output_text")
                                or ""
                            )
                            if text:
                                parts.append(text)
                    if parts:
                        # Keep the LAST agent_message (final answer)
                        assistant_text = "\n".join(parts)

            elif event_type == "error":
                last_error = event

        # ── Determine result ──────────────────────────────────────────────────

        if last_error:
            error_class = self._classify_error_event(last_error)
            message = last_error.get("message") or str(last_error)
            return ProviderResult(
                success=False,
                session_id=thread_id,
                error_class=error_class,
                error_message=message[:800],
                raw_output=raw,
            )

        if proc.returncode != 0 and not assistant_text:
            # Non-zero exit but no error event parsed — classify from raw text
            error_class = self._classify_text(raw, proc.returncode)
            return ProviderResult(
                success=False,
                session_id=thread_id,
                error_class=error_class,
                error_message=(raw[:800] if raw else "Unknown error from Codex CLI"),
                raw_output=raw,
            )

        if assistant_text:
            return ProviderResult(
                success=True,
                text=assistant_text,
                session_id=thread_id,
                raw_output=raw,
            )

        # Edge case: exit 0, no error, but also no assistant message
        return ProviderResult(
            success=False,
            session_id=thread_id,
            error_class=ErrorClass.OTHER_ERROR,
            error_message="No assistant message found in Codex JSONL output.",
            raw_output=raw,
        )

    # ── Error classification ──────────────────────────────────────────────────

    def _classify_error_event(self, event: dict) -> ErrorClass:
        message = (event.get("message") or "").lower()
        status = event.get("status", 0)

        # 429 can be either quota-exhausted or transient rate limit —
        # distinguish by checking the message content
        if status == 429 or "rate limit" in message or "quota" in message:
            if "quota" in message or "usage limit" in message or "exhausted" in message:
                return ErrorClass.QUOTA_EXHAUSTED
            return ErrorClass.TRANSIENT_RATE_LIMIT

        if status == 401 or "unauthorized" in message or "authentication" in message:
            return ErrorClass.AUTH_REQUIRED

        return ErrorClass.OTHER_ERROR

    def _classify_text(self, text: str, exit_code: int) -> ErrorClass:
        """Fallback classifier when there is no structured error event."""
        lower = text.lower()

        if "quota" in lower or "usage limit" in lower or "exhausted" in lower:
            return ErrorClass.QUOTA_EXHAUSTED

        if "rate limit" in lower or "429" in text or "too many requests" in lower:
            return ErrorClass.TRANSIENT_RATE_LIMIT

        if "unauthorized" in lower or "authentication" in lower or "401" in text:
            return ErrorClass.AUTH_REQUIRED

        return ErrorClass.OTHER_ERROR
