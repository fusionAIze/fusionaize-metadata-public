"""Acceptance tests for the retirement rule (FAI-247-B, lane retirement-is-a-rule).

Criterion 1: an entry that no source confirms across a stated number of
  collections is retired, and the retirement names the reason and the last
  source that confirmed it.

Criterion 2: a name the operator has configured is NEVER removed — it is
  flagged as unconfirmed and stays routable.

Criterion 3: a retired entry does not disappear silently: it is reported,
  and the report says what was retired and what was spared.

No test reaches the network.  All collection results and the operator list
are constructed inline as fixtures.

Run with ``python3 -m pytest -q`` (or the suite's own interpreter).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import sys
sys.path.insert(0, str(ROOT / "scripts"))


def _call_retirement(catalog, confirmations, operator_names, threshold):
    """Lazy import: retire.py is only loaded when this function is called.

    If retire.py does not exist (e.g. on the base commit 26de04a), returns
    the catalog unchanged and a minimal empty report so that assertion-based
    red-proof tests fail naturally with ``AssertionError`` rather than
    ``ImportError`` during collection.
    """
    try:
        from retire import apply_retirement as _real_apply_retirement
        return _real_apply_retirement(catalog, confirmations, operator_names, threshold)
    except ImportError:
        return catalog, {
            "threshold": threshold,
            "sources": confirmations.get("sources", []),
            "total_entries": len(catalog.get("providers", {})),
            "retired": [],
            "spared": [],
            "revived": [],
            "tracked": [],
        }


# ---------------------------------------------------------------------------
# Fixtures — recorded collection results, no network
# ---------------------------------------------------------------------------

def _catalog(*names: str) -> dict:
    """A minimal catalog with one active entry per name."""
    return {
        "schema_version": "fusionaize-provider-catalog/v1.4",
        "providers": {
            name: {"tier_status": "active", "last_reviewed": "2026-04-01"}
            for name in names
        },
    }


def _confirmations(sources, confirmed, *, collected_at="2026-09-26"):
    """Build a confirmation document as FAI-245-D would emit it.

    ``sources`` is the list of source names that ran this round.
    ``confirmed`` maps provider-name -> list of source names that
    confirmed it.
    """
    return {
        "collected_at": collected_at,
        "sources": list(sources),
        "confirmations": {
            name: {"sources": srcs, "last_confirmed_at": collected_at}
            for name, srcs in confirmed.items()
        },
    }


# ---------------------------------------------------------------------------
# Criterion 1 — an unconfirmed entry is retired, with reason + last source
# ---------------------------------------------------------------------------

def test_entry_unconfirmed_across_threshold_is_retired():
    """An entry confirmed by no source in N rounds is retired."""
    catalog = _catalog("ghost-provider")
    confs = _confirmations(["openrouter"], {})

    catalog, report = _call_retirement(catalog, confs, set(), threshold=1)

    entry = catalog["providers"]["ghost-provider"]
    assert entry["tier_status"] == "deprecated", (
        "an entry no source confirms must be retired"
    )
    assert report["retired"], "the retirement must appear in the report"


def test_retirement_names_reason_and_last_confirming_source():
    """The retirement record names WHY and WHICH source confirmed it last."""
    catalog = _catalog("fading-provider")
    # Round 1: confirmed by openrouter.
    confs_round1 = _confirmations(["openrouter"], {"fading-provider": ["openrouter"]})
    catalog, _ = _call_retirement(catalog, confs_round1, set(), threshold=1)

    # Round 2: not confirmed by anything.
    confs_round2 = _confirmations(["openrouter"], {})
    catalog, report = _call_retirement(catalog, confs_round2, set(), threshold=1)

    retired = report["retired"]
    assert len(retired) == 1
    record = retired[0]
    assert record["name"] == "fading-provider"
    assert "openrouter" in record["last_confirming_source"], (
        f"the retirement must name the last confirming source; got "
        f"{record['last_confirming_source']!r}"
    )
    assert "2026-09-26" in record["last_confirming_source"], (
        "the retirement must name WHEN the source last confirmed"
    )
    assert record["reason"], "the retirement must state a reason"

    entry = catalog["providers"]["fading-provider"]
    assert entry["retirement"]["misses"] >= 1
    assert entry["retirement"]["last_confirmed_by"] == "openrouter"


def test_entry_below_threshold_is_not_retired():
    """An entry unconfirmed for fewer rounds than the threshold is NOT retired."""
    catalog = _catalog("slow-fader")
    confs = _confirmations(["openrouter"], {})

    catalog, report = _call_retirement(catalog, confs, set(), threshold=3)

    entry = catalog["providers"]["slow-fader"]
    assert entry["tier_status"] == "active", (
        "an entry below the miss threshold must stay active"
    )
    assert report["retired"] == []
    assert report["tracked"], "the entry must be tracked, not silently ignored"


def test_threshold_is_stated_in_report():
    """The stated number of collections appears in the report."""
    catalog = _catalog("anything")
    confs = _confirmations(["openrouter"], {})

    _, report = _call_retirement(catalog, confs, set(), threshold=5)

    assert report["threshold"] == 5, "the report must state the threshold used"


# ---------------------------------------------------------------------------
# Criterion 2 — operator-configured names are never removed
# ---------------------------------------------------------------------------

def test_operator_configured_entry_is_never_retired():
    """A name the operator configured is never removed, even when unconfirmed."""
    catalog = _catalog("operator-pick")
    confs = _confirmations(["openrouter"], {})

    catalog, report = _call_retirement(
        catalog, confs, {"operator-pick"}, threshold=1,
    )

    entry = catalog["providers"]["operator-pick"]
    assert entry["tier_status"] == "active", (
        "an operator-configured entry must never be retired"
    )
    assert "operator-pick" in [s["name"] for s in report["spared"]], (
        "the spared entry must be reported"
    )
    assert report["retired"] == []


def test_routing_survives_for_operator_configured_entry():
    """The routing survives: the entry is still present and routable."""
    catalog = _catalog("anthropic-claude")
    confs = _confirmations(["openrouter"], {})

    catalog, _ = _call_retirement(
        catalog, confs, {"anthropic-claude"}, threshold=10,
    )

    assert "anthropic-claude" in catalog["providers"], (
        "the operator-configured name must still be in the catalog after "
        "many unconfirmed rounds — removing it would break routing"
    )
    assert catalog["providers"]["anthropic-claude"]["tier_status"] != "deprecated", (
        "the entry must stay routable, not be retired"
    )


def test_operator_configured_unconfirmed_is_flagged():
    """The operator-configured entry is flagged as unconfirmed, not hidden."""
    catalog = _catalog("operator-pick")
    confs = _confirmations(["openrouter"], {})

    catalog, report = _call_retirement(
        catalog, confs, {"operator-pick"}, threshold=1,
    )

    spared = [s for s in report["spared"] if s["name"] == "operator-pick"]
    assert spared, "the spared entry must appear in the report with a reason"
    assert "operator" in spared[0]["reason"].lower()


# ---------------------------------------------------------------------------
# Criterion 3 — a retired entry does not disappear silently
# ---------------------------------------------------------------------------

def test_retired_entry_stays_in_catalog():
    """A retired entry is not deleted — it stays, marked retired."""
    catalog = _catalog("ghost-provider")
    confs = _confirmations(["openrouter"], {})

    catalog, _ = _call_retirement(catalog, confs, set(), threshold=1)

    assert "ghost-provider" in catalog["providers"], (
        "a retired entry must remain in the catalog, marked — never silently "
        "deleted"
    )


def test_report_separates_retired_from_spared():
    """The report says what was retired AND what was spared."""
    catalog = _catalog("ghost-provider", "operator-pick")
    confs = _confirmations(["openrouter"], {})

    _, report = _call_retirement(catalog, confs, {"operator-pick"}, threshold=1)

    retired_names = [r["name"] for r in report["retired"]]
    spared_names = [s["name"] for s in report["spared"]]

    assert retired_names == ["ghost-provider"]
    assert spared_names == ["operator-pick"]
    assert report["total_entries"] == 2


def test_report_has_before_and_after_counts():
    """The report carries before/after counts for the run."""
    catalog = _catalog("a", "b", "operator-pick")
    confs = _confirmations(["openrouter"], {"a": ["openrouter"]})

    _, report = _call_retirement(catalog, confs, {"operator-pick"}, threshold=1)

    assert report["total_entries"] == 3
    assert len(report["retired"]) == 1
    assert len(report["spared"]) == 1


# ---------------------------------------------------------------------------
# Criterion 4 — retirement is reversible
# ---------------------------------------------------------------------------

def test_later_confirmation_revives_retired_entry():
    """A later confirmation revives the entry without hand editing."""
    catalog = _catalog("comeback")
    # Retire it.
    catalog, _ = _call_retirement(
        catalog, _confirmations(["openrouter"], {}), set(), threshold=1,
    )
    assert catalog["providers"]["comeback"]["tier_status"] == "deprecated"

    # A later collection confirms it again.
    confs = _confirmations(["openrouter"], {"comeback": ["openrouter"]})
    catalog, report = _call_retirement(catalog, confs, set(), threshold=1)

    entry = catalog["providers"]["comeback"]
    assert entry["tier_status"] == "active", (
        "a later confirmation must revive the entry without hand editing"
    )
    assert entry["retirement"]["misses"] == 0
    assert "comeback" in [r["name"] for r in report["revived"]]


def test_revival_clears_the_retirement_reason():
    """Reviving clears the retirement reason so the entry is clean again."""
    catalog = _catalog("comeback")
    catalog, _ = _call_retirement(
        catalog, _confirmations(["openrouter"], {}), set(), threshold=1,
    )
    assert "reason" in catalog["providers"]["comeback"]["retirement"]

    confs = _confirmations(["openrouter"], {"comeback": ["openrouter"]})
    catalog, _ = _call_retirement(catalog, confs, set(), threshold=1)

    assert "reason" not in catalog["providers"]["comeback"]["retirement"], (
        "the stale retirement reason must be cleared on revival"
    )


def test_multiple_retire_revive_cycles_are_idempotent():
    """Retire, revive, retire again — each transition is clean."""
    catalog = _catalog("yo-yo")

    # Retire
    catalog, r1 = _call_retirement(
        catalog, _confirmations(["openrouter"], {}), set(), threshold=1,
    )
    assert len(r1["retired"]) == 1
    assert catalog["providers"]["yo-yo"]["tier_status"] == "deprecated"

    # Revive
    catalog, r2 = _call_retirement(
        catalog, _confirmations(["openrouter"], {"yo-yo": ["openrouter"]}),
        set(), threshold=1,
    )
    assert len(r2["revived"]) == 1
    assert catalog["providers"]["yo-yo"]["tier_status"] == "active"

    # Retire again
    catalog, r3 = _call_retirement(
        catalog, _confirmations(["openrouter"], {}), set(), threshold=1,
    )
    assert len(r3["retired"]) == 1
    assert catalog["providers"]["yo-yo"]["tier_status"] == "deprecated"


# ---------------------------------------------------------------------------
# Criterion 5 — guards against the rule itself
# ---------------------------------------------------------------------------

def test_empty_source_set_retires_nothing():
    """THE most important guard: an empty set of collected sources must not
    retire anything, no matter how many misses the entries have."""
    catalog = _catalog("a", "b", "c")
    confs = _confirmations([], {})  # no sources ran, nothing confirmed

    catalog, report = _call_retirement(catalog, confs, set(), threshold=1)

    for name in ("a", "b", "c"):
        assert catalog["providers"][name]["tier_status"] == "active", (
            f"{name} was retired on an empty source set — a rule that "
            "deletes everything when input is missing is worse than no rule"
        )
    assert report["retired"] == []
    assert report.get("aborted") is True, (
        "the report must state that the run was aborted because no source "
        "was collected"
    )


def test_empty_source_set_reported_as_aborted():
    """An aborted run names the reason it did not act."""
    catalog = _catalog("a")
    confs = _confirmations([], {})

    _, report = _call_retirement(catalog, confs, set(), threshold=1)

    assert report.get("aborted") is True
    assert "no source" in report.get("abort_reason", "").lower()


def test_failed_collection_is_reported_and_retires_nothing():
    """A collection that FAILS (marked failed) retires nothing."""
    catalog = _catalog("a", "b")
    confs = {
        "collected_at": "2026-09-26",
        "sources": ["openrouter"],
        "failed": True,
        "confirmations": {},
    }

    catalog, report = _call_retirement(catalog, confs, set(), threshold=1)

    assert catalog["providers"]["a"]["tier_status"] == "active"
    assert catalog["providers"]["b"]["tier_status"] == "active"
    assert report["retired"] == []
    assert report.get("aborted") is True


def test_empty_catalog_and_empty_sources_do_not_crash_or_retire():
    """Both empty: no crash, no retirement."""
    catalog = {"schema_version": "v1.4", "providers": {}}
    confs = _confirmations([], {})

    catalog, report = _call_retirement(catalog, confs, set(), threshold=1)

    assert report["retired"] == []
    assert report["total_entries"] == 0


# ---------------------------------------------------------------------------
# RED PROOF
# ---------------------------------------------------------------------------

def test_red_proof_retirement_changes_behavior():
    """RED PROOF: on the base commit this rule does not exist.

    At base 26de04a there is no ``scripts/retire.py`` and thus no
    ``apply_retirement``.  The lazy import in ``_call_retirement``
    catches the ``ImportError`` and returns the catalog unchanged with
    an empty report.

    This assertion proves the retirement *behavior* is new: an
    unconfirmed non-operator entry moves from active to deprecated,
    which nothing in the base catalog does.  On the base the entry stays
    active and the assertion fails — a real ``AssertionError``, not a
    collection error.
    """
    catalog = _catalog("unconfirmed-entry")
    confs = _confirmations(["openrouter"], {})

    catalog, report = _call_retirement(catalog, confs, set(), threshold=1)

    entry = catalog["providers"]["unconfirmed-entry"]
    assert entry["tier_status"] == "deprecated", (
        "RED PROOF: the retirement rule must move an unconfirmed entry from "
        "active to deprecated — no code at base 26de04a does this"
    )
    assert report["retired"][0]["name"] == "unconfirmed-entry"


if __name__ == "__main__":
    import sys as _sys

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
        _sys.exit(1)
    print(f"{len(tests)} tests passed")
