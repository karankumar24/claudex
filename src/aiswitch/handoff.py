"""
Handoff management and git snapshot generation.

handoff.md is the primary mechanism for transferring context between providers.
It lives at .aiswitch/handoff.md and is OVERWRITTEN each turn (not appended)
so it stays compact and under the configured line limit.

When switching providers, the router prepends handoff.md content + a git
snapshot to the outgoing prompt so the new provider picks up exactly where
the previous one left off.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Optional

# ── Git snapshot ──────────────────────────────────────────────────────────────


def get_repo_snapshot(config: dict) -> str:
    """
    Build a compact Markdown git snapshot for context injection.

    Includes:
      - git status --porcelain
      - git log -n 5 --oneline
      - git diff --stat
      - git diff (only if within size limits, otherwise just stat)

    Returns an empty string if we're not in a git repo.
    """
    limits = config.get("limits", {})
    max_diff_lines: int = limits.get("max_diff_lines", 200)
    max_diff_bytes: int = limits.get("max_diff_bytes", 8_000)

    # Quick check: are we in a git repo at all?
    if not _run_git(["git", "rev-parse", "--is-inside-work-tree"]):
        return ""

    parts: list[str] = ["## Repo Snapshot\n"]

    status = _run_git(["git", "status", "--porcelain"])
    if status:
        parts.append("**Status:**\n```\n" + status.strip() + "\n```\n")

    log = _run_git(["git", "log", "-n", "5", "--oneline"])
    if log:
        parts.append("**Recent commits:**\n```\n" + log.strip() + "\n```\n")

    diff_stat = _run_git(["git", "diff", "--stat"])
    if diff_stat:
        parts.append("**Diff stat:**\n```\n" + diff_stat.strip() + "\n```\n")

    diff = _run_git(["git", "diff"])
    if diff:
        n_lines = diff.count("\n")
        n_bytes = len(diff.encode("utf-8"))
        if n_lines <= max_diff_lines and n_bytes <= max_diff_bytes:
            parts.append("**Full diff:**\n```diff\n" + diff.strip() + "\n```\n")
        else:
            parts.append(
                f"**Full diff omitted** ({n_lines} lines, {n_bytes} bytes). "
                "Inspect individual files as needed.\n"
            )

    return "\n".join(parts)


# ── Prompt assembly ───────────────────────────────────────────────────────────


def build_provider_prompt(
    user_prompt: str,
    config: dict,
    is_resuming: bool = False,
    handoff_content: Optional[str] = None,
) -> str:
    """
    Build the full prompt string to send to a provider.

    When is_resuming=True (i.e. we are switching providers mid-session or
    starting fresh on a provider that has no session context), we prepend:
      1. The handoff.md content (current goal, plan, last exchange)
      2. A live git snapshot (status, log, diff)
      3. The user's actual prompt

    When is_resuming=False (continuing on the same provider with an active
    session), we pass the prompt through unchanged — the provider's own
    session history already contains the context.
    """
    if not is_resuming:
        return user_prompt

    sections: list[str] = []

    if handoff_content:
        sections.append("## Context Handoff (from previous session)\n\n" + handoff_content)

    snapshot = get_repo_snapshot(config)
    if snapshot:
        sections.append(snapshot)

    sections.append("## Current Task\n\n" + user_prompt)

    return "\n\n---\n\n".join(sections)


# ── Handoff update ────────────────────────────────────────────────────────────


def update_handoff(
    user_prompt: str,
    assistant_text: str,
    provider: str,
    config: dict,
    previous_handoff: Optional[str] = None,
) -> str:
    """
    Generate a fresh handoff.md that captures the current session state.

    The format is deliberately structured so that the receiving provider
    can immediately understand:
      - What the user is ultimately trying to achieve (Current Goal)
      - What the high-level plan is (Current Plan)
      - What just happened in the last exchange (What Changed This Turn)
      - What is blocking progress (Open Questions / Blockers)
      - What to do next (Next Concrete Steps)

    We preserve the Goal / Plan / Blockers sections from the previous handoff
    so context isn't lost when the file is overwritten.
    """
    limits = config.get("limits", {})
    max_lines: int = limits.get("max_handoff_lines", 350)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Carry forward structured sections from the previous handoff if available
    prev_goal = _extract_section(previous_handoff or "", "Current Goal")
    prev_plan = _extract_section(previous_handoff or "", "Current Plan")
    prev_blockers = _extract_section(previous_handoff or "", "Open Questions / Blockers")

    content = f"""\
# AI Switch Handoff

*Last updated: {now} — Provider: {provider}*

## Current Goal

{prev_goal or "(not yet established — infer from the exchange below)"}

## Current Plan

{prev_plan or "(not yet established — infer from the exchange below)"}

## What Changed This Turn

**User asked:**
{_truncate(user_prompt, 600)}

**{provider} responded:**
{_truncate(assistant_text, 2000)}

## Open Questions / Blockers

{prev_blockers or "(none noted yet)"}

## Next Concrete Steps

(Derive from the assistant response above and update this section.)
"""

    return _enforce_line_limit(content, max_lines)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run_git(cmd: list[str]) -> str:
    """Run a git subcommand, return stdout on success or empty string on failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def _extract_section(text: str, section_name: str) -> str:
    """
    Extract the body of a level-2 Markdown section (## Section Name).
    Returns an empty string if the section is not found.
    """
    lines = text.splitlines()
    in_section = False
    body: list[str] = []
    for line in lines:
        if line.startswith(f"## {section_name}"):
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            body.append(line)
    return "\n".join(body).strip()


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, appending a note about how much was dropped."""
    if len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    return text[:max_chars] + f"\n…[{dropped} chars truncated]"


def _enforce_line_limit(text: str, max_lines: int) -> str:
    """
    If text exceeds max_lines, truncate the middle section.
    We keep the top third and bottom two-thirds so that the current-goal
    header and the next-steps footer are both preserved.
    """
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text

    keep_top = max_lines // 3
    keep_bottom = max_lines - keep_top - 3
    dropped = len(lines) - keep_top - keep_bottom

    trimmed = (
        lines[:keep_top]
        + ["", f"[… {dropped} lines omitted to stay within the {max_lines}-line limit …]", ""]
        + lines[-keep_bottom:]
    )
    return "\n".join(trimmed)
