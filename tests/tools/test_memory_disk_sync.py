"""Cross-process memory freshness: reload_if_changed_on_disk().

The system-prompt snapshot is frozen for the life of a session so the cached
prompt prefix stays byte-stable. That invariant assumes this process is the
only writer, which stopped holding once sister sessions, cron jobs, and the
hermes-tools MCP subprocess gained the memory tool. These tests pin the two
halves of the fix: a FOREIGN write forces a reload, our OWN write does not.
"""

import pytest

from tools.memory_tool import ENTRY_DELIMITER, MemoryStore


@pytest.fixture()
def mem_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture()
def store(mem_dir):
    s = MemoryStore(memory_char_limit=500, user_char_limit=300)
    s.load_from_disk()
    return s


def foreign_write(mem_dir, filename, *entries):
    """Simulate another process rewriting a memory file behind our back."""
    path = mem_dir / filename
    path.write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")
    # Force a distinct mtime: a same-nanosecond rewrite of equal length would
    # be indistinguishable from the loaded state on a coarse-grained clock.
    st = path.stat()
    import os
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    return path


class TestForeignWriteDetected:
    def test_reload_picks_up_foreign_write(self, store, mem_dir):
        store.add("memory", "ours")
        assert store.reload_if_changed_on_disk() is False

        foreign_write(mem_dir, "MEMORY.md", "ours", "written by another process")

        assert store.reload_if_changed_on_disk() is True
        assert "written by another process" in store.memory_entries

    def test_snapshot_refreshes_so_system_prompt_sees_it(self, store, mem_dir):
        """The whole point: the FROZEN snapshot must pick the change up too."""
        foreign_write(mem_dir, "MEMORY.md", "fact from sister session")

        # format_for_system_prompt returns None for an empty snapshot.
        assert "fact from sister session" not in (
            store.format_for_system_prompt("memory") or ""
        )
        assert store.reload_if_changed_on_disk() is True
        assert "fact from sister session" in store.format_for_system_prompt("memory")

    def test_user_file_tracked_independently(self, store, mem_dir):
        foreign_write(mem_dir, "USER.md", "Name: Alfred")

        assert store.reload_if_changed_on_disk() is True
        assert "Name: Alfred" in store.user_entries

    def test_deleted_file_counts_as_change(self, store, mem_dir):
        store.add("memory", "ours")
        (mem_dir / "MEMORY.md").unlink()

        assert store.reload_if_changed_on_disk() is True
        assert store.memory_entries == []

    def test_repeated_call_is_idempotent(self, store, mem_dir):
        foreign_write(mem_dir, "MEMORY.md", "once")

        assert store.reload_if_changed_on_disk() is True
        assert store.reload_if_changed_on_disk() is False


class TestOwnWriteNotDetected:
    """Self-writes must NOT reload -- that would surrender the prefix cache
    on every turn following a memory write, which is exactly the behaviour
    the frozen snapshot exists to avoid."""

    def test_add_does_not_trip_reload(self, store):
        store.add("memory", "self written")
        assert store.reload_if_changed_on_disk() is False

    def test_replace_does_not_trip_reload(self, store):
        store.add("memory", "before")
        store.replace("memory", "before", "after")
        assert store.reload_if_changed_on_disk() is False

    def test_remove_does_not_trip_reload(self, store):
        store.add("memory", "doomed")
        store.remove("memory", "doomed")
        assert store.reload_if_changed_on_disk() is False

    def test_self_write_leaves_snapshot_frozen(self, store):
        """Existing contract, asserted so the fix can't silently break it."""
        store.add("memory", "self written")
        store.reload_if_changed_on_disk()
        assert "self written" not in (store.format_for_system_prompt("memory") or "")


class TestFailsSafe:
    def test_stat_failure_does_not_raise(self, store, monkeypatch):
        monkeypatch.setattr(
            MemoryStore, "_current_signature",
            lambda self, target: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert store.reload_if_changed_on_disk() is False

    def test_reload_failure_does_not_raise(self, store, mem_dir, monkeypatch):
        foreign_write(mem_dir, "MEMORY.md", "trigger")
        monkeypatch.setattr(
            MemoryStore, "load_from_disk",
            lambda self: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert store.reload_if_changed_on_disk() is False

    def test_fresh_store_before_any_load_reloads_once(self, mem_dir):
        """An unstamped store must reload rather than assume it is current."""
        (mem_dir / "MEMORY.md").write_text("preexisting", encoding="utf-8")
        s = MemoryStore()
        assert s.reload_if_changed_on_disk() is True
        assert "preexisting" in s.memory_entries
