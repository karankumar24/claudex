<div align="center">

<img src="docs/banner.png" alt="claudex" width="720">

# Claude when you can, Codex when you can't

**When Claude Code hits its usage limit, claudex switches to Codex CLI and hands it your last exchange and the state of your repo.**

<a href="https://github.com/karankumar24/claudex/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/karankumar24/claudex/tests.yml?branch=main&style=flat-square&label=tests" alt="tests"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="MIT license"></a>

</div>

---

Claude Code and Codex both cut you off when you reach your plan's limit, and moving to the other one by hand means explaining the task all over again. claudex sits in front of both. When one runs out, it asks, then reruns your prompt on the other with the context attached.

<table>
<tr>
<th width="50%">Without claudex</th>
<th width="50%">With claudex</th>
</tr>
<tr>
<td valign="top">

> **You:** now refactor it to use sessions instead
>
> **Claude:** You've hit your limit · resets 6pm (America/Los_Angeles)

</td>
<td valign="top">

> **You:** now refactor it to use sessions instead
>
> **claudex:** claude unavailable (QUOTA_EXHAUSTED). Switch to codex and continue? **y**
>
> **Codex:** From the handoff, we were going through the JWT auth flow. Here's how I'd move it to sessions...

</td>
</tr>
</table>

## How it works

- `claudex chat` sends each prompt to Claude first.
- When Claude hits its limit, claudex asks, then sends the same prompt to Codex with your last exchange and a snapshot of your repo.
- It reads when your limit resets and waits until then before using Claude again.

## Install

Needs Python 3.11 or newer, and both [Claude Code](https://code.claude.com/docs/en/overview) and [Codex CLI](https://github.com/openai/codex) installed and logged in. Then:

```bash
pipx install git+https://github.com/karankumar24/claudex.git
```

## Usage

Run it from the root of your repo:

```bash
claudex chat                                # a conversation; type exit to leave
claudex ask "what does this function do?"   # one prompt, then exit
claudex status                              # what's ready and what's waiting
claudex reset                               # delete claudex's files for this repo
```

Add `--auto-switch yes` to switch without asking.

To have plain `claude` and `codex` pick for you, run `claudex install-wrappers` and add `~/.claudex/bin` to your PATH. Settings go in `.claudex/config.toml`, and [config.py](src/claudex/config.py) lists them all.

## Your data

claudex keeps its files in `.claudex/` in your repo and talks only to git and the two CLIs. It saves every prompt and reply there, so add `.claudex/` to your `.gitignore`.

## Good to know

- Only your last exchange carries over, not the whole conversation.
- The wrappers only choose which CLI to open. They don't switch in the middle of a session.
- Codex starts in read-only mode. You can change that in the config.

## Contributing

Issues and pull requests are welcome. For a dev setup, run `pip install -e . pytest`, then `pytest`. MIT licensed.
