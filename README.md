# claudex

I built claudex for a very practical reason. I use the regular $20 Claude Code and $20 Codex plans, and paying for max plans on both is just not realistic for me right now. So when one tool hits its limit, I switch to the other and keep working.

That worked, but the context switching got old fast. I kept repeating the same task background, goals, and file context again and again. This project is my fix for that.

claudex sits in front of `claude` (Claude Code CLI) and `codex` (Codex CLI), tracks session state, and hands off cleanly when a switch is needed. You stay in flow instead of restarting your train of thought every time.

## How it works

```
you ──► claudex ──► claude   (preferred)
                       │ quota hit
                       ▼
                     codex    (fallback, receives handoff.md + git snapshot)
```

On every successful turn, `claudex` writes:
- `.claudex/state.json`, sessions, cooldowns, turn count
- `.claudex/handoff.md`, rolling structured summary (goal / plan / last exchange / next steps)
- `.claudex/transcript.ndjson`, append only log of every turn

When a failover happens, the new provider receives the handoff summary and a live git snapshot (status, log, diff) prepended to your prompt so it picks up without missing a beat.
Fallback attempts always start a fresh session on the fallback provider to avoid stale cross-task thread contamination.

## Install

```bash
# From this repo, editable install works from any directory
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

### Invisible wrappers (claude / claudecode / codex)

Install wrapper launchers so starting `claude`, `claudecode`, or `codex` automatically
runs through claudex with failover + continuity:

```bash
claudex install-wrappers
```

By default wrappers are installed to `~/.claudex/bin`.
Add it to your shell PATH before other CLI bins:

```bash
export PATH="$HOME/.claudex/bin:$PATH"
```

Wrappers installed:
- `claude` -> `claudex launch --prefer-provider claude -- "$@"`
- `claudecode` -> `claudex launch --prefer-provider claude -- "$@"`
- `codex` -> `claudex launch --prefer-provider codex -- "$@"`

Wrapper mode is transparent (native CLI is exec'd directly after provider selection):
- claudex chooses provider and prints only a one-line switch notice when needed
- then it launches the native `codex`/`claude` UI directly (full native progress output)

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
