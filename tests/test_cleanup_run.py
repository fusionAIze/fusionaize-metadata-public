"""Acceptance tests for the FAI-247-F cleanup run.

Verifies that the retirement rule produces the correct outcome with
real operator and collection inputs.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RETIRE_SCRIPT = ROOT / "scripts" / "retire.py"
BUILD_SCRIPT = ROOT / "scripts" / "build-catalog.py"
CATALOG_PATH = ROOT / "providers" / "catalog.v1.json"
CONFIRMATIONS_PATH = Path("/tmp/confirmations.json")
OPERATOR_PATH = Path("/tmp/operator-list.json")


# ── test fixtures (real data snapshot) ──────────────────────────────────

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
    """Re-run retire.py and return the parsed report (does not write)."""
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


# ── AC1: diff is produced by retire.py, not hand edits ──────────────────

def test_catalog_has_retirement_blocks(catalog):
    """Every deprecated entry carries a retirement block with a reason."""
    for name, entry in catalog["providers"].items():
        if entry.get("tier_status") == "deprecated":
            assert "retirement" in entry, (
                f"{name} is deprecated but has no retirement block"
            )
            assert "reason" in entry["retirement"], (
                f"{name} retirement block has no reason"
            )
            assert "unconfirmed" in entry["retirement"]["reason"], (
                f"{name} retirement reason doesn't mention unconfirmed"
            )


def test_deprecated_entries_have_retired_at(catalog):
    """Every deprecated entry has a retired_at timestamp."""
    for name, entry in catalog["providers"].items():
        if entry.get("tier_status") == "deprecated":
            assert "retired_at" in entry.get("retirement", {}), (
                f"{name} deprecated but missing retired_at"
            )


def test_folder_indexes_agree_with_catalog(catalog):
    """Every provider folder _original block matches the catalog entry."""
    providers_dir = ROOT / "providers"
    for folder in sorted(providers_dir.iterdir()):
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
            # tier_status must match
            cat_ts = cat_entry.get("tier_status")
            orig_ts = orig.get("tier_status")
            if cat_ts and cat_ts != orig_ts:
                # Only assert when catalog has an explicit value
                assert cat_ts == orig_ts, (
                    f"{source_name}: catalog tier_status={cat_ts} "
                    f"but folder _original has tier_status={orig_ts}"
                )
            # retirement must match
            cat_ret = cat_entry.get("retirement")
            orig_ret = orig.get("retirement")
            if cat_ret and cat_ret != orig_ret:
                assert cat_ret == orig_ret, (
                    f"{source_name}: retirement mismatch "
                    f"catalog={cat_ret} vs folder={orig_ret}"
                )


# ── AC2: no operator-configured entry is missing or deprecated ──────────

OPERATOR_NAMES_IN_CATALOG = frozenset({
    "anthropic-claude", "anthropic-haiku", "anthropic-sonnet",
    "blackbox-free", "deepseek-v4-flash", "deepseek-v4-pro",
    "gemini-flash", "gemini-flash-lite", "kilo-opus", "kilo-sonnet",
    "kilocode", "openai-codex", "openai-gpt4o", "openai-images",
    "openrouter-fallback",
})


def test_operator_list_is_not_empty(operator_names):
    """Red-proof: an empty operator list means the carve-out is broken."""
    assert len(operator_names) > 0, (
        "Riegel: operator list is empty — the carve-out would not fire"
    )


def test_known_operator_names_exist_in_catalog(catalog):
    """Specific operator-configured entries must be present."""
    for name in OPERATOR_NAMES_IN_CATALOG:
        assert name in catalog["providers"], (
            f"Riegel: operator-configured '{name}' is missing from catalog"
        )


def test_no_operator_entry_is_deprecated(catalog):
    """Operator-configured entries must never be deprecated."""
    for name in OPERATOR_NAMES_IN_CATALOG:
        entry = catalog["providers"].get(name, {})
        ts = entry.get("tier_status")
        assert ts != "deprecated", (
            f"Riegel: operator-configured '{name}' is deprecated "
            f"(tier_status={ts}) — the carve-out failed"
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
    assert ret.get("last_confirmed_by", "").lower() != "unknown", (
        f"kilocode last_confirmed_by={ret.get('last_confirmed_by')} "
        f"— must be a real source, not 'unknown'"
    )


def test_gemini_flash_not_deprecated(catalog):
    """gemini-flash is operator-configured — must not be deprecated."""
    entry = catalog["providers"]["gemini-flash"]
    assert entry.get("tier_status") != "deprecated"


def test_openai_codex_not_deprecated(catalog):
    """openai-codex is operator-configured — must not be deprecated."""
    entry = catalog["providers"]["openai-codex"]
    assert entry.get("tier_status") != "deprecated"


# ── AC3: before/after counts in result ──────────────────────────────────

EXPECTED_RETIRED = frozenset({
    "clawrouter", "kilo-auto-balanced", "kilo-auto-free",
    "kilo-auto-frontier", "lmstudio", "longcat", "vllm",
})


def test_retire_report_has_expected_counts(retire_report):
    """The retire report carries before/after counts."""
    assert retire_report["total_entries"] == 66, (
        f"expected 66 total entries, got {retire_report['total_entries']}"
    )
    assert retire_report["threshold"] == 1
    assert len(retire_report["sources"]) == 3


def test_exactly_seven_retired(retire_report):
    """7 entries should be retired — the unconfirmed non-operator ones."""
    retired_names = {r["name"] for r in retire_report["retired"]}
    assert retired_names == EXPECTED_RETIRED, (
        f"expected retired={sorted(EXPECTED_RETIRED)}, "
        f"got={sorted(retired_names)}"
    )


def test_zero_spared(retire_report):
    """All operator entries were confirmed, so none needed sparing."""
    assert len(retire_report["spared"]) == 0, (
        f"expected 0 spared, got {retire_report['spared']}"
    )


def test_all_confirmed_entries_refreshed(retire_report, catalog):
    """Every entry in the confirmations has misses=0 after the run.

    FAI-247-C folders are excluded — they were reverted after sync
    and their catalog entries don't carry retirement blocks.
    """
    conf = json.loads(CONFIRMATIONS_PATH.read_text())
    for name in conf["confirmations"]:
        if name in FAI247C_SOURCES:
            continue
        entry = catalog["providers"].get(name)
        if entry is None:
            continue
        ret = entry.get("retirement", {})
        assert ret.get("misses", -1) == 0, (
            f"{name}: expected misses=0, got {ret.get('misses')}"
        )


# ── AC4: unknowns are named, not guessed ────────────────────────────────

def test_deprecated_entries_have_named_last_confirming_source(retire_report):
    """Deprecated entries explicitly name their last source — no guessing."""
    for item in retire_report["retired"]:
        name = item["name"]
        last_src = item["last_confirming_source"]
        # "unknown on unknown" is acceptable when the entry was never
        # confirmed by any source (genuinely unknown).
        if "unknown" in last_src.lower():
            # Verify this is one of the genuinely never-confirmed entries
            assert name in EXPECTED_RETIRED, (
                f"{name}: last_confirming_source={last_src} "
                f"but entry is not in the expected unconfirmed set"
            )


def test_confirmed_entries_have_real_last_confirmed_by(catalog):
    """Entries in the confirmations must name a real source.

    FAI-247-C folders are excluded — they were reverted after sync.
    """
    conf = json.loads(CONFIRMATIONS_PATH.read_text())
    for name in conf["confirmations"]:
        if name in FAI247C_SOURCES:
            continue
        entry = catalog["providers"].get(name)
        if entry is None:
            continue
        ret = entry.get("retirement", {})
        lcb = ret.get("last_confirmed_by", "")
        assert lcb and lcb.lower() != "unknown", (
            f"{name}: last_confirmed_by={lcb} — must name a real source"
        )


# ── FAI-247-C exclusion ─────────────────────────────────────────────────

FAI247C_SOURCES = frozenset({
    "byteplus", "deepseek", "deepseek-v4-flash", "deepseek-v4-pro",
    "deepseek-flash-vision-exp", "openrouter-fallback", "mistral",
})


def test_fai247c_folders_are_unchanged():
    """FAI-247-C folders must not have been touched by this run."""
    for source in FAI247C_SOURCES:
        idx_path = ROOT / "providers" / source / "index.json"
        if not idx_path.exists():
            continue
        provider = json.loads(idx_path.read_text())
        for model in provider.get("models", []):
            orig = model.get("_original", {})
            assert "retirement" not in orig, (
                f"{source}/{model['_source']}: _original has retirement — "
                f"FAI-247-C folder was touched"
            )
            assert "tier_status" not in orig or orig["tier_status"] in (
                None, "active", "preview", "expired"
            ), (
                f"{source}/{model['_source']}: unexpected tier_status={orig.get('tier_status')}"
            )


# ── Invariant: catalog rebuild is stable ────────────────────────────────

def test_catalog_rebuild_is_stable():
    """Rebuilding the catalog a second time produces zero diff."""
    # Run build once to get baseline
    subprocess.run(
        [sys.executable, str(BUILD_SCRIPT)],
        cwd=ROOT, capture_output=True, check=True,
    )
    first = CATALOG_PATH.read_text()

    # Run build again
    subprocess.run(
        [sys.executable, str(BUILD_SCRIPT)],
        cwd=ROOT, capture_output=True, check=True,
    )
    second = CATALOG_PATH.read_text()

    assert first == second, (
        "Riegel: catalog rebuild is not stable — second run produced a diff"
    )
