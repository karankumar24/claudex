"""
claudex CLI entry point.

Commands
--------
  claudex chat                      — interactive REPL loop
  claudex ask "<prompt>"            — single-turn one-shot mode
  claudex status [--active]         — show provider state (+ active turn metadata)
  claudex install-wrappers          — install codex/claudecode wrapper scripts
  claudex uninstall-wrappers        — remove wrapper scripts
  claudex reset                     — clear .claudex/ for the current repo
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import os
from pathlib import Path
import shlex
import stat
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .config import load_config
from .handoff import update_handoff
from .models import Provider, ProviderState
from .router import get_available_providers, run_with_retry
from .state import (
    CLAUDEX_DIR,
    clear_active_run,
    clear_claudex,
    load_active_run,
    load_handoff,
    load_state,
    save_active_run,
    save_handoff,
    save_state,
)
from .transcript import record_turn

# ── Typer app ─────────────────────────────────────────────────────────────────

app = typer.Typer(
    name="claudex",
    help="Automatic failover between Claude Code CLI and Codex CLI.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)


WRAPPER_MARKER = "CLAUDEX_WRAPPER"
DEFAULT_WRAPPER_DIR = Path.home() / ".local" / "bin"


class AutoSwitchPolicy(str, Enum):
    ASK = "ask"
    YES = "yes"
    NO = "no"


def _format_cooldown(ps: ProviderState, now: datetime) -> str:
    if not (ps.cooldown_until and ps.cooldown_until > now):
        return "—"

    remaining = ps.cooldown_until - now
    mins = max(0, int(remaining.total_seconds() / 60))
    return f"[yellow]{mins} min[/yellow]"


def _format_cooldown_until(ps: ProviderState, now: datetime) -> str:
    if not (ps.cooldown_until and ps.cooldown_until > now):
        return "—"
    until_utc = ps.cooldown_until.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    until_local = ps.cooldown_until.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return f"{until_utc} / {until_local}"


def _format_cooldown_source(ps: ProviderState, now: datetime) -> str:
    if not (ps.cooldown_until and ps.cooldown_until > now):
        return "—"
    return ps.cooldown_source or "unknown"


def _coerce_auto_switch(value: object) -> AutoSwitchPolicy:
    raw = str(value or "").strip().lower()
    if raw in ("yes", "always", "true", "1"):
        return AutoSwitchPolicy.YES
    if raw in ("no", "never", "false", "0"):
        return AutoSwitchPolicy.NO
    return AutoSwitchPolicy.ASK


def _resolve_auto_switch(
    explicit: Optional[AutoSwitchPolicy],
    config: dict,
) -> AutoSwitchPolicy:
    if explicit is not None:
        return explicit
    switch_cfg = config.get("switch", {})
    return _coerce_auto_switch(switch_cfg.get("confirmation", AutoSwitchPolicy.ASK.value))


def _with_preferred_provider(
    config: dict,
    preferred_provider: Optional[Provider],
) -> dict:
    if preferred_provider is None:
        return config

    ordered = [preferred_provider.value]
    configured = config.get("provider_order", ["claude", "codex"])
    for name in configured:
        if name not in ordered and name in {Provider.CLAUDE.value, Provider.CODEX.value}:
            ordered.append(name)

    # Ensure both providers are present in case config omitted one.
    if Provider.CLAUDE.value not in ordered:
        ordered.append(Provider.CLAUDE.value)
    if Provider.CODEX.value not in ordered:
        ordered.append(Provider.CODEX.value)

    merged = dict(config)
    merged["provider_order"] = ordered
    return merged


def _excerpt(text: str, max_len: int = 160) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_len:
        return normalized
    return normalized[:max_len] + "..."


# ── Shared turn executor ──────────────────────────────────────────────────────


def _run_turn(
    user_prompt: str,
    config: dict,
    *,
    preferred_provider: Optional[Provider] = None,
    auto_switch: Optional[AutoSwitchPolicy] = None,
    run_mode: str = "turn",
) -> tuple[bool, Optional[Provider]]:
    """
    Execute one prompt turn end-to-end:
      1. Load state + handoff.
      2. Route to best available provider (with retry/failover).
      3. Save updated state.
      4. Update handoff.md.
      5. Append to transcript.ndjson.
      6. Print the response (or error).

    Returns (success, provider_used).
    """
    state = load_state()
    handoff_content = load_handoff()
    turn_config = _with_preferred_provider(config, preferred_provider)
    switch_policy = _resolve_auto_switch(auto_switch, turn_config)

    switch_meta: dict[str, Optional[str]] = {
        "switch_from": None,
        "switch_to": None,
        "switch_prompt_decision": None,
    }

    active_state = {
        "pid": os.getpid(),
        "mode": run_mode,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider": None,
        "prompt_excerpt": _excerpt(user_prompt),
    }
    save_active_run(active_state)

    def _on_provider_start(provider: Provider) -> None:
        active_state["provider"] = provider.value
        save_active_run(active_state)

    def _confirm_switch(
        from_provider: Provider,
        to_provider: Provider,
        failed_result,
    ) -> bool:
        reason = failed_result.error_class.value if failed_result.error_class else "ERROR"
        switch_meta["switch_from"] = from_provider.value
        switch_meta["switch_to"] = to_provider.value

        if switch_policy == AutoSwitchPolicy.YES:
            approved = True
            err_console.print(
                f"\n[bold yellow]⚡ {from_provider.value} unavailable ({reason}) — "
                f"switching to {to_provider.value}.[/bold yellow]\n"
            )
        elif switch_policy == AutoSwitchPolicy.NO:
            approved = False
            err_console.print(
                f"\n[bold yellow]⚡ {from_provider.value} unavailable ({reason}) — "
                f"switch blocked by policy.[/bold yellow]\n"
            )
        else:
            if not sys.stdin.isatty():
                approved = False
                err_console.print(
                    f"\n[bold yellow]⚡ {from_provider.value} unavailable ({reason}) — "
                    f"cannot prompt in non-interactive mode.[/bold yellow]\n"
                )
            else:
                approved = typer.confirm(
                    (
                        f"⚡ {from_provider.value} unavailable ({reason}). "
                        f"Switch to {to_provider.value} and continue?"
                    ),
                    default=False,
                )

        switch_meta["switch_prompt_decision"] = "approved" if approved else "denied"
        return approved

    try:
        result, provider, updated_state = run_with_retry(
            user_prompt=user_prompt,
            state=state,
            config=turn_config,
            handoff_content=handoff_content,
            confirm_switch=_confirm_switch,
            on_provider_start=_on_provider_start,
        )
    finally:
        clear_active_run()

    save_state(updated_state)

    if result is None:
        err_console.print(
            "\n[bold red]✗ All providers are in cooldown.[/bold red] "
            "Run [bold]claudex status[/bold] to see timers.\n"
        )
        return False, None

    if result.success:
        # Print which provider answered, then the response
        console.print(f"\n[dim]◆ {provider.value}[/dim]\n")
        console.print(Markdown(result.text or ""))

        # Update the rolling handoff summary
        new_handoff = update_handoff(
            user_prompt=user_prompt,
            assistant_text=result.text or "",
            provider=provider.value,
            config=turn_config,
            previous_handoff=handoff_content,
        )
        save_handoff(new_handoff)

        # Append success entry to transcript
        ps = updated_state.get_provider_state(provider)
        record_turn(
            provider=provider,
            user_prompt=user_prompt,
            assistant_text=result.text,
            session_id=ps.session_id,
            cooldown_until=ps.cooldown_until,
            cooldown_source=ps.cooldown_source,
            cooldown_reason=ps.cooldown_reason,
            switch_from=switch_meta["switch_from"],
            switch_to=switch_meta["switch_to"],
            switch_prompt_decision=switch_meta["switch_prompt_decision"],
        )
        return True, provider

    # Surface the classified error
    err_console.print(
        f"\n[bold red]✗ {provider.value if provider else '?'} error[/bold red] "
        f"[{result.error_class.value if result.error_class else 'UNKNOWN'}] "
        f"{result.error_message}\n"
    )
    ps = updated_state.get_provider_state(provider) if provider else None
    session_id = result.session_id or (ps.session_id if ps else None)
    record_turn(
        provider=provider,
        user_prompt=user_prompt,
        assistant_text=None,
        session_id=session_id,
        cooldown_until=ps.cooldown_until if ps else None,
        cooldown_source=ps.cooldown_source if ps else None,
        cooldown_reason=ps.cooldown_reason if ps else None,
        error=(
            f"{result.error_class.value}: {result.error_message}"
            if result.error_class
            else str(result.error_message)
        ),
        switch_from=switch_meta["switch_from"],
        switch_to=switch_meta["switch_to"],
        switch_prompt_decision=switch_meta["switch_prompt_decision"],
    )
    return False, provider


def _render_active_state(entry: Optional[dict]) -> None:
    if not entry:
        console.print("[bold]Active turn:[/bold] none")
        return
    console.print("[bold]Active turn:[/bold] running")
    console.print(f"[bold]PID:[/bold]         {entry.get('pid', '—')}")
    console.print(f"[bold]Mode:[/bold]        {entry.get('mode', '—')}")
    console.print(f"[bold]Provider:[/bold]    {entry.get('provider') or 'pending'}")
    console.print(f"[bold]Started at:[/bold]  {entry.get('started_at', '—')}")
    console.print(f"[bold]Prompt:[/bold]      {entry.get('prompt_excerpt', '—')}")


def _wrapper_script(
    preferred: Provider,
    real_codex_bin: Optional[str] = None,
) -> str:
    preferred_name = preferred.value
    lines = [
        "#!/usr/bin/env sh",
        f"# {WRAPPER_MARKER}",
        "set -e",
    ]

    if preferred == Provider.CODEX:
        if real_codex_bin:
            quoted = shlex.quote(real_codex_bin)
            lines.extend(
                [
                    f"REAL_CODEX_BIN={quoted}",
                    'if [ "${CLAUDEX_INNER_PROVIDER_CALL:-0}" = "1" ]; then',
                    '  exec "$REAL_CODEX_BIN" "$@"',
                    "fi",
                ]
            )

    lines.extend(
        [
            'AUTO_SWITCH="${CLAUDEX_AUTO_SWITCH:-ask}"',
            'if [ "$#" -eq 0 ]; then',
            f'  exec claudex chat --prefer-provider {preferred_name} --auto-switch "$AUTO_SWITCH"',
            "fi",
            f'exec claudex ask --prefer-provider {preferred_name} --auto-switch "$AUTO_SWITCH" "$@"',
        ]
    )
    return "\n".join(lines) + "\n"


def _write_wrapper(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _is_claudex_wrapper(path: Path) -> bool:
    try:
        if not path.exists() or not path.is_file():
            return False
        return WRAPPER_MARKER in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _find_real_binary(name: str, wrapper_dir: Path) -> Optional[str]:
    """
    Resolve an executable for `name` from PATH, skipping claudex wrapper files.
    """
    wrapper_target = (wrapper_dir / name).resolve()
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir:
            continue
        candidate = Path(raw_dir) / name
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == wrapper_target:
            continue
        if not candidate.exists() or not candidate.is_file():
            continue
        if not os.access(candidate, os.X_OK):
            continue
        if _is_claudex_wrapper(candidate):
            continue
        return str(candidate)
    return None


# ── Commands ──────────────────────────────────────────────────────────────────


@app.command()
def chat(
    prefer_provider: Optional[Provider] = typer.Option(
        None,
        "--prefer-provider",
        help="Temporarily prioritize this provider first for the current session.",
    ),
    auto_switch: Optional[AutoSwitchPolicy] = typer.Option(
        None,
        "--auto-switch",
        help="Fallback confirmation policy: ask | yes | no.",
    ),
) -> None:
    """
    Start an interactive REPL.
    Each prompt you type is routed to the best available provider.
    Failover is automatic; in ask mode you'll be prompted before switching.
    Type 'exit' or Ctrl-C / Ctrl-D to quit.
    """
    config = load_config()

    console.print(
        Panel(
            "[bold green]claudex chat[/bold green]  "
            "[dim]Ctrl-C or 'exit' to quit[/dim]",
            title="Claudex",
            expand=False,
        )
    )

    while True:
        try:
            user_input = console.input("\n[bold cyan]you>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "/exit", "/quit"):
            console.print("[dim]Goodbye.[/dim]")
            break

        try:
            _run_turn(
                user_input,
                config,
                preferred_provider=prefer_provider,
                auto_switch=auto_switch,
                run_mode="chat",
            )
        except Exception as exc:  # noqa: BLE001
            err_console.print(f"\n[bold red]Unexpected error:[/bold red] {exc}\n")


@app.command()
def ask(
    prompt: list[str] = typer.Argument(
        ...,
        metavar="PROMPT...",
        help="Prompt to send to the best available provider.",
    ),
    prefer_provider: Optional[Provider] = typer.Option(
        None,
        "--prefer-provider",
        help="Temporarily prioritize this provider first for this turn.",
    ),
    auto_switch: Optional[AutoSwitchPolicy] = typer.Option(
        None,
        "--auto-switch",
        help="Fallback confirmation policy: ask | yes | no.",
    ),
) -> None:
    """
    Send a single prompt (one-shot mode) and print the response.
    Exits with code 1 on error.
    """
    config = load_config()
    user_prompt = " ".join(prompt).strip()
    success, _ = _run_turn(
        user_prompt,
        config,
        preferred_provider=prefer_provider,
        auto_switch=auto_switch,
        run_mode="ask",
    )
    if not success:
        raise typer.Exit(1)


@app.command()
def status(
    active: bool = typer.Option(
        False,
        "--active",
        help="Show active in-flight turn metadata if present.",
    ),
) -> None:
    """
    Print the current provider state: preference order, session IDs, cooldowns.
    """
    config = load_config()
    state = load_state()
    now = datetime.now(timezone.utc)

    console.print()

    # ── Summary row ───────────────────────────────────────────────────────────
    available = get_available_providers(state, config, now=now)
    active_provider = state.last_provider.value if state.last_provider else "none"
    avail_names = ", ".join(p.value for p in available) or "none"

    console.print(f"[bold]Last provider:[/bold] {active_provider}")
    console.print(f"[bold]Available:[/bold]     {avail_names}")
    console.print(f"[bold]Total turns:[/bold]   {state.turn_count}")

    if active:
        console.print()
        _render_active_state(load_active_run())
    console.print()

    # ── Per-provider table ────────────────────────────────────────────────────
    provider_order = config.get("provider_order", ["claude", "codex"])
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Status")
    table.add_column("Session ID")
    table.add_column("Last Used")
    table.add_column("Cooldown")
    table.add_column("Cooldown Until")
    table.add_column("Cooldown Source")

    for p_name in provider_order:
        try:
            p = Provider(p_name)
        except ValueError:
            continue

        ps = state.get_provider_state(p)
        in_cooldown = bool(ps.cooldown_until and ps.cooldown_until > now)

        status_str = "[red]✗ cooldown[/red]" if in_cooldown else "[green]✓ ready[/green]"
        session_str = f"[dim]{ps.session_id[:20]}…[/dim]" if ps.session_id else "—"
        last_used_str = (
            ps.last_used.strftime("%Y-%m-%d %H:%M") if ps.last_used else "—"
        )
        cooldown_str = _format_cooldown(ps, now)
        cooldown_until_str = _format_cooldown_until(ps, now)
        cooldown_source_str = _format_cooldown_source(ps, now)

        table.add_row(
            p_name,
            status_str,
            session_str,
            last_used_str,
            cooldown_str,
            cooldown_until_str,
            cooldown_source_str,
        )

    console.print(table)
    console.print()


@app.command("install-wrappers")
def install_wrappers(
    directory: Path = typer.Option(
        DEFAULT_WRAPPER_DIR,
        "--dir",
        help="Directory where codex/claudecode wrapper scripts will be created.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Overwrite existing files even if they are not claudex wrappers.",
    ),
) -> None:
    """
    Install invisible launcher wrappers for `codex` and `claudecode`.

    - `codex`      -> claudex chat/ask with codex preferred first
    - `claudecode` -> claudex chat/ask with claude preferred first
    """
    real_codex = _find_real_binary("codex", directory)
    if not real_codex:
        err_console.print(
            "[bold red]Could not locate the real codex binary in PATH.[/bold red]"
        )
        raise typer.Exit(1)

    wrappers = {
        "codex": _wrapper_script(Provider.CODEX, real_codex_bin=real_codex),
        "claudecode": _wrapper_script(Provider.CLAUDE),
    }

    written: list[Path] = []
    for name, content in wrappers.items():
        path = directory / name
        if path.exists() and not overwrite and not _is_claudex_wrapper(path):
            err_console.print(
                f"[bold red]Refusing to overwrite non-claudex file:[/bold red] {path}"
            )
            raise typer.Exit(1)
        _write_wrapper(path, content)
        written.append(path)

    console.print("[green]✓[/green] Installed wrappers:")
    for path in written:
        console.print(f"  - {path}")
    console.print()
    console.print(
        "[dim]Ensure this directory is first in PATH so these wrappers shadow "
        "the original binaries.[/dim]"
    )


@app.command("uninstall-wrappers")
def uninstall_wrappers(
    directory: Path = typer.Option(
        DEFAULT_WRAPPER_DIR,
        "--dir",
        help="Directory containing codex/claudecode wrapper scripts.",
    ),
) -> None:
    """
    Remove wrapper scripts previously created by `claudex install-wrappers`.
    """
    removed = 0
    for name in ("codex", "claudecode"):
        path = directory / name
        if not path.exists():
            continue
        if not _is_claudex_wrapper(path):
            console.print(f"[yellow]Skipping non-claudex file:[/yellow] {path}")
            continue
        path.unlink()
        removed += 1
        console.print(f"[green]✓[/green] Removed {path}")

    if removed == 0:
        console.print("[dim]No claudex wrappers found to remove.[/dim]")


@app.command()
def reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """
    Delete all .claudex/ state for the current repository.
    This clears sessions, handoff context, and the transcript log.
    """
    if not CLAUDEX_DIR.exists():
        console.print("[dim]Nothing to reset — .claudex/ does not exist.[/dim]")
        return

    if not yes:
        confirmed = typer.confirm(
            "Delete all .claudex/ state (sessions, handoff, transcript)?"
        )
        if not confirmed:
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    clear_claudex()
    console.print("[green]✓[/green] Cleared .claudex/ for this repository.")


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    app()


if __name__ == "__main__":
    main()
