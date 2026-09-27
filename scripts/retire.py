"""Retirement rule: entries no source confirms across a threshold of
collection rounds are retired. Actively configured operator names are
never removed — they stay routable.

Interface contract with FAI-245-D (collection)
----------------------------------------------
The collection produces a JSON file consumed by this script::

    {
      "collected_at": "2026-09-26",
      "sources": ["openrouter", "litellm", "omniroute"],
      "confirmations": {
        "<provider-name>": {
          "sources": ["openrouter"],
          "last_confirmed_at": "2026-09-26"
        }
      }
    }

Every key under ``confirmations`` matches a key in
``providers/catalog.v1.json``.  An entry *not* listed was not confirmed
by any source in this round.

Interface contract with the operator
------------------------------------
The operator provider list is a JSON array of provider names that are
actively configured in the live gateway::

    ["anthropic-claude", "openai-codex", "gemini-flash"]

These names are *never* retired — even when unconfirmed they stay
routable and are reported as spared.

Usage
-----
.. code:: bash

    python scripts/retire.py \\
      --catalog providers/catalog.v1.json \\
      --confirmations collected.json \\
      --operator operator-list.json \\
      --threshold 3 \\
      [--write]

The report is printed to stdout as JSON.  With ``--write`` the catalog
file is updated in place.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any


class OperatorListEmptyError(ValueError):
    """Raised when the operator provider list is empty or missing.

    An empty list is *fatal*, not permissive: the carve-out in
    ``apply_retirement`` is what keeps actively configured routes
    routable, and without it every operator name counts as unconfirmed
    and gets retired — the rule would empty the gateway it exists to
    protect.  Raising makes the refusal impossible for a caller to read
    as success, whether it invokes the script or calls the rule
    directly.
    """


def load_catalog(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def load_confirmations(path: Path) -> dict[str, Any]:
    """Load collected confirmations.

    Returns the full document.  Callers use ``doc["confirmations"]``
    for the per-provider map.
    """
    return json.loads(path.read_text())


def load_operator_list(path: Path) -> set[str]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError("operator list must be a JSON array")
    return set(data)


# ----------------------------------------------------------------------
# Folder sync: the catalog is the source, the folders hold the copies
# ----------------------------------------------------------------------
#
# The catalog is rebuilt by ``scripts/build-catalog.py`` from the provider
# folders; every catalog entry is reconstructed from the ``_original``
# block of the model whose ``_source`` matches its name.  So a change the
# retirement rule makes to the catalog has to be mirrored back into that
# block, or the *next* rebuild silently reverts it.  That is why the sync
# lives here and not in the test: without it the cleanup run is not a
# run, it is an edit that the build undoes.

_RETIREMENT_FIELDS = ("tier_status", "retirement")


def _retirement_state(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the catalog fields that belong in a folder's ``_original``."""
    return {field: entry[field] for field in _RETIREMENT_FIELDS
            if field in entry}


def sync_folders(
    providers_dir: Path,
    catalog: dict[str, Any],
) -> int:
    """Copy ``tier_status`` and ``retirement`` from *catalog* into folders.

    Returns the number of folders whose ``index.json`` was rewritten.
    Only the folders that disagree are touched, so a second call is a
    no-op — the same idempotence ``build-catalog.py`` relies on.
    """
    updated = 0
    for folder in sorted(providers_dir.iterdir()):
        idx_path = folder / "index.json"
        if not folder.is_dir() or not idx_path.exists():
            continue

        provider = json.loads(idx_path.read_text())
        folder_changed = False

        for model in provider.get("models", []):
            state = _retirement_state(
                catalog["providers"].get(model["_source"], {})
            )
            if not state:
                continue

            original = model.get("_original", {})
            if all(original.get(k) == v for k, v in state.items()):
                continue

            for key, value in state.items():
                original[key] = value
            model["_original"] = original
            folder_changed = True

        if folder_changed:
            # ensure_ascii=False: the folders are UTF-8 documents, and the
            # catalog is written the same way.  Escaping here would make
            # the two disagree the moment a note carries a real dash.
            idx_path.write_text(
                json.dumps(provider, indent=2, ensure_ascii=False) + "\n"
            )
            updated += 1

    return updated


def _last_source_info(entry: dict[str, Any], confirmations: dict[str, Any],
                      name: str) -> tuple[str | None, str | None]:
    """Return ``(source_description, date)`` for the last known confirmation.

    Both are ``None`` when *no source has ever confirmed the entry* — an
    entry that appeared in this round's catalog but in no confirmation
    set and with no recorded retirement.  Returning the string
    ``"unknown"`` here would dress that absence up as a source name and
    a date, so ``last_confirming_source`` read ``unknown on unknown``;
    the caller reports the absence explicitly instead.
    """
    if name in confirmations:
        conf = confirmations[name]
        sources = ", ".join(conf.get("sources", []))
        return (sources or None, conf.get("last_confirmed_at"))

    retirement = entry.get("retirement", {})
    if retirement:
        return (
            retirement.get("last_confirmed_by"),
            retirement.get("last_confirmed_at"),
        )
    return (None, None)


#: Entries whose home folder belongs to another lane (FAI-247-C) and must
#: be left exactly as that lane leaves them.  The folder is the source for
#: the catalog rebuild, so any field this rule stamped on such an entry
#: would be reverted — or, worse, resurrected — by the next merge.  The
#: cleanup therefore touches none of them: not their ``retirement`` block,
#: not their ``tier_status``.  This is the same kind of carve-out as the
#: operator list, for the same reason: an entry the rule does not own is
#: an entry the rule does not rewrite.
FOREIGN_ENTRIES = frozenset({
    "byteplus",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-flash-vision-exp",
    "mistral",
    "openrouter-fallback",
})


def apply_retirement(
    catalog: dict[str, Any],
    confirmations: dict[str, Any],
    operator_names: set[str],
    threshold: int,
    foreign_names: frozenset[str] = FOREIGN_ENTRIES,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the retirement rule.

    Returns ``(modified_catalog, report)``.  The catalog is mutated in
    place; the caller is responsible for writing it back.

    *foreign_names* are entries the rule leaves untouched (see
    ``FOREIGN_ENTRIES``); they are not counted as refreshed, spared or
    retired — they are simply not this rule's to write.
    """
    today = date.today().isoformat()
    conf_map = confirmations.get("confirmations", {})
    sources_list = confirmations.get("sources", [])

    report: dict[str, Any] = {
        "run_at": today,
        "threshold": threshold,
        "sources": sources_list,
        "total_entries": len(catalog["providers"]),
        "retired": [],
        "spared": [],
        "revived": [],
        "tracked": [],
        "refreshed": [],
    }

    # ------------------------------------------------------------------
    # Guards: a failed collection retires nothing
    # ------------------------------------------------------------------
    if not sources_list or confirmations.get("failed") is True:
        report["refused"] = True
        report["aborted"] = True
        report["abort_reason"] = (
            "refused: no sources were collected — "
            if not sources_list else "refused: the collection failed — "
        ) + "a rule that retires everything on missing input is worse than no rule"
        return catalog, report

    # ------------------------------------------------------------------
    # --- operator guard: begin ---
    #
    # An EMPTY (or missing) operator list is fatal, not permissive.  The
    # carve-out below is what keeps actively configured routes routable;
    # without it every operator name counts as unconfirmed and is retired.
    # A cleanup that silently empties the gateway is the exact failure
    # this rule exists to prevent, so the rule RAISES instead of
    # returning a report a caller could mistake for success.  ``main()``
    # turns the raise into exit code 2.
    #
    # The markers delimit this guard: the FAI-247-F tests copy this file
    # with everything between them deleted, to prove the guard is the
    # thing that makes the empty list fatal.
    # ------------------------------------------------------------------
    if not operator_names:
        raise OperatorListEmptyError(
            "refused: the operator provider list is empty or missing — "
            "without it every actively configured route would be retired, "
            "so the run is aborted instead of emptying the gateway"
        )
    # --- operator guard: end ---

    for name, entry in catalog["providers"].items():
        # --- Foreign entry: leave it exactly as its owning lane left it ---
        # No confirmation stamp, no miss, no retirement — the folder is the
        # source of truth for these entries and the sync must not write them.
        if name in foreign_names:
            continue

        confirmed_this_round = name in conf_map

        if confirmed_this_round:
            conf = conf_map[name]
            entry_sources = conf.get("sources", [])
            last_at = conf.get("last_confirmed_at", today)

            # Record last confirmation in the entry
            if "retirement" not in entry:
                entry["retirement"] = {}
            entry["retirement"]["misses"] = 0
            entry["retirement"]["last_confirmed_by"] = ", ".join(entry_sources)
            entry["retirement"]["last_confirmed_at"] = last_at

            # Revive if previously retired
            if entry.get("tier_status") == "deprecated":
                entry["tier_status"] = "active"
                entry["retirement"].pop("reason", None)
                entry["retirement"].pop("retired_at", None)
                report["revived"].append({
                    "name": name,
                    "confirmed_by": entry_sources,
                    "revived_at": today,
                })
            else:
                # Freshly confirmed: this is the "refresh" the report counts.
                report["refreshed"].append({
                    "name": name,
                    "confirmed_by": entry_sources,
                    "confirmed_at": last_at,
                })
            continue

        # --- Not confirmed this round ---

        # Never retire operator-configured names
        if name in operator_names:
            report["spared"].append({
                "name": name,
                "reason": "actively configured by operator",
            })
            continue

        # Track / increment misses
        if "retirement" not in entry:
            entry["retirement"] = {}

        # An entry that is already retired stays as it was retired.  Re-
        # running the rule — on the same collection round, or on the very
        # catalog its previous run wrote — must not move the counter: the
        # retirement is a finished event, and ``misses: N`` records the
        # round in which it happened, not a tally that grows forever.
        # Advancing it would make every ``retire.py --write`` rewrite the
        # whole catalog and the rebuild that follows would never settle.
        if entry.get("tier_status") == "deprecated":
            continue

        misses = entry["retirement"].get("misses", 0) + 1
        entry["retirement"]["misses"] = misses

        if misses >= threshold:
            last_source, last_at = _last_source_info(entry, conf_map, name)
            entry["tier_status"] = "deprecated"
            entry["retirement"]["reason"] = (
                f"unconfirmed across {misses} collection rounds "
                f"(threshold: {threshold})"
            )
            entry["retirement"]["retired_at"] = today
            entry["retirement"]["last_confirmed_by"] = last_source
            entry["retirement"]["last_confirmed_at"] = last_at
            report["retired"].append({
                "name": name,
                "reason": entry["retirement"]["reason"],
                # No source ever confirmed this entry, so there is no
                # "last" source to name — say that, rather than emitting
                # the sentinel "unknown on unknown" as if it were a fact.
                "last_confirming_source": (
                    f"{last_source} on {last_at}" if last_source and last_at
                    else None
                ),
                "retired_at": today,
            })
        else:
            report["tracked"].append({
                "name": name,
                "misses": misses,
            })

    # ------------------------------------------------------------------
    # Counts.  ``without_confirmation`` is the size of the bucket that got
    # no source at all (retired + spared + still-tracked), and the three
    # disjoint categories cover every entry this rule owns exactly once:
    #
    #     refreshed + retired + spared + tracked == total_entries - foreign
    #
    # A spared entry *was* unconfirmed, so it belongs to the without-
    # confirmation count as well; it is not retired, which is the point.
    #
    # The foreign entries are counted in ``total_entries`` (the run still
    # sees the whole catalog) but in none of the buckets: they are not
    # this rule's to write, and the partition test below them is what
    # keeps the arithmetic honest — the buckets plus the foreign set cover
    # the catalog and nothing else.
    # ------------------------------------------------------------------
    report["refreshed_count"] = len(report["refreshed"])
    report["retired_count"] = len(report["retired"])
    report["spared_count"] = len(report["spared"])
    report["tracked_count"] = len(report["tracked"])
    report["revived_count"] = len(report["revived"])
    report["without_confirmation"] = (
        report["retired_count"]
        + report["spared_count"]
        + report["tracked_count"]
    )
    report["foreign"] = sorted(set(catalog["providers"]) & foreign_names)
    report["foreign_count"] = len(report["foreign"])

    return catalog, report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the retirement rule to the provider catalog."
    )
    parser.add_argument(
        "--catalog", required=True, type=Path,
        help="Path to providers/catalog.v1.json",
    )
    parser.add_argument(
        "--confirmations", required=True, type=Path,
        help="Path to collected confirmations JSON (from FAI-245-D)",
    )
    parser.add_argument(
        "--operator", required=True, type=Path,
        help="Path to operator provider list JSON",
    )
    parser.add_argument(
        "--threshold", required=True, type=int,
        help="Number of unconfirmed rounds before retirement",
    )
    parser.add_argument(
        "--providers-dir", type=Path, default=None,
        help="Provider folder root to mirror the result into "
             "(default: the directory holding --catalog)",
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Write the modified catalog back to --catalog and mirror "
             "tier_status/retirement into the provider folders",
    )
    args = parser.parse_args()

    catalog = load_catalog(args.catalog)
    confirmations = load_confirmations(args.confirmations)
    operator_names = load_operator_list(args.operator)

    if args.threshold < 1:
        print(json.dumps({"error": "threshold must be >= 1"}, indent=2))
        sys.exit(2)

    try:
        catalog, report = apply_retirement(
            catalog, confirmations, operator_names, args.threshold,
        )
    except OperatorListEmptyError as exc:
        # The refusal is a failure, not an empty report: print the
        # reason so it is on the record and exit non-zero.
        print(json.dumps({"refused": True, "aborted": True,
                          "abort_reason": str(exc)}, indent=2))
        sys.exit(2)

    if args.write and not report.get("aborted"):
        # ensure_ascii=False: the catalog carries real UTF-8 (en dashes,
        # arrows).  Escaping it here would differ from the checked-in
        # file that ``build-catalog.py`` writes from the folders.
        args.catalog.write_text(
            json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"
        )
        providers_dir = args.providers_dir or args.catalog.parent
        report["folders_updated"] = sync_folders(providers_dir, catalog)

    print(json.dumps(report, indent=2))

    # A refused run is a failure, not a no-op: the caller must not read
    # an aborted report as "nothing to retract".  The report still goes
    # to stdout so the reason is on the record.
    if report.get("aborted"):
        sys.exit(2)


if __name__ == "__main__":
    main()
