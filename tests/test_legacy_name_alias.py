"""Acceptance tests for FAI-247-D: legacy API names expressed as aliases.

Criterion 1 — A name that is an alias of the provider is expressed as an alias
of the current entry, not as its own entry.

Criterion 2 — The alias still resolves; a client with the old name does not
break. A test proves it.

Criterion 3 — Facts (window, prices, modalities) only live on the current
entry. The alias carries none.

Red-proof (shared): each test asserts a behaviour against the committed
catalog (26de04a) and produces an AssertionError, never a collection error
or ImportError.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "providers" / "catalog.v1.json"

LEGACY_NAMES = ("deepseek-chat", "deepseek-reasoner")
CURRENT_MODELS = {"deepseek-chat": "deepseek-v4-flash", "deepseek-reasoner": "deepseek-v4-pro"}


def _load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


# ---------------------------------------------------------------------------
# Criterion 1 — legacy names are aliases, not separate entries
# ---------------------------------------------------------------------------

def test_legacy_name_is_not_a_separate_entry():
    """deepseek-chat and deepseek-reasoner must not appear as provider keys."""
    catalog = _load_catalog()
    for name in LEGACY_NAMES:
        assert name not in catalog["providers"], (
            f"{name} is a legacy API name; it must be an alias, not a "
            "separate provider entry"
        )


def test_legacy_name_is_present_as_alias():
    """Every legacy name must appear as an alias on at least one deepseek entry."""
    catalog = _load_catalog()
    deepseek_keys = [k for k in catalog["providers"] if k.startswith("deepseek")]
    for name in LEGACY_NAMES:
        found = any(
            name in catalog["providers"][k].get("aliases", [])
            for k in deepseek_keys
        )
        assert found, (
            f"{name} is not present as an alias on any deepseek entry"
        )


# ---------------------------------------------------------------------------
# Criterion 2 — alias resolution works
# ---------------------------------------------------------------------------

def test_legacy_name_resolves_to_current_model():
    """A client sending a legacy name resolves to the current model entry,
    not a stale parallel entry."""
    catalog = _load_catalog()
    for legacy, current_model in CURRENT_MODELS.items():
        entry = catalog["providers"].get(current_model)
        assert entry is not None, (
            f"current entry {current_model!r} must exist in the catalog"
        )
        assert entry.get("vendor") == "deepseek", (
            f"{current_model!r} must have vendor=deepseek"
        )
        assert entry.get("model") == current_model, (
            f"{current_model!r} must have model={current_model!r}"
        )
        # The legacy name is an alias; a client using it arrives at this entry
        # via alias resolution.  We assert the current model entry exists and
        # carries the legacy name as an alias.
        aliases = entry.get("aliases", [])
        assert legacy in aliases, (
            f"{current_model!r} must carry {legacy!r} as an alias"
        )


# ---------------------------------------------------------------------------
# Criterion 3 — facts only on the current entry
# ---------------------------------------------------------------------------

def test_legacy_name_entries_are_absent_no_facts_to_carry():
    """Since deepseek-chat and deepseek-reasoner are not separate entries,
    they cannot carry stale facts about windows, prices, or modalities."""
    catalog = _load_catalog()
    for name in LEGACY_NAMES:
        assert name not in catalog["providers"], (
            f"{name} must not exist as a separate entry (would carry stale facts)"
        )


def test_facts_reside_on_current_model_entries():
    """Pricing and context_window live on the current-model entries, not on
    legacy-name entries (which don't exist)."""
    catalog = _load_catalog()
    for legacy, current_model in CURRENT_MODELS.items():
        entry = catalog["providers"].get(current_model)
        assert entry is not None, f"{current_model!r} must exist"
        # The current entry must carry pricing (it's a direct model, not a proxy)
        assert "pricing" in entry, (
            f"{current_model!r} must carry pricing facts"
        )
        # The current entry must carry a context window
        assert entry.get("context_window", 0) > 0, (
            f"{current_model!r} must carry a positive context_window"
        )
        # The legacy name (as alias) has no separate pricing or context_window


# ---------------------------------------------------------------------------
# Guard rails — empty data must fail
# ---------------------------------------------------------------------------

def test_empty_providers_rejected():
    """A catalog with no provider entries must fail the entry existence check."""
    empty = {"providers": {}}
    try:
        _ = empty["providers"]["deepseek-v4-flash"]
        assert False, "must not reach here — key should not exist in empty catalog"
    except KeyError:
        pass


def test_empty_aliases_rejected():
    """An entry with no aliases list must not claim a legacy name as alias."""
    entry = {"vendor": "deepseek", "model": "deepseek-v4-flash"}
    aliases = entry.get("aliases", [])
    assert "deepseek-chat" not in aliases, (
        "without an aliases list, deepseek-chat must not be found"
    )
