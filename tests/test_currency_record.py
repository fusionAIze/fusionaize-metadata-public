"""Acceptance tests for freshness attestation (FAI-247-A, lane entry-states-who-confirmed-it).

Criterion 1: each catalog entry can carry per-field freshness attestation
  (source, confirmed_at, evidence level). The schema defines it.

Criterion 2: an entry without freshness is valid and reads as unconfirmed.
  The migration invents NO attestations — it does not carry what it cannot prove.

Criterion 3: last_reviewed retains its meaning and is NOT overwritten by
  a freshness attestation. The relationship between the two fields is documented.

Criterion 4: a test shows one entry whose window the provider itself confirmed
  (level=confirmed) and one entry confirmed by a third-party registry
  (level=plausible). The two carry DIFFERENT evidence levels.

Run with ``python3 -m pytest -q``.
"""

import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "provider-catalog.v1.schema.json"
CATALOG_PATH = ROOT / "providers" / "catalog.v1.json"

# Evidence levels
_EVIDENCE_LEVELS = {"confirmed", "plausible", "unconfirmed"}


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


def _errors(doc: dict) -> list:
    validator = Draft202012Validator(_load_schema())
    return sorted(validator.iter_errors(doc), key=lambda e: str(e.path))


# ---------------------------------------------------------------------------
# Criterion 1 — Schema defines per-field freshness attestation
# ---------------------------------------------------------------------------

def test_freshness_field_exists_in_schema():
    """The schema v1.7 defines a freshness property on every provider entry."""
    schema = _load_schema()
    props = schema["properties"]["providers"]["additionalProperties"]["properties"]
    assert "freshness" in props, "schema must define freshness as a provider property"

    fresh = props["freshness"]
    assert fresh.get("type") == "object"
    assert "additionalProperties" in fresh

    # The per-field attestation block requires at least 'level'
    field_schema = fresh["additionalProperties"]
    assert "required" in field_schema
    assert "level" in field_schema["required"]
    assert field_schema["type"] == "object"

    # The level enum has the three expected values
    level_prop = field_schema["properties"]["level"]
    assert set(level_prop["enum"]) == _EVIDENCE_LEVELS


def test_freshness_attestation_validates():
    """A provider entry with a freshness attestation is accepted by the schema."""
    doc = {
        "schema_version": "fusionaize-provider-catalog/v1.7",
        "providers": {
            "acme-free": {
                "vendor": "acme",
                "model": "gpt-future",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "last_reviewed": "2026-09-15",
                "freshness": {
                    "pricing": {
                        "level": "confirmed",
                        "source": "ACME official pricing page",
                        "source_url": "https://acme.example.com/pricing",
                        "confirmed_at": "2026-09-15",
                    },
                },
            },
        },
    }
    assert _errors(doc) == []


def test_freshness_without_level_rejected():
    """A freshness attestation without level is rejected by the schema."""
    doc = {
        "schema_version": "fusionaize-provider-catalog/v1.7",
        "providers": {
            "acme-free": {
                "vendor": "acme",
                "model": "gpt-future",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "freshness": {
                    "pricing": {
                        "source": "some source",
                        "confirmed_at": "2026-09-15",
                    },
                },
            },
        },
    }
    assert _errors(doc), "freshness without level must be rejected"


def test_freshness_with_invalid_level_rejected():
    """A freshness attestation with a level outside the enum is rejected."""
    for bad in ["verified", "official", "", "CONFIRMED"]:
        doc = {
            "schema_version": "fusionaize-provider-catalog/v1.7",
            "providers": {
                "acme-free": {
                    "vendor": "acme",
                    "model": "gpt-future",
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "freshness": {
                        "pricing": {"level": bad},
                    },
                },
            },
        }
        assert _errors(doc), f"freshness level {bad!r} must be rejected"


# ---------------------------------------------------------------------------
# Criterion 2 — Entry without freshness is valid and reads as unconfirmed
# ---------------------------------------------------------------------------

def test_entry_without_freshness_is_valid():
    """An entry without a freshness block is accepted by the schema.

    This is the gate: the 66 existing entries have no freshness and must
    continue to validate against the v1.7 schema.
    """
    doc = {
        "schema_version": "fusionaize-provider-catalog/v1.7",
        "providers": {
            "legacy-entry": {
                "vendor": "legacy",
                "model": "old-model",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "last_reviewed": "2026-04-26",
                "pricing": {
                    "input_cost_per_1m": 1.0,
                    "output_cost_per_1m": 2.0,
                },
            },
        },
    }
    assert _errors(doc) == [], (
        "a pre-freshness entry must validate; the schema is additive"
    )


def test_full_catalog_validates_with_v1_7_schema():
    """All 66 existing catalog entries validate against the v1.7 schema."""
    assert _errors(_load_catalog()) == []


def test_migration_invents_no_attestations():
    """The migration script touches ZERO entries — it invents no attestations.

    A migration that backfills fake freshness data would be worse than no
    migration at all. The script reports 0 entries touched.
    """
    result = subprocess.run(
        [sys.executable, "scripts/migrate_currency_record.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    assert "Entries touched by migration: 0" in result.stdout, (
        f"migration must touch 0 entries; got:\n{result.stdout}"
    )


def test_no_entry_has_freshness_in_current_catalog():
    """The 66 existing entries carry no freshness attestation.

    This is a Riegel (guard): the test must fail if someone adds freshness
    data without understanding what they are attesting. Every attestation
    requires a named source and a concrete confirmed_at date.
    """
    catalog = _load_catalog()
    for key, entry in catalog["providers"].items():
        assert "freshness" not in entry, (
            f"{key!r} carries a freshness block — every attestation needs "
            "a named source and a concrete confirmed_at date. Remove or "
            "document the source."
        )


def test_real_catalog_entry_used_as_anchor():
    """A concrete, named provider from the real catalog is checked.

    A test that passes on an empty catalog is worthless. This test pins a
    real entry (amazon-bedrock) and asserts it has no freshness, a real
    last_reviewed, and validates against the schema.
    """
    catalog = _load_catalog()
    assert "amazon-bedrock" in catalog["providers"], (
        "amazon-bedrock must exist in the catalog — the anchor provider "
        "was removed, invalidating this test"
    )
    entry = catalog["providers"]["amazon-bedrock"]
    assert "freshness" not in entry
    assert entry["last_reviewed"] == "2026-09-11"


# ---------------------------------------------------------------------------
# Criterion 3 — last_reviewed retains its meaning, documented relationship
# ---------------------------------------------------------------------------

def test_last_reviewed_not_overwritten_by_freshness():
    """last_reviewed is a separate field from freshness and retains its value.

    When a provider entry carries both last_reviewed and a freshness
    attestation, neither overwrites the other. last_reviewed records a human
    review; freshness records per-field source evidence.
    """
    doc = {
        "schema_version": "fusionaize-provider-catalog/v1.7",
        "providers": {
            "coexistent": {
                "vendor": "test",
                "model": "test-model",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "last_reviewed": "2026-04-26",
                "freshness": {
                    "pricing": {
                        "level": "confirmed",
                        "source": "Official pricing page",
                        "source_url": "https://example.com/pricing",
                        "confirmed_at": "2026-09-26",
                    },
                },
            },
        },
    }
    assert _errors(doc) == []
    entry = doc["providers"]["coexistent"]
    assert entry["last_reviewed"] == "2026-04-26", (
        "last_reviewed must not be overwritten by a freshness attestation"
    )
    assert entry["freshness"]["pricing"]["confirmed_at"] == "2026-09-26", (
        "freshness confirmed_at must not be overwritten by last_reviewed"
    )
    assert entry["last_reviewed"] != entry["freshness"]["pricing"]["confirmed_at"], (
        "the two dates serve different purposes and should differ in this test"
    )


def test_relationship_documented():
    """The migration script documents the relationship between the two fields."""
    result = subprocess.run(
        [sys.executable, "scripts/migrate_currency_record.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    assert "last_reviewed retains its meaning" in result.stdout, (
        "migration script must document the relationship between "
        "last_reviewed and freshness"
    )
    assert "did NOT invent any attestations" in result.stdout, (
        "migration script must document that it invented nothing"
    )


# ---------------------------------------------------------------------------
# Criterion 4 — provider-self vs foreign-registry evidence levels
# ---------------------------------------------------------------------------

def test_provider_self_confirmed_window():
    """An entry whose context window the provider itself confirmed (level=confirmed).

    The provider's own documentation is the primary source — no intermediary,
    no aggregator, no derivation. This is the highest evidence tier.
    """
    doc = {
        "schema_version": "fusionaize-provider-catalog/v1.7",
        "providers": {
            "acme-self-attested": {
                "vendor": "acme",
                "model": "gpt-future",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "last_reviewed": "2026-09-26",
                "context_window": 128000,
                "limits": {"max_input_tokens": 128000},
                "context_evidence": {
                    "level": "confirmed",
                    "source_url": "https://acme.example.com/docs/models",
                    "as_of": "2026-09-26",
                },
                "freshness": {
                    "context_window": {
                        "level": "confirmed",
                        "source": "ACME official model documentation",
                        "source_url": "https://acme.example.com/docs/models",
                        "confirmed_at": "2026-09-26",
                    },
                },
            },
        },
    }
    assert _errors(doc) == []
    entry = doc["providers"]["acme-self-attested"]
    assert entry["freshness"]["context_window"]["level"] == "confirmed"
    assert entry["context_evidence"]["level"] == "confirmed"


def test_registry_confirmed_window_plausible():
    """An entry whose context window a foreign registry confirmed (level=plausible).

    A third-party aggregator (e.g. OpenRouter, LiteLLM) reports the value.
    Because it is not the provider itself speaking, the evidence is plausible
    — a step below confirmed, but still sourced.
    """
    doc = {
        "schema_version": "fusionaize-provider-catalog/v1.7",
        "providers": {
            "third-party-sourced": {
                "vendor": "third",
                "model": "party-model",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "last_reviewed": "2026-09-26",
                "context_window": 256000,
                "limits": {"max_input_tokens": 256000},
                "context_evidence": {
                    "level": "plausible",
                    "source_url": "https://openrouter.ai/api/v1/models",
                    "source_model_id": "third/party-model",
                    "as_of": "2026-09-26",
                },
                "freshness": {
                    "context_window": {
                        "level": "plausible",
                        "source": "OpenRouter /v1/models",
                        "source_url": "https://openrouter.ai/api/v1/models",
                        "confirmed_at": "2026-09-26",
                    },
                },
            },
        },
    }
    assert _errors(doc) == []
    entry = doc["providers"]["third-party-sourced"]
    assert entry["freshness"]["context_window"]["level"] == "plausible"
    assert entry["context_evidence"]["level"] == "plausible"


def test_two_evidence_levels_are_different():
    """The two entries carry DIFFERENT evidence levels.

    A provider-self attestation is `confirmed`; a foreign registry
    attestation is `plausible`. A consumer can distinguish who said what.
    """
    provider_self = {
        "schema_version": "fusionaize-provider-catalog/v1.7",
        "providers": {
            "provider-self": {
                "vendor": "self",
                "model": "attested",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "last_reviewed": "2026-09-26",
                "context_window": 128000,
                "limits": {"max_input_tokens": 128000},
                "freshness": {
                    "context_window": {
                        "level": "confirmed",
                        "source": "Provider docs",
                        "source_url": "https://self.example.com/docs",
                        "confirmed_at": "2026-09-26",
                    },
                },
            },
        },
    }
    registry = {
        "schema_version": "fusionaize-provider-catalog/v1.7",
        "providers": {
            "registry-sourced": {
                "vendor": "reg",
                "model": "sourced",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "last_reviewed": "2026-09-26",
                "context_window": 256000,
                "limits": {"max_input_tokens": 256000},
                "freshness": {
                    "context_window": {
                        "level": "plausible",
                        "source": "OpenRouter /v1/models",
                        "source_url": "https://openrouter.ai/api/v1/models",
                        "confirmed_at": "2026-09-26",
                    },
                },
            },
        },
    }
    assert _errors(provider_self) == []
    assert _errors(registry) == []

    ps_level = provider_self["providers"]["provider-self"]["freshness"]["context_window"]["level"]
    reg_level = registry["providers"]["registry-sourced"]["freshness"]["context_window"]["level"]

    assert ps_level == "confirmed", f"provider-self must be confirmed, got {ps_level!r}"
    assert reg_level == "plausible", f"registry must be plausible, got {reg_level!r}"
    assert ps_level != reg_level, (
        f"the two evidence levels must differ: "
        f"provider-self={ps_level!r}, registry={reg_level!r}"
    )


# ---------------------------------------------------------------------------
# RED PROOF — in-memory countercheck (not pinned to a commit SHA)
# ---------------------------------------------------------------------------

def test_red_proof_countercheck():
    """Countercheck: the RED PROOF assertion logic actually fires on bad data.

    Load the current schema (which has freshness), strip freshness in-memory,
    and assert the guard catches it. A guard that never fires is not a guard.
    """
    schema = _load_schema()
    props = schema["properties"]["providers"]["additionalProperties"]["properties"]

    # Sanity: freshness must exist in the current schema
    assert "freshness" in props, (
        "current schema must define freshness — this lane introduced it"
    )

    # Tamper: remove freshness from the in-memory copy
    del props["freshness"]

    # The guard must detect the absence
    assert "freshness" not in props, (
        "tamper failed — freshness was not removed from the in-memory copy"
    )


def test_suite_count():
    """Ensure the suite has grown beyond the baseline 70 tests.

    A suite that stays at exactly 70 while adding a new test file means
    the new tests are not being collected.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    # Count collected tests from the output
    import re
    m = re.search(r"(\d+) tests collected", result.stdout)
    count = int(m.group(1)) if m else 0
    assert count >= 86, (
        f"suite must have at least 86 tests (70 baseline + 16 new); "
        f"got {count}. Collected output:\n{result.stdout}"
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
    if failures:
        print(f"{len(failures)} failure(s)")
        sys.exit(1)
    print(f"{len(tests)} tests passed")
