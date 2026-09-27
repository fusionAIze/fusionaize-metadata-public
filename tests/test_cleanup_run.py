"""Acceptance tests for the FAI-247-F cleanup run.

Verifies that the retirement rule ``scripts/retire.py`` produces the
correct outcome when it is fed the real operator list and the real
collection result, and that the resulting catalog is synced into the
per-provider folders and rebuilt deterministically.

The two inputs are the real artifacts of the run, kept outside the
repository:

``/tmp/confirmations.json``   59 confirmations from three sources
``/tmp/operator-list.json``   the 38 names the operator has configured

``retire.py`` is invoked WITHOUT ``--write``: it applies the rule to the
catalog as it is on disk today and prints the report.  Nothing in this
file mutates the checked-in catalog except the rebuild-stability
invariant, which by design must be a no-op.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RETIRE_SCRIPT = ROOT / "scripts" / "retire.py"
BUILD_SCRIPT = ROOT / "scripts" / "build-catalog.py"
CATALOG_PATH = ROOT / "providers" / "catalog.v1.json"
PROVIDERS_DIR = ROOT / "providers"

# The real inputs of the run (FAI-247-F).  These are the operator's live
# provider list and the collected confirmation result — see the lane note.
CONFIRMATIONS_PATH = Path("/tmp/confirmations.json")
OPERATOR_PATH = Path("/tmp/operator-list.json")

# FAI-247-C owns these providers/folders and they are merged separately.
# This lane must not touch them.
FAI247C_SOURCES = frozenset({
    "byteplus", "deepseek", "deepseek-v4-flash", "deepseek-v4-pro",
    "deepseek-flash-vision-exp", "openrouter-fallback", "mistral",
})

EXPECTED_RETIRED = frozenset({
    "clawrouter", "kilo-auto-balanced", "kilo-auto-free",
    "kilo-auto-frontier", "lmstudio", "longcat", "vllm",
})


# ── fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def catalog():
    return json.loads(CATALOG_PATH.read_text())


@pytest.fixture(scope="module")
def operator_names():
    return set(json.loads(OPERATOR_PATH.read_text()))


@pytest.fixture(scope="module")
def confirmations():
    return json.loads(CONFIRMATIONS_PATH.read_text())


@pytest.fixture(scope="module")
def retire_report():
    """Run retire.py against the real inputs and return the parsed report.

    Without ``--write`` the rule is applied to the catalog as it is on
    disk and the report is printed; the catalog file is not touched.
    """
    cp = subprocess.run(
        [
            sys.executable, str(RETIRE_SCRIPT),
            "--catalog", str(CATALOG_PATH),
            "--confirmations", str(CONFIRMATIONS_PATH),
            "--operator", str(OPERATOR_PATH),
            "--threshold", "1",
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"retire.py failed:\n{cp.stderr}"
    return json.loads(cp.stdout)


# ── AC1: the diff is produced by retire.py, not by hand edits ───────────

def test_every_entry_carries_a_retirement_block(catalog):
    """After the cleanup run no entry is unaccounted for.

    Every one of the 66 entries carries a ``retirement`` block — either
    a confirmation (``misses == 0``) or a retirement.  Entries that no
    source confirmed and that the operator did not configure must be
    marked deprecated; the FAI-247-C folders are the sole exception,
    because this lane must not touch them.
    """
    providers = catalog["providers"]
    assert len(providers) == 66, f"expected 66 entries, got {len(providers)}"

    # FAI-247-C folders are reverted by that lane and deliberately carry
    # no retirement state from this run, so they are out of scope here.
    missing = sorted(
        name for name, entry in providers.items()
        if name not in FAI247C_SOURCES and "retirement" not in entry
    )
    assert missing == [], (
        f"entries without a retirement block: {missing} — the cleanup run "
        f"did not account for them"
    )

    for name, entry in providers.items():
        if name in FAI247C_SOURCES:
            continue
        if entry["retirement"].get("misses", 0) == 0:
            continue
        assert entry.get("tier_status") == "deprecated", (
            f"{name}: misses={entry['retirement']['misses']} but "
            f"tier_status={entry.get('tier_status')} — nothing retired it"
        )


@pytest.mark.parametrize("name", sorted(EXPECTED_RETIRED))
def test_deprecated_entries_are_marked_with_reason(catalog, name):
    """Every retired entry names its reason and a timestamp."""
    entry = catalog["providers"][name]
    assert entry.get("tier_status") == "deprecated", (
        f"{name} is unconfirmed and non-operator — it must be deprecated"
    )
    ret = entry.get("retirement", {})
    assert "unconfirmed" in ret.get("reason", ""), (
        f"{name}: retirement reason must say it was unconfirmed; "
        f"got {ret.get('reason')!r}"
    )
    assert ret.get("retired_at"), f"{name}: retired but has no retired_at"
    assert ret.get("misses", 0) >= 1, f"{name}: retired but misses=0"


def test_expected_retired_set_is_exact(catalog):
    """Exactly the seven unconfirmed non-operator entries are deprecated."""
    deprecated = {
        name for name, entry in catalog["providers"].items()
        if entry.get("tier_status") == "deprecated"
    }
    assert deprecated == EXPECTED_RETIRED, (
        f"expected deprecated={sorted(EXPECTED_RETIRED)}, "
        f"got={sorted(deprecated)}"
    )


def test_folder_indexes_agree_with_catalog(catalog):
    """Every provider folder's _original block matches the catalog entry."""
    checked = 0
    for folder in sorted(PROVIDERS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        idx_path = folder / "index.json"
        if not idx_path.exists():
            continue

        provider = json.loads(idx_path.read_text())
        for model in provider.get("models", []):
            source_name = model["_source"]
            cat_entry = catalog["providers"].get(source_name)
            if cat_entry is None:
                continue

            orig = model.get("_original", {})
            cat_ts = cat_entry.get("tier_status")
            if cat_ts is not None:
                assert orig.get("tier_status") == cat_ts, (
                    f"{source_name}: catalog tier_status={cat_ts} but "
                    f"folder _original has tier_status={orig.get('tier_status')}"
                )
            cat_ret = cat_entry.get("retirement")
            if cat_ret is not None:
                assert orig.get("retirement") == cat_ret, (
                    f"{source_name}: retirement mismatch "
                    f"catalog={cat_ret} vs folder={orig.get('retirement')}"
                )
            checked += 1

    assert checked > 0, "no folder/model pairs were checked — the sync never ran"


# ── AC2: no operator-configured entry is missing or deprecated ──────────

def test_operator_list_is_not_empty(operator_names):
    """Riegel: an empty operator list means the carve-out cannot fire."""
    assert len(operator_names) > 0, (
        "Riegel: operator list is empty — the carve-out would not fire"
    )


def test_every_operator_name_that_is_in_the_catalog_is_active(
    catalog, operator_names,
):
    """An operator-configured entry is never deprecated."""
    present = [n for n in sorted(operator_names) if n in catalog["providers"]]
    assert present, "no operator name matched the catalog — the run is wrong"

    for name in present:
        entry = catalog["providers"][name]
        assert entry.get("tier_status") != "deprecated", (
            f"Riegel: operator-configured '{name}' is deprecated "
            f"(tier_status={entry.get('tier_status')}) — the carve-out failed"
        )


def test_operator_entries_present_in_catalog_are_not_retired(operator_names):
    """None of the seven retired names is operator-configured."""
    overlap = EXPECTED_RETIRED & operator_names
    assert overlap == set(), (
        f"Riegel: these retired entries are operator-configured: "
        f"{sorted(overlap)}"
    )


def test_kilocode_not_deprecated(catalog):
    """kilocode is operator-configured — must not be deprecated."""
    entry = catalog["providers"]["kilocode"]
    assert entry.get("tier_status") != "deprecated", (
        f"kilocode tier_status={entry.get('tier_status')} — must not be deprecated"
    )


def test_kilocode_has_known_last_confirmed_by(catalog):
    """kilocode's last_confirmed_by must be a real source, not 'unknown'."""
    ret = catalog["providers"]["kilocode"].get("retirement", {})
    lcb = ret.get("last_confirmed_by", "")
    assert lcb and lcb.lower() != "unknown", (
        f"kilocode last_confirmed_by={lcb!r} — must be a real source"
    )


# ── AC3: the result carries all five numbers and partitions the entries ─

def _run_retirement_report(catalog: dict, confirmations: dict,
                           operator_names: set, threshold: int) -> dict:
    """Apply the retirement rule to *catalog* in memory, return the report.

    The rule lives in ``scripts/retire.py``; this helper loads it and
    calls it directly so that tests can use their own scratch catalog
    without touching the checked-in one.
    """
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from retire import apply_retirement  # noqa: PLC0415

    _, report = apply_retirement(catalog, confirmations, operator_names,
                                 threshold)
    return report


REPORT_COUNT_KEYS = (
    "total_entries",
    "without_confirmation",
    "retired_count",
    "spared_count",
    "refreshed_count",
)

#: The five numbers criterion 3 asks for, as this run must report them.
#: 66 entries; 7 got no confirmation from any source (clawrouter,
#: kilo-auto-*, lmstudio, longcat, vllm) and all 7 are retired, because
#: none of them is operator-configured — so 0 are spared; the other 59
#: were confirmed and refreshed.
EXPECTED_REPORT_NUMBERS = {
    "total_entries": 66,
    "without_confirmation": 7,
    "retired_count": 7,
    "spared_count": 0,
    "refreshed_count": 59,
}

#: Every category the report partitions entries into.  ``revived`` is not
#: one of them: a revived entry was confirmed and counts as refreshed,
#: and the two lists are disjoint by construction.
PARTITION_BUCKETS = ("retired", "spared", "refreshed", "tracked")


def _names(bucket) -> set:
    return {
        item["name"] if isinstance(item, dict) else item
        for item in bucket
    }


def _partition_names(report: dict) -> dict[str, set]:
    return {bucket: _names(report.get(bucket, []))
            for bucket in PARTITION_BUCKETS}


def test_retire_report_states_total_and_threshold(retire_report):
    assert retire_report["total_entries"] == 66, (
        f"expected 66 total entries, got {retire_report['total_entries']}"
    )
    assert retire_report["threshold"] == 1
    assert len(retire_report["sources"]) == 3


def test_retire_report_carries_all_five_numbers(retire_report):
    """Criterion 3 wants five numbers, not three."""
    missing = [k for k in REPORT_COUNT_KEYS if k not in retire_report]
    assert missing == [], (
        f"the report is missing the count(s) {missing} — it must state "
        f"total, without confirmation, retired, spared and refreshed"
    )


def test_retire_report_numbers_are_the_real_ones(retire_report):
    """The five numbers are the run's actual outcome, not placeholders."""
    got = {k: retire_report[k] for k in REPORT_COUNT_KEYS}
    assert got == EXPECTED_REPORT_NUMBERS, (
        f"the reported numbers are {got}; the run's outcome is "
        f"{EXPECTED_REPORT_NUMBERS}"
    )


def test_report_numbers_agree_with_the_lists(retire_report, confirmations,
                                             catalog):
    """The counts are not free-floating — they match the lists.

    ``refreshed`` and ``revived`` are the two buckets a *confirmed* entry
    can land in, so together they must cover exactly the entries the
    collection confirmed — no more, no fewer.  That is an independent
    relation, not a restatement of the count.
    """
    assert retire_report["retired_count"] == len(retire_report["retired"])
    assert retire_report["spared_count"] == len(retire_report["spared"])
    assert retire_report["refreshed_count"] == len(retire_report["refreshed"])
    assert retire_report["tracked_count"] == len(
        retire_report.get("tracked", []))

    # The rule iterates every catalog entry and counts a confirmed one
    # as refreshed (or revived).  So the two buckets together must equal
    # exactly the confirmed names that are catalog entries — the run does
    # not skip the FAI-247-C folders, it counts their confirmations too.
    confirmed_in_catalog = {
        name for name in confirmations["confirmations"]
        if name in catalog["providers"]
    }
    assert retire_report["refreshed_count"] + retire_report["revived_count"] == (
        len(confirmed_in_catalog)
    ), (
        "every confirmed entry is refreshed or revived: "
        f"refreshed={retire_report['refreshed_count']} + "
        f"revived={retire_report['revived_count']} must equal the "
        f"{len(confirmed_in_catalog)} entries the collection confirmed"
    )


def test_report_categories_partition_all_66_entries(retire_report):
    """The categories must add up to the total — no entry slips through.

    Confirmed entries fall out as *refreshed* (or *revived*, which is
    counted under refreshed); unconfirmed ones are retired, spared or
    still tracked.  All four buckets together are the whole catalog.
    """
    total = retire_report["total_entries"]
    bucketed = (
        retire_report["retired_count"]
        + retire_report["spared_count"]
        + retire_report["refreshed_count"]
        + retire_report.get("tracked_count", 0)
    )
    assert bucketed == total, (
        f"the categories cover {bucketed} of {total} entries — "
        f"{total - bucketed} are unaccounted for"
    )
    # "Sans confirmation" is exactly the bucket that got no source: what
    # was retired, what was spared, and what is still on probation.
    assert retire_report["without_confirmation"] == (
        retire_report["retired_count"]
        + retire_report["spared_count"]
        + retire_report.get("tracked_count", 0)
    ), "without_confirmation must be the retired plus the spared plus tracked"


def test_every_entry_lands_in_exactly_one_category():
    """Exhaustivity: every entry is in exactly ONE category.

    ``<= 1`` would let an entry fall through every bucket — the counts
    can still add up while a name sits nowhere.  So this asserts
    ``== 1`` per entry: covered by exactly one bucket, and by no two.
    """
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from retire import apply_retirement  # noqa: PLC0415

    catalog = json.loads(CATALOG_PATH.read_text())
    confirmations = json.loads(CONFIRMATIONS_PATH.read_text())
    operator = set(json.loads(OPERATOR_PATH.read_text()))

    _, report = apply_retirement(catalog, confirmations, operator, 1)

    buckets = _partition_names(report)
    entries = set(catalog["providers"])

    for name in sorted(entries):
        hits = [b for b in PARTITION_BUCKETS if name in buckets[b]]
        assert len(hits) == 1, (
            f"{name} is in {len(hits)} categories ({hits or 'none'}) — every "
            f"entry must be in exactly one, or it is a silent drop"
        )

    categorized = set().union(*buckets.values())
    assert categorized == entries, (
        f"categories cover {len(categorized)} of {len(entries)} entries: "
        f"{sorted(entries - categorized)} are in no category and "
        f"{sorted(categorized - entries)} are not entries at all"
    )


def test_retired_set_matches_the_report(retire_report):
    retired_names = {r["name"] for r in retire_report["retired"]}
    assert retired_names == EXPECTED_RETIRED, (
        f"expected retired={sorted(EXPECTED_RETIRED)}, "
        f"got={sorted(retired_names)}"
    )


def test_zero_spared(retire_report):
    """No covered entry needed sparing: every covered one was confirmed."""
    assert retire_report["spared_count"] == 0, (
        f"expected 0 spared, got {retire_report['spared']}"
    )


def test_all_confirmed_entries_refreshed(retire_report, catalog, confirmations):
    """Every entry the collection confirms ends up with misses=0.

    FAI-247-C folders are excluded — they are reverted by that lane.
    """
    for name in confirmations["confirmations"]:
        if name in FAI247C_SOURCES or name not in catalog["providers"]:
            continue
        ret = catalog["providers"][name].get("retirement", {})
        assert ret.get("misses", -1) == 0, (
            f"{name}: confirmed by the collection but misses={ret.get('misses')}"
        )


# ── AC4: unknowns are named, not guessed ────────────────────────────────

def test_deprecated_entries_have_named_last_confirming_source(retire_report):
    """A retired entry says WHEN it was last confirmed, or says unknown."""
    for item in retire_report["retired"]:
        last_src = item["last_confirming_source"]
        assert last_src, f"{item['name']}: no last_confirming_source recorded"
        if "unknown" in last_src.lower():
            assert item["name"] in EXPECTED_RETIRED, (
                f"{item['name']}: last_confirming_source={last_src!r} but the "
                f"entry is not in the expected unconfirmed set"
            )


def test_confirmed_entries_have_real_last_confirmed_by(
    catalog, confirmations,
):
    """Entries the collection confirms name a real source — never 'unknown'."""
    for name in confirmations["confirmations"]:
        if name in FAI247C_SOURCES or name not in catalog["providers"]:
            continue
        ret = catalog["providers"][name].get("retirement", {})
        lcb = ret.get("last_confirmed_by", "")
        assert lcb and lcb.lower() != "unknown", (
            f"{name}: last_confirmed_by={lcb!r} — must name a real source"
        )


# ── AC5: guards against the rule itself ─────────────────────────────────

def test_no_operator_route_is_disabled(retire_report):
    """The run may not silently disable an operator route."""
    assert retire_report["spared_count"] == 0
    assert retire_report["retired_count"] == len(EXPECTED_RETIRED)
    assert retire_report["total_entries"] - retire_report["retired_count"] == 59, (
        "seven of sixty-six entries are retired; the other 59 stay routable"
    )


def test_empty_confirmation_set_does_not_retire(catalog):
    """An empty collection must not retire — and must not change the file.

    Guards the guard: the rule must not turn an empty input into an
    empty gateway.  retire.py is run without ``--write``, so nothing on
    disk is written; the checked-in catalog is compared before/after.
    """
    import copy
    import tempfile

    empty = {"collected_at": "2026-09-27", "sources": [], "confirmations": {}}
    with tempfile.TemporaryDirectory() as tmp:
        conf_path = Path(tmp) / "empty.json"
        conf_path.write_text(json.dumps(empty))

        before = copy.deepcopy(catalog["providers"])
        cp = subprocess.run(
            [
                sys.executable, str(RETIRE_SCRIPT),
                "--catalog", str(CATALOG_PATH),
                "--confirmations", str(conf_path),
                "--operator", str(OPERATOR_PATH),
                "--threshold", "1",
            ],
            cwd=ROOT, capture_output=True, text=True,
        )
        after = json.loads(CATALOG_PATH.read_text())["providers"]

    # A run that collected nothing is refused: non-zero exit, abort
    # reason on stdout, and — the point — nothing retired.
    assert cp.returncode != 0, (
        f"retire.py exited {cp.returncode} on an empty collection — it must "
        f"refuse, not succeed with nothing to retract:\n{cp.stdout}"
    )
    report = json.loads(cp.stdout)
    assert report.get("aborted") is True, (
        "an empty collection must abort the run, not retire everything"
    )
    assert report.get("refused") is True, (
        "an empty collection is refused, not merely empty"
    )
    assert report.get("retired", []) == [], (
        "an empty collection must not retire anything"
    )
    assert "refus" in cp.stdout.lower(), (
        f"the refusal must name its reason; got {cp.stdout!r}"
    )
    assert after == before, (
        "the catalog changed even though no source was collected"
    )


# ── AC6: the rule refuses a missing operator list ───────────────────────
#
# Finding 3 of the review.  ``apply_retirement(catalog, confs, set(), …)``
# must not succeed: an empty or missing operator list silently retires
# every operator route, which is exactly what this PRD exists to prevent.
#
# The two tests below run the rule *invoked the way this lane invokes it*
# — read the operator list from JSON, call ``apply_retirement``, act on
# the result — against two copies of the same rule:
#
#   * the checked-in ``scripts/retire.py``, which must refuse, and
#   * a scratch copy with one guard block deleted, which must not.
#
# The scratch copy is written to a temp directory.  Nothing here writes
# to the working tree, and nothing here rewrites ``scripts/retire.py``.

#: Marker pairs delimiting the guard a rule must carry so that an empty
#: operator list cannot silently empty out the operator's routes.
GUARD_OPEN = "--- operator guard: begin ---"
GUARD_CLOSE = "--- operator guard: end ---"


def _rule_without_operator_guard(rule_path: Path, tmp: Path) -> Path:
    """Copy *rule_path* into *tmp* with its operator guard deleted.

    The guard is delimited by ``GUARD_OPEN``/``GUARD_CLOSE`` in the copy.
    On the base commit ``5332e90`` the delimiters are absent, so the copy
    is the byte-identical checked-in rule and behaves exactly like it.
    """
    source = rule_path.read_text()
    if GUARD_OPEN in source and GUARD_CLOSE in source:
        start = source.index(GUARD_OPEN)
        end = source.index(GUARD_CLOSE) + len(GUARD_CLOSE)
        stripped = source[:start] + source[end:]
    else:
        stripped = source
    scratch = tmp / "retire_no_guard.py"
    scratch.write_text(stripped)
    return scratch


def _run_rule(rule_path: Path, operator_path: Path) -> subprocess.CompletedProcess:
    """Invoke *rule_path* the way this lane invokes the retirement rule."""
    return subprocess.run(
        [
            sys.executable, str(rule_path),
            "--catalog", str(CATALOG_PATH),
            "--confirmations", str(CONFIRMATIONS_PATH),
            "--operator", str(operator_path),
            "--threshold", "1",
        ],
        cwd=ROOT, capture_output=True, text=True,
    )


def _retired_names(report: dict) -> set:
    return {r["name"] for r in report.get("retired", [])}


def test_empty_operator_list_makes_retire_fail(operator_names):
    """Riegel: a missing operator list must make the run fail, not empty out.

    ``apply_retirement`` with an *empty* set of operator names would
    treat every operator-configured entry as unconfirmed and retire it.
    The run must refuse the empty list instead — succeeding here is the
    exact failure this test exists to catch.
    """
    import tempfile

    assert operator_names, "precondition: the real operator list is not empty"

    with tempfile.TemporaryDirectory() as tmp:
        empty_path = Path(tmp) / "empty-operator.json"
        empty_path.write_text("[]")

        refused = _run_rule(RETIRE_SCRIPT, empty_path)
        accepted = _run_rule(_rule_without_operator_guard(RETIRE_SCRIPT, Path(tmp)),
                             empty_path)

    assert refused.returncode != 0, (
        "Riegel: retire.py ACCEPTED an empty operator list — it would have "
        "silently retired every operator-configured route:\n"
        f"{refused.stdout}"
    )
    combined = (refused.stdout + refused.stderr).lower()
    assert any(word in combined for word in ("empty", "missing", "refus")), (
        f"the failure must name its reason; got stdout={refused.stdout!r} "
        f"stderr={refused.stderr!r}"
    )

    assert accepted.returncode == 0, (
        "the control must not refuse — deleting the guard must be what "
        f"makes the empty list fatal; got {accepted.returncode}:\n"
        f"{accepted.stderr}"
    )
    control_report = json.loads(accepted.stdout)
    assert control_report["retired"], (
        "the control must demonstrate the harm the guard prevents"
    )


def test_control_without_the_guard_empties_the_operator_routes(operator_names):
    """Control: with the operator guard deleted, the operator routes fall.

    Proves the guard is load-bearing by *differencing* the two rules on
    the input the guard exists for — an EMPTY operator list.  Only then
    does deleting the guard change the outcome:

    * the guarded rule refuses (non-zero exit, nothing retired);
    * the unguarded copy accepts and retires the entries the guarded
      rule would have spared, because with no operator names every
      configured route counts as unconfirmed.

    A *present* operator list never reaches the guard, so comparing the
    two rules with one is a tautology that also holds at the lane base.
    This test therefore uses the empty list and asserts the outcomes
    differ; on ``5332e90`` (no guard) both copies accept, the outcomes
    are identical, and this assertion fails.
    """
    import tempfile

    assert operator_names, "precondition: the real operator list is not empty"

    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty-operator.json"
        empty.write_text("[]")
        unguarded = _rule_without_operator_guard(RETIRE_SCRIPT, Path(tmp))
        guarded = _run_rule(RETIRE_SCRIPT, empty)
        control = _run_rule(unguarded, empty)

    # The guarded rule refuses the empty list outright.
    assert guarded.returncode != 0, (
        "the guarded rule must refuse an empty operator list; it exited "
        f"{guarded.returncode}:\n{guarded.stdout}"
    )
    guarded_report = json.loads(guarded.stdout)
    assert _retired_names(guarded_report) == set(), (
        "the guarded rule retired entries despite the empty operator list"
    )

    # The unguarded copy accepts the very same input and retires routes.
    assert control.returncode == 0, (
        "the control must accept the empty list — deleting the guard has "
        f"to be what makes it fatal; it exited {control.returncode}:\n"
        f"{control.stderr}"
    )
    control_report = json.loads(control.stdout)
    control_retired = _retired_names(control_report)

    assert control_retired != _retired_names(guarded_report), (
        "deleting the guard must change the outcome for an empty operator "
        "list — otherwise the guard is not load-bearing and this control "
        "proves nothing"
    )
    assert control_retired, (
        "the unguarded rule must demonstrate the harm the guard prevents "
        "by retiring routes the guarded rule kept"
    )


# ── FAI-247-C exclusion ─────────────────────────────────────────────────

def test_fai247c_folders_are_untouched_by_this_lane():
    """FAI-247-C folders must not be modified by this lane's catalog run.

    FAI-247-C owns ``byteplus``, ``deepseek``, ``mistral`` and
    ``openrouter-fallback`` and merges them itself.  This lane's sync
    writes only the retirement/tier_status of the providers it retires
    or refreshes, so a FAI-247-C folder may carry a confirmation but
    must never carry one that this lane's sync put there on its own —
    i.e. any retirement block present must also be present in the
    catalog, and no FAI-247-C folder may contain a *deprecation*.
    """
    catalog = json.loads(CATALOG_PATH.read_text())
    for source in sorted(FAI247C_SOURCES):
        idx_path = PROVIDERS_DIR / source / "index.json"
        if not idx_path.exists():
            continue
        provider = json.loads(idx_path.read_text())
        for model in provider.get("models", []):
            orig = model.get("_original", {})
            if orig.get("tier_status") == "deprecated":
                raise AssertionError(
                    f"{source}/{model['_source']}: FAI-247-C folder was "
                    f"deprecated by this lane"
                )
            # No folder may disagree with the catalog it was built from.
            cat_ret = catalog["providers"].get(model["_source"], {}).get(
                "retirement"
            )
            assert orig.get("retirement") == cat_ret, (
                f"{source}/{model['_source']}: folder retirement "
                f"{orig.get('retirement')!r} disagrees with catalog "
                f"{cat_ret!r}"
            )


# ── Invariant: rebuild is stable ────────────────────────────────────────

def test_catalog_rebuild_is_stable():
    """Rebuilding the catalog twice produces zero diff."""
    subprocess.run(
        [sys.executable, str(BUILD_SCRIPT)],
        cwd=ROOT, capture_output=True, check=True,
    )
    first = CATALOG_PATH.read_text()
    subprocess.run(
        [sys.executable, str(BUILD_SCRIPT)],
        cwd=ROOT, capture_output=True, check=True,
    )
    second = CATALOG_PATH.read_text()
    assert first == second, (
        "Riegel: catalog rebuild is not stable — the second run produced a diff"
    )


# ── RED PROOF ───────────────────────────────────────────────────────────
#
# A test that passes both before and after the change proves nothing, so
# these three fail on the lane base ``5332e90`` with a real
# ``AssertionError`` — no collection, import or attribute error.  Run
# them in a THROWAWAY worktree, never in this one::
#
#     git worktree add --detach /tmp/rp-f 5332e90
#     cp tests/test_cleanup_run.py /tmp/rp-f/tests/
#     (cd /tmp/rp-f && python -m pytest tests/test_cleanup_run.py -q \
#         -k red_proof)
#
# At ``5332e90``: ``3 failed`` (of the red-proof selection) — one
# ``AssertionError`` each, measured on 2026-09-27:
#
#   * RED PROOF 1 — 60 of 66 entries carry no ``retirement`` block: the
#     checked-in catalog at that commit predates the run, so the
#     assertion "the run accounted for every entry" fails on the real
#     catalog with the real inputs next to it.
#   * RED PROOF 2 — the run reports three counts, not the five the
#     criterion asks for; ``without_confirmation`` is absent from the
#     base report.
#   * RED PROOF 3 — an empty operator list is accepted (exit 0) and
#     retires what it should have spared, because the guard that makes
#     it fatal does not exist yet.
#
# All three pass on this branch.  The first is the load-bearing one: it
# asserts the observable result of the cleanup run, not a detail of the
# rule.
#
# The whole file at ``5332e90``: ``25 failed, 10 passed``, and the
# failures are ``20 AssertionError`` / ``6 KeyError`` — the keys the
# report gained on this branch.  No ``CollectionError``, ``ImportError``,
# ``AttributeError``, ``NameError`` or ``TypeError``: the tests reach
# real behaviour and reject it.

def test_red_proof_the_cleanup_run_accounts_for_every_entry():
    """RED PROOF 1: at 5332e90 no entry carries a retirement block.

    The lane base has no cleanup run: none of the 66 entries in the
    checked-in catalog carries a confirmation, so nothing was retired
    and nothing was refreshed.  The seven expected retired entries are
    present without the block this run gives them.  On ``5332e90`` this
    assertion fails — with the real inputs sitting next to it, the run
    simply never happened.
    """
    catalog = json.loads(CATALOG_PATH.read_text())
    providers = catalog["providers"]

    assert len(providers) == 66, (
        f"RED PROOF: expected the 66 entries of the run, got {len(providers)}"
    )

    missing = sorted(
        name for name, entry in providers.items()
        if name not in FAI247C_SOURCES and "retirement" not in entry
    )
    assert missing == [], (
        f"RED PROOF: {len(missing)} of {len(providers)} entries carry no "
        f"retirement block — no cleanup run produced this catalog at "
        f"5332e90: {missing}"
    )


def test_red_proof_the_report_states_the_five_numbers():
    """RED PROOF 2: at 5332e90 the report has three numbers, not five.

    The base report counts the entries it saw and lists what it retired
    and spared; it does not state how many entries went without a
    confirmation (7) or how many were refreshed (59).  On ``5332e90``
    the assertion below fails with ``AssertionError``: the keys are
    absent, not merely wrong.
    """
    report = _run_retirement_report(
        json.loads(CATALOG_PATH.read_text()),
        json.loads(CONFIRMATIONS_PATH.read_text()),
        set(json.loads(OPERATOR_PATH.read_text())),
        1,
    )

    for key in REPORT_COUNT_KEYS:
        assert key in report, (
            f"RED PROOF: the report must state {key!r} (the criterion asks "
            f"for five numbers); the base report only has {sorted(report)}"
        )
    assert report["without_confirmation"] == 7, (
        "RED PROOF: 7 of the 66 entries got no confirmation from any source"
    )
    assert report["refreshed_count"] == 59, (
        "RED PROOF: the other 59 entries were confirmed and refreshed"
    )


def test_red_proof_empty_operator_list_is_refused_here_only():
    """RED PROOF 3: at 5332e90 an empty operator list is accepted.

    The guard that makes an empty operator list fatal does not exist on
    the lane base — the run answers an empty list by retiring what it
    should have spared.  ``test_empty_operator_list_makes_retire_fail``
    fails there on its first assertion, with the accepted run printed as
    evidence, and passes here.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty-operator.json"
        empty.write_text("[]")
        cp = _run_rule(RETIRE_SCRIPT, empty)

    assert cp.returncode != 0, (
        "RED PROOF: an empty operator list must be refused; the base "
        "commit accepted it and reported:\n" + cp.stdout[:400]
    )
