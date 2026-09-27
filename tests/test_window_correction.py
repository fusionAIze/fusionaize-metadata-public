"""Tests for FAI-247-C: window correction from probe data.

The faigate probe-window (2026-09-26) measured the actual API ceiling for
every provider.  Five entries contradicted their catalog value — the probe
wins.  Each correction records the superseded value, the probe source, and
the probe date in ``context_evidence``.

Affected entries (probe → catalog):
  byteplus/seed-1-8-251228:       245760 → 131072
  deepseek/deepseek-v4-flash:    1048576 →  65536
  deepseek/deepseek-v4-pro:      1048576 →  65536
  openrouter-fallback/auto-router: 128000 → 200000
  mistral/mistral-large-latest:    128000 → 131072

All fixtures are recorded collection results — no network access in tests.

Run with ``python3 -m pytest -q tests/test_window_correction.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Known contradictions: catalog → probe
# ---------------------------------------------------------------------------

# Each entry: (provider_id, model__source, catalog_value, probe_value)
_CONTRADICTIONS: list[tuple[str, str, int, int]] = [
    ("byteplus",              "byteplus",              245760, 131072),
    ("deepseek",              "deepseek-v4-flash",    1048576,  65536),
    ("deepseek",              "deepseek-v4-pro",      1048576,  65536),
    ("openrouter-fallback",   "openrouter-fallback",   128000, 200000),
    ("mistral",               "mistral",               128000, 131072),
]

# Entries whose probe matches the catalog and that must NOT be touched.
# deepseek-flash-vision-exp has contextLength=1000000 in the catalog and
# its probe recorded context_window_min_measured=200091 (a lower bound,
# not a contradiction).  It must remain unchanged.
_UNTOUCHED_ENTRIES: list[tuple[str, str, int]] = [
    ("deepseek", "deepseek-flash-vision-exp", 1000000),
]


def _load_index(provider_id: str) -> dict:
    return json.loads(
        (ROOT / "providers" / provider_id / "index.json").read_text()
    )


def _model_by_source(provider: dict, source: str) -> dict | None:
    for m in provider.get("models", []):
        if m.get("_source") == source:
            return m
    return None


# ---------------------------------------------------------------------------
# Criterion 1 — Five windows carry the probe value, with superseded_value,
# source, and timestamp in context_evidence
# ---------------------------------------------------------------------------


def test_five_contradicted_windows_carry_probe_value():
    """Every contradicted entry carries the measured probe value as
    ``contextLength`` (not the catalog value)."""
    for prov_id, src_name, catalog_val, probe_val in _CONTRADICTIONS:
        provider = _load_index(prov_id)
        model = _model_by_source(provider, src_name)
        assert model is not None, (
            f"{prov_id}/{src_name}: model entry not found in index.json"
        )
        assert model["contextLength"] == probe_val, (
            f"{prov_id}/{src_name}: expected contextLength={probe_val} "
            f"(probe value), got {model['contextLength']}"
        )


def test_each_correction_records_superseded_value():
    """Every corrected entry carries ``superseded_value`` in
    ``context_evidence``, naming the previous catalog value."""
    for prov_id, src_name, catalog_val, probe_val in _CONTRADICTIONS:
        provider = _load_index(prov_id)
        model = _model_by_source(provider, src_name)
        evidence = model.get("_original", {}).get("context_evidence", {})
        assert evidence.get("superseded_value") == catalog_val, (
            f"{prov_id}/{src_name}: expected superseded_value={catalog_val}, "
            f"got {evidence.get('superseded_value')}"
        )


def test_each_correction_records_source():
    """Every corrected entry names the probe source in ``context_evidence``."""
    for prov_id, src_name, _catalog_val, _probe_val in _CONTRADICTIONS:
        provider = _load_index(prov_id)
        model = _model_by_source(provider, src_name)
        evidence = model.get("_original", {}).get("context_evidence", {})
        source = evidence.get("source", "")
        assert "faigate" in source.lower() or "probe" in source.lower(), (
            f"{prov_id}/{src_name}: context_evidence.source must name the "
            f"probe source, got {source!r}"
        )


def test_each_correction_records_timestamp():
    """Every corrected entry records the probe date in ``context_evidence``."""
    for prov_id, src_name, _catalog_val, _probe_val in _CONTRADICTIONS:
        provider = _load_index(prov_id)
        model = _model_by_source(provider, src_name)
        evidence = model.get("_original", {}).get("context_evidence", {})
        as_of = evidence.get("as_of", "")
        assert as_of == "2026-09-26", (
            f"{prov_id}/{src_name}: expected as_of='2026-09-26', got {as_of!r}"
        )


def test_each_correction_is_confirmed():
    """Every corrected entry has evidence level ``confirmed`` (probe is
    the provider's own response)."""
    for prov_id, src_name, _catalog_val, _probe_val in _CONTRADICTIONS:
        provider = _load_index(prov_id)
        model = _model_by_source(provider, src_name)
        evidence = model.get("_original", {}).get("context_evidence", {})
        assert evidence.get("level") == "confirmed", (
            f"{prov_id}/{src_name}: expected context_evidence.level="
            f"'confirmed', got {evidence.get('level')!r}"
        )


def test_limits_match_context_length():
    """Every corrected entry has ``limits.max_input_tokens`` matching the
    new contextLength."""
    for prov_id, src_name, _catalog_val, probe_val in _CONTRADICTIONS:
        provider = _load_index(prov_id)
        model = _model_by_source(provider, src_name)
        limits = model.get("_original", {}).get("limits", {})
        assert limits.get("max_input_tokens") == probe_val, (
            f"{prov_id}/{src_name}: expected limits.max_input_tokens="
            f"{probe_val}, got {limits.get('max_input_tokens')}"
        )


# ---------------------------------------------------------------------------
# Criterion 3 — A provider whose probe and catalog agree is left untouched
# ---------------------------------------------------------------------------


def test_matching_entries_are_untouched():
    """Entries whose probe matched the catalog value have their original
    contextLength preserved unchanged."""
    for prov_id, src_name, expected in _UNTOUCHED_ENTRIES:
        provider = _load_index(prov_id)
        model = _model_by_source(provider, src_name)
        assert model is not None, (
            f"{prov_id}/{src_name}: model entry not found in index.json"
        )
        assert model["contextLength"] == expected, (
            f"{prov_id}/{src_name}: expected contextLength={expected} "
            f"(unchanged), got {model['contextLength']} — "
            "a non-contradicted entry was modified"
        )


def test_matching_entry_preserves_original_evidence():
    """The untouched entry's context_evidence block is preserved, not
    overwritten with a probe correction."""
    prov_id, src_name, _expected = _UNTOUCHED_ENTRIES[0]
    provider = _load_index(prov_id)
    model = _model_by_source(provider, src_name)
    evidence = model.get("_original", {}).get("context_evidence", {})
    # deepseek-flash-vision-exp was confirmed by OmniRoute, not by a probe.
    assert evidence.get("level") != "confirmed" or evidence.get("source") != "faigate probe-window", (
        f"{prov_id}/{src_name}: the untouched entry must NOT carry "
        "a faigate probe-window correction"
    )
    # The original OmniRoute evidence must still be present.
    assert evidence.get("level") is not None, (
        f"{prov_id}/{src_name}: original context_evidence was stripped"
    )


# ---------------------------------------------------------------------------
# Criterion 4 — Correction delta measurement
# ---------------------------------------------------------------------------

# Expected deltas: (provider_id, src_name, superseded, probe, delta)
_EXPECTED_DELTAS: list[tuple[str, str, int, int, int]] = [
    ("byteplus",              "byteplus",              245760, 131072, -114688),
    ("deepseek",              "deepseek-v4-flash",    1048576,  65536, -983040),
    ("deepseek",              "deepseek-v4-pro",      1048576,  65536, -983040),
    ("openrouter-fallback",   "openrouter-fallback",   128000, 200000,   72000),
    ("mistral",               "mistral",               128000, 131072,    3072),
]


def test_each_correction_delta_is_recorded():
    """Every corrected entry has a measurable delta (probe − superseded).

    The delta documents the magnitude of the correction.  Negative means
    the probe was smaller than the catalog claim; positive means the probe
    discovered a larger window than documented.
    """
    for prov_id, src_name, catalog_val, probe_val, expected_delta in _EXPECTED_DELTAS:
        provider = _load_index(prov_id)
        model = _model_by_source(provider, src_name)
        assert model is not None, (
            f"{prov_id}/{src_name}: model not found"
        )
        evidence = model.get("_original", {}).get("context_evidence", {})
        superseded = evidence.get("superseded_value")
        assert superseded == catalog_val, (
            f"{prov_id}/{src_name}: expected superseded_value={catalog_val}, "
            f"got {superseded}"
        )
        current = model["contextLength"]
        assert current == probe_val, (
            f"{prov_id}/{src_name}: expected contextLength={probe_val}, "
            f"got {current}"
        )
        delta = current - superseded
        assert delta == expected_delta, (
            f"{prov_id}/{src_name}: expected delta={expected_delta} "
            f"(probe {probe_val} − catalog {catalog_val}), got {delta}"
        )


def test_correction_delta_distribution():
    """Report the distribution of correction deltas across all five entries.

    Three entries shrank (negative delta), two grew (positive delta).
    The largest absolute correction is 983040 tokens (DeepSeek V4 Flash/Pro).
    The smallest absolute correction is 3072 tokens (Mistral Large Latest).
    """
    deltas: dict[str, int] = {}
    for prov_id, src_name, catalog_val, probe_val, _expected_delta in _EXPECTED_DELTAS:
        provider = _load_index(prov_id)
        model = _model_by_source(provider, src_name)
        assert model is not None
        current = model["contextLength"]
        delta = current - catalog_val
        deltas[f"{prov_id}/{src_name}"] = delta

    # Distribution report as structured assertion.
    expected_deltas = {
        "byteplus/byteplus":                    -114688,
        "deepseek/deepseek-v4-flash":           -983040,
        "deepseek/deepseek-v4-pro":             -983040,
        "openrouter-fallback/openrouter-fallback": 72000,
        "mistral/mistral":                          3072,
    }
    assert deltas == expected_deltas, (
        f"Correction delta distribution changed: "
        f"expected {expected_deltas}, got {deltas}.  "
        f"If the change is intentional, update _EXPECTED_DELTAS."
    )

    # Summary invariants
    negative = sum(1 for d in deltas.values() if d < 0)
    positive = sum(1 for d in deltas.values() if d > 0)
    assert negative == 3, f"expected 3 negative deltas, got {negative}"
    assert positive == 2, f"expected 2 positive deltas, got {positive}"


# ---------------------------------------------------------------------------
# RED PROOF — against base commit 5332e90
# ---------------------------------------------------------------------------

def test_red_proof_deepseek_corrected_to_65536():
    """RED PROOF: deepseek-v4-flash carries 65536 (the probe value).

    At base commit 5332e90 the deepseek-v4-flash entry had
    contextLength=1048576.  This assertion proves the correction
    actually took effect: an assertion on the old value (1048576)
    would fail, proving the test is not vacuous.
    """
    provider = _load_index("deepseek")
    model = _model_by_source(provider, "deepseek-v4-flash")
    assert model is not None, (
        "RED PROOF: deepseek-v4-flash model not found — "
        "test would pass vacuously"
    )
    assert model["contextLength"] == 65536, (
        "RED PROOF: deepseek-v4-flash must be corrected to 65536. "
        "On base 5332e90 it was 1048576 — this assertion fails there, "
        "proving the test is not vacuous."
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
        except Exception as exc:
            failures.append(name)
            print(f"FAIL {name} ({type(exc).__name__}): {exc}")
    if failures:
        print(f"\n{failures} failure(s)")
        sys.exit(1)
    print(f"\n{len(tests)} tests passed")
