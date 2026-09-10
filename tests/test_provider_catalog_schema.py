"""Acceptance tests for provider-catalog.v1.schema.json.

v1.2 coverage (pre-existing):
  1. valid v1.2 document is accepted
  2. invalid modality is rejected
  3. evidence.level outside {belegt, plausibel, unbestaetigt} is rejected
  4. a v1.1 consumer reading a v1.2 catalog produces 0 errors

v1.3 coverage (TASK-C1..C4 — the ID path scheme):

  TASK-C1  modalities split into input_modalities/output_modalities
  TASK-C2  vendor/model/variant/hop as separate fields; canonical ID path
           generated from them (never parsed from a string); uniqueness;
           no segment repetition; alias ambiguity resolves via vendor/model
  TASK-C3  max_input_tokens / max_output_tokens require evidence
  TASK-C4  auto/ reserved namespace; intent-vs-model collision reported

Round 3 (review findings):
  MUSS-1   recommended_model is derived, must equal `model` (or be null)
  MUSS-2   auto/ prefix reserved in the SCHEMA (vendor + hop), not just in tests
  MUSS-3   hop has uniqueItems; repeated intermediary rejected
  MUSS-4   shrink-guard also compares aliases + carried fields per entry

Run with ``python3 -m pytest -q``.
"""

import json
import subprocess
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "provider-catalog.v1.schema.json"
CATALOG_PATH = ROOT / "providers" / "catalog.v1.json"

# Provider keys that may be dropped from the catalog without the shrink guard
# failing. Add a key here ONLY when the removal is a conscious decision, with
# the reason captured in the commit that edits this list.
ALLOWED_REMOVALS: set[str] = set()

ROUTING_MODES = [
    "auto/auto",
    "auto/coding-auto",
    "auto/coding-fast",
    "auto/coding-premium",
    "auto/eco",
    "auto/free",
    "auto/premium",
]


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


def _errors(doc: dict) -> list:
    validator = Draft202012Validator(_load_schema())
    return sorted(validator.iter_errors(doc), key=lambda e: str(e.path))


def _make_doc(version: str = "fusionaize-provider-catalog/v1.2") -> dict:
    return {
        "schema_version": version,
        "providers": {
            "example": {
                "recommended_model": "example-vision",
                "modalities": ["text", "vision"],
                "evidence": {"level": "belegt"},
                "free_tier": {"enabled": True, "request_limit_per_day": 10},
                "pricing": {
                    "input_cost_per_1m": 1.0,
                    "output_cost_per_1m": 2.0,
                    "image_tokens_max": 384,
                },
            },
        },
    }


def _canonical_path(entry: dict) -> str:
    """Reconstruct the canonical ID path from the separate fields."""
    hop = entry.get("hop") or []
    vendor = entry["vendor"]
    model = entry["model"]
    variant = entry.get("variant")
    segs = list(hop) + [vendor, model]
    path = "/".join(segs)
    if variant:
        path += ":" + variant
    return path


# ---------------------------------------------------------------------------
# v1.2 (pre-existing)
# ---------------------------------------------------------------------------

def test_valid_v1_2_doc_accepted():
    assert _errors(_make_doc()) == []


def test_invalid_modality_rejected():
    doc = _make_doc()
    doc["providers"]["example"]["modalities"] = ["text", "smell"]
    assert _errors(doc), "v1.2 schema should reject an invalid modality"


def test_evidence_level_outside_enum_rejected():
    doc = _make_doc()
    for bad in ["official", "verified", "confirmed", "", "BELEGT"]:
        doc["providers"]["example"]["evidence"]["level"] = bad
        assert _errors(doc), f"evidence.level {bad!r} should be rejected"


def test_v1_1_consumer_reads_v1_2_catalog_with_zero_errors():
    v1_1_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["schema_version", "providers"],
        "properties": {
            "schema_version": {"type": "string"},
            "providers": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "recommended_model": {"type": ["string", "null"]},
                        "pricing": {"type": "object"},
                    },
                    "additionalProperties": True,
                },
            },
        },
        "additionalProperties": True,
    }
    doc = _make_doc()
    validator = Draft202012Validator(v1_1_schema)
    assert sorted(validator.iter_errors(doc), key=lambda e: str(e.path)) == []


# ---------------------------------------------------------------------------
# TASK-C1 — modalities split into input/output
# ---------------------------------------------------------------------------

def test_input_output_modalities_are_enforced():
    doc = _make_doc()
    doc["providers"]["example"]["input_modalities"] = ["text", "image"]
    doc["providers"]["example"]["output_modalities"] = ["text"]
    assert _errors(doc) == []

    bad = _make_doc()
    bad["providers"]["example"]["input_modalities"] = ["text", "smell"]
    assert _errors(bad), "an invalid input modality must be rejected, not silently accepted"

    bad_out = _make_doc()
    bad_out["providers"]["example"]["output_modalities"] = ["music"]
    assert _errors(bad_out), "an invalid output modality must be rejected"


def test_vision_model_directionality_is_split():
    vision = _load_catalog()["providers"]["deepseek-flash-vision-exp"]
    assert vision["input_modalities"] == ["text", "image"]
    assert vision["output_modalities"] == ["text"]


def test_legacy_consumer_reads_new_catalog_with_zero_errors():
    # A pre-v1.3 consumer only knows the flat `modalities` field. The new
    # catalog adds input_modalities/output_modalities, so it must still load
    # cleanly (additionalProperties:true at every level).
    v1_2_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "schema_version": {"type": "string"},
            "providers": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "modalities": {"type": "array"},
                        "vendor": {"type": "string"},
                        "model": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
        "additionalProperties": True,
    }
    catalog = _load_catalog()
    assert catalog["schema_version"] == "fusionaize-provider-catalog/v1.3"
    assert catalog["providers"]["deepseek-flash-vision-exp"]["input_modalities"]
    validator = Draft202012Validator(v1_2_schema)
    assert sorted(validator.iter_errors(catalog), key=lambda e: str(e.path)) == []


# ---------------------------------------------------------------------------
# TASK-C2 — vendor/model/variant/hop separate; canonical ID generated
# ---------------------------------------------------------------------------

def test_blackbox_hop_vendor_model_split():
    bb = _load_catalog()["providers"]["blackbox-free"]
    assert bb["hop"] == ["blackboxai"]
    assert bb["vendor"] == "x-ai"
    assert bb["model"] == "grok-code-fast-1"
    assert _canonical_path(bb) == "blackboxai/x-ai/grok-code-fast-1"


def test_every_physical_entry_has_vendor_and_model():
    for key, entry in _load_catalog()["providers"].items():
        assert entry.get("vendor"), f"{key} missing vendor"
        assert entry.get("model"), f"{key} missing model"


def test_no_generated_path_repeats_a_segment():
    for key, entry in _load_catalog()["providers"].items():
        segs = list(entry.get("hop") or []) + [entry["vendor"], entry["model"]]
        assert len(segs) == len(set(segs)), \
            f"{key} repeats a segment in path {_canonical_path(entry)}"


def test_no_two_entries_claim_the_same_path():
    seen = {}
    for key, entry in _load_catalog()["providers"].items():
        path = _canonical_path(entry)
        assert path not in seen, f"{key} collides with {seen[path]} on {path}"
        seen[path] = key


def test_recommended_model_is_derived_not_independent():
    # v1.3: recommended_model lost its independent meaning. It is now derived
    # from the exploded identity fields and therefore must equal `model` (or be
    # null, meaning "do not auto-select"). A value that differs from `model`
    # (and thus from the generated path) is a second, driftable truth that this
    # scheme exists to eliminate.
    for key, entry in _load_catalog()["providers"].items():
        rm = entry.get("recommended_model")
        assert rm is None or rm == entry["model"], \
            (f"{key}: recommended_model={rm!r} does not match model="
             f"{entry['model']!r}")


def test_alias_ambiguity_resolves_via_vendor_model():
    # The pre-split catalog had ambiguous plain model names: 'gpt-4o' served by
    # three provider entries, 'gemini-3.1-pro' by three, 'claude-opus-4-7' by two.
    # Splitting into vendor/model must collapse each recommended model name to a
    # single (vendor, model) identity regardless of how many provider entries
    # (differing only by hop or variant) carry it.
    by_recommended = {}
    for key, entry in _load_catalog()["providers"].items():
        if not entry.get("recommended_model"):
            continue
        name = entry["recommended_model"]
        resolved = (entry["vendor"], entry["model"])
        by_recommended.setdefault(name, set()).add(resolved)
    for ambiguous in ["gpt-4o", "gemini-3.1-pro", "claude-opus-4-7"]:
        assert ambiguous in by_recommended
        assert len(by_recommended[ambiguous]) == 1, \
            f"{ambiguous!r} still ambiguous: {by_recommended[ambiguous]}"


def test_schema_rejects_vendor_without_model():
    doc = _make_doc()
    doc["providers"]["example"]["vendor"] = "anthropic"
    assert _errors(doc), "a vendor without a model must be rejected"


def test_schema_rejects_model_without_vendor():
    doc = _make_doc()
    doc["providers"]["example"]["model"] = "claude-opus-4-7"
    assert _errors(doc), "a model without a vendor must be rejected"


def test_schema_reserves_auto_prefix_in_vendor():
    doc = _make_doc()
    doc["providers"]["example"]["vendor"] = "auto"
    doc["providers"]["example"]["model"] = "x"
    assert _errors(doc), "vendor 'auto' must be rejected (reserved intent namespace)"

    for bad in ["auto/coding-fast", "auto/eco"]:
        doc = _make_doc()
        doc["providers"]["example"]["vendor"] = bad
        doc["providers"]["example"]["model"] = "x"
        assert _errors(doc), f"vendor {bad!r} must be rejected (reserved auto/ prefix)"

    ok = _make_doc()
    ok["providers"]["example"]["vendor"] = "autocode"
    ok["providers"]["example"]["model"] = "x"
    assert _errors(ok) == [], "'autocode' must NOT be rejected (auto prefix is not a word boundary)"


def test_schema_reserves_auto_prefix_in_hop():
    doc = _make_doc()
    doc["providers"]["example"]["vendor"] = "vendor"
    doc["providers"]["example"]["model"] = "model"
    doc["providers"]["example"]["hop"] = ["auto"]
    assert _errors(doc), "hop segment 'auto' must be rejected (reserved intent namespace)"


def test_hop_rejects_repeated_segment():
    doc = _make_doc()
    doc["providers"]["example"]["vendor"] = "vendor"
    doc["providers"]["example"]["model"] = "model"
    doc["providers"]["example"]["hop"] = ["h", "h"]
    assert _errors(doc), "a hop that lists the same intermediary twice must be rejected (loop)"


# ---------------------------------------------------------------------------
# TASK-C3 — capacity fields require evidence
# ---------------------------------------------------------------------------

def test_capacity_without_evidence_rejected():
    for field in ["max_input_tokens", "max_output_tokens"]:
        doc = _make_doc()
        doc["providers"]["example"]["capacity"] = {field: 128000}
        assert _errors(doc), f"capacity.{field} without evidence must be rejected"


def test_capacity_with_evidence_accepted():
    doc = _make_doc()
    doc["providers"]["example"]["capacity"] = {
        "max_input_tokens": 128000,
        "max_output_tokens": 16384,
        "evidence": {"source_url": "https://example.com/model-docs"},
    }
    assert _errors(doc) == []

    # A non-integer or non-positive token value must be rejected even with evidence.
    for field in ["max_input_tokens", "max_output_tokens"]:
        doc = _make_doc()
        doc["providers"]["example"]["capacity"] = {
            field: 0,
            "evidence": {"source_url": "https://example.com/model-docs"},
        }
        assert _errors(doc), f"capacity.{field}=0 must be rejected"


def test_existing_catalog_without_capacity_remains_valid():
    assert _errors(_load_catalog()) == []
    assert _load_catalog()["schema_version"] == "fusionaize-provider-catalog/v1.3"


# ---------------------------------------------------------------------------
# TASK-C4 — auto/ reserved namespace + collision reporting
# ---------------------------------------------------------------------------

def _intent_names(catalog: dict) -> list:
    return [m.removeprefix("auto/") for m in catalog["routing_modes"]]


def _find_intent_model_collisions(catalog: dict) -> list:
    """Report (intent, collision) pairs instead of silently resolving them."""
    intents = set(_intent_names(catalog))
    collisions = []
    for key, entry in catalog["providers"].items():
        names = {entry.get("model"), entry.get("vendor")}
        for n in names:
            if n in intents:
                collisions.append((n, key))
    return collisions


def test_auto_namespace_reserved_for_physical_paths():
    catalog = _load_catalog()
    for key, entry in catalog["providers"].items():
        assert not _canonical_path(entry).startswith("auto/"), \
            f"{key} claims the reserved auto/ prefix"


def test_routing_modes_are_represented_as_auto_names():
    assert _load_catalog()["routing_modes"] == ROUTING_MODES


def test_intent_model_collision_is_reported_not_resolved():
    catalog = _load_catalog()
    # OpenRouter's `auto` model collides with the reserved `auto` intent. The
    # loader must REPORT this rather than silently dropping or remapping it.
    collisions = _find_intent_model_collisions(catalog)
    assert ("auto", "openrouter-fallback") in collisions, \
        "openrouter/auto should be reported as colliding with the 'auto' intent"


# ---------------------------------------------------------------------------
# SHRINK-GUARD — no provider may vanish without an explicit acknowledgement
# ---------------------------------------------------------------------------

# A provider may change without failing the suite ONLY when the change is a
# conscious decision. ALLOWED_REMOVALS covers keys dropped from the catalog;
# ALLOWED_CHANGES covers per-entry losses INSIDE a surviving provider — a lost
# alias, a deleted carrying field (vendor/model/hop/variant), or a changed
# value. Each acknowledged change must cite the reason in the commit that
# edits this structure.
ALLOWED_CHANGES: dict[str, set[str]] = {}

# Fields that carry the v1.3 identity of a provider. Dropping one of these, or
# changing its value, is a loss that must be acknowledged rather than silently
# accreted (the same failure mode as a silently dropped provider key).
CARRIED_FIELDS = ("vendor", "model", "variant", "hop")


def _baseline_provider_keys() -> set[str]:
    """Provider keys as checked in on origin/main.

    Compared against the working tree so a provider that is silently dropped
    (rather than consciously removed) fails the suite.
    """
    out = subprocess.run(
        ["git", "show", "origin/main:providers/catalog.v1.json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return set(json.loads(out)["providers"].keys())


def _baseline_provider(key: str) -> dict:
    """The origin/main entry for a provider key."""
    out = subprocess.run(
        ["git", "show", "origin/main:providers/catalog.v1.json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["providers"].get(key, {})


def _entry_losses(key: str, baseline: dict, current: dict) -> set[str]:
    """Losses inside a surviving provider entry, described as stable strings.

    A "loss" is anything a downstream consumer could have relied on that is now
    gone: a removed alias, a removed carrying field, or a changed carrying-field
    value. Additions (new aliases, new fields) are never losses.
    """
    losses: set[str] = set()

    base_aliases = set(baseline.get("aliases", []))
    curr_aliases = set(current.get("aliases", []))
    for alias in sorted(base_aliases - curr_aliases):
        losses.add(f"alias {alias!r} removed")

    for field in CARRIED_FIELDS:
        old = baseline.get(field)
        new = current.get(field)
        if old is None:
            continue
        if new is None:
            losses.add(f"field {field!r} removed")
        elif old != new:
            losses.add(f"field {field!r} changed {old!r} -> {new!r}")

    return losses


def test_no_provider_shrinks_against_main():
    current = set(_load_catalog()["providers"].keys())
    baseline = _baseline_provider_keys()
    vanished = (baseline - current) - ALLOWED_REMOVALS
    assert not vanished, (
        "providers vanished without an explicit acknowledgement: "
        + ", ".join(sorted(vanished))
        + ". If the removal is intentional, add the key(s) to ALLOWED_REMOVALS."
    )
    assert len(current) >= len(baseline) - len(ALLOWED_REMOVALS), \
        "provider count shrank below the baseline minus acknowledged removals"


def test_no_provider_entry_loses_aliases_or_carried_fields():
    current = _load_catalog()["providers"]
    baseline = _baseline_provider_keys()
    unacknowledged: dict[str, set[str]] = {}
    for key in sorted(baseline & set(current)):
        losses = _entry_losses(key, _baseline_provider(key), current[key])
        outstanding = losses - ALLOWED_CHANGES.get(key, set())
        if outstanding:
            unacknowledged[key] = outstanding
    assert not unacknowledged, (
        "surviving providers lost aliases or carrying fields without an "
        "explicit acknowledgement: "
        + "; ".join(f"{k}: {', '.join(sorted(v))}" for k, v in unacknowledged.items())
        + ". If the change is intentional, add the descriptor(s) to ALLOWED_CHANGES."
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
    if failures:
        print(f"{len(failures)} failure(s)")
        sys.exit(1)
    print(f"{len(tests)} tests passed")
