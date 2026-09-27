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
import subprocess
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


# FAI-247-B2 tightened the contract: an EMPTY operator list now aborts the
# run (``aborted``/``abort_reason``) instead of retiring entries, because a
# run without the operator carve-out cannot tell a dead entry from a live
# route.  These tests are about *what happens to entries*, not about the
# operator list, so they pass a placeholder operator name.  The tests that
# specifically pin the empty-operator-list failure in
# tests/test_retirement_rule_real_input.py use a genuinely empty set.
#
# This does NOT remove a rigel: ``test_empty_source_set_*`` and
# ``test_failed_collection_*`` keep their empty inputs, and neither depends
# on the operator list for its verdict.
_PLACEHOLDER_OPERATOR = {"gateway-local"}


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

    catalog, report = _call_retirement(catalog, confs, _PLACEHOLDER_OPERATOR, threshold=1)

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
    catalog, _ = _call_retirement(catalog, confs_round1, _PLACEHOLDER_OPERATOR, threshold=1)

    # Round 2: not confirmed by anything.
    confs_round2 = _confirmations(["openrouter"], {})
    catalog, report = _call_retirement(catalog, confs_round2, _PLACEHOLDER_OPERATOR, threshold=1)

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

    catalog, report = _call_retirement(catalog, confs, _PLACEHOLDER_OPERATOR, threshold=3)

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

    _, report = _call_retirement(catalog, confs, _PLACEHOLDER_OPERATOR, threshold=5)

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

    catalog, _ = _call_retirement(catalog, confs, _PLACEHOLDER_OPERATOR, threshold=1)

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
        catalog, _confirmations(["openrouter"], {}), _PLACEHOLDER_OPERATOR, threshold=1,
    )
    assert catalog["providers"]["comeback"]["tier_status"] == "deprecated"

    # A later collection confirms it again.
    confs = _confirmations(["openrouter"], {"comeback": ["openrouter"]})
    catalog, report = _call_retirement(catalog, confs, _PLACEHOLDER_OPERATOR, threshold=1)

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
        catalog, _confirmations(["openrouter"], {}), _PLACEHOLDER_OPERATOR, threshold=1,
    )
    assert "reason" in catalog["providers"]["comeback"]["retirement"]

    confs = _confirmations(["openrouter"], {"comeback": ["openrouter"]})
    catalog, _ = _call_retirement(catalog, confs, _PLACEHOLDER_OPERATOR, threshold=1)

    assert "reason" not in catalog["providers"]["comeback"]["retirement"], (
        "the stale retirement reason must be cleared on revival"
    )


def test_multiple_retire_revive_cycles_are_idempotent():
    """Retire, revive, retire again — each transition is clean."""
    catalog = _catalog("yo-yo")

    # Retire
    catalog, r1 = _call_retirement(
        catalog, _confirmations(["openrouter"], {}), _PLACEHOLDER_OPERATOR, threshold=1,
    )
    assert len(r1["retired"]) == 1
    assert catalog["providers"]["yo-yo"]["tier_status"] == "deprecated"

    # Revive
    catalog, r2 = _call_retirement(
        catalog, _confirmations(["openrouter"], {"yo-yo": ["openrouter"]}),
        _PLACEHOLDER_OPERATOR, threshold=1,
    )
    assert len(r2["revived"]) == 1
    assert catalog["providers"]["yo-yo"]["tier_status"] == "active"

    # Retire again
    catalog, r3 = _call_retirement(
        catalog, _confirmations(["openrouter"], {}), _PLACEHOLDER_OPERATOR, threshold=1,
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

    catalog, report = _call_retirement(catalog, confs, _PLACEHOLDER_OPERATOR, threshold=1)

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

    _, report = _call_retirement(catalog, confs, _PLACEHOLDER_OPERATOR, threshold=1)

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

    catalog, report = _call_retirement(catalog, confs, _PLACEHOLDER_OPERATOR, threshold=1)

    assert catalog["providers"]["a"]["tier_status"] == "active"
    assert catalog["providers"]["b"]["tier_status"] == "active"
    assert report["retired"] == []
    assert report.get("aborted") is True


def test_empty_catalog_and_empty_sources_do_not_crash_or_retire():
    """Both empty: no crash, no retirement."""
    catalog = {"schema_version": "v1.4", "providers": {}}
    confs = _confirmations([], {})

    catalog, report = _call_retirement(catalog, confs, _PLACEHOLDER_OPERATOR, threshold=1)

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

    catalog, report = _call_retirement(catalog, confs, _PLACEHOLDER_OPERATOR, threshold=1)

    entry = catalog["providers"]["unconfirmed-entry"]
    assert entry["tier_status"] == "deprecated", (
        "RED PROOF: the retirement rule must move an unconfirmed entry from "
        "active to deprecated — no code at base 26de04a does this"
    )
    assert report["retired"][0]["name"] == "unconfirmed-entry"



# ===========================================================================
# FAI-247-B2 — the same rule driven by a REAL collector survey
#
# The fixtures and helpers above are built by hand in the shape retire.py
# documents.  The collector in the private repo (FAI-245-D) emits a
# different shape, and the two were never put side by side, so the rule ran
# into the void against real input while each lane stayed green against its
# own invention.  The tests below close that gap against a byte-exact copy
# of a real collector run and the real operator list.
#
# Fixtures: tests/fixtures/collected.json (66 entries, 2 confirmed, 57
# plausible, 7 unlisted, 65 contradictions) and
# tests/fixtures/operator-providers.json (38 names: 15 ids, 1 alias, 22
# unresolved faigate route names).  No test reaches the network.
# ===========================================================================

FIXTURES = ROOT / "tests" / "fixtures"
CATALOG = ROOT / "providers" / "catalog.v1.json"
COLLECTED = FIXTURES / "collected.json"
OPERATOR = FIXTURES / "operator-providers.json"
RETIRE = ROOT / "scripts" / "retire.py"


def _run_cli(catalog_path, survey_path, operator_path, threshold, *extra):
    """Run scripts/retire.py as a process and return the CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(RETIRE),
         "--catalog", str(catalog_path),
         "--survey", str(survey_path),
         "--operator", str(operator_path),
         "--threshold", str(threshold),
         *extra],
        capture_output=True, text=True,
    )


def _call_retirement_real(catalog, survey, operator_names, threshold):
    """Drive the rule against the collector's real shape, via the adapter.

    The named adapter is ``adapt_collected_survey``: it translates the
    collector's ``{enrichment, contradictions, coverage}`` document into
    the ``{sources, confirmations}`` map the rule consumes.  This is the
    production wiring (retire.py's CLI uses it too) — the test does not
    build its own input.

    The adapter is imported lazily for the same reason
    ``_call_retirement`` is: at the base commit it does not exist.  Let
    the ``ImportError`` escape here and the tests fail during collection
    with a traceback that proves nothing; catch it and return an empty
    report instead, so each test reaches its own assertion and fails
    with a real ``AssertionError``.  A red proof has to be an assertion
    the fix flips, not a missing import.
    """
    try:
        from retire import adapt_collected_survey, apply_retirement
    except ImportError:
        return catalog, {
            "threshold": threshold,
            "sources": [],
            "total_entries": len(catalog.get("providers", {})),
            "retired": [],
            "spared": [],
            "revived": [],
            "tracked": [],
            "operator_names_resolved": 0,
            "operator_names_unresolved": [],
        }

    confirmations = adapt_collected_survey(survey)
    return apply_retirement(catalog, confirmations, operator_names, threshold)


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _real_survey():
    return _fixture("collected.json")


def _real_catalog():
    return json.loads(CATALOG.read_text())


def _real_operator_names():
    return set(_fixture("operator-providers.json")["providers"])


# ---------------------------------------------------------------------------
# Fixture integrity — the proof must hang on the real collector output
# ---------------------------------------------------------------------------

def test_fixture_is_the_real_collector_shape():
    """The fixture is a collector run, not a hand-built stand-in.

    The collector's documented top-level shape is
    ``{schema_version, enrichment, contradictions, coverage}``.  A
    confirmation document in retire.py's old shape (``sources`` /
    ``confirmations``) is exactly what this file must NOT contain.
    """
    survey = _real_survey()

    assert survey["schema_version"] == "fusionaize-collected/v1"
    assert set(survey) >= {"enrichment", "contradictions", "coverage"}
    assert "confirmations" not in survey, (
        "the fixture must be the collector's output, not a hand-built "
        "confirmation document"
    )
    assert "sources" not in survey, (
        "the fixture must be the collector's output, not a hand-built "
        "confirmation document"
    )

    coverage = survey["coverage"]
    assert coverage["total_providers"] == 66
    assert coverage["confirmed"] == 2
    assert coverage["plausible"] == 57
    assert coverage["unlisted"] == 7
    assert len(survey["contradictions"]) == 65
    assert len(survey["enrichment"]) == 59


def test_fixture_operator_list_has_38_names():
    """The operator fixture is the real live list of 38 names."""
    names = _real_operator_names()
    assert len(names) == 38
    assert "anthropic-claude" in names


# ---------------------------------------------------------------------------
# Criterion 1 — the rule reads what the survey really delivers
# ---------------------------------------------------------------------------

def test_rule_actually_acts_on_the_real_survey():
    """Against real input the rule must act, not abort into the void.

    This is the core regression: with the real survey and the real
    operator list the old rule reported zero retired, zero spared and
    zero tracked — its abort guard fired on a ``sources`` key that the
    collector never emits.  A run that touches nothing is not a run.
    """
    catalog, report = _call_retirement_real(
        _real_catalog(), _real_survey(), _real_operator_names(), 1,
    )

    assert not report.get("aborted"), (
        "a real, non-empty survey must not be reported as an aborted run"
    )
    acted = len(report["retired"]) + len(report["spared"]) + len(report["tracked"])
    assert acted > 0, (
        "the rule touched no entry at all against a real survey — it is "
        "reading a shape the collector does not produce"
    )
    assert report["total_entries"] == 66


def test_confirmed_entries_from_real_survey_are_not_retired():
    """The two confirmed entries are exactly the ones the survey confirms.

    The collector marks ``byteplus`` and ``byteplus-plan`` as
    ``confirmed`` (provider API answered).  Everything else is
    ``plausible`` (present in one mixed registry) or ``unlisted``.
    A confirmed entry must never be retired, however often the rule runs.
    """
    survey = _real_survey()
    catalog, report = _call_retirement_real(
        _real_catalog(), survey, _real_operator_names(), 1,
    )

    assert survey["enrichment"]["byteplus"]["api_models"]["evidence"]["level"] == "confirmed"
    assert survey["enrichment"]["byteplus-plan"]["api_models"]["evidence"]["level"] == "confirmed"

    retired_names = {r["name"] for r in report["retired"]}
    assert "byteplus" not in retired_names
    assert "byteplus-plan" not in retired_names
    assert catalog["providers"]["byteplus"].get("tier_status") != "deprecated"
    assert catalog["providers"]["byteplus-plan"].get("tier_status") != "deprecated"


def test_real_survey_retires_an_unconfirmed_non_operator_entry():
    """With threshold 1 an unconfirmed, non-operator entry is retired.

    The honest candidate is an entry the survey does not vouch for *at
    any level* and the operator does not configure.  Seven catalog ids
    are absent from the survey's enrichment entirely; ``qwen-portal`` is
    not one of them (it is merely ``plausible``), so the preconditions
    are read out of the fixture rather than assumed.

    ``plausible`` is a miss for retirement purposes: only a ``confirmed``
    entry — one a provider API actually answered — counts as a
    confirmation.  ``qwen-portal`` must therefore be retired too.
    """
    survey = _real_survey()
    operator_names = _real_operator_names()
    providers = _real_catalog()["providers"]

    # Anchor the expectation to the fixture instead of assuming it.
    absent = sorted(set(providers) - set(survey["enrichment"]))
    assert absent == [
        "clawrouter", "kilo-auto-balanced", "kilo-auto-free",
        "kilo-auto-frontier", "lmstudio", "longcat", "vllm",
    ]
    for name in absent:
        assert name not in operator_names

    catalog, report = _call_retirement_real(
        _real_catalog(), survey, operator_names, 1,
    )
    retired_names = {r["name"] for r in report["retired"]}

    for name in absent:
        assert name in retired_names, (
            f"{name!r} is neither confirmed by the survey nor configured by "
            "the operator and must be retired at threshold 1"
        )
        assert catalog["providers"][name]["tier_status"] == "deprecated"

    # A plausible-but-unconfirmed entry is a miss as well.
    assert survey["enrichment"]["qwen-portal"]["registry_models"]["evidence"]["level"] == "plausible"
    assert "qwen-portal" in retired_names, (
        "only 'confirmed' entries count as confirmations; a 'plausible' "
        "entry must not be treated as confirmed"
    )


# ---------------------------------------------------------------------------
# Criterion 1b — operator names resolve by id, then alias; the rest are
# local route names and are not catalog identities
# ---------------------------------------------------------------------------

def test_operator_names_resolve_by_id_then_alias():
    """Every operator name that matches a catalog identity is spared by it.

    Of the 38 operator names, 15 are catalog ids and 1 resolves through
    an alias (``deepseek-v4-flash-vision-exp`` ->
    ``deepseek-flash-vision-exp``).  All 16 must be spared.
    """
    catalog = _real_catalog()
    providers = catalog["providers"]
    alias_owner = {
        alias: pid
        for pid, entry in providers.items()
        for alias in entry.get("aliases", [])
    }

    by_id = [n for n in _real_operator_names() if n in providers]
    by_alias = {
        n: alias_owner[n]
        for n in _real_operator_names()
        if n not in providers and n in alias_owner
    }

    assert len(by_id) == 15
    assert by_alias == {"deepseek-v4-flash-vision-exp": "deepseek-flash-vision-exp"}

    _, report = _call_retirement_real(
        catalog, _real_survey(), _real_operator_names(), 1,
    )
    spared = {s["name"] for s in report["spared"]}

    for name in by_id:
        assert name in spared, f"operator id {name!r} must be spared"
    for operator_name, provider_id in by_alias.items():
        assert provider_id in spared, (
            f"operator name {operator_name!r} resolves through an alias to "
            f"{provider_id!r}, which must be spared"
        )


def test_operator_names_that_resolve_to_nothing_do_not_hide_misses():
    """A route name with no catalog identity is not a missing entry.

    22 of the 38 operator names (e.g. ``gemini-pro``,
    ``openai-codex-5.4-high``) are local faigate route names: they match
    no catalog id and no alias.  They must not be invented as catalog
    entries, and they must not be counted as spared catalog entries
    either.  The catalog is left exactly as wide as it was.
    """
    catalog = _real_catalog()
    providers = catalog["providers"]
    alias_owner = {
        alias
        for entry in providers.values()
        for alias in entry.get("aliases", [])
    }
    operator_names = _real_operator_names()
    unresolved = {
        n for n in operator_names
        if n not in providers and n not in alias_owner
    }

    assert len(unresolved) == 22

    before = set(providers)
    catalog, report = _call_retirement_real(
        catalog, _real_survey(), operator_names, 1,
    )

    assert set(catalog["providers"]) == before, (
        "a route name that resolves to no catalog identity must not add an "
        "entry to the catalog"
    )
    spared = {s["name"] for s in report["spared"]}
    assert not (unresolved & spared), (
        "an unresolved route name is not a catalog entry and must not be "
        "reported as a spared entry"
    )


# ---------------------------------------------------------------------------
# Criterion 2 — the rule has a fixed point
# ---------------------------------------------------------------------------

def test_second_run_over_its_own_output_changes_nothing():
    """THE fixed point: run twice, the second run is a no-op.

    The rule is applied to the real catalog with a real survey.  Its
    output is fed back in unchanged.  The second run must produce the
    same catalog and the same verdict — in particular ``misses`` must
    not climb, because nothing about the survey changed between runs.

    The comparison runs against an *independent snapshot* of the
    first-run catalog, not against the object the first run returned.
    ``apply_retirement`` mutates its argument in place and returns the
    same object, so ``after_second == after_first`` compares the run-1
    object with itself and is true even for a rule that is not a fixed
    point at all.  A deep snapshot makes the assertion falsifiable: a
    genuinely non-idempotent rule fails it.
    """
    survey = _real_survey()
    operator_names = _real_operator_names()

    after_first, first = _call_retirement_real(
        _real_catalog(), survey, operator_names, 3,
    )
    # Snapshot BEFORE the second run: a rule that mutates in place (and
    # returns the same object) would otherwise carry the second run's
    # changes into the comparison and make it vacuous.
    snapshot_after_first = json.loads(json.dumps(after_first))

    after_second, second = _call_retirement_real(
        after_first, survey, operator_names, 3,
    )

    assert after_second == snapshot_after_first, (
        "the rule has no fixed point: applying it to its own output changed "
        "the catalog"
    )
    assert json.loads(json.dumps(after_second)) == snapshot_after_first, (
        "the rule has no fixed point: the second run's output differs from "
        "the first run's output"
    )
    assert second["retired"] == first["retired"]
    assert second["spared"] == first["spared"]
    assert second["revived"] == first["revived"]


def test_misses_do_not_escalate_across_runs_on_an_unchanged_survey():
    """misses must not grow when the survey did not change.

    The collector's output is authoritative for the round.  Counting the
    same unchanged survey twice is not two collection rounds, and an
    entry must not creep from tracked to retired just because the rule
    was invoked again.

    The preconditions pin that the first run actually *recorded* a miss:
    without them the "misses did not escalate" assertion would also hold
    for a rule that does nothing at all, and the test would pass for the
    wrong reason.
    """
    survey = _real_survey()
    operator_names = _real_operator_names()
    threshold = 5

    catalog, first = _call_retirement_real(
        _real_catalog(), survey, operator_names, threshold,
    )
    misses_first = {
        name: entry.get("retirement", {}).get("misses", 0)
        for name, entry in catalog["providers"].items()
    }

    assert first["tracked"], (
        "precondition: the first run must have recorded misses to track"
    )
    assert any(m > 0 for m in misses_first.values()), (
        "precondition: at least one entry must carry a real miss count, "
        "otherwise 'did not escalate' is vacuous"
    )

    catalog, second = _call_retirement_real(
        catalog, survey, operator_names, threshold,
    )
    misses_second = {
        name: entry.get("retirement", {}).get("misses", 0)
        for name, entry in catalog["providers"].items()
    }

    assert misses_second == misses_first, (
        "misses escalated on an unchanged survey — the rule is counting its "
        "own invocations as collection rounds"
    )
    assert sorted(second["tracked"], key=lambda t: t["name"]) == sorted(
        first["tracked"], key=lambda t: t["name"]
    )


def test_fixed_point_holds_for_a_repeat_of_the_real_survey_after_retirement():
    """A retired run re-applied is stable, including the abort guard.

    Belt and braces on the same property: whatever the first run
    decided, re-running on its output must not revive or re-retire
    anything.
    """
    survey = _real_survey()
    operator_names = _real_operator_names()

    catalog, first = _call_retirement_real(
        _real_catalog(), survey, operator_names, 1,
    )
    assert first["retired"], "precondition: threshold 1 retires something"

    catalog, second = _call_retirement_real(catalog, survey, operator_names, 1)

    assert second["retired"] == []
    assert second["revived"] == []
    assert not second.get("aborted")


# ---------------------------------------------------------------------------
# Criterion 3 — empty survey or empty operator list must FAIL loudly
# ---------------------------------------------------------------------------

def test_empty_survey_fails_and_names_the_reason():
    """An empty survey raises and says why — it does not pass silently.

    Failure here means the run does not return an ordinary success
    report: either it raises, or it returns a run explicitly marked
    ``aborted`` with the missing evidence named as the reason.  What it
    may NOT do is report success while touching nothing — "did nothing"
    is not "aborted and named".

    Note the input is the survey fed straight to ``apply_retirement``,
    unadapted, which is what a CLI caller hands to ``--survey``.  The
    adapter would strip the ``schema_version`` marker and the empty
    survey would reach the rule as a legacy document; the rule still
    aborts it, but for the legacy "no source ran" reason rather than
    the empty-survey reason this criterion is about.
    """
    catalog = _real_catalog()
    empty_survey = {
        "schema_version": "fusionaize-collected/v1",
        "enrichment": {},
        "contradictions": [],
        "coverage": {"total_providers": 0, "confirmed": 0, "plausible": 0,
                     "unlisted": 0},
    }

    try:
        _, report = _call_retirement(catalog, empty_survey, _real_operator_names(),
                                     threshold=1)
    except (ValueError, RuntimeError) as exc:
        message = str(exc).lower()
        assert "empty" in message or "no survey" in message or "no entry" in message, (
            f"the failure must name the reason; got {str(exc)!r}"
        )
        assert catalog == _real_catalog(), (
            "a failed run must not have modified the catalog"
        )
        return

    raise AssertionError(
        "an empty survey returned a report instead of failing — a rule that "
        "does nothing and reports success hides a broken collection"
    )


def test_empty_survey_does_not_retire_or_spare_anything():
    """On an empty survey nothing is retired and nothing is spared."""
    catalog = _real_catalog()
    empty_survey = {
        "schema_version": "fusionaize-collected/v1",
        "enrichment": {},
        "contradictions": [],
        "coverage": {"total_providers": 0, "confirmed": 0, "plausible": 0,
                     "unlisted": 0},
    }

    try:
        result_catalog, report = _call_retirement_real(
            catalog, empty_survey, _real_operator_names(), threshold=1,
        )
    except (ValueError, RuntimeError):
        return  # failing loudly is the required behaviour, covered above

    assert result_catalog == catalog, (
        "an empty survey must not modify the catalog"
    )
    assert report["retired"] == []
    assert report["spared"] == []
    assert report.get("aborted") is True, (
        "if the rule does not raise, it must at least report the run as "
        "aborted with a named reason"
    )
    assert report.get("abort_reason"), (
        "an aborted run must name the reason it did not act"
    )


def test_empty_operator_list_fails_and_names_the_reason():
    """An empty operator list FAILS and says why.

    With no operator names the rule has no way to honour the carve-out
    that keeps live routes routable, so it must not decide a retirement
    at all.  Failing here means the run does not return an ordinary
    success report: either it raises, or it returns a run explicitly
    marked ``aborted`` with the operator list named as the reason.  What
    it may NOT do is quietly process the survey as if the carve-out did
    not matter — that would retire routes the operator is actively
    serving.
    """
    catalog = _real_catalog()

    try:
        _, report = _call_retirement_real(
            catalog, _real_survey(), set(), threshold=1,
        )
    except (ValueError, RuntimeError) as exc:
        assert "operator" in str(exc).lower(), (
            f"the failure must name the operator list as the reason; got "
            f"{str(exc)!r}"
        )
        assert catalog["providers"]["anthropic-claude"]["tier_status"] == "active", (
            "a failed run must not have modified the catalog"
        )
        return

    assert report.get("aborted") is True, (
        "an empty operator list returned an ordinary report — running "
        "without the carve-out would retire live routes"
    )
    assert "operator" in report.get("abort_reason", "").lower(), (
        f"the abort must name the operator list as the reason; got "
        f"{report.get('abort_reason')!r}"
    )
    assert report["retired"] == [], (
        "an aborted run must not have retired anything"
    )


def test_empty_operator_list_does_not_retire_anything():
    """On an empty operator list nothing is retired, however it behaves."""
    catalog = _real_catalog()
    before = json.loads(json.dumps(catalog))

    try:
        result_catalog, report = _call_retirement_real(
            catalog, _real_survey(), set(), threshold=1,
        )
    except (ValueError, RuntimeError):
        return

    assert result_catalog == before, (
        "an empty operator list must not modify the catalog"
    )
    assert report["retired"] == []
    assert report.get("aborted") is True
    assert report.get("abort_reason")


def test_loud_failure_is_not_a_silent_success():
    """Riegel against the check itself.

    A rule that simply does nothing and returns a green report would
    pass a naive "nothing was retired" assertion.  This test pins the
    difference: the empty-input paths must NOT return an ordinary
    success report.  The report must either not be returned at all, or
    be explicitly marked aborted with a reason.
    """
    catalog = _real_catalog()
    empty_survey = {
        "schema_version": "fusionaize-collected/v1",
        "enrichment": {},
        "contradictions": [],
        "coverage": {},
    }

    try:
        _, report = _call_retirement_real(
            catalog, empty_survey, _real_operator_names(), threshold=1,
        )
    except (ValueError, RuntimeError):
        return  # loud failure — correct

    assert report.get("aborted") is True and report.get("abort_reason"), (
        "the rule neither raised nor marked the run aborted: 'did nothing' "
        "is not 'aborted and named'"
    )


# ---------------------------------------------------------------------------
# Criterion 3 at the process level — an input that cannot support a
# decision must not exit 0, and must not write.
#
# The API-level tests above accept "raises" OR "returns an aborted
# report".  For a batch caller -- the ``--write`` path exists exactly for
# that -- only ``$?`` is visible, so an aborted run that exits 0 and
# rewrites the catalog is the "reports success and does nothing" failure
# the criterion forbids.  These tests pin the process contract directly.
# ---------------------------------------------------------------------------

def test_cli_empty_survey_exits_nonzero_and_writes_nothing(tmp_path):
    """An empty survey makes the CLI exit non-zero and leave the file alone."""
    target = tmp_path / "catalog.json"
    target.write_bytes(CATALOG.read_bytes())
    before = target.read_bytes()

    empty_survey = tmp_path / "empty-survey.json"
    empty_survey.write_text(json.dumps({
        "schema_version": "fusionaize-collected/v1",
        "enrichment": {},
        "contradictions": [],
        "coverage": {},
    }))

    result = _run_cli(target, empty_survey, OPERATOR, 1, "--write")

    assert result.returncode != 0, (
        "an empty survey must fail loudly; the process exited 0 with "
        f"stdout={result.stdout!r}"
    )
    assert target.read_bytes() == before, (
        "a failed run must not write the catalog"
    )


def test_cli_empty_operator_list_exits_nonzero_and_writes_nothing(tmp_path):
    """An empty operator list makes the CLI exit non-zero and leave the file alone.

    This is the process-level half of criterion 3.  The rule refused to
    act (the abort was named) but the process still exited 0 and rewrote
    the catalog — a caller reading ``$?`` saw a green run.
    """
    target = tmp_path / "catalog.json"
    target.write_bytes(CATALOG.read_bytes())
    before = target.read_bytes()

    empty_operator = tmp_path / "empty-operator.json"
    empty_operator.write_text(json.dumps({"providers": []}))

    result = _run_cli(target, COLLECTED, empty_operator, 1, "--write")

    assert result.returncode != 0, (
        "an empty operator list must fail loudly; the process exited 0 with "
        f"stdout={result.stdout!r}"
    )
    assert target.read_bytes() == before, (
        "a failed run must not write the catalog"
    )


def test_cli_empty_operator_list_names_the_reason_on_stderr_or_stdout(tmp_path):
    """The non-zero exit is accompanied by a reason naming the operator list."""
    target = tmp_path / "catalog.json"
    target.write_bytes(CATALOG.read_bytes())

    empty_operator = tmp_path / "empty-operator.json"
    empty_operator.write_text(json.dumps({"providers": []}))

    result = _run_cli(target, COLLECTED, empty_operator, 1)

    combined = (result.stdout + result.stderr).lower()
    assert "operator" in combined, (
        "the failure must name the operator list as the reason; got "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


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
