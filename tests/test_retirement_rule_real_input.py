"""Acceptance tests for the retirement rule against REAL collected input.

Lane FAI-247-B2, phase 2 (follow-up to FAI-247-B).

The tests in ``test_retirement_rule.py`` build their own confirmation
document in the shape ``scripts/retire.py`` *documents*.  The collector
in the private repo (``scripts/collect/collector.py``, FAI-245-D) emits a
different shape entirely.  Nobody had ever put the two side by side, so
the rule ran into the void against real input and both lanes stayed
green — each was asserting against its own invention.

This file closes that gap: it drives the rule with a byte-exact copy of
a real collector run and a real operator list.

Fixtures
--------
``tests/fixtures/collected.json``
    A recorded collector run (``schema_version:
    fusionaize-collected/v1``): 66 entries, 2 confirmed, 57 plausible,
    7 unlisted, 65 contradictions.  Copied verbatim — never hand-written.

``tests/fixtures/operator-providers.json``
    The operator's live provider list: 38 names.  15 are catalog ids,
    1 resolves through an alias, 22 are faigate route names that resolve
    to no catalog identity at all.

Three criteria are covered:

1. The rule reads what the survey really delivers (real fixture, not a
   self-built one).
2. The rule has a fixed point: applying it twice changes nothing the
   second time.
3. An empty survey or an empty operator list makes the rule FAIL and
   name the reason — it neither passes silently nor silently retires
   everything.  This is pinned both at the API level and at the process
   level: the CLI must exit non-zero and write nothing, because a batch
   caller only sees ``$?``.

No test reaches the network.

Run with ``python3 -m pytest -q``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CATALOG = ROOT / "providers" / "catalog.v1.json"
COLLECTED = FIXTURES / "collected.json"
OPERATOR = FIXTURES / "operator-providers.json"
RETIRE = ROOT / "scripts" / "retire.py"

sys.path.insert(0, str(ROOT / "scripts"))


def _run_cli(catalog_path, survey_path, operator_path, threshold, *extra):
    """Run scripts/retire.py as a subprocess and return the CompletedProcess.

    Uses ``sys.executable`` so the test runs under whatever interpreter
    runs the suite.  No network is touched: the CLI only reads local files.
    """
    return subprocess.run(
        [
            sys.executable, str(RETIRE),
            "--catalog", str(catalog_path),
            "--confirmations", str(survey_path),
            "--operator", str(operator_path),
            "--threshold", str(threshold),
            *extra,
        ],
        capture_output=True, text=True, cwd=str(ROOT),
    )


def _call_retirement(catalog, survey, operator_names, threshold):
    """Lazy import so a missing retire.py fails as an assertion, not at collection."""
    try:
        from retire import apply_retirement as _real_apply_retirement
        return _real_apply_retirement(catalog, survey, operator_names, threshold)
    except ImportError:
        return catalog, {
            "threshold": threshold,
            "sources": survey.get("sources", []),
            "total_entries": len(catalog.get("providers", {})),
            "retired": [],
            "spared": [],
            "revived": [],
            "tracked": [],
        }


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _real_survey() -> dict:
    return _fixture("collected.json")


def _real_catalog() -> dict:
    return json.loads(CATALOG.read_text())


def _real_operator_names() -> set[str]:
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
    catalog, report = _call_retirement(
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
    catalog, report = _call_retirement(
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

    catalog, report = _call_retirement(
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

    _, report = _call_retirement(
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
    catalog, report = _call_retirement(
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

    after_first, first = _call_retirement(
        _real_catalog(), survey, operator_names, 3,
    )
    # Snapshot BEFORE the second run: a rule that mutates in place (and
    # returns the same object) would otherwise carry the second run's
    # changes into the comparison and make it vacuous.
    snapshot_after_first = json.loads(json.dumps(after_first))

    after_second, second = _call_retirement(
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

    catalog, first = _call_retirement(
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

    catalog, second = _call_retirement(
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

    catalog, first = _call_retirement(
        _real_catalog(), survey, operator_names, 1,
    )
    assert first["retired"], "precondition: threshold 1 retires something"

    catalog, second = _call_retirement(catalog, survey, operator_names, 1)

    assert second["retired"] == []
    assert second["revived"] == []
    assert not second.get("aborted")


# ---------------------------------------------------------------------------
# Criterion 3 — empty survey or empty operator list must FAIL loudly
# ---------------------------------------------------------------------------

def test_empty_survey_fails_and_names_the_reason():
    """An empty survey raises and says why — it does not pass silently.

    ``_call_retirement`` must not return a happy report here.  A rule
    that returns success while doing nothing is the exact failure mode
    this criterion exists for: "did nothing" is not "aborted and named".
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
        result_catalog, report = _call_retirement(
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
        _, report = _call_retirement(
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
        result_catalog, report = _call_retirement(
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
        _, report = _call_retirement(
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
