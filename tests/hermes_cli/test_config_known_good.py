"""Last-known-good config generations (fork patch — completes openai/codex#31188).

Upstream keeps a last-known-good config only in ``_LAST_EXPANDED_CONFIG_BY_PATH``,
a module global. When ``config.yaml`` is corrupted while nothing is running, every
process that starts afterwards finds that dict empty and serves ``DEFAULT_CONFIG``
— silently dropping every user override, including security-relevant
``approvals.deny`` rules. The fork adds a disk tier: ``config.yaml.lkg.N``
generations, byte-copied on a proven-good parse and read back when the in-process
tier is empty.

Tier order under a parse failure:

    1. in-process last-known-good  (upstream)
    2. newest parsing ``config.yaml.lkg.N``  (fork)
    3. DEFAULT_CONFIG  (upstream)

The write half (``_snapshot_known_good``) has an open retention decision, so the
tests covering it skip themselves while the body is a stub and start running for
real the moment it lands — no marker to remember to remove.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hermes_cli import config as config_mod

CORRUPT_YAML = 'model:\n  default: "x"\n  "C: "\\\\bad\\", "key\\""\n'


# --------------------------------------------------------------------------
# stub gate
# --------------------------------------------------------------------------

def _snapshot_is_implemented() -> bool:
    """True once ``_snapshot_known_good`` has a real body.

    Calls the function rather than inspecting its source (AGENTS.md bans
    source-reading tests). A stub raises ``NotImplementedError``; any other
    outcome — success or a genuine bug — counts as implemented, so a broken
    body surfaces as a test failure instead of a silent skip.
    """
    with tempfile.TemporaryDirectory() as d:
        probe = Path(d) / "config.yaml"
        probe.write_text("agent:\n  reasoning_effort: low\n", encoding="utf-8")
        st = probe.stat()
        try:
            config_mod._snapshot_known_good(
                probe, (st.st_mtime_ns, st.st_size), str(probe)
            )
        except NotImplementedError:
            return False
        except Exception:
            return True
        return True


needs_snapshot_body = pytest.mark.skipif(
    not _snapshot_is_implemented(),
    reason="_snapshot_known_good body is still the TODO(kiize) stub",
)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HERMES_HOME with every config-loader cache reset.

    ``_load_config_impl`` memoizes on (mtime, size) in ``_LOAD_CONFIG_CACHE``,
    retains the last good config in ``_LAST_EXPANDED_CONFIG_BY_PATH``, dedupes
    stderr warnings via ``_CONFIG_PARSE_WARNED``, and skips redundant snapshots
    via ``_LKG_SNAPSHOT_SIG``. All four are module globals that would otherwise
    leak a previous test's state into this one — the tier-order tests below are
    exactly the ones that state would silently invalidate.
    """
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for cache in (
        config_mod._LOAD_CONFIG_CACHE,
        config_mod._RAW_CONFIG_CACHE,
        config_mod._LAST_EXPANDED_CONFIG_BY_PATH,
        config_mod._LKG_SNAPSHOT_SIG,
    ):
        cache.clear()
    config_mod._CONFIG_PARSE_WARNED.clear()
    return hermes_home


@pytest.fixture
def cfg(home):
    """Path of the temp ``config.yaml`` (not yet created)."""
    return home / "config.yaml"


def _write_generation(cfg_path: Path, generation: int, text: str) -> Path:
    """Place a generation via the production path helper, never a literal name."""
    path = config_mod._lkg_path(cfg_path, generation)
    path.write_text(text, encoding="utf-8")
    return path


def _sig(path: Path):
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


# --------------------------------------------------------------------------
# read side: _load_known_good
# --------------------------------------------------------------------------

def test_generation_round_trips_through_the_path_helper(cfg):
    """What ``_lkg_path`` names, ``_load_known_good`` must find.

    Asserting the round-trip instead of the literal ``config.yaml.lkg.1`` keeps
    this green through a suffix rename and red if the two halves ever disagree
    on where generations live.
    """
    written = _write_generation(cfg, 1, "custom_prompt: from-gen-one\n")

    result = config_mod._load_known_good(cfg)

    assert result is not None
    loaded, source = result
    assert loaded["custom_prompt"] == "from-gen-one"
    assert source == written


def test_newest_parsing_generation_wins(cfg):
    """Generation 1 is newest; an older one is used only when it doesn't parse."""
    _write_generation(cfg, 1, "custom_prompt: newest\n")
    _write_generation(cfg, 2, "custom_prompt: older\n")

    loaded, source = config_mod._load_known_good(cfg)

    assert loaded["custom_prompt"] == "newest"
    assert source == config_mod._lkg_path(cfg, 1)


def test_unparsable_generation_is_skipped_for_the_next_one(cfg):
    _write_generation(cfg, 1, CORRUPT_YAML)
    _write_generation(cfg, 2, "custom_prompt: survivor\n")

    loaded, source = config_mod._load_known_good(cfg)

    assert loaded["custom_prompt"] == "survivor"
    assert source == config_mod._lkg_path(cfg, 2)


def test_unparsable_generation_is_preserved_not_deleted(cfg):
    """Same posture as ``_backup_corrupt_config``: hermes preserves evidence.

    A generation that no longer parses may still hold settings the user wants to
    hand-recover. Reading past it must never be a destructive act.
    """
    broken = _write_generation(cfg, 1, CORRUPT_YAML)
    _write_generation(cfg, 2, "custom_prompt: survivor\n")

    config_mod._load_known_good(cfg)

    assert broken.is_file()
    assert broken.read_text(encoding="utf-8") == CORRUPT_YAML


def test_no_generations_returns_none(cfg):
    assert config_mod._load_known_good(cfg) is None


def test_non_mapping_generation_is_not_treated_as_config(cfg):
    """A YAML document that parses to a list/scalar is not a config.

    ``_deep_merge`` downstream assumes a mapping; returning a list here would
    blow up the loader instead of falling through to the next tier.
    """
    _write_generation(cfg, 1, "- just\n- a\n- list\n")
    _write_generation(cfg, 2, "custom_prompt: real-config\n")

    loaded, source = config_mod._load_known_good(cfg)

    assert loaded["custom_prompt"] == "real-config"
    assert source == config_mod._lkg_path(cfg, 2)


# --------------------------------------------------------------------------
# read side: describe_known_good
# --------------------------------------------------------------------------

def test_describe_reports_one_line_per_existing_generation(cfg):
    _write_generation(cfg, 1, "custom_prompt: a\n")
    _write_generation(cfg, 3, "custom_prompt: c\n")

    lines = config_mod.describe_known_good(cfg)

    assert len(lines) == 2
    assert config_mod._lkg_path(cfg, 1).name in lines[0]
    assert config_mod._lkg_path(cfg, 3).name in lines[1]


def test_describe_is_empty_when_no_generations_exist(cfg):
    assert config_mod.describe_known_good(cfg) == []


# --------------------------------------------------------------------------
# tier order, end to end through load_config()
# --------------------------------------------------------------------------

def test_corrupt_config_restores_user_values_from_a_disk_generation(cfg, monkeypatch):
    """Tier 2 in a fresh process: the whole point of the patch.

    Also pins that the restored content re-enters the NORMAL pipeline rather
    than short-circuiting — a ``max_turns`` top-level key must be migrated under
    ``agent`` and a ``${VAR}`` reference must be expanded, exactly as they would
    be for a live-parsed file. A short-circuiting restore passes the "is my
    value there" check and fails these two.
    """
    monkeypatch.setenv("LKG_TEST_VAR", "expanded-value")
    cfg.write_text(CORRUPT_YAML, encoding="utf-8")
    _write_generation(
        cfg,
        1,
        "custom_prompt: 'hi ${LKG_TEST_VAR}'\n"
        "max_turns: 42\n"
        "approvals:\n"
        "  deny:\n"
        "    - 'rm -rf /'\n",
    )

    loaded = config_mod.load_config()

    assert loaded["approvals"]["deny"] == ["rm -rf /"]
    assert loaded["custom_prompt"] == "hi expanded-value"
    assert loaded["agent"]["max_turns"] == 42
    assert "max_turns" not in loaded
    # DEFAULT_CONFIG is still the base the generation merged onto.
    assert "model" in loaded


def test_corrupt_config_without_generations_falls_back_to_defaults(cfg):
    """Upstream behavior preserved when the disk tier has nothing to offer."""
    cfg.write_text(
        CORRUPT_YAML.replace('default: "x"', 'default: "x"\n  # user had settings'),
        encoding="utf-8",
    )

    loaded = config_mod.load_config()

    assert loaded["model"] == config_mod.DEFAULT_CONFIG["model"]


def test_in_process_tier_wins_over_the_disk_generation(cfg):
    """Tier 1 before tier 2: a live process keeps serving what it last loaded.

    The in-process copy is by definition newer than any generation, so a
    long-running gateway whose user mid-edits config.yaml into broken YAML must
    not silently rewind to an older snapshot.
    """
    cfg.write_text("custom_prompt: live-and-current\n", encoding="utf-8")
    assert config_mod.load_config()["custom_prompt"] == "live-and-current"

    _write_generation(cfg, 1, "custom_prompt: stale-generation\n")
    cfg.write_text(CORRUPT_YAML, encoding="utf-8")
    config_mod._LOAD_CONFIG_CACHE.clear()

    assert config_mod.load_config()["custom_prompt"] == "live-and-current"


def test_missing_config_does_not_poison_the_in_process_tier(cfg):
    """Regression: a load with no config.yaml must not register DEFAULT_CONFIG.

    Upstream wrote ``_LAST_EXPANDED_CONFIG_BY_PATH`` unconditionally on every
    successful load — including loads where config.yaml did not exist, where the
    "loaded config" is just DEFAULT_CONFIG. The read side only checks whether an
    in-process value is present, so that defaults snapshot outranked the disk
    tier and served defaults anyway. Any process touching config before the file
    exists (first run, import-time load, profile creation) poisoned the tier for
    its whole lifetime.
    """
    assert not cfg.exists()
    config_mod.load_config()  # poisoning load

    _write_generation(cfg, 1, "custom_prompt: from-generation\n")
    cfg.write_text(CORRUPT_YAML, encoding="utf-8")
    config_mod._LOAD_CONFIG_CACHE.clear()

    assert config_mod.load_config()["custom_prompt"] == "from-generation"


def test_restore_is_announced_and_names_its_source(cfg, capsys):
    """A silent restore lets you run for weeks on a stale snapshot unaware."""
    cfg.write_text(CORRUPT_YAML, encoding="utf-8")
    source = _write_generation(cfg, 1, "custom_prompt: restored\n")

    config_mod.load_config()

    err = capsys.readouterr().err
    assert source.name in err


def test_restore_still_preserves_the_corrupt_file_and_its_backup(cfg):
    """Restoring must not mutate config.yaml, and evidence is still snapshotted.

    "hermes never silently mutates the user's config" is upstream's posture and
    the reason a hand-fixed file is picked up on the next load. The disk tier
    restores in memory only.
    """
    cfg.write_text(CORRUPT_YAML, encoding="utf-8")
    _write_generation(cfg, 1, "custom_prompt: restored\n")

    config_mod.load_config()

    assert cfg.read_text(encoding="utf-8") == CORRUPT_YAML
    assert list(cfg.parent.glob("config.yaml.corrupt.*.bak"))


def test_a_healthy_config_ignores_generations_entirely(cfg):
    """Generations are a failure path, never a source for a file that parses."""
    cfg.write_text("custom_prompt: live\n", encoding="utf-8")
    _write_generation(cfg, 1, "custom_prompt: generation\n")

    assert config_mod.load_config()["custom_prompt"] == "live"


# --------------------------------------------------------------------------
# write side: _snapshot_known_good  (skipped while the body is a stub)
# --------------------------------------------------------------------------

@needs_snapshot_body
def test_first_snapshot_creates_generation_one_byte_identical(cfg):
    text = "custom_prompt: original\n"
    cfg.write_text(text, encoding="utf-8")

    config_mod._snapshot_known_good(cfg, _sig(cfg), str(cfg))

    gen1 = config_mod._lkg_path(cfg, 1)
    assert gen1.read_bytes() == cfg.read_bytes()
    assert gen1.read_text(encoding="utf-8") == text


@needs_snapshot_body
def test_snapshot_keeps_env_refs_verbatim(cfg, monkeypatch):
    """The copy must be of the FILE, never of the expanded in-memory dict.

    At snapshot time the loaded config has already resolved ``${OPENAI_API_KEY}``
    to the literal secret. Serializing that dict would write every .env key into
    a plaintext file sitting next to config.yaml. A byte copy cannot.
    """
    monkeypatch.setenv("LKG_SECRET", "sk-do-not-write-me")
    cfg.write_text("auxiliary:\n  curator:\n    api_key: '${LKG_SECRET}'\n", encoding="utf-8")

    config_mod._snapshot_known_good(cfg, _sig(cfg), str(cfg))

    body = config_mod._lkg_path(cfg, 1).read_text(encoding="utf-8")
    assert "${LKG_SECRET}" in body
    assert "sk-do-not-write-me" not in body


@needs_snapshot_body
def test_rotation_pushes_older_generations_down(cfg):
    cfg.write_text("custom_prompt: first\n", encoding="utf-8")
    config_mod._snapshot_known_good(cfg, _sig(cfg), str(cfg))

    cfg.write_text("custom_prompt: second\n", encoding="utf-8")
    config_mod._snapshot_known_good(cfg, _sig(cfg), str(cfg))

    assert "second" in config_mod._lkg_path(cfg, 1).read_text(encoding="utf-8")
    assert "first" in config_mod._lkg_path(cfg, 2).read_text(encoding="utf-8")


@needs_snapshot_body
def test_ring_never_exceeds_the_generation_cap(cfg):
    for i in range(config_mod._LKG_GENERATIONS + 3):
        cfg.write_text(f"custom_prompt: v{i}\n", encoding="utf-8")
        config_mod._snapshot_known_good(cfg, _sig(cfg), str(cfg))

    present = [
        g for g in range(1, config_mod._LKG_GENERATIONS + 2)
        if config_mod._lkg_path(cfg, g).exists()
    ]
    assert present == list(range(1, config_mod._LKG_GENERATIONS + 1))


@needs_snapshot_body
def test_unchanged_signature_is_a_no_op(cfg):
    cfg.write_text("custom_prompt: only\n", encoding="utf-8")
    sig = _sig(cfg)

    first = config_mod._snapshot_known_good(cfg, sig, str(cfg))
    second = config_mod._snapshot_known_good(cfg, sig, str(cfg))

    assert first is not None
    assert second is None
    assert not config_mod._lkg_path(cfg, 2).exists()


@needs_snapshot_body
def test_return_value_describes_the_shift_or_is_none(cfg):
    """``edit_config`` branches on this to print "shifted" vs "unchanged"."""
    cfg.write_text("custom_prompt: first\n", encoding="utf-8")
    shift = config_mod._snapshot_known_good(cfg, _sig(cfg), str(cfg))

    assert isinstance(shift, str) and shift.strip()


@needs_snapshot_body
def test_snapshot_failure_never_blocks_a_config_load(cfg, monkeypatch):
    """Best-effort, like ``_backup_corrupt_config``.

    A snapshot problem — read-only dir, disk full, a half-finished body — must
    degrade to "no new generation", never to "hermes can't read its config".
    """
    cfg.write_text("custom_prompt: live\n", encoding="utf-8")
    monkeypatch.setattr(
        config_mod,
        "_snapshot_known_good",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert config_mod.load_config()["custom_prompt"] == "live"


@needs_snapshot_body
def test_a_proven_good_load_snapshots_itself(cfg):
    """The loader is the primary caller; ``edit_config`` is the explicit one."""
    cfg.write_text("custom_prompt: proven-good\n", encoding="utf-8")

    config_mod.load_config()

    assert "proven-good" in config_mod._lkg_path(cfg, 1).read_text(encoding="utf-8")


@needs_snapshot_body
def test_a_restored_config_is_never_snapshotted_back(cfg):
    """Guarded by ``parsed_live_file``.

    Without it, loading with a corrupt config.yaml would rotate the restored
    generation back into slot 1 on every load, walking real history off the ring
    while the live file is still broken.
    """
    cfg.write_text(CORRUPT_YAML, encoding="utf-8")
    _write_generation(cfg, 1, "custom_prompt: restored\n")
    before = config_mod._lkg_path(cfg, 1).read_bytes()

    config_mod.load_config()

    assert config_mod._lkg_path(cfg, 1).read_bytes() == before
    assert not config_mod._lkg_path(cfg, 2).exists()
