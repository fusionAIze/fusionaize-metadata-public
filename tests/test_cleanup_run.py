"""Acceptance tests for the FAI-247-F cleanup run.

Verifies that the retirement rule ``scripts/retire.py`` produces the
correct outcome when it is fed the real operator list and the real
collection result, and that the resulting catalog is synced into the
per-provider folders and rebuilt deterministically.

The two inputs are the real artifacts of the run, committed with the
tests so a fresh checkout can run the suite:

``tests/fixtures/confirmations.json``  59 confirmations from three sources
``tests/fixtures/operator-list.json``  the 38 names the operator configured

``tests/fai247f_inputs.py`` derives both from the raw collection and
operator documents and reproduces them byte-for-byte, so the inputs are
reproducible rather than hand-carried::

    python tests/fai247f_inputs.py

``retire.py --write`` is run only in a *scratch copy* of the provider
tree, never against the working one: the tests check the claim that the
checked-in catalog is the rule's output by reproducing that output
elsewhere and comparing.  The one exception is
``test_catalog_rebuild_is_stable``, which runs the rebuild in place and
by design must be a no-op.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RETIRE_SCRIPT = ROOT / "scripts" / "retire.py"
BUILD_SCRIPT = ROOT / "scripts" / "build-catalog.py"
CATALOG_PATH = ROOT / "providers" / "catalog.v1.json"
PROVIDERS_DIR = ROOT / "providers"

# The real inputs of the run (FAI-247-F): the operator's configured
# provider list and the collected confirmation result, both committed
# under tests/ so the suite reads the repository, never a file in
# ``$HOME`` or ``/tmp``.  The catalog the rule was applied *to* is not a
# third fixture: it is the folder tree with the rule's own stamps
# stripped and rebuilt (``_pre_rule_catalog``), which is what the pre-rule
# catalog actually was — a file checked in beside them would be a copy
# that could drift from that tree.
CONFIRMATIONS_PATH = ROOT / "tests" / "fixtures" / "confirmations.json"
OPERATOR_PATH = ROOT / "tests" / "fixtures" / "operator-list.json"

EXPECTED_RETIRED = frozenset({
    "clawrouter", "kilo-auto-balanced", "kilo-auto-free",
    "kilo-auto-frontier", "lmstudio", "longcat", "vllm",
})

#: Entries FAI-247-C owns and merges separately.  The folders are the
#: source the catalog is rebuilt from, so a field this lane stamped on one
#: of them would be reverted — or resurrected — by the next merge.  The
#: cleanup therefore leaves them exactly as that lane left them: no
#: confirmation stamp, no miss, no retirement, no ``tier_status`` write.
#: They are counted in ``total_entries`` (the run still sees the whole
#: catalog) but in none of its buckets, so the partition arithmetic in
#: the report tests adds them as a fifth, disjoint category.
EXPECTED_FOREIGN = frozenset({
    "byteplus",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-flash-vision-exp",
    "mistral",
    "openrouter-fallback",
})

def _strip_rule_fields(path: Path) -> None:
    """Recover the pre-rule tree from a folder's ``_original`` blocks.

    ``retirement`` is the run's own field, so it is removed everywhere.
    ``tier_status`` is *not*: entries carry an ``active``/``preview``/
    ``expired`` status that predates this lane, and the rebuild must keep
    it.  The run's only ``tier_status`` write is ``deprecated`` — that is
    the one value to strip, or the stripped tree would lose every
    pre-existing status and the rebuilt catalog would disagree with the
    checked-in one on eighteen entries.
    """
    provider = json.loads(path.read_text())
    for model in provider.get("models", []):
        original = model.get("_original")
        if original is None:
            continue
        original.pop("retirement", None)
        if original.get("tier_status") == "deprecated":
            original.pop("tier_status", None)
    path.write_text(json.dumps(provider, indent=2, ensure_ascii=False) + "\n")


def _pre_rule_catalog() -> dict:
    """Rebuild the catalog from a copy of the folders with no rule stamps.

    This is the catalog the run actually consumed: ``build-catalog.py``
    regenerates the catalog from the folders, so stripping the rule's two
    fields from the tree and rebuilding gives back the pre-run document.
    The copy is a temp directory, so the working tree is untouched.
    """
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "providers"
        shutil.copytree(PROVIDERS_DIR, scratch,
                        ignore=shutil.ignore_patterns("*.pyc"))
        for folder in sorted(scratch.iterdir()):
            idx = folder / "index.json"
            if folder.is_dir() and idx.exists():
                _strip_rule_fields(idx)

        # Drop the checked-in catalog: build_catalog reads it only to
        # preserve the supplementary sections (which are not the rule's
        # work) and the generated_at timestamp, so with no catalog the
        # rebuild returns exactly the tree's content.
        (scratch / "catalog.v1.json").unlink()

        import importlib.util  # noqa: PLC0415
        import inspect  # noqa: PLC0415

        spec = importlib.util.spec_from_file_location(
            "_build_catalog", BUILD_SCRIPT,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # ``build_catalog`` takes a second ``catalog_path`` argument on
        # this branch, so the rebuild can be aimed at the scratch tree.
        # At the lane base it takes one argument and always writes the
        # catalog it is run from — there, the scratch tree *is* the
        # checked-out tree, and the checked-in catalog is the pre-rule
        # catalog.  Reaching for it is what lets the red proof fail on a
        # real assertion instead of erroring in this helper.
        parameters = inspect.signature(module.build_catalog).parameters
        if len(parameters) >= 2:
            return module.build_catalog(scratch, scratch / "catalog.v1.json")
        return json.loads(CATALOG_PATH.read_text())


def _pre_rule_run_inputs() -> tuple[dict, dict, set]:
    """A pre-rule catalog, confirmations and operator list for one run.

    Guards against a rule that retires everything on missing input need
    *some* unconfirmed entry that is not operator-configured; the live
    run has none (every unconfirmed entry is operator-configured, so it
    is spared).  Placing the run a round ahead — dropping a handful of
    confirmed entries from the confirmations — puts ordinary entries on
    probation, which is what the second guard test exercises.
    """
    catalog = _pre_rule_catalog()
    confirmations = json.loads(CONFIRMATIONS_PATH.read_text())
    operator = set(json.loads(OPERATOR_PATH.read_text()))

    confirmed = set(confirmations["confirmations"])
    on_probation = sorted(confirmed)[:5]
    dropped = {
        "collected_at": confirmations["collected_at"],
        "sources": confirmations["sources"],
        "confirmations": {
            name: entry for name, entry in confirmations["confirmations"].items()
            if name not in on_probation
        },
    }
    return catalog, dropped, operator


# ── fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def catalog():
    return json.loads(CATALOG_PATH.read_text())


@pytest.fixture(scope="module")
def catalog_before():
    """The catalog as it stood BEFORE the run — the folder-rebuilt tree.

    The checked-in catalog already has the rule's ``retirement`` blocks
    and ``deprecated`` statuses on it, so re-running the rule on it
    retires nothing new and a report produced from it has empty
    ``retired``/``spared`` lists.  The pre-rule state is what
    ``build-catalog.py`` produces from the folders with those two fields
    stripped: fresh entries, no stamps.  Every report below describes
    the run applied to *that*.
    """
    return _pre_rule_catalog()


@pytest.fixture(scope="module")
def pre_rule_confirmations():
    """The collection result with the just-collected entries removed.

    The second guard test needs a run the rule accepts but in which some
    entries get no source — on the real input every unconfirmed entry is
    operator-configured, so such a run must be built.  See
    ``_pre_rule_run_inputs``.  The other tests read the real fixtures.
    """
    return _pre_rule_run_inputs()[1]


@pytest.fixture(scope="module")
def pre_rule_operator():
    return _pre_rule_run_inputs()[2]


@pytest.fixture(scope="module")
def operator_names():
    return set(json.loads(OPERATOR_PATH.read_text()))


@pytest.fixture(scope="module")
def confirmations():
    return json.loads(CONFIRMATIONS_PATH.read_text())


@pytest.fixture(scope="module")
def retire_report(catalog_before, confirmations, operator_names):
    """Apply the rule to the run's input and return the report.

    In memory, through the same ``apply_retirement`` the CLI calls: the
    input is the pre-rule catalog rebuilt from the folders, not the file
    in ``providers/`` (the run's *output*, which already carries the
    ``retirement`` stamps).  Applying the rule to the output retires
    nothing — its work is done — so the report would describe a no-op.
    """
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from retire import apply_retirement  # noqa: PLC0415

    import copy
    _, report = apply_retirement(
        copy.deepcopy(catalog_before), confirmations, operator_names, 1,
    )
    return report


# ── AC1: the diff is produced by retire.py, not by hand edits ───────────
#
# The load-bearing claim of the lane is that the checked-in catalog *is*
# the output of the rule: run ``retire.py --write`` and then
# ``build-catalog.py`` and you get the same file back, byte for byte.
# The tests below check that claim by actually running the rule in a
# scratch copy of the provider tree — not by inspecting the checked-in
# file for the blocks a hand edit could also have added.
#
# ``_run_rule_in_scratch`` copies the provider folders and the catalog
# into a temp directory, runs the rule (and optionally the rebuild) there,
# and returns the resulting catalog.  Nothing touches the working tree.

def _run_rule_in_scratch(tmp: Path, *, rebuild: bool = False) -> dict:
    """Apply retire.py --write in a scratch provider tree.

    Returns the catalog the rule produced.  With ``rebuild=True`` the
    catalog is then regenerated from the folders, which is the round trip
    the scheduled refresh performs.
    """
    scratch = tmp / "providers"
    shutil.copytree(PROVIDERS_DIR, scratch, ignore=shutil.ignore_patterns("*.pyc"))
    scratch_catalog = scratch / "catalog.v1.json"

    cp = subprocess.run(
        [
            sys.executable, str(RETIRE_SCRIPT),
            "--catalog", str(scratch_catalog),
            "--providers-dir", str(scratch),
            "--confirmations", str(CONFIRMATIONS_PATH),
            "--operator", str(OPERATOR_PATH),
            "--threshold", "1",
            "--write",
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"retire.py --write failed:\n{cp.stderr}"

    if rebuild:
        # Rebuild the SCRATCH tree, not the working one: pass the scratch
        # provider root so the scheduled refresh is reproduced in place.
        cp = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), str(scratch)],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert cp.returncode == 0, f"build-catalog.py failed:\n{cp.stderr}"

    return json.loads((scratch / "catalog.v1.json").read_text())


def test_the_checked_in_catalog_is_the_rules_output(tmp_path):
    """Running the rule reproduces the checked-in catalog exactly.

    This is the difference between "a rule ran" and "the file was
    edited": the rule is applied to a copy of the tree, and the copy it
    produces must equal the catalog in the repository.  A hand edit —
    e.g. escaping the non-ASCII the rule writes raw, or forgetting the
    retirement block the rule adds — shows up here as a diff.
    """
    produced = _run_rule_in_scratch(tmp_path)
    checked_in = json.loads(CATALOG_PATH.read_text())
    assert produced == checked_in, (
        "the checked-in catalog is not what retire.py produces — the diff "
        "was edited by hand, not applied by the rule"
    )


def test_the_rule_and_the_rebuild_agree(tmp_path):
    """After the rule writes, the scheduled rebuild changes nothing.

    ``build-catalog.py`` ignores the catalog and regenerates it from the
    folders, so the rule has to mirror ``tier_status``/``retirement``
    into the folders or the weekly refresh silently reverts the whole
    cleanup.  This asserts the round trip is a fixed point.
    """
    produced = _run_rule_in_scratch(tmp_path, rebuild=True)
    checked_in = json.loads(CATALOG_PATH.read_text())
    assert produced == checked_in, (
        "rebuilding from the folders does not reproduce the catalog — the "
        "rule wrote the catalog but not the folders it is built from"
    )


def test_the_rule_is_idempotent(tmp_path):
    """A second ``retire.py --write`` on its own output is a no-op.

    The first run retires seven entries and confirms the rest; running it
    again on the catalog it just wrote — the same collection round — must
    not move a single field.  A ``misses`` counter that keeps climbing,
    or a confirmation stamped with a new date, is the bug this catches.
    """
    once = _run_rule_in_scratch(tmp_path)

    scratch = tmp_path / "providers"
    cp = subprocess.run(
        [
            sys.executable, str(RETIRE_SCRIPT),
            "--catalog", str(scratch / "catalog.v1.json"),
            "--providers-dir", str(scratch),
            "--confirmations", str(CONFIRMATIONS_PATH),
            "--operator", str(OPERATOR_PATH),
            "--threshold", "1",
            "--write",
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"second retire.py --write failed:\n{cp.stderr}"
    twice = json.loads((scratch / "catalog.v1.json").read_text())

    assert twice == once, (
        "re-running the rule on its own output changed the catalog — the "
        "cleanup is not idempotent"
    )


def test_the_rule_maps_the_run_input_to_the_checked_in_catalog(tmp_path):
    """The rule turns the run's input into exactly the checked-in catalog.

    Take the pre-rule tree — the folders with the rule's stamps stripped,
    rebuilt — and apply the rule.  The result must equal the file in
    ``providers/``: the catalog on disk is the run's *output*, not a hand
    edit, and the report the fixture produces is this run's own.
    """
    scratch = tmp_path / "providers"
    shutil.copytree(PROVIDERS_DIR, scratch,
                    ignore=shutil.ignore_patterns("*.pyc"))
    for folder in sorted(scratch.iterdir()):
        idx = folder / "index.json"
        if folder.is_dir() and idx.exists():
            _strip_rule_fields(idx)

    target = scratch / "catalog.v1.json"
    # The run's input is the pre-rule *providers* map inside the document
    # the run actually consumes.  The supplementary sections
    # (``model_caps``, ``model_versions``, ``routing_modes``) and the
    # ``generated_at`` stamp are not the rule's work — the rule carries
    # them through unchanged — so the target starts from the checked-in
    # document's shell and swaps in the pre-rule entries.  Rebuilding the
    # shell from scratch would stamp "now" and drop those sections, which
    # would make this a test of the clock and of build-catalog.py rather
    # than of the rule.
    checked_in = json.loads(CATALOG_PATH.read_text())
    pre_rule = dict(checked_in)
    pre_rule["providers"] = _pre_rule_catalog()["providers"]
    target.write_text(json.dumps(pre_rule, ensure_ascii=False))

    cp = subprocess.run(
        [
            sys.executable, str(RETIRE_SCRIPT),
            "--catalog", str(target),
            "--providers-dir", str(scratch),
            "--confirmations", str(CONFIRMATIONS_PATH),
            "--operator", str(OPERATOR_PATH),
            "--threshold", "1",
            "--write",
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"retire.py --write failed:\n{cp.stderr}"

    produced = json.loads(target.read_text())
    checked_in = json.loads(CATALOG_PATH.read_text())
    assert produced == checked_in, (
        "applying the rule to the run's input did not reproduce the "
        "checked-in catalog — the committed file is not this run's output"
    )


def test_every_entry_carries_a_retirement_block(catalog):
    """After the cleanup run no entry is unaccounted for.

    Every one of the 66 entries the rule *owns* carries a ``retirement``
    block — either a confirmation (``misses == 0``) or a retirement.  The
    six FAI-247-C entries are the exception, and it is a deliberate one:
    their folders are the source the catalog is rebuilt from, so a block
    this lane wrote there would be reverted by the next merge.  The rule
    leaves them exactly as that lane left them.  Entries that no source
    confirmed and that the operator did not configure must be marked
    deprecated.
    """
    providers = catalog["providers"]
    assert len(providers) == 66, f"expected 66 entries, got {len(providers)}"

    missing = sorted(
        name for name, entry in providers.items()
        if "retirement" not in entry
    )
    assert missing == sorted(EXPECTED_FOREIGN), (
        f"entries without a retirement block: {missing} — expected exactly "
        f"the foreign set {sorted(EXPECTED_FOREIGN)}, and no other entry; "
        f"every entry the rule owns must be accounted for"
    )

    for name, entry in providers.items():
        if name in EXPECTED_FOREIGN:
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


#: The folders the run's source tree is built from.  Every catalog entry
#: the rule *owns* is mirrored into exactly one of them, so the number of
#: ``retirement`` blocks compared below is fixed by the catalog; a sync
#: that silently skipped a folder would make this count drop and fail.
EXPECTED_FOLDERS = frozenset(
    p.name for p in PROVIDERS_DIR.iterdir() if p.is_dir()
) - EXPECTED_FOREIGN


def test_folder_indexes_agree_with_catalog(catalog):
    """Every provider folder's _original block matches the catalog entry.

    The comparison is only worth anything if it actually compared
    something, so it is counted: every catalog entry the rule owns must
    be reached through a folder, and the number of comparisons must be
    exactly the size of that set.  A vacuous pass — an empty candidate
    set, or a folder the sync skipped — is caught by the count, not
    waved through by ``checked > 0``.
    """
    owned = set(catalog["providers"]) - EXPECTED_FOREIGN
    checked: set[str] = set()

    for folder in sorted(PROVIDERS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        idx_path = folder / "index.json"
        if not idx_path.exists():
            continue

        provider = json.loads(idx_path.read_text())
        for model in provider.get("models", []):
            source_name = model["_source"]
            if source_name in EXPECTED_FOREIGN:
                continue
            cat_entry = catalog["providers"].get(source_name)
            assert cat_entry is not None, (
                f"{folder.name}/{source_name}: the folder holds a model the "
                f"catalog does not — the rebuild would resurrect it"
            )

            orig = model.get("_original", {})
            cat_ts = cat_entry.get("tier_status")
            assert orig.get("tier_status") == cat_ts, (
                f"{source_name}: catalog tier_status={cat_ts} but "
                f"folder _original has tier_status={orig.get('tier_status')}"
            )
            cat_ret = cat_entry.get("retirement")
            assert cat_ret is not None, (
                f"{source_name}: owned catalog entry carries no retirement "
                f"block — the sync never ran for {folder.name}"
            )
            assert orig.get("retirement") == cat_ret, (
                f"{source_name}: retirement mismatch "
                f"catalog={cat_ret} vs folder={orig.get('retirement')}"
            )
            checked.add(source_name)

    assert checked == owned, (
        f"compared {len(checked)} folder entries against the catalog; the "
        f"rule owns {len(owned)} — folders never reached: "
        f"{sorted(owned - checked)}, unexpected: {sorted(checked - owned)}"
    )
    assert checked, "no folder/model pairs were checked — the sync never ran"


# ── AC2: no operator-configured entry is missing or deprecated ──────────

def test_operator_list_is_not_empty(operator_names):
    """Riegel: an empty operator list means the carve-out cannot fire."""
    assert len(operator_names) > 0, (
        "Riegel: operator list is empty — the carve-out would not fire"
    )


def test_operator_names_missing_from_the_catalog_are_stated(
    catalog, operator_names,
):
    """The operator list is a superset of the catalog — say how much so.

    The operator configures routes that are not catalog entries; 23 of
    the 38 names are absent.  Reporting only the intersection, as the
    predecessor test did, hides that: the run's coverage claim would be
    built on the 15 names it happened to match and never mention the 23
    it did not.  This test states the *difference* as its own claim, so
    a shrinking catalog — an entry an operator route silently vanished
    into — is a failure here rather than an invisible filter.
    """
    missing = set(operator_names) - set(catalog["providers"])
    catalog_side = set(operator_names) & set(catalog["providers"])

    # Nothing is claimed about which side a name lands on a priori; what
    # is claimed is that the partition is reported and non-trivial: the
    # list reaches past the catalog (there really are operator-only
    # names), and it overlaps it (there really are catalog routes to
    # protect).  Either half collapsing to empty means the operator list
    # and the catalog have drifted apart, not that the run succeeded.
    assert missing, (
        "the operator list has no names outside the catalog — the "
        "operator-only routes the list exists to carry are gone"
    )
    assert catalog_side, (
        "no operator name is a catalog entry — the carve-out has nothing "
        "to protect and the run's coverage claim is empty"
    )
    assert missing | catalog_side == set(operator_names), (
        "every operator name is either a catalog entry or a missing one"
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


def _expected_report_numbers(catalog_before: dict, confirmations: dict,
                             operator_names: set) -> dict:
    """The five numbers, derived from the run's inputs — not typed in.

    Every number here is *defined* by the inputs: the total is the size
    of the catalog, the refreshed are the entries the collection
    confirmed (minus the FAI-247-C ones the rule does not write), and
    the unconfirmed are everyone else — retired plus spared.  Deriving
    them means the test states the rule's contract (``refreshed`` +
    ``without_confirmation`` + ``foreign`` == total) rather than echoing
    a constant a future edit to the inputs would leave stale.
    """
    names = set(catalog_before["providers"])
    foreign = names & EXPECTED_FOREIGN
    owned = names - foreign
    confirmed = set(confirmations["confirmations"]) & owned
    unconfirmed = owned - confirmed
    retired = unconfirmed - set(operator_names)
    spared = unconfirmed & set(operator_names)
    return {
        "total_entries": len(names),
        "without_confirmation": len(unconfirmed),
        "retired_count": len(retired),
        "spared_count": len(spared),
        "refreshed_count": len(confirmed),
    }

#: Every category the report partitions entries into.  ``revived`` is not
#: one of them: a revived entry was confirmed and counts as refreshed,
#: and the two lists are disjoint by construction.  ``foreign`` is not a
#: bucket of the *rule* either — it is the set the rule leaves alone — but
#: it is a disjoint category, and it is the one that makes the partition
#: cover the whole catalog.
PARTITION_BUCKETS = ("retired", "spared", "refreshed", "tracked")


def _names(bucket) -> set:
    return {
        item["name"] if isinstance(item, dict) else item
        for item in bucket
    }


def _partition_names(report: dict) -> dict[str, set]:
    """Every category the report divides entries into.

    The four rule buckets plus ``foreign`` — the entries the rule saw and
    deliberately did not write.  Together they are the catalog.
    """
    names = {bucket: _names(report.get(bucket, []))
             for bucket in PARTITION_BUCKETS}
    names["foreign"] = set(report.get("foreign", []))
    return names


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


def test_retire_report_numbers_are_the_real_ones(
    retire_report, catalog_before, confirmations, operator_names,
):
    """The five numbers are the run's actual outcome, not placeholders.

    The expectation is derived from the run's own inputs, so this checks
    the rule against its contract rather than against a number copied
    into the test — a hardcoded ``53`` would pass while the collection
    it claims to describe moved underneath it.
    """
    expected = _expected_report_numbers(
        catalog_before, confirmations, operator_names,
    )
    got = {k: retire_report[k] for k in REPORT_COUNT_KEYS}
    assert got == expected, (
        f"the reported numbers are {got}; the run's inputs imply {expected}"
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

    # The rule iterates every catalog entry it *owns* and counts a
    # confirmed one as refreshed (or revived).  The FAI-247-C entries are
    # confirmed by the collection too, but the rule does not write them —
    # they are the ``foreign`` set.  So the two buckets together must equal
    # exactly the confirmed names that are catalog entries *and* not
    # foreign: every such entry is refreshed, none is skipped.
    confirmed_in_catalog = {
        name for name in confirmations["confirmations"]
        if name in catalog["providers"]
        and name not in retire_report["foreign"]
    }
    assert retire_report["refreshed_count"] + retire_report["revived_count"] == (
        len(confirmed_in_catalog)
    ), (
        "every confirmed entry the rule owns is refreshed or revived: "
        f"refreshed={retire_report['refreshed_count']} + "
        f"revived={retire_report['revived_count']} must equal the "
        f"{len(confirmed_in_catalog)} entries the collection confirmed "
        f"(the foreign set {sorted(retire_report['foreign'])} is excluded)"
    )


def test_report_categories_partition_all_66_entries(retire_report):
    """The categories must add up to the total — no entry slips through.

    Confirmed entries fall out as *refreshed* (or *revived*, which is
    counted under refreshed); unconfirmed ones are retired, spared or
    still tracked.  All four buckets plus the foreign set together are
    the whole catalog.
    """
    total = retire_report["total_entries"]
    bucketed = (
        retire_report["retired_count"]
        + retire_report["spared_count"]
        + retire_report["refreshed_count"]
        + retire_report.get("tracked_count", 0)
        + retire_report["foreign_count"]
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


def test_every_entry_lands_in_exactly_one_category(retire_report, catalog_before):
    """Exhaustivity: every entry is in exactly ONE category.

    ``<= 1`` would let an entry fall through every bucket — the counts
    can still add up while a name sits nowhere.  So this asserts
    ``== 1`` per entry: covered by exactly one category, and by no two.
    ``foreign`` is a category here: it is where the rule's untouched
    entries live, and leaving it out would let all six of them vanish
    while the four rule buckets still summed to 60.
    """
    buckets = _partition_names(retire_report)
    entries = set(catalog_before["providers"])

    for name in sorted(entries):
        hits = [b for b in buckets if name in buckets[b]]
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

    No entry the rule owns is skipped: the run is a single pass over the
    whole catalog, so an entry the collection confirmed — whichever
    folder owns it — must come out refreshed with no misses.  The
    FAI-247-C entries are the deliberate exception: the rule does not
    write them, so a confirmation there leaves no block behind.
    """
    for name in confirmations["confirmations"]:
        if name not in catalog["providers"]:
            continue
        if name in EXPECTED_FOREIGN:
            assert "retirement" not in catalog["providers"][name], (
                f"{name}: foreign entry was written by this lane"
            )
            continue
        ret = catalog["providers"][name].get("retirement", {})
        assert ret.get("misses", -1) == 0, (
            f"{name}: confirmed by the collection but misses={ret.get('misses')}"
        )


# ── AC4: unknowns are named, not guessed ────────────────────────────────

def test_deprecated_entries_state_the_absence_of_a_source(retire_report):
    """A retired entry says plainly that no source ever confirmed it.

    Every entry this run retired was confirmed by no source, ever: its
    ``last_confirming_source`` is ``None`` — the absence, reported as
    such.  What it must never do is dress that absence up as the sentinel
    ``"unknown on unknown"``, a made-up source name and date that read as
    a fact.  The two assertions below pin both halves: the value is
    ``None``, and it is not the sentinel *or any string* — a fake source
    name is rejected even if it does not contain the word "unknown".
    """
    for item in retire_report["retired"]:
        last_src = item["last_confirming_source"]
        assert last_src is None, (
            f"{item['name']}: last_confirming_source={last_src!r} — no source "
            f"ever confirmed this entry, and the report must say so as "
            f"``None``, never as a fabricated source name or the sentinel "
            f"``unknown on unknown``"
        )
        assert item["name"] in EXPECTED_RETIRED, (
            f"{item['name']}: no confirming source recorded, but it is "
            f"not one of the never-confirmed entries {sorted(EXPECTED_RETIRED)}"
        )


def test_last_confirming_source_is_not_always_null():
    """Positive control: the field *can* carry a real source and date.

    The test above asserts every retired entry has ``None``; on its own
    that would also pass if the rule hardcoded ``None`` for every entry,
    proof of nothing.  This drives the same rule to the other branch — an
    entry that *was* confirmed in an earlier round and has since gone
    unconfirmed past the threshold — and asserts the report names the
    source it last saw.  Only together do the two tests show the null in
    the run's report is a real absence and not a constant.
    """
    import copy

    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from retire import apply_retirement  # noqa: PLC0415

    catalog = _pre_rule_catalog()
    name = sorted(EXPECTED_RETIRED)[0]
    # A retirement already on the record: the entry was confirmed by
    # ``openrouter`` in an earlier round, then stopped being confirmed.
    catalog["providers"][name]["retirement"] = {
        "misses": 0,
        "last_confirmed_by": "openrouter",
        "last_confirmed_at": "2026-09-01",
    }
    # This round: that entry gets no confirmation at all, everyone else
    # still does — so it crosses the threshold alone.
    confirmations = json.loads(CONFIRMATIONS_PATH.read_text())
    confirmations["confirmations"].pop(name, None)

    _, report = apply_retirement(
        copy.deepcopy(catalog), confirmations,
        set(json.loads(OPERATOR_PATH.read_text())), 1,
    )
    retired = {r["name"]: r for r in report["retired"]}
    assert name in retired, (
        f"{name}: expected the entry to be retired once it lost its source"
    )
    assert retired[name]["last_confirming_source"] == "openrouter on 2026-09-01", (
        f"{name}: the report must name the source that last confirmed it; "
        f"got {retired[name]['last_confirming_source']!r}"
    )


def test_confirmed_entries_have_real_last_confirmed_by(
    catalog, confirmations,
):
    """Entries the collection confirms name a real source — never 'unknown'."""
    for name in confirmations["confirmations"]:
        if name not in catalog["providers"]:
            continue
        if name in EXPECTED_FOREIGN:
            # The rule does not write these, so there is no source to name.
            assert "retirement" not in catalog["providers"][name], (
                f"{name}: foreign entry was written by this lane"
            )
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
# The rule raises ``OperatorListEmptyError``; the CLI turns that into a
# non-zero exit, and the direct-call contract is asserted too.
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


def _run_rule(rule_path: Path, operator_path: Path,
              catalog_path: Path = CATALOG_PATH,
              confirmations_path: Path = CONFIRMATIONS_PATH,
              ) -> subprocess.CompletedProcess:
    """Invoke *rule_path* the way this lane invokes the retirement rule.

    Defaults to the checked-in catalog and the real confirmations; the
    guard tests pass a scratch catalog/confirmations so the run reaches
    *some* ordinary unconfirmed entry (see ``_pre_rule_run_inputs``).
    """
    return subprocess.run(
        [
            sys.executable, str(rule_path),
            "--catalog", str(catalog_path),
            "--confirmations", str(confirmations_path),
            "--operator", str(operator_path),
            "--threshold", "1",
        ],
        cwd=ROOT, capture_output=True, text=True,
    )


def _retired_names(report: dict) -> set:
    return {r["name"] for r in report.get("retired", [])}


def test_empty_operator_list_makes_retire_fail(
    operator_names, catalog_before, pre_rule_confirmations,
):
    """Riegel: a missing operator list must make the run fail, not empty out.

    ``apply_retirement`` with an *empty* set of operator names would
    treat every operator-configured entry as unconfirmed and retire it.
    The run must refuse the empty list instead — succeeding here is the
    exact failure this test exists to catch.

    Refusal has to hold on *both* ways in: the CLI exits non-zero, and a
    direct ``apply_retirement(catalog, confs, set(), …)`` call raises
    rather than returning a report a caller could read as success.
    """
    assert operator_names, "precondition: the real operator list is not empty"

    with tempfile.TemporaryDirectory() as tmp:
        empty_path = Path(tmp) / "empty-operator.json"
        empty_path.write_text("[]")
        catalog_path = Path(tmp) / "catalog.json"
        conf_path = Path(tmp) / "conf.json"
        catalog_path.write_text(json.dumps(catalog_before))
        conf_path.write_text(json.dumps(pre_rule_confirmations))

        refused = _run_rule(RETIRE_SCRIPT, empty_path, catalog_path, conf_path)
        accepted = _run_rule(
            _rule_without_operator_guard(RETIRE_SCRIPT, Path(tmp)),
            empty_path, catalog_path, conf_path,
        )

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

    # The direct-call contract: a caller that bypasses ``main()`` must not
    # be able to treat the refusal as a successful (empty) run either.
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from retire import OperatorListEmptyError, apply_retirement  # noqa: PLC0415

    with pytest.raises(OperatorListEmptyError):
        apply_retirement(
            json.loads(json.dumps(catalog_before)),
            pre_rule_confirmations,
            set(),
            1,
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


def test_control_without_the_guard_empties_the_operator_routes(
    operator_names, catalog_before, pre_rule_confirmations,
):
    """Control: with the operator guard deleted, the operator routes fall.

    Proves the guard is load-bearing by *differencing* the two rules on
    the input the guard exists for — an EMPTY operator list, on a catalog
    with entries no source confirmed.  Only then does deleting the guard
    change the outcome:

    * the guarded rule refuses (non-zero exit, nothing retired);
    * the unguarded copy accepts and retires those entries, because with
      no operator names every unconfirmed route counts as a miss.

    A *present* operator list never reaches the guard, so comparing the
    two rules with one is a tautology that also holds at the lane base.
    This test therefore uses the empty list and asserts the outcomes
    differ; on ``5332e90`` (no guard) both copies accept, the outcomes
    are identical, and this assertion fails.
    """
    assert operator_names, "precondition: the real operator list is not empty"

    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty-operator.json"
        empty.write_text("[]")
        catalog_path = Path(tmp) / "catalog.json"
        conf_path = Path(tmp) / "conf.json"
        catalog_path.write_text(json.dumps(catalog_before))
        conf_path.write_text(json.dumps(pre_rule_confirmations))
        unguarded = _rule_without_operator_guard(RETIRE_SCRIPT, Path(tmp))
        guarded = _run_rule(RETIRE_SCRIPT, empty, catalog_path, conf_path)
        control = _run_rule(unguarded, empty, catalog_path, conf_path)

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


# ── FAI-247-C: the entries this rule does not own ───────────────────────
#
# FAI-247-C merges the ``byteplus``, ``deepseek``, ``mistral`` and
# ``openrouter-fallback`` folders separately.  A folder *name* is the
# wrong key for that: ``deepseek`` is not a catalog entry at all — the
# entries are ``deepseek-v4-flash`` and friends — and the lane also owns
# the ``deepseek-flash-vision-exp`` entry that lives in another folder.
# The set that matters is the one the rule itself carves out, named by
# *entry*: ``EXPECTED_FOREIGN``.  These tests prove the carve-out holds
# against the artifact, and check it against the rule's own declaration
# so the two cannot drift.


def test_foreign_entries_are_untouched_in_the_catalog(catalog):
    """The six FAI-247-C entries carry nothing this lane wrote.

    A ``retirement`` block or a ``deprecated`` status on one of them
    would be reverted — or resurrected — by that lane's next merge,
    because the folder it owns is the source this catalog is rebuilt
    from.  The rule sees them (they are in ``total_entries``) and writes
    none of them.
    """
    for source in sorted(EXPECTED_FOREIGN):
        assert source in catalog["providers"], (
            f"{source}: expected an FAI-247-C entry in the catalog — the "
            f"carve-out set no longer names the entries it protects"
        )
        entry = catalog["providers"][source]
        assert "retirement" not in entry, (
            f"{source}: FAI-247-C entry carries a retirement block this "
            f"lane wrote — the next merge would revert it"
        )
        assert entry.get("tier_status") != "deprecated", (
            f"{source}: FAI-247-C entry was deprecated by this lane"
        )


def test_rule_and_folders_agree_on_the_foreign_set():
    """The rule's declared carve-out is exactly the set the tests guard.

    ``FOREIGN_ENTRIES`` in ``retire.py`` is the one place the carve-out
    is declared; ``EXPECTED_FOREIGN`` here is the one place it is
    asserted.  If they drift, one of the two is wrong and the cleanup
    silently starts writing an entry another lane owns.
    """
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from retire import FOREIGN_ENTRIES  # noqa: PLC0415

    assert set(FOREIGN_ENTRIES) == set(EXPECTED_FOREIGN), (
        f"retire.py carves out {sorted(FOREIGN_ENTRIES)} but the tests "
        f"guard {sorted(EXPECTED_FOREIGN)} — the definition and the proof "
        f"have drifted"
    )


# ── Invariant: rebuild is stable ────────────────────────────────────────

def test_catalog_rebuild_is_stable():
    """Rebuilding the catalog twice produces the same bytes, unchanged.

    Stability alone is weak — a rebuild could stably produce something
    other than the checked-in catalog.  So this asserts both: the rebuild
    is a fixed point *and* its first output is byte-identical to the file
    already on disk.  That is what makes the weekly refresh a no-op.
    """
    before = CATALOG_PATH.read_text()

    subprocess.run(
        [sys.executable, str(BUILD_SCRIPT)],
        cwd=ROOT, capture_output=True, check=True,
    )
    first = CATALOG_PATH.read_text()
    assert first == before, (
        "rebuilding from the folders changed the checked-in catalog — the "
        "folders and the catalog disagree"
    )

    subprocess.run(
        [sys.executable, str(BUILD_SCRIPT)],
        cwd=ROOT, capture_output=True, check=True,
    )
    second = CATALOG_PATH.read_text()
    assert second == first, (
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
#   * RED PROOF 1 — none of the 66 entries carries a ``retirement``
#     block: the checked-in catalog at that commit predates the run, so
#     the assertion "the run accounted for every entry the rule owns"
#     fails on the real catalog with the real inputs next to it.
#   * RED PROOF 2 — the run reports three counts, not the five the
#     criterion asks for; ``without_confirmation`` and ``foreign_count``
#     are absent from the base report.
#   * RED PROOF 3 — an empty operator list is accepted (exit 0) and
#     retires what it should have spared, because the guard that makes
#     it fatal does not exist yet.
#
# All three pass on this branch.  The first is the load-bearing one: it
# asserts the observable result of the cleanup run, not a detail of the
# rule.
#
# The whole file at ``5332e90``: ``30 failed, 9 passed``, and the
# failures are ``51 AssertionError`` / ``11 KeyError`` — the keys the
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
        if "retirement" not in entry
    )
    assert missing == sorted(EXPECTED_FOREIGN), (
        f"RED PROOF: {len(missing)} of {len(providers)} entries carry no "
        f"retirement block — expected only the foreign set "
        f"{sorted(EXPECTED_FOREIGN)}; every entry the rule owns must carry "
        f"the block this run gives it: {missing}"
    )

    # The retired seven are the entries this run exists for: the base
    # catalog has them, unmarked.  Asserting the exact set keeps the
    # proof aimed at the run's outcome, not at a lone key.
    deprecated = sorted(
        name for name, entry in providers.items()
        if entry.get("tier_status") == "deprecated"
    )
    assert deprecated == sorted(EXPECTED_RETIRED), (
        f"RED PROOF: the run retires exactly {sorted(EXPECTED_RETIRED)}; "
        f"the base catalog has {deprecated}"
    )


def test_red_proof_the_report_states_the_five_numbers(
    catalog_before, confirmations, operator_names,
):
    """RED PROOF 2: at 5332e90 the report has three numbers, not five.

    The base report counts the entries it saw and lists what it retired
    and spared; it does not state how many entries went without a
    confirmation (7) or how many were refreshed (53).  On ``5332e90``
    the assertion below fails with ``AssertionError``: the keys are
    absent, not merely wrong.
    """
    report = _run_retirement_report(
        json.loads(json.dumps(catalog_before)),
        confirmations,
        operator_names,
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
    assert report["refreshed_count"] == 53, (
        "RED PROOF: 53 entries the rule owns were confirmed and refreshed"
    )
    assert report["foreign_count"] == 6, (
        "RED PROOF: 6 entries belong to FAI-247-C and are left to that lane"
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
