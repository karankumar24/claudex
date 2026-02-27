"""
Configuration loading with layered precedence:
  1. Built-in defaults
  2. User-global:  ~/.config/claudex/config.toml
  3. Repo-local:   .claudex/config.toml  (highest priority)

All config is read-only at runtime; create/edit the TOML files manually.
"""

from __future__ import annotations

import tomllib
from typing import Any

from .state import REPO_CONFIG_FILE, USER_CONFIG_FILE

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict[str, Any] = {
    # Providers tried in this order; first available one wins
    "provider_order": ["claude", "codex"],

    "codex": {
        # None means use codex's own default model
        "model": None,
        # "read-only" is safest; other valid values:
        #   "workspace-write", "danger-full-access", "full-auto"
        "sandbox": "read-only",
    },

    "claude": {
        # Extra tools to allow, e.g. ["Bash", "Edit"]
        "allowed_tools": [],
    },

    "limits": {
        # Maximum lines of `git diff` to include in the repo snapshot
        "max_diff_lines": 200,
        # Maximum bytes of `git diff` before we drop to stat-only
        "max_diff_bytes": 8_000,
        # Maximum lines for handoff.md (rolling summary is truncated to this)
        "max_handoff_lines": 350,
    },

    "retry": {
        # How many times to retry a TRANSIENT_RATE_LIMIT before switching
        "max_retries": 3,
        # Exponential backoff base in seconds (wait = base ** attempt)
        "backoff_base": 2.0,
        # Cap for a single backoff wait
        "backoff_max": 30.0,
        # How long (minutes) to cool down a QUOTA_EXHAUSTED provider
        "cooldown_minutes": 60,
        # How long (minutes) to cool down after exhausted transient retries
        "transient_cooldown_minutes": 5,
    },
}


# ── Loader ────────────────────────────────────────────────────────────────────


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into `base`, returning a new dict."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_toml_file(path) -> dict[str, Any]:
    """
    Load a TOML file into a dict.
    Returns {} if the file cannot be read or parsed.
    """
    try:
        with path.open("rb") as f:
            loaded = tomllib.load(f)
            return loaded if isinstance(loaded, dict) else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load_config() -> dict[str, Any]:
    """
    Return the merged configuration dict.
    Keys from higher-priority sources override lower ones (but nested dicts merge).
    """
    config = dict(DEFAULT_CONFIG)

    # User-global config (lower priority)
    if USER_CONFIG_FILE.exists():
        user_cfg = _load_toml_file(USER_CONFIG_FILE)
        config = _deep_merge(config, user_cfg)

    # Repo-local config (highest priority)
    if REPO_CONFIG_FILE.exists():
        repo_cfg = _load_toml_file(REPO_CONFIG_FILE)
        config = _deep_merge(config, repo_cfg)

    return config
