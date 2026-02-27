"""
Shared pytest fixtures.

Key fixture: `isolated_dir` — changes the working directory to a fresh
temporary directory for every test that requests it. This ensures that
.claudex/ operations never bleed between tests or touch the real repo.
"""

import pytest


@pytest.fixture
def isolated_dir(tmp_path, monkeypatch):
    """
    Change CWD to a fresh temp directory.
    All .claudex/ paths in state.py/handoff.py are relative to CWD,
    so this fully isolates each test.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path
