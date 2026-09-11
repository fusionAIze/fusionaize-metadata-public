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

  Round 4 (review findings):
   MUSS-1   auto/ reserved on EVERY path segment (vendor, model, variant, hop)
            — not just vendor + hop; model/variant were still pass-through
   MUSS-2   recommended_model is truly DERIVED (option a): removed from the
            data file, reconstructed at load from tier_status (auto-selectable
            -> model, non-auto-selectable -> null); a stored value is gone, so
            a new entry cannot contradict it
   MUSS-3   shrink-guard baseline corrected from origin/main (v1.2, no identity
            fields) to HEAD (v1.3, full identity); it now actually sees losses
            inside v1.3 entries

  Round 5 (TASK-G2 — context_window/limits for the seven fallback-less entries):
   Every entry that has no downstream embedded fallback (four manufacturer
   rollups, two plan rollups, one experiment model) now declares a positive
   context_window and a limits dict. Model entries are measured (belegt);
   manufacturer/plan rollups are derived (plausibel) with provenance. The
   entry_type field (model/vendor/plan) records why a rollup has a derived
   value rather than a single true window.

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


def test_recommended_model_is_derived_not_stored():
    # v1.3 (round 4, MUSS-2 option a): recommended_model is DERIVED, never
    # stored in the data file. The routing key is the identity path built from
    # vendor/model/variant/hop; recommended_model is reconstructed at load from
    # tier_status: `model` for an auto-selectable provider, `null` otherwise.
    # Because it is not stored, a NEW entry (edited by no one but the author)
    # cannot carry a value that contradicts the identity path.
    catalog = _load_catalog()
    for key, entry in catalog["providers"].items():
        assert "recommended_model" not in entry, \
            f"{key}: recommended_model must not be stored (it is derived)"


def test_new_entry_cannot_contradict_derived_recommended_model():
    # A hypothetical brand-new entry that nobody has reviewed. Even if its
    # author tries to set a stored recommended_model, the schema treats it as
    # an unknown/ignored property (additionalProperties:true) and the loader
    # derives the value from the identity fields + tier_status, so no stored
    # driftable truth can take hold.
    doc = _make_doc()
    doc["providers"]["brand-new"] = {
        "vendor": "acme",
        "model": "gpt-future",
        "hop": [],
        "recommended_model": "acme/gpt-future",  # a stale fused string
    }
    assert _errors(doc) == [], \
        "a derived-only field must not be rejected as a schema error (tolerated for back-compat)"
    # The derived rule ignores the stored value entirely: auto-selectable
    # (no tier_status) -> derived == model.
    entry = doc["providers"]["brand-new"]
    assert _derived_recommended_model(entry) == "gpt-future"
    assert _derived_recommended_model(entry) != entry["recommended_model"], \
        "the stored stale string must NOT be what the loader derives"


def _derived_recommended_model(entry: dict):
    """The load-time reconstruction of recommended_model (option a)."""
    tier = entry.get("tier_status", "active")
    if tier in ("expired", "deprecated"):
        return None
    return entry["model"]


def test_derived_recommended_model_matches_tier_status():
    catalog = _load_catalog()
    for key, entry in catalog["providers"].items():
        derived = _derived_recommended_model(entry)
        tier = entry.get("tier_status", "active")
        if tier in ("expired", "deprecated"):
            assert derived is None, f"{key}: non-auto-selectable must derive null"
        else:
            assert derived == entry["model"], \
                f"{key}: auto-selectable must derive model={entry['model']!r}"
    # The one non-auto-selectable entry is kilocode (expired).
    assert _derived_recommended_model(catalog["providers"]["kilocode"]) is None


def _derived_recommended_models(catalog: dict) -> dict:
    """Map of derived recommended_model name -> set of (vendor, model) identities."""
    by_name = {}
    for key, entry in catalog["providers"].items():
        rm = _derived_recommended_model(entry)
        if rm is None:
            continue
        by_name.setdefault(rm, set()).add((entry["vendor"], entry["model"]))
    return by_name


def test_alias_ambiguity_resolves_via_vendor_model():
    # The pre-split catalog had ambiguous plain model names: 'gpt-4o' served by
    # three provider entries, 'gemini-3.1-pro' by three, 'claude-opus-4-7' by two.
    # Splitting into vendor/model must collapse each derived recommended model
    # name to a single (vendor, model) identity regardless of how many provider
    # entries (differing only by hop or variant) carry it.
    by_recommended = _derived_recommended_models(_load_catalog())
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


def test_schema_reserves_auto_prefix_in_model():
    doc = _make_doc()
    doc["providers"]["example"]["vendor"] = "vendor"
    doc["providers"]["example"]["model"] = "auto"
    assert _errors(doc), "model 'auto' must be rejected (reserved intent namespace)"

    for bad in ["auto/coding-fast", "auto/eco"]:
        doc = _make_doc()
        doc["providers"]["example"]["vendor"] = "vendor"
        doc["providers"]["example"]["model"] = bad
        assert _errors(doc), f"model {bad!r} must be rejected (reserved auto/ prefix)"


def test_schema_reserves_auto_prefix_in_variant():
    doc = _make_doc()
    doc["providers"]["example"]["vendor"] = "vendor"
    doc["providers"]["example"]["model"] = "model"
    doc["providers"]["example"]["variant"] = "auto"
    assert _errors(doc), "variant 'auto' must be rejected (reserved intent namespace)"

    for bad in ["auto/coding-fast", "auto/eco"]:
        doc = _make_doc()
        doc["providers"]["example"]["vendor"] = "vendor"
        doc["providers"]["example"]["model"] = "model"
        doc["providers"]["example"]["variant"] = bad
        assert _errors(doc), f"variant {bad!r} must be rejected (reserved auto/ prefix)"


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


def test_intent_model_collision_is_impossible_not_reported():
    # v1.3 (round 4, MUSS-1): the auto/ intent namespace is reserved on EVERY
    # path segment (vendor, model, variant, hop), so a physical entry can no
    # longer carry `auto` as a model (or any other segment). OpenRouter's "auto"
    # router is therefore named `auto-router`, leaving the reserved token to the
    # intent namespace alone. A collision is now impossible by construction.
    catalog = _load_catalog()
    collisions = _find_intent_model_collisions(catalog)
    assert collisions == [], \
        f"reserved intent names still collide with physical entries: {collisions}"
    # The former colliding model is represented without the reserved token.
    assert catalog["providers"]["openrouter-fallback"]["model"] == "auto-router"


# ---------------------------------------------------------------------------
# TASK-D1 — facts migrated from faigate's hand-written Python tables
# ---------------------------------------------------------------------------

# The 23 input-token ceilings from faigate's _MODEL_INPUT_CAPS, migrated
# verbatim (value for value) into the catalog. The keys are concrete model IDs;
# none of them carries a per-entry provenance, so every migrated value is
# `unbestaetigt`, not `belegt`. The exact set is asserted below so a silent drop
# of one of the 23 entries fails the suite (the same failure mode the shrink
# guard catches for providers).

MODEL_CAPS = {
    "deepseek-v4-pro": 1000000,
    "deepseek-v4-flash": 1000000,
    "gpt-5.6-sol": 922000,
    "gpt-5.6-terra": 922000,
    "gpt-5.6-luna": 922000,
    "gpt-5.5": 1050000,
    "gpt-5.5-pro": 1050000,
    "o3": 200000,
    "o3-mini": 200000,
    "o4-mini": 200000,
    "claude-opus-5": 1000000,
    "claude-sonnet-5": 1000000,
    "claude-haiku-4-5": 200000,
    "claude-code": 262144,
    "gemini-3.1-pro": 1048576,
    "gemini-3.1-flash": 1048576,
    "gemini-3-flash-lite": 1048576,
    "llama-4-maverick": 131072,
    "llama-4-scout": 131072,
    "qwen-3.6-27b": 262144,
    "qwen3-coder": 262144,
    "glm-5.3": 1000000,
    "kimi-k2.6": 262144,
}

# FAI-215 (lane f1, Teil A): the 20 configured model strings from faigate that
# lacked a cap entry. Resolved against the LiteLLM registry first, OpenRouter
# second. Each entry carries its own evidence (level + source_url + as_of);
# a `belegt` value was taken verbatim from a named source, `unbestaetigt` is a
# derived value (OpenRouter's `auto` reports a 2M placeholder context_length
# that is a routing default, not a concrete model ceiling).
EVIDENCED_MODEL_CAPS = {
    "claude-opus-4-6": 1000000,
    "claude-sonnet-4-6": 1000000,
    "gemini-2.5-pro": 1048576,
    "gemini-2.5-flash": 1048576,
    "gemini-2.5-flash-lite": 1048576,
    "gpt-4o": 128000,
    "gpt-5.4": 1050000,
    "gpt-5.3-codex": 272000,
    "gpt-5.1-codex-mini": 272000,
    "deepseek-v4-flash-vision-exp": 1000000,
    "grok-code-fast-1": 256000,
    "glm-5": 204800,
    "auto-router": 2000000,
}

# MKC-INIT (lane init): caps added from the three router sources (OmniRoute,
# OpenRouter, LiteLLM) during the catalog re-initialization. Splits from
# EVIDENCED_MODEL_CAPS because these carry a wider evidence spread: OpenRouter
# values are `belegt`, the LiteLLM-only value (claude-3-5-haiku) is `plausibel`.
MKC_INIT_CAPS = {
    "claude-haiku-3-5": (200000, "plausibel"),
    "gemini-3.6-flash": (1048576, "belegt"),
    "gemini-3.7-flash": (1048576, "belegt"),
    "gemini-3.8-flash": (1048576, "belegt"),
    "gpt-oss-120b": (131072, "belegt"),
}

# MKC-G3 (lane g3): provenance lift for the 24 unbestaetigt entries from the
# earlier _MODEL_INPUT_CAPS migration.  Values verified against or.json
# (OpenRouter /v1/models) and ll.json (LiteLLM model_prices_and_context_window).
# 16 confirmed as `belegt`, 1 raised to `plausibel` (gemini-3.1-pro, single
# third-party LL source contradicts the catalog value), 6 left `unbestaetigt`
# (no bare-name source in either registry).
MKC_G3_CAPS = {
    # belegt — both OpenRouter and LiteLLM confirm
    "claude-opus-5":   (1000000, "belegt"),
    "claude-sonnet-5": (1000000, "belegt"),
    "gpt-5.5":         (1050000, "belegt"),
    "gpt-5.5-pro":     (1050000, "belegt"),
    "kimi-k2.6":       (262144,  "belegt"),
    "o3":              (200000,  "belegt"),
    "o3-mini":         (200000,  "belegt"),
    "o4-mini":         (200000,  "belegt"),
    "qwen3-coder":     (262144,  "belegt"),
    # belegt — LiteLLM confirms, OpenRouter absent
    "claude-haiku-4-5": (200000, "belegt"),
    # belegt — LiteLLM confirms, OpenRouter contradicts (noted in evidence)
    "deepseek-v4-flash": (1000000, "belegt"),
    "deepseek-v4-pro":   (1000000, "belegt"),
    "glm-5.3":           (1000000, "belegt"),
    "gpt-5.6-luna":      (922000,  "belegt"),
    "gpt-5.6-sol":       (922000,  "belegt"),
    "gpt-5.6-terra":     (922000,  "belegt"),
    # plausibel — single third-party LL source contradicts catalog value
    "gemini-3.1-pro":    (1048576, "plausibel"),
}

# The canonical-lane -> (concrete model, human label) pairs merged from
# faigate's _ACTIVE_MODEL_VERSIONS and _MODEL_VERSION_LABELS.
MODEL_VERSIONS = {
    "google/gemini-flash": ("gemini-3-flash", "Gemini 3 Flash"),
    "google/gemini-flash-lite": ("gemini-3-flash-lite", "Gemini 3 Flash-Lite"),
    "google/gemini-pro-high": ("gemini-3.1-pro", "Gemini 3.1 Pro (High)"),
    "google/gemini-pro-low": ("gemini-3.1-pro", "Gemini 3.1 Pro (Low)"),
    "anthropic/opus-4.6": ("claude-opus-4-6", "Claude Opus 4.6"),
    "anthropic/sonnet-4.6": ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    "openai/gpt-4o": ("gpt-4o", "GPT-4o"),
    "deepseek/reasoner": ("deepseek-reasoner", "DeepSeek R1 (Reasoner)"),
    "deepseek/chat": ("deepseek-chat", "DeepSeek V3 (Chat)"),
}


def test_all_23_model_caps_present_with_evidence():
    catalog = _load_catalog()
    caps = catalog["model_caps"]
    expected_total = 23 + len(EVIDENCED_MODEL_CAPS) + len(MKC_INIT_CAPS)
    assert len(caps) == expected_total, (
        f"expected {expected_total} model cap entries, got {len(caps)}"
    )
    for model_id, expected in MODEL_CAPS.items():
        assert model_id in caps, f"cap for {model_id!r} missing"
        entry = caps[model_id]
        assert "evidence" in entry, f"cap for {model_id!r} missing evidence block"
        assert entry["max_input_tokens"] == expected, (
            f"cap for {model_id!r} drifted: {entry['max_input_tokens']} != {expected}"
        )


def test_evidenced_model_caps_present():
    # FAI-215: the cap entries that close the gap carry their own evidence and
    # values resolved from the LiteLLM registry (primary) or OpenRouter
    # (secondary). Every one must carry a source_url; a `belegt` value must not
    # be silently unbestaetigt.
    caps = _load_catalog()["model_caps"]
    for model_id, expected in EVIDENCED_MODEL_CAPS.items():
        assert model_id in caps, f"cap for {model_id!r} missing"
        entry = caps[model_id]
        assert "evidence" in entry, f"cap for {model_id!r} missing evidence block"
        assert entry["max_input_tokens"] == expected, (
            f"cap for {model_id!r} drifted: {entry['max_input_tokens']} != {expected}"
        )
        assert entry["evidence"].get("source_url"), (
            f"cap for {model_id!r} must carry a source_url"
        )
        assert entry["evidence"]["level"] in ("belegt", "unbestaetigt"), (
            f"cap for {model_id!r} has invalid level {entry['evidence']['level']!r}"
        )


def test_mkc_init_caps_present_with_evidence():
    # MKC-INIT: caps added from OmniRoute/OpenRouter/LiteLLM during the catalog
    # re-initialization. Each carries its own evidence level; a LiteLLM-only
    # value is `plausibel`, an OpenRouter-sourced value is `belegt`.
    caps = _load_catalog()["model_caps"]
    for model_id, (expected, level) in MKC_INIT_CAPS.items():
        assert model_id in caps, f"cap for {model_id!r} missing"
        entry = caps[model_id]
        assert "evidence" in entry, f"cap for {model_id!r} missing evidence block"
        assert entry["max_input_tokens"] == expected, (
            f"cap for {model_id!r} drifted: {entry['max_input_tokens']} != {expected}"
        )
        assert entry["evidence"].get("source_url"), (
            f"cap for {model_id!r} must carry a source_url"
        )
        assert entry["evidence"]["level"] == level, (
            f"cap for {model_id!r} has level {entry['evidence']['level']!r}, expected {level!r}"
        )


def test_mkc_g3_caps_present_with_evidence():
    # MKC-G3: provenance lift.  Each promoted entry must carry a source URL and
    # exactly the level stated in MKC_G3_CAPS; value must be unchanged.
    caps = _load_catalog()["model_caps"]
    for model_id, (expected, level) in MKC_G3_CAPS.items():
        assert model_id in caps, f"cap for {model_id!r} missing"
        entry = caps[model_id]
        assert "evidence" in entry, f"cap for {model_id!r} missing evidence block"
        assert entry["max_input_tokens"] == expected, (
            f"cap for {model_id!r} drifted: {entry['max_input_tokens']} != {expected}"
        )
        ev = entry["evidence"]
        assert ev.get("level") == level, (
            f"cap for {model_id!r} has level {ev.get('level')!r}, expected {level!r}"
        )
        has_source = bool(ev.get("source_url") or ev.get("source_urls"))
        assert has_source, f"cap for {model_id!r} must carry source_url or source_urls"


def test_model_caps_are_unbestaetigt_not_belegt():
    # The migrated caps carry no source, so none may claim `belegt`. This is the
    # truth about the value, not a weakness: a hand-written Python table without
    # a provenance URL is unverified by public standards. The FAI-215 evidenced
    # caps are exempt — they DO carry a source and may therefore be `belegt`.
    # The MKC-INIT caps are likewise exempt (OpenRouter `belegt` and LiteLLM
    # `plausibel`). The MKC-G3 caps are exempt: their evidence was verified
    # against or.json/ll.json (2026-09-11) and promoted to `belegt` or
    # `plausibel` where a source was found.
    caps = _load_catalog()["model_caps"]
    mkc_g3_keys = set(MKC_G3_CAPS)
    for model_id, entry in caps.items():
        if model_id in EVIDENCED_MODEL_CAPS or model_id in MKC_INIT_CAPS or model_id in mkc_g3_keys:
            continue
        level = entry["evidence"]["level"]
        assert level == "unbestaetigt", (
            f"cap for {model_id!r} has evidence.level={level!r}; "
            "without a source it must be unbestaetigt"
        )


def test_model_versions_present_with_evidence():
    catalog = _load_catalog()
    assert set(catalog["model_versions"].keys()) == set(MODEL_VERSIONS.keys())
    for lane, (model, label) in MODEL_VERSIONS.items():
        entry = catalog["model_versions"][lane]
        assert entry["model"] == model, f"{lane}: model drifted"
        assert entry["label"] == label, f"{lane}: label drifted"
        assert "evidence" in entry, f"{lane}: missing evidence block"


def test_model_versions_are_unbestaetigt():
    versions = _load_catalog()["model_versions"]
    for lane, entry in versions.items():
        assert entry["evidence"]["level"] == "unbestaetigt", (
            f"{lane}: evidence.level must be unbestaetigt, got {entry['evidence']['level']!r}"
        )


def test_catalog_still_valid_with_model_caps_and_versions():
    assert _errors(_load_catalog()) == []


# ---------------------------------------------------------------------------
# TASK-G2 — every entry declares context_window + limits (or documents why not)
# ---------------------------------------------------------------------------

# The faigate runtime merges this catalog with an embedded fallback catalog and
# asserts EVERY provider carries a positive context_window and a limits dict
# (tests test_provider_catalog_declares_context_window_everywhere and
# test_provider_catalog_declares_in_band_input_cap). Seven entries here used to
# lack both: four manufacturer rollups (anthropic, openai, google, google-vertex),
# two plan rollups (byteplus-plan, volcengine-plan) and one real model
# (deepseek-flash-vision-exp). They are the entries with NO embedded fallback, so
# a missing field here could not be backfilled downstream. The invariant must be
# enforced at the source of truth, not recovered later.

# Entry keys that carry a DERIVED (plausibel) context_window rather than a
# measured (belegt) one. A manufacturer/plan rollup spans several models with
# different windows, so it has no single true value — the schema's entry_type
# records that, and the derived value is an orientation, never evidence.
DERIVED_CONTEXT_ENTRIES = {
    "anthropic",
    "openai",
    "google",
    "google-vertex",
    "byteplus-plan",
    "volcengine-plan",
}

# The one real model that must carry a measured value with a named public source.
MEASURED_CONTEXT_ENTRIES = {"deepseek-flash-vision-exp"}

# The seven entries this lane is responsible for. They are the entries with no
# embedded fallback in the downstream consumer, so a missing context_window here
# could never be backfilled. (Entries that DO have an embedded fallback obtain
# their context_window there; this repo is not the sole source for those.)
TASK_G2_ENTRIES = DERIVED_CONTEXT_ENTRIES | MEASURED_CONTEXT_ENTRIES


def test_g2_entries_declare_context_window_and_limits():
    """The seven fallback-less entries carry a positive context_window + limits.

    Every one of the seven must declare both, because the downstream consumer
    asserts both on the merged catalog and there is no embedded value to recover
    for these keys.
    """
    providers = _load_catalog()["providers"]
    for key in TASK_G2_ENTRIES:
        entry = providers[key]
        ctx = entry.get("context_window")
        assert isinstance(ctx, int) and ctx > 0, (
            f"{key!r} must declare a positive integer context_window, got {ctx!r}"
        )
        limits = entry.get("limits")
        assert isinstance(limits, dict), (
            f"{key!r} must declare limits as a dict, got {limits!r}"
        )
        cap = limits.get("max_input_tokens")
        assert isinstance(cap, int) and cap > 0, (
            f"{key!r} limits.max_input_tokens must be a positive integer, got {cap!r}"
        )


def test_derived_context_entries_are_marked_plausibel_with_provenance():
    """A derived (rollup) context_window must be plausibel and name its source."""
    providers = _load_catalog()["providers"]
    for key in DERIVED_CONTEXT_ENTRIES:
        entry = providers[key]
        assert entry.get("entry_type") in ("vendor", "plan"), (
            f"{key!r} must be entry_type vendor/plan, got {entry.get('entry_type')!r}"
        )
        evidence = entry.get("context_evidence") or {}
        assert evidence.get("level") == "plausibel", (
            f"{key!r} derived context must be plausibel, got {evidence.get('level')!r}"
        )
        assert evidence.get("derived_from"), (
            f"{key!r} derived context must name derived_from"
        )


def test_measured_context_entry_is_belegt_with_source():
    """deepseek-flash-vision-exp must carry a measured value with a named source."""
    entry = _load_catalog()["providers"]["deepseek-flash-vision-exp"]
    assert entry.get("entry_type") == "model"
    evidence = entry.get("context_evidence") or {}
    assert evidence.get("level") == "belegt", (
        f"deepseek-flash-vision-exp must be belegt, got {evidence.get('level')!r}"
    )
    assert evidence.get("source_url"), "a belegt context must cite a source_url"
    assert entry["context_window"] == entry["limits"]["max_input_tokens"]


# ---------------------------------------------------------------------------
# SHRINK-GUARD — no provider may vanish without an explicit acknowledgement
# ---------------------------------------------------------------------------

# A provider may change without failing the suite ONLY when the change is a
# conscious decision. ALLOWED_REMOVALS covers keys dropped from the catalog;
# ALLOWED_CHANGES covers per-entry losses INSIDE a surviving provider — a lost
# alias, a deleted carrying field (vendor/model/hop/variant), or a changed
# value. Each acknowledged change must cite the reason in the commit that
# edits this structure.
ALLOWED_CHANGES: dict[str, set[str]] = {
    # Round 4, MUSS-1: openrouter-fallback's model was `auto`, colliding with the
    # reserved auto/ intent namespace. Renamed to `auto-router` (already an alias)
    # so a physical entry no longer claims the reserved token.
    "openrouter-fallback": {"field 'model' changed 'auto' -> 'auto-router'"},
}

# Fields that carry the v1.3 identity of a provider. Dropping one of these, or
# changing its value, is a loss that must be acknowledged rather than silently
# accreted (the same failure mode as a silently dropped provider key).
CARRIED_FIELDS = ("vendor", "model", "variant", "hop")


def _baseline_provider_keys() -> set[str]:
    """Provider keys as last committed on this branch (HEAD).

    Compared against the working tree so a provider that is silently dropped
    (rather than consciously removed) fails the suite.

    v1.2's baseline (origin/main) had no vendor/model/variant/hop fields, so
    guarding against it could only ever see alias losses — never the v1.3
    identity fields. The guard must compare against the state that REALLY
    preceded the current edit: the branch tip, which is v1.3 and carries those
    fields.
    """
    out = subprocess.run(
        ["git", "show", "HEAD:providers/catalog.v1.json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return set(json.loads(out)["providers"].keys())


def _baseline_provider(key: str) -> dict:
    """The HEAD (branch tip) entry for a provider key."""
    out = subprocess.run(
        ["git", "show", "HEAD:providers/catalog.v1.json"],
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


# ---------------------------------------------------------------------------
# TASK-G3 — derived evidence level must not exceed source evidence level
# ---------------------------------------------------------------------------
# Invariant: if a provider entry carries context_evidence.derived_from pointing
# at a model_caps entry, the derived level may be AT MOST equal to the source
# level.  Ordering: unbestaetigt < plausibel < belegt.
#
# Three rollup entries in the g2 catalog violate this today (pre-fix):
#   providers.google        <- model_caps["gemini-3.1-pro"]  (unbestaetigt)
#   providers.byteplus-plan <- model_caps["kimi-k2.6"]       (unbestaetigt)
#   providers.volcengine-plan <- model_caps["kimi-k2.6"]     (unbestaetigt)
# All three carry level="plausibel" which is HIGHER than "unbestaetigt".
# The test must produce an AssertionError on the current HEAD before the fix,
# not an import or setup error.

_LEVEL_ORDER = {"unbestaetigt": 0, "plausibel": 1, "belegt": 2}


def test_derived_context_level_does_not_exceed_source_level():
    """Every context_evidence.derived_from entry must have level <= source level.

    Ordering: unbestaetigt=0 < plausibel=1 < belegt=2.
    A rollup that derives from an unbestaetigt source must itself be
    unbestaetigt; it may not claim plausibel or belegt.
    """
    catalog = _load_catalog()
    caps = catalog.get("model_caps", {})
    providers = catalog["providers"]

    violations: list[str] = []
    for pkey, entry in providers.items():
        ce = entry.get("context_evidence")
        if ce is None:
            continue
        derived_from = ce.get("derived_from")
        if not derived_from:
            continue
        # Parse model ID from e.g. 'model_caps["kimi-k2.6"]'
        if not (derived_from.startswith('model_caps["') and derived_from.endswith('"]')):
            continue
        source_id = derived_from[len('model_caps["'):-2]
        if source_id not in caps:
            continue
        source_level = caps[source_id].get("evidence", {}).get("level")
        derived_level = ce.get("level")
        if source_level not in _LEVEL_ORDER or derived_level not in _LEVEL_ORDER:
            continue
        if _LEVEL_ORDER[derived_level] > _LEVEL_ORDER[source_level]:
            violations.append(
                f"{pkey!r}: context_evidence.level={derived_level!r} exceeds "
                f"source model_caps[{source_id!r}].evidence.level={source_level!r}"
            )

    assert not violations, (
        "derived context evidence level exceeds its source level:\n  "
        + "\n  ".join(violations)
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
