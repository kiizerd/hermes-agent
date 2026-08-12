"""Token usage plumbing for the ACP client.

claude-agent-acp returns per-turn usage on the session/prompt result
(camelCase fields built by its sessionUsage()); before this plumbing the
client hardcoded zeros, so Hermes' context meter never moved on ACP turns.

No test here constructs ``CopilotACPClient()`` -- its env-derived command
resolution can resolve a live agent from the developer's real .env and
spawn a subprocess. ``object.__new__`` + instance-attribute ``_rpc`` keeps
everything in-process.
"""

from __future__ import annotations

from agent.copilot_acp_client import CopilotACPClient, _acp_usage_chunk

PROMPT_RESPONSE_USAGE = {
    "inputTokens": 10,
    "outputTokens": 20,
    "cachedReadTokens": 300,
    "cachedWriteTokens": 40,
    "totalTokens": 370,
}


class TestUsageChunkMapping:
    def test_maps_camelcase_prompt_response_usage(self):
        usage = _acp_usage_chunk("m", PROMPT_RESPONSE_USAGE).usage
        # OpenAI convention: prompt_tokens INCLUDES cached tokens
        # (details.cached_tokens is a subset); Anthropic's inputTokens
        # excludes cache reads/writes, so they are folded in.
        assert usage.prompt_tokens == 350
        assert usage.completion_tokens == 20
        assert usage.total_tokens == 370
        assert usage.prompt_tokens_details.cached_tokens == 300

    def test_missing_or_malformed_usage_degrades_to_zeros(self):
        for bad in (None, {}, {"inputTokens": "x"}, {"inputTokens": -5}, 7):
            usage = _acp_usage_chunk("m", bad).usage
            assert (
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.total_tokens,
            ) == (0, 0, 0), bad
        # No-arg call keeps the historical zero shape.
        assert _acp_usage_chunk("m").usage.total_tokens == 0

    def test_partial_usage_counts_what_is_present(self):
        usage = _acp_usage_chunk("m", {"outputTokens": 7}).usage
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 7
        assert usage.total_tokens == 7


class TestPromptSessionCapturesUsage:
    def _client(self, rpc_result):
        client = object.__new__(CopilotACPClient)
        client._rpc = lambda *a, **kw: rpc_result
        return client

    def test_usage_stashed_from_prompt_result(self):
        client = self._client(
            {"stopReason": "end_turn", "usage": dict(PROMPT_RESPONSE_USAGE)}
        )
        client._prompt_session("hi", session_id="s", timeout_seconds=5)
        assert client._last_turn_usage == PROMPT_RESPONSE_USAGE

    def test_non_dict_result_clears_stale_usage(self):
        """A turn without usage must not report the PREVIOUS turn's counts."""
        client = self._client(None)
        client._last_turn_usage = {"inputTokens": 99}
        client._prompt_session("hi", session_id="s", timeout_seconds=5)
        assert client._last_turn_usage is None

    def test_malformed_usage_field_clears_stale_usage(self):
        client = self._client({"stopReason": "end_turn", "usage": "not-a-dict"})
        client._last_turn_usage = {"inputTokens": 99}
        client._prompt_session("hi", session_id="s", timeout_seconds=5)
        assert client._last_turn_usage is None
