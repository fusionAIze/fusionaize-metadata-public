"""The two live inputs of the FAI-247-F cleanup run.

The run consumes the collection result of FAI-245-D and the operator's
configured provider list.  Both are kept outside the repository; this
module turns them into the documents ``scripts/retire.py`` reads, so the
acceptance tests exercise the rule against the *real* run rather than
against a hand-written fixture.

``collected.json`` names the confirmed entries in its ``enrichment``
section: an entry is confirmed when at least one registry source returned
models for it.  The catalog entries that appear in no enrichment block got
no model back from any source — those are the ones the retirement rule
exists to retire.

Call :func:`write_inputs` to materialise the two documents.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The collection result of FAI-245-D (a ``fusionaize-collected/v1`` doc).
COLLECTED_PATH = Path.home() / "collected.json"

#: The operator's configured provider list (``recorded_at``/``providers``).
OPERATOR_SOURCE_PATH = Path.home() / "operator-providers.json"

#: The two documents this module writes for ``retire.py``.
CONFIRMATIONS_PATH = Path("/tmp/confirmations.json")
OPERATOR_PATH = Path("/tmp/operator-list.json")


def load_collection(path: Path = COLLECTED_PATH) -> dict[str, Any]:
    """Return the raw collection result."""
    return json.loads(path.read_text())


def load_operator(path: Path = OPERATOR_SOURCE_PATH) -> dict[str, Any]:
    """Return the raw operator list document."""
    return json.loads(path.read_text())


def derive_confirmations(collected: dict, collected_at: str) -> dict:
    """Turn a collection result into a ``--confirmations`` document.

    An entry is *confirmed* when at least one registry source returned at
    least one model for it.  The sources are the registry names that
    answered — ``openrouter``, ``litellm``, ``omniroutes`` — deduplicated
    and sorted so a re-derivation is byte-stable.
    """
    confirmations: dict[str, dict] = {}
    for name, entry in sorted(collected["enrichment"].items()):
        models = entry.get("registry_models", {}).get("value", {})
        sources = sorted({
            src["source"]
            for model in models.values()
            for src in model.get("sources", [])
        })
        if sources:
            confirmations[name] = {
                "sources": sources,
                "last_confirmed_at": collected_at,
            }
    return {
        "collected_at": collected_at,
        "sources": sorted({
            src for entry in confirmations.values() for src in entry["sources"]
        }),
        "confirmations": confirmations,
    }


def write_inputs(
    collected_path: Path = COLLECTED_PATH,
    operator_source_path: Path = OPERATOR_SOURCE_PATH,
) -> tuple[Path, Path]:
    """Write both run inputs and return their paths.

    ``collected_at`` is the collection timestamp of the run: the moment the
    operator list was recorded and the registries were consulted.  Both
    inputs carry it, so the report and the confirmations agree on which
    collection round this is.
    """
    operator_doc = load_operator(operator_source_path)
    collected_at = operator_doc["recorded_at"]
    collected = load_collection(collected_path)

    CONFIRMATIONS_PATH.write_text(
        json.dumps(derive_confirmations(collected, collected_at), indent=2) + "\n"
    )
    OPERATOR_PATH.write_text(
        json.dumps(operator_doc["providers"], indent=2) + "\n"
    )
    return CONFIRMATIONS_PATH, OPERATOR_PATH


if __name__ == "__main__":
    conf, ops = write_inputs()
    print(f"wrote {conf} and {ops}")
