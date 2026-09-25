"""Tests for scripts/build-catalog.py: generating catalog.v1.json from folders.

The invariant: running ``scripts/build-catalog.py`` without changing any
provider folder must produce zero diff against the checked-in catalog.

This is the reverse direction of the FAI-245-A tests, which proved the
folders preserve every field of every old entry.  Here we prove the
build script reconstructs the old catalog *exactly* from the folders.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_DIR = ROOT / "providers"
CATALOG_PATH = PROVIDERS_DIR / "catalog.v1.json"
SCRIPT_PATH = ROOT / "scripts" / "build-catalog.py"

BASE_SHA = "d19d4cf"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_show(ref: str, path: str) -> bytes:
    """Run ``git show <ref>:<path>`` and return stdout."""
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout


# ---------------------------------------------------------------------------
# RED PROOF
# ---------------------------------------------------------------------------


def test_red_proof_catalog_changed_since_base():
    """RED PROOF: the checked-in catalog differs from the base catalog.

    At base commit *BASE_SHA* the catalog was hand-maintained and matched
    the folder structure of that time.  On this branch the build script
    regenerated it, so the checked-in version must differ from base.

    If this test is run with ``HEAD == BASE_SHA`` (i.e. against the base
    commit), both ``git show`` calls return the same content and the
    assertion fails — proving the test is not vacuous.

    Riegel: if the two are identical, the catalog was never regenerated.
    """
    base_catalog = _git_show(BASE_SHA, "providers/catalog.v1.json")
    head_catalog = _git_show("HEAD", "providers/catalog.v1.json")

    assert base_catalog != head_catalog, (
        f"Riegel: HEAD catalog is byte-identical to base ({BASE_SHA}). "
        "The build script did not produce a change — either the script "
        "is a no-op or the catalog was not regenerated."
    )


# ---------------------------------------------------------------------------
# Build script round-trip
# ---------------------------------------------------------------------------


def test_build_script_runs():
    """The build script runs without error and produces valid JSON.

    Riegel: if the script is missing or fails, this catches it.
    """
    assert SCRIPT_PATH.exists(), (
        f"Riegel: {SCRIPT_PATH} does not exist"
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"Riegel: build script failed (exit {result.returncode}):\n"
        f"{result.stderr}"
    )

    # The file must be valid JSON
    catalog = json.loads(CATALOG_PATH.read_text())
    assert "providers" in catalog, (
        "Riegel: generated catalog has no 'providers' key"
    )
    assert len(catalog["providers"]) > 0, (
        "Riegel: generated catalog has no providers"
    )


def test_build_output_matches_checked_in():
    """The generated catalog is byte-identical to what is checked in.

    Running the build script reads every provider folder and writes
    catalog.v1.json.  The checked-in version at HEAD must match
    exactly — otherwise someone edited the catalog by hand.

    Riegel: if the catalogs differ, list the first few diverging keys.
    """
    # Re-run to ensure we compare fresh output
    subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=ROOT, capture_output=True, check=True,
    )

    generated = json.loads(CATALOG_PATH.read_text())
    checked_in = json.loads(_git_show("HEAD", "providers/catalog.v1.json"))

    # Compare
    assert generated.keys() == checked_in.keys(), (
        "Riegel: top-level keys differ: "
        f"generated={sorted(generated.keys())} "
        f"checked_in={sorted(checked_in.keys())}"
    )

    assert generated["providers"].keys() == checked_in["providers"].keys(), (
        "Riegel: provider entry keys differ"
    )

    # Deep compare
    assert generated == checked_in, (
        "Riegel: generated catalog differs from checked-in catalog. "
        "The provider folders have drifted from the committed catalog."
    )


def test_generated_catalog_is_idempotent():
    """Running the build script twice produces the same output.

    Riegel: if the output changes between runs, the script has a
    non-determinism bug (e.g. timestamp or ordering).
    """
    # Run once
    subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=ROOT, capture_output=True, check=True,
    )
    first = CATALOG_PATH.read_bytes()

    # Run twice
    subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=ROOT, capture_output=True, check=True,
    )
    second = CATALOG_PATH.read_bytes()

    assert first == second, (
        "Riegel: second build differs from first — script is not idempotent"
    )


# ---------------------------------------------------------------------------
# Structural integrity
# ---------------------------------------------------------------------------


def test_generated_catalog_has_66_entries():
    """The generated catalog has exactly 66 entries, matching the old count.

    Riegel: if the count is zero, the script produced an empty catalog.
    """
    subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=ROOT, capture_output=True, check=True,
    )
    catalog = json.loads(CATALOG_PATH.read_text())
    n = len(catalog["providers"])
    assert n > 0, "Riegel: generated catalog has 0 provider entries"
    assert n == 66, f"Expected 66 provider entries, got {n}"


def test_every_entry_has_vendor_and_model():
    """Every generated entry has 'vendor' and 'model' fields.

    Riegel: if any entry lacks these, the reconstruction is broken.
    """
    subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=ROOT, capture_output=True, check=True,
    )
    catalog = json.loads(CATALOG_PATH.read_text())
    violations = []
    for name, entry in catalog["providers"].items():
        if "vendor" not in entry:
            violations.append(f"{name}: missing vendor")
        if "model" not in entry:
            violations.append(f"{name}: missing model")
    assert not violations, (
        "Riegel: entries with missing fields:\n  " + "\n  ".join(violations)
    )


if __name__ == "__main__":
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
        except Exception as exc:
            failures.append(name)
            print(f"FAIL {name} ({type(exc).__name__}): {exc}")
    if failures:
        print(f"\n{failures} failure(s)")
        sys.exit(1)
    print(f"\n{len(tests)} tests passed")
