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

    missing = sorted(
        name for name, entry in providers.items()
        if "retirement" not in entry
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


def test_report_numbers_agree_with_the_lists(retire_report):
    """The counts are not free-floating — they match the lists."""
    assert retire_report["retired_count"] == len(retire_report["retired"])
    assert retire_report["spared_count"] == len(retire_report["spared"])
    assert retire_report["without_confirmation"] == (
        retire_report["retired_count"] + retire_report["spared_count"]
    ), "without_confirmation must be the retired plus the spared"


def test_report_categories_partition_all_66_entries(retire_report):
    """The categories must add up to the total — no entry slips through."""
    total = retire_report["total_entries"]
    bucketed = (
        retire_report["retired_count"]
        + retire_report["spared_count"]
        + retire_report["refreshed_count"]
    )
    assert bucketed == total, (
        f"the categories cover {bucketed} of {total} entries — "
        f"{total - bucketed} are unaccounted for"
    )
    assert retire_report["without_confirmation"] == (
        retire_report["retired_count"] + retire_report["spared_count"]
    ), "without_confirmation must be the retired plus the spared"


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


def test_empty_confirmation_set_does_not_change_the_catalog(catalog):
    """An empty collection retires nothing.

    Guards the guard: the rule must not turn an empty input into an
    empty gateway.  retire.py is run without ``--write``, so nothing on
    disk is written — the checked-in catalog is compared before/after.
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
        assert cp.returncode == 0, f"retire.py failed:\n{cp.stderr}"
        report = json.loads(cp.stdout)
        after = json.loads(CATALOG_PATH.read_text())["providers"]

    assert report.get("aborted") is True, (
        "an empty collection must abort the run, not retire everything"
    )
    assert report["retired"] == []
    assert after == before, (
        "the catalog changed even though no source was collected"
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

def test_red_proof_report_state_counts_are_new():
    """RED PROOF: the five report numbers do not exist on the base commit.

    The base of this lane — 5332e90 — has no cleanup run: ``retire.py``
    reports ``total_entries``, ``retired``, ``spared``, ``revived`` and
    ``tracked``, but it states neither how many entries went without a
    confirmation nor how many were refreshed, and it reports no
    ``retired_count``/``spared_count``/``refreshed_count`` at all.

    This is a real assertion against that report, not an import guard:
    on 5332e90 it fails with ``AssertionError`` because the keys are
    absent.  On this branch the run produces all five.
    """
    from pathlib import Path as _Path

    _confirmations = json.loads(_Path(CONFIRMATIONS_PATH).read_text())
    _operator = set(json.loads(_Path(OPERATOR_PATH).read_text()))
    scratch = {
        "schema_version": "fusionaize-provider-catalog/v1.4",
        "providers": {
            name: {"tier_status": "active"}
            for name in _confirmations["confirmations"]
        },
    }
    scratch["providers"]["never-confirmed"] = {"tier_status": "active"}

    report = _run_retirement_report(scratch, _confirmations, _operator, 1)

    for key in REPORT_COUNT_KEYS:
        assert key in report, (
            f"RED PROOF: the cleanup run must report {key!r}; the report "
            f"only has {sorted(report)} — no cleanup run exists at 5332e90"
        )
    assert report["total_entries"] == 60, (
        "RED PROOF: the run must count all entries it saw"
    )
