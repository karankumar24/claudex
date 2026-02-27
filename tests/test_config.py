"""
Tests for layered config loading and TOML parse resilience.
"""

import claudex.config as config_module


def test_load_config_ignores_malformed_repo_toml(isolated_dir, monkeypatch):
    repo_dir = isolated_dir / ".claudex"
    repo_dir.mkdir()
    repo_cfg = repo_dir / "config.toml"
    repo_cfg.write_text("provider_order = [claude, ]\n")

    monkeypatch.setattr(config_module, "USER_CONFIG_FILE", isolated_dir / "user.toml")
    monkeypatch.setattr(config_module, "REPO_CONFIG_FILE", repo_cfg)

    cfg = config_module.load_config()
    assert cfg["provider_order"] == ["claude", "codex"]
    assert cfg["retry"]["cooldown_minutes"] == 60


def test_load_config_merges_user_then_repo(isolated_dir, monkeypatch):
    user_cfg = isolated_dir / "user.toml"
    user_cfg.write_text(
        "\n".join(
            [
                "provider_order = [\"codex\", \"claude\"]",
                "[retry]",
                "max_retries = 1",
            ]
        )
    )

    repo_dir = isolated_dir / ".claudex"
    repo_dir.mkdir()
    repo_cfg = repo_dir / "config.toml"
    repo_cfg.write_text(
        "\n".join(
            [
                "[codex]",
                "model = \"o4-mini\"",
                "[retry]",
                "max_retries = 4",
            ]
        )
    )

    monkeypatch.setattr(config_module, "USER_CONFIG_FILE", user_cfg)
    monkeypatch.setattr(config_module, "REPO_CONFIG_FILE", repo_cfg)

    cfg = config_module.load_config()
    assert cfg["provider_order"] == ["codex", "claude"]
    assert cfg["codex"]["model"] == "o4-mini"
    assert cfg["retry"]["max_retries"] == 4
