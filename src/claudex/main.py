"""
claudex CLI entry point.

Commands
--------
  claudex chat           — interactive REPL loop
  claudex ask "<prompt>" — single-turn one-shot mode
  claudex status         — show provider state, sessions, cooldowns
  claudex reset          — clear .claudex/ for the current repo
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .config import load_config
from .handoff import update_handoff
from .models import Provider
from .router import get_available_providers, run_with_retry
from .state import (
    CLAUDEX_DIR,
    clear_claudex,
    load_handoff,
    load_state,
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


# ── Shared turn executor ──────────────────────────────────────────────────────


def _run_turn(user_prompt: str, config: dict) -> tuple[bool, Optional[Provider]]:
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
    previous_provider = state.last_provider
    handoff_content = load_handoff()

    result, provider, updated_state = run_with_retry(
        user_prompt=user_prompt,
        state=state,
        config=config,
        handoff_content=handoff_content,
    )

    save_state(updated_state)

    if result is None:
        err_console.print(
            "\n[bold red]✗ All providers are in cooldown.[/bold red] "
            "Run [bold]claudex status[/bold] to see timers.\n"
        )
        return False, None

    # ── Show switch notice BEFORE the response ────────────────────────────────
    if (
        previous_provider is not None
        and provider is not None
        and previous_provider != provider
    ):
        err_console.print(
            f"\n[bold yellow]⚡ {previous_provider.value} unavailable — "
            f"switching to {provider.value} (context injected)[/bold yellow]\n"
        )

    if result.success:
        # Print which provider answered, then the response
        console.print(f"\n[dim]◆ {provider.value}[/dim]\n")
        console.print(Markdown(result.text or ""))

        # Update the rolling handoff summary
        new_handoff = update_handoff(
            user_prompt=user_prompt,
            assistant_text=result.text or "",
            provider=provider.value,
            config=config,
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
        )
        return True, provider

    else:
        # Surface the classified error
        err_console.print(
            f"\n[bold red]✗ {provider.value if provider else '?'} error[/bold red] "
            f"[{result.error_class.value if result.error_class else 'UNKNOWN'}] "
            f"{result.error_message}\n"
        )
        record_turn(
            provider=provider,
            user_prompt=user_prompt,
            assistant_text=None,
            error=(
                f"{result.error_class.value}: {result.error_message}"
                if result.error_class
                else str(result.error_message)
            ),
        )
        return False, provider


# ── Commands ──────────────────────────────────────────────────────────────────


@app.command()
def chat() -> None:
    """
    Start an interactive REPL.
    Each prompt you type is routed to the best available provider.
    Failover is automatic — you'll see a notice if the active provider changes.
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
            _run_turn(user_input, config)
        except Exception as exc:  # noqa: BLE001
            err_console.print(f"\n[bold red]Unexpected error:[/bold red] {exc}\n")


@app.command()
def ask(
    prompt: str = typer.Argument(..., help="Prompt to send to the best available provider."),
) -> None:
    """
    Send a single prompt (one-shot mode) and print the response.
    Exits with code 1 on error.
    """
    config = load_config()
    success, _ = _run_turn(prompt, config)
    if not success:
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """
    Print the current provider state: preference order, session IDs, cooldowns.
    """
    config = load_config()
    state = load_state()
    now = datetime.now(timezone.utc)

    console.print()

    # ── Summary row ───────────────────────────────────────────────────────────
    available = get_available_providers(state, config, now=now)
    active = state.last_provider.value if state.last_provider else "none"
    avail_names = ", ".join(p.value for p in available) or "none"

    console.print(f"[bold]Last provider:[/bold] {active}")
    console.print(f"[bold]Available:[/bold]     {avail_names}")
    console.print(f"[bold]Total turns:[/bold]   {state.turn_count}")
    console.print()

    # ── Per-provider table ────────────────────────────────────────────────────
    provider_order = config.get("provider_order", ["claude", "codex"])
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Status")
    table.add_column("Session ID")
    table.add_column("Last Used")
    table.add_column("Cooldown")

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
        cooldown_str = "—"
        if in_cooldown and ps.cooldown_until:
            remaining = ps.cooldown_until - now
            mins = int(remaining.total_seconds() / 60)
            cooldown_str = f"[yellow]{mins} min[/yellow]"

        table.add_row(p_name, status_str, session_str, last_used_str, cooldown_str)

    console.print(table)
    console.print()


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
