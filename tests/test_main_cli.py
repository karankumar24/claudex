from typer.testing import CliRunner

import claudex.main as main_module


RUNNER = CliRunner()


def test_ask_accepts_unquoted_multi_word_prompt(monkeypatch):
    captured: dict[str, str] = {}

    def fake_run_turn(user_prompt, config, **kwargs):
        captured["prompt"] = user_prompt
        return True, None

    monkeypatch.setattr(main_module, "_run_turn", fake_run_turn)
    monkeypatch.setattr(main_module, "load_config", lambda: {})

    result = RUNNER.invoke(main_module.app, ["ask", "help", "me", "with", "task"])
    assert result.exit_code == 0
    assert captured["prompt"] == "help me with task"
