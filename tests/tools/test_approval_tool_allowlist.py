"""Tests for the tool allowlist (approvals.tool_allowlist in config.yaml).

approvals.tool_allowlist is a list of fnmatch globs matched against a
non-shell tool's stable provider key (e.g. ``copilot-acp:skill_manage``). A
match auto-approves the call with no card. It is grant-only — the tool-side
counterpart to command_allowlist, which cannot reach tool calls because the
ACP bridge keys them with an unpredictable per-target hash.
"""

import pytest

from tools import approval as mod


@pytest.fixture
def allow_config(monkeypatch):
    """Install a tool_allowlist into the approvals config and return a setter."""

    state = {"config": {"mode": "manual", "tool_allowlist": []}}

    def set_allow(patterns, **extra):
        state["config"] = {
            "mode": "manual", "tool_allowlist": list(patterns), **extra
        }

    monkeypatch.setattr(mod, "_get_approval_config", lambda: state["config"])
    return set_allow


class TestMatchToolAllowlist:
    def test_empty_list_is_noop(self, allow_config):
        allow_config([])
        assert mod._match_tool_allowlist("copilot-acp:skill_manage") is None

    def test_missing_key_is_noop(self, monkeypatch):
        monkeypatch.setattr(mod, "_get_approval_config", lambda: {"mode": "manual"})
        assert mod._match_tool_allowlist("copilot-acp:skill_manage") is None

    def test_empty_tool_key_is_noop(self, allow_config):
        allow_config(["copilot-acp:*"])
        assert mod._match_tool_allowlist("") is None

    def test_config_load_failure_denies(self, monkeypatch):
        """A config read failure must NOT auto-approve (fail closed to gating)."""
        def boom():
            raise RuntimeError("config unavailable")
        monkeypatch.setattr(mod, "_get_approval_config", boom)
        assert mod._match_tool_allowlist("copilot-acp:skill_manage") is None

    def test_exact_match(self, allow_config):
        allow_config(["copilot-acp:skill_manage"])
        assert (
            mod._match_tool_allowlist("copilot-acp:skill_manage")
            == "copilot-acp:skill_manage"
        )

    def test_glob_match(self, allow_config):
        allow_config(["copilot-acp:*"])
        assert mod._match_tool_allowlist("copilot-acp:memory") == "copilot-acp:*"

    def test_case_insensitive(self, allow_config):
        allow_config(["COPILOT-ACP:Skill_Manage"])
        assert mod._match_tool_allowlist("copilot-acp:skill_manage") is not None

    def test_non_match_returns_none(self, allow_config):
        allow_config(["copilot-acp:memory"])
        assert mod._match_tool_allowlist("copilot-acp:skill_manage") is None

    def test_blank_entries_ignored(self, allow_config):
        allow_config(["", "   ", 123, None])
        assert mod._match_tool_allowlist("copilot-acp:skill_manage") is None


class TestIsToolAllowlisted:
    def test_true_on_match(self, allow_config):
        allow_config(["copilot-acp:skill_manage"])
        assert mod.is_tool_allowlisted("copilot-acp:skill_manage") is True

    def test_false_on_no_match(self, allow_config):
        allow_config(["copilot-acp:memory"])
        assert mod.is_tool_allowlisted("copilot-acp:skill_manage") is False

    def test_false_on_empty_list(self, allow_config):
        allow_config([])
        assert mod.is_tool_allowlisted("copilot-acp:skill_manage") is False
