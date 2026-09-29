"""Retirement rule: entries a source does not confirm across a threshold
of collection rounds are retired. Actively configured operator names are
never removed — they stay routable.

Input contract with FAI-245-D (collection)
------------------------------------------
``scripts/collect/collector.py`` (private ``fusionaize-metadata`` repo)
emits a survey in this shape::

    {
      "schema_version": "fusionaize-collected/v1",
      "enrichment": {
        "<provider-id>": {
          "api_models":      {"value": [...], "evidence": {"level": ..., "source": ...}},
          "registry_models": {"value": {...}, "evidence": {"level": ..., "source": ...}}
        }
      },
      "contradictions": [ ... ],
      "coverage": {"total_providers": 66, "confirmed": 2, "plausible": 57,
                   "unlisted": 7, "by_stage": {...}}
    }

There is no ``sources`` key and no ``confirmations`` key.  The evidence
level per entry is what says whether a provider answered this round:

``confirmed``   a provider API returned its live model list (stage 2)
``plausible``   only mixed registries mention it (stage 1)
``unlisted``    nothing in the survey vouches for it

Only ``confirmed`` counts as a confirmation.  Everything else is a miss.

``adapt_collected_survey`` is the *named adapter* between that shape and
the rule's internal form.  We chose the adapter rather than teaching
``apply_retirement`` the collector's shape directly: the shape is 1.6 MB
of per-model payload with two different ``value`` types (list for
``api_models``, dict for ``registry_models``), and letting that leak
into the rule would tie the retirement decision to details of the
survey's per-model representation that have nothing to do with
retirement.  The adapter narrows the survey to exactly the retirement
question — *was this provider confirmed this round?* — and the rule
keeps working on a small, testable structure.  The legacy
``sources``/``confirmations`` shape is still accepted, so the FAI-247-B
tests and the previously documented interface keep working.

Interface contract with the operator
------------------------------------
The operator provider list is a JSON document with a ``providers`` array
of names configured in the live gateway::

    {"recorded_at": "2026-09-27", "source": "operator live config",
     "providers": ["anthropic-claude", "openai-codex", "gemini-flash"]}

A bare JSON array is also accepted.  Names are *never* retired — even
when unconfirmed they stay routable and are reported as spared.

Operator names resolve against the catalog **by id first, then by
alias**; a name that resolves to neither is a local faigate route name,
not a catalog identity, and is ignored (it is neither an entry to retire
nor a missing catalog entry).

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
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# Evidence level that counts as a confirmation for the round.
CONFIRMED_LEVEL = "confirmed"


def load_catalog(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def load_confirmations(path: Path) -> dict[str, Any]:
    """Load a collected survey (or a legacy confirmation document).

    Returns the full document.  Callers use ``doc["confirmations"]``
    for the per-provider map; ``adapt_collected_survey`` converts the
    collector's real shape into that map.
    """
    return json.loads(path.read_text())


def load_operator_list(path: Path) -> set[str]:
    """Load the operator's configured provider names.

    Accepts the collector-era document ``{"providers": [...]}`` as well
    as a bare JSON array.
    """
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = data.get("providers")
    if not isinstance(data, list):
        raise ValueError(
            "operator list must be a JSON array or an object with a "
            "'providers' array"
        )
    return set(data)


# ---------------------------------------------------------------------------
# Adapter: the collector's survey -> the rule's confirmation map
# ---------------------------------------------------------------------------

def _evidence_level(entry: dict[str, Any]) -> str | None:
    """Best evidence level reported for one enrichment entry.

    ``confirmed`` outranks everything else, so an entry that has both a
    provider API answer (``api_models``) and a registry mention
    (``registry_models``) is confirmed.
    """
    levels = [
        field.get("evidence", {}).get("level")
        for field in entry.values()
        if isinstance(field, dict) and isinstance(field.get("evidence"), dict)
    ]
    if CONFIRMED_LEVEL in levels:
        return CONFIRMED_LEVEL
    for level in levels:
        if level:
            return level
    return None


def _evidence_sources(entry: dict[str, Any]) -> list[str]:
    """The source names behind the entry, in stable order."""
    sources: list[str] = []
    for field in entry.values():
        if not isinstance(field, dict):
            continue
        evidence = field.get("evidence")
        if not isinstance(evidence, dict):
            continue
        for source in str(evidence.get("source") or "").split(","):
            source = source.strip()
            if source and source not in sources:
                sources.append(source)
    return sources


def _survey_round_id(survey: dict[str, Any]) -> str:
    """A stable identifier for *this* survey's content.

    The collector does not record ``collected_at``, so the round cannot
    be identified by a timestamp.  It is identified by the evidence
    itself instead: the per-provider evidence is hashed to a short
    fingerprint.  Two runs over the same evidence yield the same
    fingerprint and the same round; a run whose evidence changed yields
    a new one.  That is what makes the rule a fixed point — re-applying
    it to its own output is the same round, not a new collection.
    """
    relevant = {
        provider_id: {
            field: value.get("evidence")
            for field, value in entry.items()
            if isinstance(value, dict)
        }
        for provider_id, entry in survey.get("enrichment", {}).items()
        if isinstance(entry, dict)
    }
    canonical = json.dumps(
        relevant, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()[:16]


def adapt_collected_survey(survey: dict[str, Any]) -> dict[str, Any]:
    """Named adapter: collector survey -> confirmation document.

    ``confirmed`` entries become entries under ``confirmations``; every
    other entry (``plausible``, ``unlisted``) is a miss.  The result is
    the shape ``apply_retirement`` consumes.  ``round_id`` names the
    round the evidence belongs to; ``collected_at`` is carried through
    only when the survey actually has one.
    """
    confirmations: dict[str, Any] = {}
    for provider_id, entry in survey.get("enrichment", {}).items():
        if not isinstance(entry, dict):
            continue
        if _evidence_level(entry) != CONFIRMED_LEVEL:
            continue
        confirmations[provider_id] = {
            "sources": _evidence_sources(entry),
            "last_confirmed_at": survey.get("collected_at") or date.today().isoformat(),
        }
    return {
        "collected_at": survey.get("collected_at"),
        "round_id": _survey_round_id(survey),
        "sources": sorted({
            source
            for confirmation in confirmations.values()
            for source in confirmation["sources"]
        }),
        "confirmations": confirmations,
    }


def _last_source_info(entry: dict[str, Any], confirmations: dict[str, Any],
                      name: str) -> tuple[str, str]:
    """Return (source_description, date) for the last known confirmation."""
    if name in confirmations:
        conf = confirmations[name]
        sources = ", ".join(conf.get("sources", []))
        last_at = conf.get("last_confirmed_at", "unknown")
        return (sources, last_at)

    retirement = entry.get("retirement", {})
    if retirement:
        return (
            retirement.get("last_confirmed_by", "unknown"),
            retirement.get("last_confirmed_at", "unknown"),
        )
    return ("unknown", "unknown")


def is_collected_survey(document: dict[str, Any]) -> bool:
    """True when ``document`` is a collector survey, not a confirmation map.

    The collector's ``schema_version`` is the reliable marker.  A
    fallback keyed on ``enrichment`` catches a survey whose version
    string is missing, without mistaking a legacy confirmation document
    (which has ``confirmations``) for a survey.
    """
    if not isinstance(document, dict):
        return False
    if isinstance(document.get("schema_version"), str) and "collected" in document["schema_version"]:
        return True
    return "enrichment" in document and "confirmations" not in document


def resolve_operator_ids(
    catalog: dict[str, Any], operator_names: set[str],
) -> tuple[dict[str, str], list[str]]:
    """Resolve operator names to catalog ids, by id first then by alias.

    Returns ``(resolved, unresolved)``.  ``resolved`` maps a catalog id
    to the operator name that addressed it.  ``unresolved`` holds names
    that match no id and no alias — local faigate route names, which are
    *not* catalog identities.
    """
    providers = catalog.get("providers", {})
    alias_owner: dict[str, str] = {}
    for provider_id, entry in providers.items():
        for alias in entry.get("aliases", []):
            # An id wins over any alias: never overwrite a real id.
            if alias not in providers:
                alias_owner[alias] = provider_id

    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for name in sorted(operator_names):
        if name in providers:
            resolved[name] = name
        elif name in alias_owner:
            resolved[alias_owner[name]] = name
        else:
            unresolved.append(name)
    return resolved, unresolved


def apply_retirement(
    catalog: dict[str, Any],
    confirmations: dict[str, Any],
    operator_names: set[str],
    threshold: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the retirement rule.

    ``confirmations`` may be either a collector survey (adapted here by
    ``adapt_collected_survey``) or an already-adapted confirmation
    document.  Returns ``(modified_catalog, report)``.  The catalog is
    mutated in place; the caller is responsible for writing it back.

    Raises ``ValueError`` when the input cannot support a decision: an
    empty survey, a failed collection, or an empty operator list.  A
    rule that reports success while doing nothing hides exactly the
    broken collection it exists to catch.
    """
    today = date.today().isoformat()

    if is_collected_survey(confirmations):
        adapt_notes = "survey adapted from collector shape"
        confirmation_doc = adapt_collected_survey(confirmations)
    else:
        adapt_notes = "confirmation document used as given"
        confirmation_doc = confirmations

    conf_map = confirmation_doc.get("confirmations", {})
    sources_list = confirmation_doc.get("sources", [])
    # The round this input belongs to.  A content fingerprint (collected
    # survey) or the collection timestamp (legacy document) identifies it;
    # a bare confirmation document without either cannot identify a
    # round, and the miss accounting below handles that ``None``.
    round_id = confirmation_doc.get("round_id") or confirmation_doc.get("collected_at")

    report: dict[str, Any] = {
        "run_at": today,
        "threshold": threshold,
        "sources": sources_list,
        "input": adapt_notes,
        "total_entries": len(catalog["providers"]),
        "retired": [],
        "spared": [],
        "revived": [],
        "tracked": [],
    }

    # ------------------------------------------------------------------
    # Guard 1: a failed collection retires nothing
    # ------------------------------------------------------------------
    if confirmations.get("failed") is True:
        report["aborted"] = True
        report["abort_reason"] = (
            "collection failed — a rule that retires everything on missing "
            "input is worse than no rule"
        )
        return catalog, report

    # ------------------------------------------------------------------
    # Guard 2: an input that carries no evidence at all cannot support a
    # retirement decision, so the rule must not make one.
    #
    # The distinction matters.  A run that lists providers but confirms
    # none of them is a legitimate all-miss round: entries are *tracked*,
    # not retired.  A run that contains no evidence whatsoever cannot be
    # told apart from a collection that broke, and the rule must not
    # guess.  It therefore reports the run as aborted with a named
    # reason and touches nothing.
    #
    # Two input shapes reach this guard, and they fail differently on
    # purpose:
    #
    # * Collected survey (the real collector's shape).  ``enrichment`` is
    #   the honest marker — it is empty exactly when the collection
    #   delivered nothing.  The check may not key on ``round_id``: the
    #   adapter always derives a fingerprint from the survey, and an
    #   *empty* survey still hashes to a fingerprint, so that check never
    #   fired for the very case the guard exists for.  This is a broken
    #   collection in the FAI-245-D pipeline: raise.
    # * Legacy confirmation document.  An empty ``sources`` list is that
    #   shape's documented "nothing ran this round" signal, and FAI-247-B
    #   pins it as a *graceful* abort (see the ``aborted``/``abort_reason``
    #   contract in tests/test_retirement_rule.py).  Keep aborting.
    #
    # Previously this guard keyed on the legacy ``sources`` list for both
    # shapes, which the real collector never emits — so against real
    # input the rule silently aborted and reported a green run.
    # ------------------------------------------------------------------
    if is_collected_survey(confirmations):
        if not confirmations.get("enrichment"):
            raise ValueError(
                "empty survey: the collected input contains no evidence for "
                "any provider — refusing to run the retirement rule on it, "
                "because 'nothing confirmed' and 'the collection broke' "
                "cannot be told apart"
            )
    elif not conf_map and not sources_list:
        report["aborted"] = True
        report["abort_reason"] = (
            "no source was collected this round — refusing to retire on "
            "missing input"
        )
        return catalog, report

    # Resolve operator names by id, then alias.  Names that resolve to
    # nothing are local route names, not catalog identities.
    operator_ids, unresolved_operator_names = resolve_operator_ids(
        catalog, operator_names,
    )
    report["operator_names_resolved"] = len(operator_ids)
    report["operator_names_unresolved"] = sorted(unresolved_operator_names)

    # ------------------------------------------------------------------
    # Guard 3: an empty operator list raises, and says why.
    #
    # The operator carve-out is what keeps live routes routable.  With
    # no operator names the rule has no way to tell a genuinely dead
    # entry from one the operator is actively serving, so it may not
    # deprecate anything — retiring on a missing operator list would be
    # the same failure as retiring on a missing survey.
    #
    # This raises rather than returning an ``aborted`` report.  Returning
    # one let the CLI exit 0 and still pass ``--write``, so a run that
    # explicitly refused to act was indistinguishable from a successful
    # one at the process level — and it rewrote the catalog on the way.
    # The docstring above and the CLI's own contract ("name the reason,
    # exit non-zero, write nothing") both require the raise: an input
    # that cannot support a decision is an error, not a shrug.
    # ------------------------------------------------------------------
    if not operator_names:
        raise ValueError(
            "empty operator list: without the operator carve-out the rule "
            "cannot tell a dead entry from a live route the operator is "
            "actively serving — refusing to retire anything"
        )

    for name, entry in catalog["providers"].items():
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
            # The confirmation *is* this round's evidence, so the miss
            # counter must not remember this round as one it already
            # counted — otherwise the next unconfirmed round would look
            # like a repeat and never register, and a retire -> revive ->
            # retire cycle would stall after the first retirement.
            entry["retirement"].pop("last_miss_round", None)

            # Revive if previously retired
            if entry.get("tier_status") == "deprecated":
                entry["tier_status"] = "active"
                entry["retirement"].pop("reason", None)
                entry["retirement"].pop("retired_at", None)
                entry["retirement"].pop("retired_round", None)
                report["revived"].append({
                    "name": name,
                    "confirmed_by": entry_sources,
                    "revived_at": today,
                })
            else:
                # Confirmed this round but not revived — the survey
                # vouches for the entry, so the rule has no reason to
                # retire it.  Report as spared so the exhaustiveness
                # partition is complete.
                report["spared"].append({
                    "name": name,
                    "confirmed_by": entry_sources,
                    "last_confirmed_at": last_at,
                })
            continue

        # --- Not confirmed this round ---

        # Never retire operator-configured names.  Match on the resolved
        # catalog id so that an operator name addressing a provider
        # through an alias is spared too.
        if name in operator_ids:
            report["spared"].append({
                "name": name,
                "operator_name": operator_ids[name],
                "reason": "actively configured by operator",
            })
            continue

        # ------------------------------------------------------------------
        # Fixed point.
        #
        # The survey says which round we are in: an entry is confirmed
        # this round or it is not.  A miss is therefore *recorded*, not
        # incremented — re-running on an unchanged survey must not push
        # an entry from tracked to retired.  Previously ``misses`` was
        # ``previous + 1``, so the rule was counting its own invocations
        # as collection rounds and had no fixed point: the second run on
        # its own output retired entries the first run only tracked.
        #
        # A miss is recorded once per round.  When the round cannot be
        # identified (no ``collected_at`` and no ``round_id`` — a bare
        # confirmation document), repeat calls cannot be told apart from
        # a genuine new collection, so the rule reproduces the last
        # recorded state instead of inventing a miss.
        # ------------------------------------------------------------------
        if "retirement" not in entry:
            entry["retirement"] = {}
        if round_id is None:
            misses = entry["retirement"].get("misses", 0)
        elif entry["retirement"].get("last_miss_round") == round_id:
            misses = entry["retirement"].get("misses", 0)
        else:
            misses = entry["retirement"].get("misses", 0) + 1
            entry["retirement"]["last_miss_round"] = round_id
        entry["retirement"]["misses"] = misses

        if misses >= threshold:
            last_source, last_at = _last_source_info(entry, conf_map, name)
            entry["tier_status"] = "deprecated"
            entry["retirement"]["reason"] = (
                f"unconfirmed across {misses} collection rounds "
                f"(threshold: {threshold})"
            )

            # A retirement is *reported* whenever a run (re-)decides it.
            # The deciding run is named by the round it belongs to, so
            # re-running an unchanged survey does not re-report the same
            # transition — that is the fixed point — while a repeat that
            # is not the same round (an unidentifiable document, a later
            # collection, or a retire-after-revive cycle) reports again.
            # A ``None`` round never matches a recorded round id, which
            # keeps the unidentifiable case reportable.
            reportable = (
                entry["retirement"].get("retired_round") != round_id
            )
            if reportable:
                entry["retirement"]["retired_round"] = round_id
                entry["retirement"]["retired_at"] = today
                entry["retirement"]["last_confirmed_by"] = last_source
                entry["retirement"]["last_confirmed_at"] = last_at
                report["retired"].append({
                    "name": name,
                    "reason": entry["retirement"]["reason"],
                    "last_confirming_source": f"{last_source} on {last_at}",
                    "retired_at": today,
                })
        else:
            report["tracked"].append({
                "name": name,
                "misses": misses,
            })

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
        "--write", action="store_true",
        help="Write the modified catalog back to --catalog",
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
    except ValueError as exc:
        # Loud failure: name the reason, exit non-zero, write nothing.
        print(json.dumps({"error": str(exc)}, indent=2))
        sys.exit(2)

    # An aborted report is not a success.  Guards that abort rather than
    # raise (a failed collection, an empty legacy source set) must not
    # report exit 0 or let ``--write`` touch the catalog: "did nothing"
    # is not "succeeded", and a caller checking ``$?`` would read the
    # refusal as a green run.
    if report.get("aborted"):
        print(json.dumps(report, indent=2))
        sys.exit(2)

    if args.write:
        args.catalog.write_text(json.dumps(catalog, indent=2) + "\n")

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
