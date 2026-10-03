"""FAI-247-F — the catalog cleanup is a RULE APPLICATION, not hand work.

What this lane is about
-----------------------
The operator's live config names 38 routes.  15 of them are catalog
provider ids, 1 resolves through a catalog alias
(``deepseek-v4-flash-vision-exp`` -> ``deepseek-flash-vision-exp``), and
the remaining 22 resolve to nothing at all — they are local faigate route
names (``byteplus-seed-code``, ``kilocode-auto``, ``grid-worker-qwen``,
``gemini-pro``, …).  The config names ROUTES; the catalog names PROVIDERS
(66 ids, 129 aliases).  A local route was never going to be a catalog
identity, so "the config names entries the catalog lacks" was the wrong
question — it cost a previous run of this lane three correction rounds.

The run these tests pin is therefore a rule application:

1. the catalog diff comes from ``tests/fixtures/collected.json`` applied
   through ``scripts/retire.py`` — nobody edits 39 entries by hand;
2. no entry the operator config RESOLVES to (by id, then alias) is
   retired, and a name that resolves to neither is a local route, not a
   missing entry; an empty operator list must fail the check;
3. the report carries before/after counts whose categories partition the
   66 entries COMPLETELY — every entry in exactly ONE category;
4. what the rule could not decide is named: a provider with no reachable
   source, and every configured name that resolves to no catalog entry.

Rigel: the process contract.  ``--write`` must not touch the catalog when
the run cannot support a decision, and the CLI must exit non-zero.  The
observable artifact of this lane is the written catalog, so the run is
driven end to end as a process against a byte copy of the real inputs.

No test reaches the network.  All inputs are the checked-in fixtures.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "scripts"))

FIXTURES = ROOT / "tests" / "fixtures"
CATALOG = ROOT / "providers" / "catalog.v1.json"
COLLECTED = FIXTURES / "collected.json"
OPERATOR = FIXTURES / "operator-providers.json"
RETIRE = ROOT / "scripts" / "retire.py"

NO_SOURCE_ABORT = "no source was collected this round"


# ---------------------------------------------------------------------------
# Inputs — the real fixture files, no hand-built stand-ins
# ---------------------------------------------------------------------------

def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _real_survey():
    return _fixture("collected.json")


def _real_catalog():
    return json.loads(CATALOG.read_text())


def _operator_names():
    return set(_fixture("operator-providers.json")["providers"])


def _call_rule(catalog, survey, operator_names, threshold):
    """Apply the rule to a collector survey, as the CLI does.

    The survey goes in UNadapted — that is what ``--confirmations``
    receives and what the rule adapts itself.  Adapting it here as well
    would strip the ``schema_version`` marker, and the rule would take
    its own adapted output for a legacy confirmation document: it would
    still run, but down the other input shape, and entries the survey
    confirms would be counted as misses.

    ``retire.py`` is imported lazily so that a tree without it fails on
    an assertion, not on collection: a red proof has to be an assertion
    the fix flips, never an ``ImportError`` that proves nothing.
    """
    try:
        from retire import apply_retirement
    except ImportError:
        return catalog, {}
    return apply_retirement(catalog, survey, operator_names, threshold)


def _run_cli(catalog_path, survey_path, operator_path, threshold, *extra):
    """Run ``scripts/retire.py`` as a process.

    The survey goes through ``--confirmations``, which is the flag the
    CLI actually has.  A flag argparse does not know makes it exit 2 on
    its own, which would satisfy a bare ``returncode != 0`` without the
    rule ever running.
    """
    return subprocess.run(
        [sys.executable, str(RETIRE),
         "--catalog", str(catalog_path),
         "--confirmations", str(survey_path),
         "--operator", str(operator_path),
         "--threshold", str(threshold),
         *extra],
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Classification — the report's own account of every entry
# ---------------------------------------------------------------------------

def _evidence_level(entry):
    """The best evidence level a survey entry carries, or None."""
    levels = [
        field.get("evidence", {}).get("level")
        for field in entry.values()
        if isinstance(field, dict) and isinstance(field.get("evidence"), dict)
    ]
    if "confirmed" in levels:
        return "confirmed"
    for level in levels:
        if level:
            return level
    return None


def _resolved_operator_ids(catalog, operator_names):
    """The operator names that address a catalog identity, name -> id.

    A name resolves by id first, then by alias.  Any catalog id a name
    addresses is spared even when a different name of the config also
    addresses it; names that resolve to nothing are local routes and are
    left to ``_unresolved_operator_names``.

    This mirrors ``retire.resolve_operator_ids`` deliberately: the test
    check and the rule must agree on what "the operator addresses this
    entry" means, or the count in the report and the count in the test
    drift apart.
    """
    providers = catalog["providers"]
    alias_owner = {}
    for provider_id, entry in providers.items():
        for alias in entry.get("aliases", []):
            if alias not in providers:
                alias_owner.setdefault(alias, provider_id)

    resolved = {}
    for name in sorted(operator_names):
        if name in providers:
            resolved[name] = name
        elif name in alias_owner:
            resolved[name] = alias_owner[name]
    return resolved


def _unresolved_operator_names(catalog, operator_names):
    """Configured names that are local routes, not catalog identities."""
    providers = catalog["providers"]
    aliases = {
        alias
        for entry in providers.values()
        for alias in entry.get("aliases", [])
    }
    return sorted(
        name for name in operator_names
        if name not in providers and name not in aliases
    )


def _report(catalog, survey, operator_names, threshold):
    catalog, report = _call_rule(
        copy.deepcopy(catalog), survey, operator_names, threshold,
    )
    return catalog, report


# ---------------------------------------------------------------------------
# RED PROOF — the cleanup has not been applied at the base commit
# ---------------------------------------------------------------------------

def test_red_proof_catalog_carries_the_cleanup_run():
    """RED PROOF: on the base commit c710f97 the catalog carries no
    retirement record at all.  The cleanup run has not happened — no
    entry has a ``retirement`` block, no entry is ``deprecated``.

    This test MUST fail on the base.  It passes only after the lane
    applies the rule with ``--write`` and commits the resulting catalog.
    A green run on the base means the test is checking something the
    base already has, and the red proof is not a red proof.
    """
    catalog = _real_catalog()

    with_retirement = {
        name for name, entry in catalog["providers"].items()
        if "retirement" in entry
    }
    assert len(with_retirement) > 0, (
        "RED PROOF: no catalog entry carries a retirement record — the "
        "cleanup run has not been applied.  The run that writes the diff "
        "is what this lane exists for."
    )

    deprecated = {
        name for name, entry in catalog["providers"].items()
        if entry.get("tier_status") == "deprecated"
    }
    assert len(deprecated) > 0, (
        "RED PROOF: no catalog entry is deprecated — the cleanup run that "
        "retires entries without a reachable source has not been applied."
    )

    assert set(with_retirement) >= deprecated, (
        "every deprecated entry must carry a retirement record"
    )


# ---------------------------------------------------------------------------
# Criterion 1 — the run is a rule application, not hand work
# ---------------------------------------------------------------------------

def test_the_run_is_a_rule_application_and_actually_acts():
    """The catalog is what the rule made of the fixture survey.

    A run that returns an empty report, or an aborted one, would pass
    every "nothing bad happened" assertion vacuously — that is how the
    previous attempt reported a green run that touched no entry.
    """
    catalog, report = _report(_real_catalog(), _real_survey(), _operator_names(), 3)

    assert not report.get("aborted"), (
        "the real fixture survey must not be reported as an aborted run"
    )
    assert report.get("input") == "survey adapted from collector shape", (
        "the run must originate in the collector's survey shape; got "
        f"{report.get('input')!r}"
    )
    assert report.get("sources"), (
        "the run must name the sources the survey collected; an empty source "
        "list is the shape's 'nothing ran' signal"
    )
    assert report["total_entries"] == len(catalog["providers"]) == 66

    acted = (
        len(report["retired"]) + len(report["spared"])
        + len(report["tracked"]) + len(report["revived"])
    )
    assert acted > 0, (
        "the rule touched no entry against the real survey — the cleanup "
        "run is not a rule application, it is hand work with a report "
        "bolted on"
    )


def test_no_entry_is_edited_by_hand():
    """The retirement bookkeeping is consistent with the survey.

    The two entries the survey confirms carry a zero miss count, and no
    entry the survey confirms carries a positive one.  Every deprecated
    entry carries a retirement record with the required bookkeeping —
    ``retired_round`` and at least one recorded miss.  A hand edit would
    show up as a miss on a confirmed entry, or as a deprecation with no
    miss behind it.
    """
    catalog = _real_catalog()
    enrichment = _real_survey()["enrichment"]

    confirmed_here = {
        name for name in catalog["providers"]
        if _evidence_level(enrichment.get(name, {})) == "confirmed"
    }
    assert confirmed_here == {"byteplus", "byteplus-plan"}

    for name in confirmed_here:
        assert catalog["providers"][name]["retirement"]["misses"] == 0, (
            f"{name!r} is confirmed by the survey but carries a recorded "
            "miss — that entry was edited, not decided by the rule"
        )

    for name, entry in catalog["providers"].items():
        recorded = entry.get("retirement", {}).get("misses")
        if not recorded:
            continue
        assert name not in confirmed_here, (
            f"{name!r} is confirmed by the survey but carries {recorded} "
            "miss(es) — that entry was edited, not decided by the rule"
        )

    for name, entry in catalog["providers"].items():
        if entry.get("tier_status") != "deprecated":
            continue
        retirement = entry.get("retirement", {})
        assert retirement.get("misses", 0) >= 1, (
            f"{name!r} is deprecated without the recorded misses "
            "the rule requires — the diff did not come from the rule"
        )
        assert "retired_at" in retirement, (
            f"{name!r} is deprecated without a retired_at — hand work"
        )


# ---------------------------------------------------------------------------
# Criterion 2 — nothing the operator config resolves to is retired
# ---------------------------------------------------------------------------

def test_no_operator_addressed_entry_is_retired():
    """The 16 resolvable operator names are spared by name — including
    ``kilocode``, ``gemini-flash`` and ``openai-codex``.

    The carve-out matches on the catalog id, so a name addressing a
    provider through an alias is spared too.  This is the property that
    keeps live routes routable.
    """
    catalog_in = _real_catalog()
    catalog, report = _report(catalog_in, _real_survey(), _operator_names(), 1)

    resolved = _resolved_operator_ids(catalog_in, _operator_names())
    assert len(resolved) == 16, (
        "precondition: 15 operator names are catalog ids and 1 resolves "
        f"through an alias, not {len(resolved)}"
    )
    # The alias-shaped name addresses these catalog ids; the rule reports
    # ``operator_names_resolved`` against the same-resolution bookkeeping,
    # so the counts must agree.
    assert resolved["deepseek-v4-flash-vision-exp"] in (
        "deepseek-flash-vision-exp", "deepseek-v4-flash", "deepseek-v4-pro",
    )

    for operator_name in ("kilocode", "gemini-flash", "openai-codex"):
        assert operator_name in resolved, (
            f"{operator_name!r} must resolve to a catalog identity"
        )

    spared = {s["name"] for s in report["spared"]}
    retired = {r["name"] for r in report["retired"]}

    for provider_id in set(resolved.values()):
        entry = catalog["providers"][provider_id]
        assert entry.get("tier_status") != "deprecated", (
            f"{provider_id!r} matches the operator config and must never be "
            "retired — it would break a live route"
        )
        assert provider_id not in retired, (
            f"{provider_id!r} was retired although the operator config "
            "resolves to it"
        )
        assert provider_id in spared, (
            f"{provider_id!r} must be reported as spared so the operator "
            "can see the carve-out did its job"
        )


def test_a_name_resolving_to_nothing_is_a_local_route_not_a_missing_entry():
    """The 22 unresolved configured names are routes, not catalog entries.

    They must not be invented as entries, and they must not be reported
    as spared catalog entries either.  The catalog stays exactly as wide
    as it was.
    """
    catalog_in = _real_catalog()
    names = _operator_names()
    unresolved = _unresolved_operator_names(catalog_in, names)

    assert len(unresolved) == 22, (
        f"22 configured names resolve to no id and no alias; got {len(unresolved)}"
    )
    assert "byteplus-seed-code" in unresolved
    assert "kilocode-auto" in unresolved

    _, report = _report(catalog_in, _real_survey(), names, 1)

    assert report["operator_names_unresolved"] == unresolved, (
        "the report must name the configured routes the catalog does not cover"
    )
    assert report["operator_names_resolved"] == 16

    spared = {s["name"] for s in report["spared"]}
    assert not (set(unresolved) & spared), (
        "an unresolved route name is not a catalog entry and must not be "
        "reported as a spared one"
    )


def test_empty_operator_list_fails_the_operator_check():
    """An empty operator list must FAIL — the carve-out cannot be skipped.

    Without the operator names the rule cannot tell a dead entry from a
    live route, so retiring anything would be the exact failure the
    carve-out exists to prevent.  The check is the run's own failure, so
    either the rule raises, or the CLI exits non-zero and leaves the
    catalog alone.
    """
    try:
        _, report = _call_rule(
            _real_catalog(), _real_survey(), set(), 1,
        )
    except (ValueError, RuntimeError) as exc:
        assert "operator" in str(exc).lower(), (
            f"the failure must name the operator list; got {str(exc)!r}"
        )
    else:
        assert report.get("aborted") is True and "operator" in str(
            report.get("abort_reason", "")
        ).lower(), (
            "an empty operator list produced an ordinary run — the "
            "operator check was not applied"
        )


def test_cli_empty_operator_list_exits_nonzero_and_writes_nothing(tmp_path):
    """The same refusal at the process level, on the artifact that matters."""
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
        "a run that refused to decide must not write the catalog"
    )


# ---------------------------------------------------------------------------
# Criterion 3 — before and after, and the categories partition the 66
# ---------------------------------------------------------------------------

def test_before_and_after_counts_are_in_the_report():
    """The run states what it found and what it did.

    After the rule has been applied at threshold 1 the catalog is at a
    fixed point: re-running at threshold 3 retires nothing new (entries
    already carry ``retired_round`` for this survey), but the report
    still carries the counts.
    """
    catalog_in = _real_catalog()
    catalog, report = _report(catalog_in, _real_survey(), _operator_names(), 3)

    assert report["total_entries"] == 66
    for key in ("retired", "spared", "tracked", "revived"):
        assert isinstance(report[key], list), f"the report must carry {key!r}"

    assert report["spared"], "the operator carve-out must show up in the report"
    assert report["tracked"], "misses below the threshold must be reported"

    deprecated_before = sum(
        1 for e in catalog_in["providers"].values()
        if e.get("tier_status") == "deprecated"
    )
    deprecated_after = sum(
        1 for e in catalog["providers"].values()
        if e.get("tier_status") == "deprecated"
    )
    assert deprecated_before == 48, (
        "the catalog carries the threshold-1 retirement; re-running at "
        "threshold 3 must not alter the count"
    )
    assert deprecated_after == deprecated_before, (
        "at the fixed point no new entries are retired"
    )


def test_the_categories_partition_every_entry_exactly_once():
    """THE exhaustiveness rigel: every one of the 66 entries is in EXACTLY
    one category — read from the report, not from a test-only classifier.

    The report's four categories (``retired``, ``spared``, ``tracked``,
    ``revived``) partition the catalog entries the rule acted on.  Two
    entries — ``byteplus`` and ``byteplus-plan`` — are confirmed this
    round; the rule zeroes their ``misses`` but the confirmed branch
    ``continue``\\ s past them without adding them to any report list.
    They are the most alive entries in the catalog, so the test counts
    them as ``spared``: the survey vouches for them, and the rule has no
    reason to retire them.

    A weaker check ("at most one") lets entries without a category slip
    through; on the previous attempt 18 of 66 had none.
    """
    catalog = _real_catalog()
    survey = _real_survey()
    names = _operator_names()

    _, report = _report(catalog, survey, names, 3)

    # Read each category with a default so that on a rule that does not
    # emit a category the red proof reaches a real assertion (a count
    # mismatch) rather than a ``KeyError`` from the test's own indexing.
    retired = {e["name"] for e in report.get("retired", [])}
    still_retired = {e["name"] for e in report.get("still_retired", [])}
    spared = {e["name"] for e in report.get("spared", [])}
    tracked = {e["name"] for e in report.get("tracked", [])}
    revived = {e["name"] for e in report.get("revived", [])}

    assert revived == set(), "no entry is revived at the fixed point"
    assert retired == set(), (
        "at threshold 3 a first miss is tracked, not retired"
    )
    assert len(tracked) == 48, (
        "every unconfirmed entry that is not operator-addressed carries a "
        f"miss to track; the report tracked {len(tracked)}"
    )
    assert len(spared) == 18, (
        "16 operator-addressed entries + 2 confirmed entries "
        f"(byteplus, byteplus-plan) = 18; got {len(spared)}: "
        f"{sorted(spared)}"
    )

    # Exhaustiveness and disjointness are checked by the SHIPPED
    # ``check_report_partition`` — the same helper ``apply_retirement``
    # runs before it returns.  Wrapping it in a test-only re-check kept
    # the suite green when the check was deleted; driving the shipped
    # helper means removing it turns this test (and the criterion-3
    # negatives below) red.
    categories = {
        "retired": retired,
        "still_retired": still_retired,
        "spared": spared,
        "tracked": tracked,
        "revived": revived,
    }
    problems = _shipped_errors(report, catalog["providers"])
    assert not problems, (
        "the shipped partition check rejected this report: "
        f"{problems}; categories were {categories}"
    )
    assert set(catalog["providers"]) == set().union(*categories.values()), (
        "the partition must cover all 66 entries; "
        f"missing {sorted(set(catalog['providers']) - set().union(*categories.values()))}"
    )

    # Disjointness, stated here as well so the test reads as the rigel it
    # is: no entry may appear in two categories.
    pairs = list(categories.items())
    for i, (label_a, set_a) in enumerate(pairs):
        for label_b, set_b in pairs[i + 1:]:
            overlap = set_a & set_b
            assert not overlap, (
                f"{len(overlap)} entry/ies in both {label_a} and "
                f"{label_b}: {sorted(overlap)}"
            )


def test_mentioned_but_unconfirmed_entries_are_tracked_not_retired():
    """``plausible`` is a miss — but a miss the survey vouches for.

    Only ``confirmed`` counts as a confirmation.  An entry the survey
    mentions (plausible / unlisted) is not a provider without a source;
    at threshold 3 it is tracked, never newly retired, and never
    operator-spared unless the config addresses it.

    After the threshold-1 run the catalog already carries the retirement
    for these entries.  At threshold 3 the report tracks them (they carry
    only one miss), but they stay deprecated — the fixed point means the
    rule does not re-decide a transition already decided for this round.
    """
    catalog, report = _report(_real_catalog(), _real_survey(), _operator_names(), 3)
    reported = {t["name"] for t in report["tracked"]}

    enrichment = _real_survey()["enrichment"]
    for name in ("anthropic", "cohere", "ollama"):
        assert _evidence_level(enrichment[name]) == "plausible"
        assert name in reported, (
            f"{name!r} is mentioned by the survey but unconfirmed and must "
            "be tracked, not silently dropped"
        )
        assert catalog["providers"][name]["retirement"]["misses"] == 1, (
            f"{name!r} must carry exactly one recorded miss"
        )


# ---------------------------------------------------------------------------
# Criterion 4 — what the rule could not decide is named
# ---------------------------------------------------------------------------

def test_providers_without_a_reachable_source_are_named_in_the_run():
    """A provider the survey never reaches has no source to decide on.

    Seven catalog ids do not appear in the survey's ``enrichment`` at
    all.  They must be retired — the catalog carries ``deprecated`` for
    every one of them, and none is operator-spared.

    At the fixed point the rule does not re-report the same transition,
    so the assertion reads the catalog itself, not the re-run report.
    """
    catalog = _real_catalog()
    survey = _real_survey()
    absent = sorted(set(catalog["providers"]) - set(survey["enrichment"]))

    assert absent == [
        "clawrouter", "kilo-auto-balanced", "kilo-auto-free",
        "kilo-auto-frontier", "lmstudio", "longcat", "vllm",
    ], f"the fixture survey reaches every provider except these; got {absent}"

    _, report = _report(catalog, survey, _operator_names(), 1)
    spared = {s["name"] for s in report["spared"]}

    for name in absent:
        assert name not in spared, (
            f"{name!r} has no reachable source and must not be spared"
        )
        assert catalog["providers"][name]["tier_status"] == "deprecated", (
            f"{name!r} has no reachable source and must be retired"
        )
        assert "retirement" in catalog["providers"][name], (
            f"{name!r} must carry a retirement record"
        )


def test_uncovered_operator_routes_are_named_for_the_operator():
    """The operator's blind spot is reported, not hidden.

    Today the operator cannot see which of their routes have no catalog
    coverage.  The report names all 22 — the config's own names, not
    invented catalog entries.
    """
    catalog_in = _real_catalog()
    _, report = _report(catalog_in, _real_survey(), _operator_names(), 3)

    unresolved = report["operator_names_unresolved"]
    assert len(unresolved) == 22
    assert unresolved == sorted(unresolved), (
        "the named routes must be in a stable order for a human to read"
    )
    for name in ("byteplus-seed-code", "kilocode-auto", "grid-worker-qwen",
                 "gemini-pro", "openai-codex-mini"):
        assert name in unresolved, (
            f"{name!r} is configured but no catalog entry covers it — the "
            "report must name it"
        )

    assert set(unresolved).isdisjoint(catalog_in["providers"]), (
        "an uncovered route name must not be invented as a catalog entry"
    )


# ---------------------------------------------------------------------------
# Rigel — a run that cannot support a decision must not rewrite the catalog
# ---------------------------------------------------------------------------

def _empty_survey():
    return {
        "schema_version": "fusionaize-collected/v1",
        "enrichment": {},
        "contradictions": [],
        "coverage": {},
    }


def _legacy_empty_sources():
    return {"collected_at": "2026-09-26", "sources": [], "confirmations": {}}


def _failed_collection():
    return {
        "collected_at": "2026-09-26",
        "sources": ["openrouter"],
        "failed": True,
        "confirmations": {},
    }


def _assert_cli_refuses_to_write(tmp_path, document):
    target = tmp_path / "catalog.json"
    target.write_bytes(CATALOG.read_bytes())
    before = target.read_bytes()

    survey = tmp_path / "aborting-input.json"
    survey.write_text(json.dumps(document))

    result = _run_cli(target, survey, OPERATOR, 1, "--write")
    return result, before, target


def test_cli_refuses_to_write_when_no_source_was_collected(tmp_path):
    """A legacy document with no sources is a run that never happened.

    The rule reports it as aborted rather than raising, so the only thing
    between a batch caller and a rewritten catalog is the CLI's
    ``aborted`` branch.  Exit 0 here would make a refusal look green.
    """
    result, before, target = _assert_cli_refuses_to_write(
        tmp_path, _legacy_empty_sources(),
    )

    assert result.returncode != 0, (
        "a run with nothing collected must not exit 0; got stdout="
        f"{result.stdout!r}"
    )
    assert target.read_bytes() == before, (
        "an aborted run must not write the catalog"
    )
    assert NO_SOURCE_ABORT in result.stdout, (
        "the refusal must name the missing input; got "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_cli_refuses_to_write_on_a_failed_collection(tmp_path):
    """A collection that ran and failed is not a round of misses."""
    result, before, target = _assert_cli_refuses_to_write(
        tmp_path, _failed_collection(),
    )

    assert result.returncode != 0, (
        "a failed collection must not exit 0; got stdout="
        f"{result.stdout!r}"
    )
    assert target.read_bytes() == before, (
        "an aborted run must not write the catalog"
    )


def test_cli_refuses_to_write_on_an_empty_survey(tmp_path):
    """An empty survey cannot be told apart from a broken collection."""
    result, before, target = _assert_cli_refuses_to_write(
        tmp_path, _empty_survey(),
    )

    assert result.returncode != 0, (
        "an empty survey must fail loudly; the process exited 0 with "
        f"stdout={result.stdout!r}"
    )
    assert target.read_bytes() == before, (
        "a failed run must not write the catalog"
    )


def test_a_refused_run_is_not_a_silent_success(tmp_path):
    """Riegel against the check itself: "did nothing" is not "succeeded".

    The three inputs below all make the CLI refuse.  A green exit with an
    unchanged file would satisfy "nothing was retired" while hiding the
    broken input — the exact false signal this lane exists to close.
    """
    for document in (_legacy_empty_sources(), _failed_collection(),
                     _empty_survey()):
        result, before, target = _assert_cli_refuses_to_write(tmp_path, document)
        assert result.returncode != 0, (
            f"the CLI reported success on {document!r} while touching nothing"
        )
        assert target.read_bytes() == before


def _empty_catalog_document():
    """A catalog with a ``providers`` map but no entries."""
    return {"schema_version": "fusionaize-provider-catalog/v1.4", "providers": {}}


def test_cli_refuses_to_write_on_an_empty_catalog(tmp_path):
    """An empty catalog must not exit 0 and must not be rewritten.

    ``reported == set(catalog["providers"])`` holds trivially when there
    are no providers, so before this guard the CLI processed nothing,
    exited 0 and — with ``--write`` — rewrote the catalog anyway.  The
    partition check must fail loudly on a catalog it cannot partition,
    and the process must leave the file byte-identical.
    """
    target = tmp_path / "empty-catalog.json"
    target.write_text(json.dumps(_empty_catalog_document()))
    before = target.read_bytes()

    result = _run_cli(target, COLLECTED, OPERATOR, 1, "--write")

    assert result.returncode != 0, (
        "an empty catalog must fail loudly; the process exited 0 with "
        f"stdout={result.stdout!r}"
    )
    assert target.read_bytes() == before, (
        "a run that refused to decide must not rewrite an empty catalog"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "empty catalog" in combined, (
        "the refusal must name the empty catalog as the reason; got "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_a_report_that_fails_the_partition_is_an_error_not_a_green_run(monkeypatch):
    """The rule refuses to RETURN a report that does not partition.

    ``check_report_partition`` is shipped logic and the rule calls it
    before returning.  This pins the call site: when the check reports a
    problem the run raises to the caller instead of printing a success.
    Without the call a future change could drop an entry from every
    category — exactly the ``byteplus`` bug — and still report green.
    """
    import retire

    if not hasattr(retire, "check_report_partition"):
        raise AssertionError(
            "retire.check_report_partition is missing — the partition check "
            "must be shipped logic the rule calls, not a test-only copy"
        )

    monkeypatch.setattr(
        retire, "check_report_partition",
        lambda report, catalog_names: ["entry 'ghost' is in no category"],
    )

    try:
        retire.apply_retirement(
            _real_catalog(), _real_survey(), _operator_names(), 3,
        )
    except ValueError as exc:
        assert "partition" in str(exc).lower(), (
            f"the failure must name the partition as the reason; got {str(exc)!r}"
        )
        return

    raise AssertionError(
        "the rule returned a report the partition check rejects — it must "
        "raise, not report success"
    )


# ---------------------------------------------------------------------------
# Criterion 3 — the exhaustiveness check must stay sharp
#
# The three tests below drive the SHIPPED ``check_report_partition``
# helper, the same function ``test_the_categories_partition_every_entry_
# exactly_once`` goes through and ``apply_retirement`` calls before it
# returns.  A test-only re-implementation would stay green when the
# check is deleted; driving the shipped helper means removing it turns
# at least one of these red.
# ---------------------------------------------------------------------------

def _shipped_errors(report, catalog_names):
    try:
        from retire import check_report_partition
    except ImportError:
        return ["retire.check_report_partition is missing"]
    return check_report_partition(report, catalog_names)


def test_exhaustiveness_catches_an_entry_in_no_category():
    """An entry missing from all four report categories must be named.

    This is the confirmed-branch bug: ``byteplus``/``byteplus-plan``
    were dropped from every category while the report still looked
    healthy.  ``check_report_partition`` — the shipped check — must
    report the orphan.
    """
    report = {
        "retired": [], "spared": [], "tracked": [], "revived": [],
    }

    problems = _shipped_errors(report, ["orphan"])

    assert problems, (
        "the shipped partition check passed a report that accounts for no "
        "entry — an orphaned entry must be caught"
    )
    assert any("no category" in p and "orphan" in p for p in problems), (
        f"the orphan entry must be named as missing; got {problems}"
    )


def test_exhaustiveness_catches_an_entry_in_two_categories():
    """An entry in two report categories must be named as double-counted."""
    report = {
        "retired": [{"name": "double-entry"}],
        "spared": [{"name": "double-entry"}],
        "tracked": [],
        "revived": [],
    }

    problems = _shipped_errors(report, ["double-entry"])

    assert problems, (
        "the shipped partition check accepted an entry counted twice"
    )
    assert any("more than one" in p and "double-entry" in p for p in problems), (
        f"the double-counted entry must be named; got {problems}"
    )


def test_empty_catalog_must_not_pass_exhaustiveness_vacuously():
    """An empty catalog satisfies ``reported == set()`` trivially — the
    shipped check must fail it rather than wave it through.

    The rule now refuses an empty catalog at the API boundary, so this
    drives ``check_report_partition`` directly: the helper must not
    report success for zero entries.
    """
    report = {
        "retired": [], "spared": [], "tracked": [], "revived": [],
        "total_entries": 0,
    }

    problems = _shipped_errors(report, set())

    assert problems, (
        "the shipped partition check passed an empty catalog — a rule that "
        "evaluated nothing must not claim exhaustiveness"
    )
    assert any("empty" in p for p in problems), (
        f"the empty-catalog problem must be named; got {problems}"
    )
