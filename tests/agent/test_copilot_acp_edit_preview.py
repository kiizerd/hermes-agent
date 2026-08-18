"""Inline-diff previews for edits made by a native ACP agent.

Claude Code hands Hermes the diff on the wire: an edit tool call carries
``content: [{"type": "diff", "path", "oldText", "newText"}]`` blocks
(``claude-agent-acp``'s ``tools.js`` builds them from the tool input up front
and upgrades them to per-hunk blocks from ``structuredPatch`` once the edit
lands). Hermes previously read none of it, so ACP edits rendered as a plain
tool row while Hermes' own ``write_file``/``patch`` rendered a diff card.

These tests pin the boundary translation: ACP edit tools are presented under
the Hermes names the diff and card pipelines key off, with the wire diff
carried through in the result. Nothing here constructs ``CopilotACPClient`` --
its command resolution reads the developer's real ``.env`` and would spawn a
live agent subprocess.
"""

from __future__ import annotations

import json

from agent.copilot_acp_client import (
    _acp_tool_display,
    _acp_tool_result_text,
    _acp_unified_diff,
)
from agent.display import extract_edit_diff


def _write_fields(path="/repo/app.py", content="new line\n", status="completed"):
    """A ``Write`` tool call as claude-agent-acp emits it (tools.js:76-104)."""
    return {
        "_meta": {"claudeCode": {"toolName": "Write"}},
        "kind": "edit",
        "status": status,
        "title": f"Write {path}",
        "rawInput": {"file_path": path, "content": content},
        "content": [
            {"type": "diff", "path": path, "oldText": None, "newText": content}
        ],
    }


def _edit_fields(path="/repo/app.py", old="old line\n", new="new line\n", status="completed"):
    """An ``Edit`` tool call as claude-agent-acp emits it (tools.js:105-133)."""
    return {
        "_meta": {"claudeCode": {"toolName": "Edit"}},
        "kind": "edit",
        "status": status,
        "title": f"Edit {path}",
        "rawInput": {"file_path": path, "old_string": old, "new_string": new},
        "content": [
            {"type": "diff", "path": path, "oldText": old, "newText": new}
        ],
    }


class TestAcpEditToolDisplay:
    """ACP edit tools are presented under the Hermes names the UI keys off."""

    def test_write_is_presented_as_write_file(self):
        name, args = _acp_tool_display(_write_fields())
        assert name == "write_file"

    def test_edit_is_presented_as_patch(self):
        name, args = _acp_tool_display(_edit_fields())
        assert name == "patch"

    def test_file_path_argument_is_normalised_to_path(self):
        # `agent/display.py::_resolve_local_edit_paths` and the desktop's
        # `fileEditPath` both read `path`; ACP sends `file_path`.
        _, args = _acp_tool_display(_write_fields(path="/repo/app.py"))
        assert args["path"] == "/repo/app.py"
        assert "file_path" not in args

    def test_other_arguments_survive_the_rename(self):
        _, args = _acp_tool_display(_edit_fields(old="a\n", new="b\n"))
        assert args["old_string"] == "a\n"
        assert args["new_string"] == "b\n"

    def test_non_edit_tools_keep_their_own_name_and_arguments(self):
        fields = {
            "_meta": {"claudeCode": {"toolName": "Bash"}},
            "kind": "execute",
            "rawInput": {"command": "git status"},
        }
        name, args = _acp_tool_display(fields)
        assert name == "Bash"
        assert args == {"command": "git status"}

    def test_agents_without_the_claude_sidecar_still_fall_back_to_kind(self):
        name, args = _acp_tool_display({"kind": "read", "rawInput": {}})
        assert name == "read"

    def test_display_does_not_mutate_the_incoming_raw_input(self):
        fields = _write_fields()
        _acp_tool_display(fields)
        assert fields["rawInput"]["file_path"] == "/repo/app.py"


class TestAcpUnifiedDiff:
    """Building unified diff text from ACP ``type: "diff"`` content blocks."""

    def test_edit_block_renders_both_sides(self):
        diff = _acp_unified_diff(_edit_fields(old="old line\n", new="new line\n"))
        assert diff is not None
        assert "-old line" in diff
        assert "+new line" in diff

    def test_create_block_renders_as_pure_addition(self):
        diff = _acp_unified_diff(_write_fields(content="alpha\nbeta\n"))
        assert diff is not None
        assert "+alpha" in diff
        assert "+beta" in diff
        assert not any(
            line.startswith("-") and not line.startswith("---")
            for line in diff.splitlines()
        )

    def test_file_header_is_emitted_once_per_path(self):
        fields = _edit_fields()
        fields["content"] = [
            {"type": "diff", "path": "/repo/app.py", "oldText": "a\n", "newText": "b\n"},
            {"type": "diff", "path": "/repo/app.py", "oldText": "c\n", "newText": "d\n"},
        ]
        diff = _acp_unified_diff(fields)
        assert diff is not None
        assert diff.count("+++ ") == 1
        # Both hunks survive even though the header is shared.
        assert "+b" in diff and "+d" in diff

    def test_separate_paths_get_separate_headers(self):
        fields = _edit_fields()
        fields["content"] = [
            {"type": "diff", "path": "/repo/a.py", "oldText": "a\n", "newText": "b\n"},
            {"type": "diff", "path": "/repo/b.py", "oldText": "c\n", "newText": "d\n"},
        ]
        diff = _acp_unified_diff(fields)
        assert diff is not None
        assert diff.count("+++ ") == 2

    def test_no_diff_blocks_yields_none(self):
        fields = _edit_fields()
        fields["content"] = [{"type": "content", "content": {"type": "text", "text": "hi"}}]
        assert _acp_unified_diff(fields) is None

    def test_unchanged_block_yields_none(self):
        fields = _edit_fields(old="same\n", new="same\n")
        assert _acp_unified_diff(fields) is None

    def test_malformed_content_does_not_raise(self):
        fields = _edit_fields()
        fields["content"] = ["not a dict", {"type": "diff"}, None]
        assert _acp_unified_diff(fields) is None


class TestAcpEditResultText:
    """The result string carries the wire diff so the preview can render."""

    def test_edit_result_is_json_carrying_the_diff(self):
        data = json.loads(_acp_tool_result_text(_edit_fields()))
        assert data["success"] is True
        assert "-old line" in data["diff"]
        assert data["path"] == "/repo/app.py"

    def test_failed_edit_is_not_reported_as_success(self):
        data = json.loads(_acp_tool_result_text(_edit_fields(status="failed")))
        assert data["success"] is False

    def test_raw_output_fields_are_preserved_alongside_the_diff(self):
        fields = _edit_fields()
        fields["rawOutput"] = {"filePath": "/repo/app.py", "userModified": False}
        data = json.loads(_acp_tool_result_text(fields))
        assert data["userModified"] is False
        assert "diff" in data

    def test_edit_without_diff_blocks_keeps_the_plain_text_result(self):
        fields = _edit_fields()
        fields["content"] = [
            {"type": "content", "content": {"type": "text", "text": "done"}}
        ]
        assert _acp_tool_result_text(fields) == "done"

    def test_non_edit_tool_result_is_untouched(self):
        fields = {
            "_meta": {"claudeCode": {"toolName": "Bash"}},
            "kind": "execute",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}],
        }
        assert _acp_tool_result_text(fields) == "ok"

    def test_result_is_never_empty(self):
        # A falsy result reads as "tool produced nothing" downstream.
        assert _acp_tool_result_text({"kind": "read", "status": "completed"})


class TestEndToEndPreviewExtraction:
    """The pair feeds ``extract_edit_diff``, which is what emits inline_diff."""

    def test_acp_edit_reaches_extract_edit_diff(self):
        fields = _edit_fields()
        name, _args = _acp_tool_display(fields)
        result = _acp_tool_result_text(fields)
        diff = extract_edit_diff(name, result)
        assert diff is not None
        assert "-old line" in diff and "+new line" in diff

    def test_acp_write_reaches_extract_edit_diff(self):
        # `write_file` has no ready-made-diff branch upstream; it must honour
        # one when the result carries it, or ACP creates render no preview.
        fields = _write_fields(content="alpha\n")
        name, _args = _acp_tool_display(fields)
        result = _acp_tool_result_text(fields)
        diff = extract_edit_diff(name, result)
        assert diff is not None
        assert "+alpha" in diff

    def test_hermes_write_file_still_uses_its_snapshot_path(self, tmp_path):
        # Regression: generalising the ready-made-diff branch must not stop
        # Hermes' own write_file (whose result has no `diff` key) from
        # rendering a diff off the captured snapshot.
        from agent.display import capture_local_edit_snapshot

        target = tmp_path / "note.txt"
        target.write_text("before\n", encoding="utf-8")
        snapshot = capture_local_edit_snapshot("write_file", {"path": str(target)})
        target.write_text("after\n", encoding="utf-8")

        diff = extract_edit_diff(
            "write_file",
            json.dumps({"success": True}),
            function_args={"path": str(target)},
            snapshot=snapshot,
        )
        assert diff is not None
        assert "-before" in diff and "+after" in diff
