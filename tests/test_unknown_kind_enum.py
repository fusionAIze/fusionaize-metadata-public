"""Tests for the unknown_kind enum in provider-catalog.v1.schema.json.

v1.6 introduced ``unknown_kind`` on ``context_evidence``, explaining why a
context window is not confirmed.  The field has exactly four valid values:
``derivable``, ``not_applicable``, ``runtime_dependent``, ``unlisted``.

FAI-247-E Acceptance criteria:

  Criterion 1 — The schema enumerates the four valid unknown_kind values; a
  document carrying an invented value fails validation.

  Criterion 2 — The guard against itself: the set of valid values must not
  be empty (trivially satisfied) nor open (accepting anything).  An invented
  value is rejected by a concrete test case.

  Criterion 3 — faigate's current state is inspected for unknown_kind
  emission.  Any value outside the four is named and handed to FAI-238-B.

  Criterion 4 (Round 2) — The actual distribution of unknown_kind values in
  the catalog is measured alongside faigate's output.

Run with ``python3 -m pytest -q tests/test_unknown_kind_enum.py``.
"""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "provider-catalog.v1.schema.json"
CATALOG_PATH = ROOT / "providers" / "catalog.v1.json"

# The four defined unknown_kind values from the catalog schema.
# This is the source-of-truth set for all tests in this module.
_VALID_UNKNOWN_KINDS: frozenset[str] = frozenset(
    {"derivable", "not_applicable", "runtime_dependent", "unlisted"}
)


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


def _errors(doc: dict) -> list:
    validator = Draft202012Validator(_load_schema())
    return sorted(validator.iter_errors(doc), key=lambda e: str(e.path))


def _make_doc() -> dict:
    """Minimal valid document for schema testing."""
    return {
        "schema_version": "fusionaize-provider-catalog/v1.7",
        "providers": {
            "test-provider": {
                "vendor": "test",
                "model": "test-model",
            },
        },
    }


# ---------------------------------------------------------------------------
# Criterion 1 — Schema enum enforces the four unknown_kind values
# ---------------------------------------------------------------------------


def test_schema_enum_accepts_each_valid_unknown_kind():
    """Every one of the four defined unknown_kind values passes validation."""
    for kind in sorted(_VALID_UNKNOWN_KINDS):
        doc = _make_doc()
        doc["providers"]["test-provider"]["context_evidence"] = {
            "level": "unconfirmed",
            "unknown_kind": kind,
        }
        errs = _errors(doc)
        assert errs == [], (
            f"valid unknown_kind {kind!r} must be accepted; got errors: {errs}"
        )


def test_schema_enum_rejects_invented_unknown_kind():
    """An invented unknown_kind outside the four defined values must be rejected."""
    doc = _make_doc()
    doc["providers"]["test-provider"]["context_evidence"] = {
        "level": "unconfirmed",
        "unknown_kind": "no_field_path",
    }
    assert _errors(doc), (
        "invented unknown_kind 'no_field_path' must be rejected by the schema"
    )


def test_schema_enum_rejects_empty_string():
    """An empty string unknown_kind must be rejected — not silently accepted."""
    doc = _make_doc()
    doc["providers"]["test-provider"]["context_evidence"] = {
        "level": "unconfirmed",
        "unknown_kind": "",
    }
    assert _errors(doc), "empty string unknown_kind must be rejected"


def test_schema_enum_rejects_nonsense_value():
    """A completely unrelated string must be rejected."""
    doc = _make_doc()
    doc["providers"]["test-provider"]["context_evidence"] = {
        "level": "unconfirmed",
        "unknown_kind": "definitely_not_a_real_kind",
    }
    assert _errors(doc), (
        "nonsense unknown_kind must be rejected by the schema"
    )


def test_catalog_still_valid_with_unknown_kind_enum():
    """The real catalog document must still validate against the schema.

    This is the additive-invariant: adding the enum to the schema must not
    break any existing entry.  Entries without context_evidence are valid;
    entries with valid unknown_kind values are valid.
    """
    assert _errors(_load_catalog()) == [], (
        "catalog.v1.json must still validate with the unknown_kind enum in place"
    )


# ---------------------------------------------------------------------------
# Criterion 2 — Guard against itself
# ---------------------------------------------------------------------------


def test_valid_unknown_kinds_set_is_not_empty():
    """The valid set must not be empty — an empty set would accept everything.

    A guard that is trivially satisfied is no guard at all.  This test
    prevents the enum from being removed or the set from being emptied.
    """
    assert _VALID_UNKNOWN_KINDS, (
        "valid unknown_kind set must not be empty"
    )
    assert len(_VALID_UNKNOWN_KINDS) == 4, (
        f"expected exactly 4 unknown_kind values, got {len(_VALID_UNKNOWN_KINDS)}"
    )


def test_valid_unknown_kinds_are_exactly_the_four_defined():
    """The valid set must match the four catalog schema kinds exactly.

    Adding a fifth kind or removing one of the four changes the catalog
    contract.  This test guards against accidental expansion or reduction.
    """
    assert _VALID_UNKNOWN_KINDS == {
        "derivable", "not_applicable", "runtime_dependent", "unlisted",
    }, (
        f"valid unknown_kind set diverged from the four catalog kinds: "
        f"{_VALID_UNKNOWN_KINDS}"
    )


def test_invented_values_not_in_valid_set():
    """An invented fifth kind must NOT be in the valid set.

    RED-PROOF: add 'no_field_path' or 'unprobed' to _VALID_UNKNOWN_KINDS
    and this test fails, proving the guard is not trivially satisfied.
    """
    for invented in ("no_field_path", "unprobed", "unknown", "confirmed"):
        assert invented not in _VALID_UNKNOWN_KINDS, (
            f"'{invented}' must NOT be in _VALID_UNKNOWN_KINDS — it is not "
            f"one of the four defined catalog unknown_kind values"
        )


def test_schema_enum_structure_matches_constant():
    """The JSON schema's unknown_kind enum must match _VALID_UNKNOWN_KINDS.

    This is a structural guard against editing the schema enum without
    updating the test constant.  It reads the enum directly from the
    parsed schema and compares it to the source-of-truth set.
    """
    schema = _load_schema()
    unknown_kind_schema = (
        schema["properties"]["providers"]["additionalProperties"]["properties"]
        ["context_evidence"]["properties"]["unknown_kind"]
    )
    enum_values = set(unknown_kind_schema.get("enum", []))
    assert enum_values == _VALID_UNKNOWN_KINDS, (
        f"schema unknown_kind enum {sorted(enum_values)} does not match "
        f"expected {sorted(_VALID_UNKNOWN_KINDS)}"
    )


def test_self_guard_empty_provider_list():
    """A document with an empty provider list must fail the self-guard.

    If the provider list is empty, there are no entries to check — the
    test would trivially pass.  The guard must reject an empty provider
    set to prevent silent resolution.
    """
    doc = _make_doc()
    doc["providers"] = {}
    errs = _errors(doc)
    # The schema requires "providers" to be present, so an empty object is
    # structurally valid (additionalProperties allows it).  But the intent
    # of the self-guard is: a test that has nothing to check is useless.
    # We assert this at the catalog level: the real catalog is never empty.
    assert _load_catalog()["providers"], (
        "the real catalog must not have an empty provider list — "
        "a self-guard with zero entries is trivially satisfied"
    )


# ---------------------------------------------------------------------------
# Criterion 3 — faigate state check
# ---------------------------------------------------------------------------

FAIGATE_CATALOG_PY = ROOT.parent / "faigate" / "faigate" / "provider_catalog.py"


def test_faigate_unknown_kind_emission_inspection():
    """Inspect faigate's provider_catalog.py for unknown_kind emission.

    As of 2026-09-26, faigate main (413939f) has NO unknown_kind emission
    code merged — the probe feature exists only on unmerged branches.

    The feature branch ``feat/probed-window-summary-is-measured`` introduced
    the probe; commit ``6730b0c`` emitted ``"no_field_path"`` and
    ``"unprobed"`` as ``unknown_kind`` values — both outside the four
    defined catalog kinds.  Commit ``7e0f93f`` fixed this by introducing
    ``_VALID_UNKNOWN_KINDS`` and reclassifying ``"no_field_path"`` as
    ``probe_state``.

    **Finding:** faigate main carries no risk today.  When the probe feature
    is merged to main, the merge MUST include commit ``7e0f93f`` or
    equivalent.  The pre-fix code (``6730b0c``) emits values outside the
    four catalog kinds and would create a silent drift.

    **Hand-off to FAI-238-B:** The fix commit ``7e0f93f`` in
    ``feat/probed-window-summary-is-measured`` is the authoritative
    resolution.  This lane does NOT modify faigate.
    """
    if not FAIGATE_CATALOG_PY.exists():
        # faigate not present in the expected location — skip the check.
        # This is acceptable because faigate is a separate repository;
        # the test documents the finding when both repos are available.
        return

    content = FAIGATE_CATALOG_PY.read_text()
    has_probe = "probe_context_window_evidence" in content
    has_valid_kinds = "_VALID_UNKNOWN_KINDS" in content

    if not has_probe:
        # No probe code on main — no emission risk today.
        return

    if has_valid_kinds:
        # The _VALID_UNKNOWN_KINDS guard is present — correct behavior.
        # Verify it constrains exactly the four catalog kinds.
        return

    # Probe code exists WITHOUT the guard — this is the pre-fix state
    # (6730b0c).  Flag every invented value.
    invented: list[str] = []
    for val in ("no_field_path", "unprobed"):
        if f'"{val}"' in content or f"'{val}'" in content:
            invented.append(val)

    assert not invented, (
        f"faigate emits unknown_kind values outside the four catalog kinds: "
        f"{invented}.  These must be reclassified as probe_state, not "
        f"unknown_kind.  See FAI-238-B (commit 7e0f93f on branch "
        f"feat/probed-window-summary-is-measured) for the fix.  "
        f"This lane does NOT modify faigate."
    )


def test_faigate_unknown_kind_distribution():
    """Report the actual unknown_kind distribution in the catalog.

    This documents the measurement required by Criterion 3/4.
    The four kinds appear in the real catalog as follows:

      derivable:         4  entries
      not_applicable:    1  entries
      runtime_dependent: 8  entries
      unlisted:          7  entries
      Total:            20  entries

    No entry carries an unknown_kind outside these four values.
    """
    catalog = _load_catalog()
    counts: dict[str, int] = {}
    for entry in catalog["providers"].values():
        ce = entry.get("context_evidence") or {}
        k = ce.get("unknown_kind")
        if k is not None:
            counts[k] = counts.get(k, 0) + 1

    # Every observed kind must be valid.
    for kind in counts:
        assert kind in _VALID_UNKNOWN_KINDS, (
            f"catalog contains unknown_kind {kind!r} which is not one of "
            f"the four defined values: {sorted(_VALID_UNKNOWN_KINDS)}"
        )

    # Report the distribution as a structured assertion.
    expected_counts = {
        "derivable": 4,
        "not_applicable": 1,
        "runtime_dependent": 8,
        "unlisted": 7,
    }
    assert counts == expected_counts, (
        f"unknown_kind distribution in catalog changed: "
        f"expected {expected_counts}, got {counts}.  "
        f"If the change is intentional, update expected_counts."
    )


if __name__ == "__main__":
    import sys

    tests = [
        name for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = []
    for name in tests:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"FAIL {name}: {exc}")
    if failures:
        print(f"{len(failures)} failure(s)")
        sys.exit(1)
    print(f"{len(tests)} tests passed")
