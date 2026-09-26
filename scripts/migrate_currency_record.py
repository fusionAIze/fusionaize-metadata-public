#!/usr/bin/env python3
"""Migrate catalog entries from last_reviewed-only to per-field freshness attestations.

This script reads catalog.v1.json and reports which entries carry freshness
attestations for which fields. It does NOT invent attestations — it only
records what is already present in the catalog.

The relationship between last_reviewed and freshness:

  - last_reviewed records that a human looked at the entry on a given date.
    It does not say which field was checked, against which source, or with
    what certainty.

  - freshness.<field> records per-field evidence: who confirmed it (source,
    source_url), when (confirmed_at), and at what evidence level (confirmed /
    plausible / unconfirmed).

  - An entry with last_reviewed but no freshness is valid: last_reviewed
    retains its meaning as a human review date. A freshness attestation does
    NOT overwrite last_reviewed — they are complementary records.

  - When a field carries a freshness attestation, the consumer knows the
    evidence chain behind that field. When it does not, the consumer knows
    the field is unconfirmed.

Run with ``python3 scripts/migrate_currency_record.py``.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "providers" / "catalog.v1.json"


def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


def report(catalog: dict) -> dict:
    """Report migration status. Returns stats dict."""
    providers = catalog["providers"]
    total = len(providers)

    entries_with_freshness = 0
    entries_with_last_reviewed = 0
    fields_attested = 0
    fields_by_level: dict[str, int] = {}
    entries_touched = 0

    for key, entry in providers.items():
        has_lr = "last_reviewed" in entry
        if has_lr:
            entries_with_last_reviewed += 1

        freshness = entry.get("freshness", {})
        if freshness:
            entries_with_freshness += 1
            entries_touched += 1
            for field, attestation in freshness.items():
                fields_attested += 1
                level = attestation.get("level", "unknown")
                fields_by_level[level] = fields_by_level.get(level, 0) + 1

    return {
        "total_entries": total,
        "entries_with_last_reviewed": entries_with_last_reviewed,
        "entries_with_freshness": entries_with_freshness,
        "entries_touched": entries_touched,
        "fields_attested": fields_attested,
        "fields_by_level": fields_by_level,
    }


def main() -> None:
    catalog = load_catalog()
    stats = report(catalog)

    print(f"Catalog: {stats['total_entries']} entries")
    print(f"With last_reviewed: {stats['entries_with_last_reviewed']}")
    print(f"With freshness:    {stats['entries_with_freshness']}")
    print(f"Entries touched by migration: {stats['entries_touched']}")
    print(f"Fields attested:   {stats['fields_attested']}")
    if stats["fields_by_level"]:
        print(f"By evidence level: {stats['fields_by_level']}")
    print()
    print("Migration did NOT invent any attestations.")
    print(
        "last_reviewed retains its meaning; freshness is additive and "
        "carries per-field provenance."
    )


if __name__ == "__main__":
    main()
