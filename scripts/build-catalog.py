#!/usr/bin/env python3
"""Build catalog.v1.json from provider folders under providers/<id>/index.json.

Each folder contributes a provider with connection metadata and a list of
models.  The output catalog reconstructs the old per-entry format where each
model from each source provider becomes a separate entry keyed by ``_source``.

Invariant: running this script without changing any provider folder must
produce zero diff against the checked-in catalog.v1.json.

Supplementary sections (``model_caps``, ``model_versions``, ``routing_modes``)
are preserved from the existing catalog — they are maintained independently
of the folder structure.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_DIR = ROOT / "providers"
CATALOG_PATH = PROVIDERS_DIR / "catalog.v1.json"

# ---------------------------------------------------------------------------
# Supplementary sections — maintained independently of the folder structure
# ---------------------------------------------------------------------------
_SUPPLEMENTARY_SECTIONS = ("model_caps", "model_versions", "routing_modes")

# ---------------------------------------------------------------------------
# Field ordering for generated entries
# ---------------------------------------------------------------------------
# "Standard" entries carry pricing and follow the canonical vendor-entry order.
# "Proxy" entries (no pricing, entry_type="model") use a different ordering
# inherited from the pre-folder catalog.
# ---------------------------------------------------------------------------

_STANDARD_FIELD_ORDER = [
    "aliases",
    "track",
    "offer_track",
    "provider_type",
    "tier_status",
    "expires_at",
    "auth_modes",
    "volatility",
    "evidence_level",
    "evidence_level_note",
    "modalities",
    "evidence",
    "official_source_url",
    "signup_url",
    "watch_sources",
    "notes",
    "last_reviewed",
    "pricing",
    "capacity",
    "input_modalities",
    "output_modalities",
    "entry_type",
    "vendor",
    "model",
    "hop",
    "variant",
    "context_window",
    "limits",
    "context_evidence",
    "context_window_min_measured",
    "context_measurement",
]

_PROXY_FIELD_ORDER = [
    "aliases",
    "track",
    "offer_track",
    "provider_type",
    "auth_modes",
    "volatility",
    "notes",
    "entry_type",
    "context_window",
    "limits",
    "context_evidence",
    "vendor",
    "model",
    "hop",
    "evidence_level",
    "evidence_level_note",
    "official_source_url",
    "signup_url",
    "watch_sources",
    "last_reviewed",
]


def _is_proxy_entry(provider: dict, original: dict) -> bool:
    """Return True if this entry is a "proxy" (model reference, no pricing).

    A proxy entry has entry_type="model" and carries no pricing block — it
    describes a model reachable through an aggregator rather than a direct
    vendor endpoint.
    """
    has_pricing = "pricing" in original or "pricing" in provider
    return not has_pricing


def _ordered_entry(fields: dict, is_proxy: bool) -> dict:
    """Return *fields* in the canonical order for this entry type."""
    order = _PROXY_FIELD_ORDER if is_proxy else _STANDARD_FIELD_ORDER
    ordered = {}
    for key in order:
        if key in fields:
            ordered[key] = fields[key]
    # Any field not in the canonical order goes at the end, alphabetically.
    for key in sorted(fields):
        if key not in ordered:
            ordered[key] = fields[key]
    return ordered


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------


def _reconstruct_entry(provider: dict, model: dict) -> dict:
    """Reconstruct an old-style catalog entry from a provider folder + model."""
    entry: dict = {}

    # Provider-level fields
    for field in (
        "track",
        "offer_track",
        "provider_type",
        "auth_modes",
        "volatility",
        "evidence_level",
        "evidence_level_note",
        "official_source_url",
        "signup_url",
        "watch_sources",
        "last_reviewed",
        "hop",
        "notes",
    ):
        if field in provider:
            entry[field] = provider[field]

    # Aliases (provider-level, shared across all models in the folder)
    if "aliases" in provider:
        entry["aliases"] = provider["aliases"]

    # Model-level fields from _original override provider-level where they
    # overlap (e.g. tier_status, entry_type, expires_at).
    original = model.get("_original", {})

    for field in (
        "pricing",
        "capacity",
        "input_modalities",
        "output_modalities",
        "modalities",
        "evidence",
        "entry_type",
        "tier_status",
        "expires_at",
        "evidence_level_note",
        "context_window_min_measured",
        "context_measurement",
    ):
        if field in original:
            entry[field] = original[field]

    # Renamed fields  (old-name → new-name)
    entry["vendor"] = model["vendor"]
    entry["model"] = model["id"]

    # variant
    if model.get("variant") is not None:
        entry["variant"] = original.get("variant", model["variant"])

    # context_window ← model.contextLength
    entry["context_window"] = model["contextLength"]

    # limits
    if "limits" in original:
        entry["limits"] = original["limits"]

    # context_evidence
    if "context_evidence" in original:
        entry["context_evidence"] = original["context_evidence"]

    is_proxy = _is_proxy_entry(provider, original)
    return _ordered_entry(entry, is_proxy)


def build_catalog(providers_dir: Path) -> dict:
    """Build the full catalog dict from provider folders.

    Supplementary sections (model_caps, model_versions, routing_modes) are
    preserved from the existing catalog if present.  The ``generated_at``
    timestamp is also preserved so that a no-change run produces zero diff.
    Returns a dict ready to serialise as catalog.v1.json.
    """
    # Load the existing catalog to preserve supplementary sections
    existing = {}
    if CATALOG_PATH.exists():
        existing = json.loads(CATALOG_PATH.read_text())

    catalog: dict = {
        "schema_version": "fusionaize-provider-catalog/v1.4",
        "generated_at": existing.get(
            "generated_at",
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
        "source_repo": "fusionaize-metadata-public",
        "providers": {},
    }

    # Preserve supplementary sections
    for section in _SUPPLEMENTARY_SECTIONS:
        if section in existing:
            catalog[section] = existing[section]

    folders = sorted(
        p
        for p in sorted(providers_dir.iterdir())
        if p.is_dir() and (p / "index.json").exists()
    )

    for folder in folders:
        provider = json.loads((folder / "index.json").read_text())
        for model in provider.get("models", []):
            source_name = model["_source"]
            catalog["providers"][source_name] = _reconstruct_entry(
                provider, model
            )

    return catalog


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    catalog = build_catalog(PROVIDERS_DIR)
    CATALOG_PATH.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"Wrote {CATALOG_PATH}")


if __name__ == "__main__":
    main()
