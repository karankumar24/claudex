# claudex

Local developer tool that sits in front of `claude` (Claude Code CLI) and `codex` (Codex CLI) and **automatically fails over between them** when one hits a usage limit, with full context continuity so the receiving tool knows exactly what was happening.

## How it works

```
you ──► claudex ──► claude   (preferred)
                       │ quota hit
                       ▼
                     codex    (fallback — receives handoff.md + git snapshot)
```

On every successful turn, `claudex` writes:
- `.claudex/state.json` — sessions, cooldowns, turn count
- `.claudex/handoff.md` — rolling structured summary (goal / plan / last exchange / next steps)
- `.claudex/transcript.ndjson` — append-only log of every turn

When a failover happens, the new provider receives the handoff summary and a live git snapshot (status, log, diff) prepended to your prompt so it picks up without missing a beat.
Fallback attempts always start a fresh session on the fallback provider to avoid stale cross-task thread contamination.

## Install

```bash
# From this repo — editable install works from any directory
pip install -e /path/to/claudex

# Or with pipx for isolation
pipx install -e /path/to/claudex
```

Requires Python 3.11+ and both CLIs installed:
```bash
npm i -g @anthropic-ai/claude-code   # installs `claude`
npm i -g @openai/codex               # installs `codex`
```

## Usage

### Interactive chat (REPL)

```bash
cd your-git-repo
claudex chat
claudex chat --prefer-provider codex
claudex chat --auto-switch ask   # ask | yes | no
```

```
you> explain the auth flow in this repo
◆ claude

The authentication flow uses JWT tokens…

you> now refactor it to use sessions instead
◆ claude

⚡ claude unavailable — switching to codex (context injected)
◆ codex

I can see from the handoff that we were refactoring auth…
```

### One-shot

```bash
claudex ask "what does this function do?"
claudex ask "fix the failing test in tests/test_auth.py"
claudex ask help me with a new task   # quotes optional
```

### Check status

```bash
claudex status
claudex status --active
```

```
Last provider:   claude
Available:       claude, codex
Total turns:     12

Provider  Status     Session ID   Last Used          Cooldown  Cooldown Until                         Cooldown Source
claude    ✓ ready    sess_01abc…  2025-01-14 09:32   —         —                                      —
codex     ✗ cooldown thread_xyz…  2025-01-14 08:15   47 min    2025-01-14 10:02 UTC / 2025-01-14 02:02 PST  quota_reset_time
```

### Reset state

```bash
claudex reset          # prompts for confirmation
claudex reset --yes    # skip prompt
```

### Invisible wrappers (codex / claudecode)

Install wrapper launchers so starting `codex` or `claudecode` automatically
runs through claudex with failover + continuity:

```bash
claudex install-wrappers
```

Wrappers installed:
- `codex` -> `claudex chat/ask --prefer-provider codex`
- `claudecode` -> `claudex chat/ask --prefer-provider claude`

Set fallback policy for wrappers with environment variable:

```bash
export CLAUDEX_AUTO_SWITCH=ask   # ask | yes | no
```

Remove wrappers:

```bash
claudex uninstall-wrappers
```

## Configuration

Create `.claudex/config.toml` in your repo (or `~/.config/claudex/config.toml` for global defaults):

```toml
# Provider preference order
provider_order = ["claude", "codex"]

[claude]
# Extra tools to allow (e.g. for file editing)
allowed_tools = ["Bash", "Edit", "Read"]

[codex]
model = "o4-mini"       # override model; omit to use codex default
sandbox = "read-only"   # "read-only" | "workspace-write" | "danger-full-access" | "full-auto"

[limits]
max_diff_lines = 200    # lines of git diff to include in context
max_diff_bytes = 8000   # bytes cap (whichever is smaller wins)
max_handoff_lines = 350 # rolling handoff.md size cap

[retry]
max_retries = 3         # TRANSIENT_RATE_LIMIT retries before switching
backoff_base = 2.0      # exponential backoff base (seconds)
backoff_max = 30.0      # max single wait
cooldown_minutes = 60   # fallback cooldown for QUOTA_EXHAUSTED when reset time is unavailable
transient_cooldown_minutes = 5 # cooldown after exhausted transient retries

[switch]
confirmation = "ask"    # ask | yes | no
```

## Error handling

| Error class | What triggers it | What claudex does |
|---|---|---|
| `QUOTA_EXHAUSTED` | "usage limit reached" in output | Switch immediately; use provider reset time when present, else fallback cooldown_minutes |
| `TRANSIENT_RATE_LIMIT` | 429 / "rate limit" | Retry with backoff up to max_retries, then switch with transient cooldown |
| `AUTH_REQUIRED` | 401 / "not authenticated" | Surface error and stop — you need to re-auth |
| `OTHER_ERROR` | CLI crash, parse failure | Surface error and stop |

## Project structure

```
src/claudex/
├── main.py         — typer CLI: chat / ask / status / wrapper install / reset
├── models.py       — pydantic models (Provider, ErrorClass, state)
├── state.py        — .claudex/ IO (state.json, handoff.md, transcript)
├── config.py       — layered config loading (defaults → user → repo)
├── router.py       — routing loop, retry/backoff, failover (heavily commented)
├── handoff.py      — handoff.md generation + git snapshot
├── transcript.py   — append-only turn logger
└── providers/
    ├── claude.py   — claude CLI wrapper + JSON parser
    └── codex.py    — codex CLI wrapper + JSONL event parser
```

## Running tests

```bash
pytest tests/ -v
```

## .claudex/ is safe to commit (or gitignore)

- **No secrets** are ever written to `.claudex/` — no API keys, tokens, or credentials.
- The transcript and handoff files may contain your prompt/response text, so add `.claudex/` to `.gitignore` if you prefer privacy.
- The state file stores session IDs, cooldown timestamps, and cooldown source metadata (for debugging failover decisions).
- `active.json` stores only temporary in-flight turn metadata while a turn is running.
