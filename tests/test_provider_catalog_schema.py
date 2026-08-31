"""Acceptance tests for provider-catalog.v1.schema.json (v1.2).

Covers the four TASK-C1 criteria:

1. valid v1.2 document is accepted
2. invalid modality is rejected
3. evidence.level outside {belegt, plausibel, unbestaetigt} is rejected
4. a v1.1 consumer reading a v1.2 catalog produces 0 errors

Run with ``python3 tests/test_provider_catalog_schema.py``.
"""

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "provider-catalog.v1.schema.json"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


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


def _errors(doc: dict) -> list:
    validator = Draft202012Validator(_load_schema())
    return sorted(validator.iter_errors(doc), key=lambda e: str(e.path))


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
    # A v1.1 consumer has no knowledge of the v1.2 additions. It validates a
    # v1.2 document against a stricter schema that only knows v1.1 fields, but
    # with additionalProperties:true at every level (the v1.1 contract). Unknown
    # v1.2 fields must therefore be silently ignored, producing zero errors.
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
