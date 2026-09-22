#!/usr/bin/env python3
"""Classify why a provider has no confirmed context window, and derive what can be derived.

"unconfirmed" collapses four different situations into one word, and a number
attached to a situation that can never be resolved is worse than no number: it
looks like knowledge. This routine separates them and records which one applies,
so a reader — and the router — can tell "nobody has published this yet" from
"this cannot be known from a catalog at all".

The four kinds:

``derivable``          the entry names a model whose cap is known; the window is
                       taken from there and can never outrank its source.
``runtime_dependent``  a local runner or a router: the window is a property of
                       whatever model is loaded or picked per request, so no
                       catalog value can be correct. The recorded number is an
                       operator-overridable floor, not a claim about a model.
``not_applicable``     the entry is not a text model and has no context window.
``unlisted``           a real model that no consulted source publishes. The
                       recorded number keeps its origin and stays an orientation.

Re-runnable and idempotent: it reads the current catalog, decides again from the
same inputs, and writes only what changed.

Usage:
  python3 scripts/classify-context-defaults.py [--write] [--sources DIR]
Without --write it prints the decisions and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

CATALOG = pathlib.Path("providers/catalog.v1.json")

# A provider whose window is decided per request or per loaded model. No catalog
# value can be right for these; the number is the floor this gateway assumes.
RUNTIME_DEPENDENT = {
    "lmstudio": "local runner — the window is whatever model the operator loaded",
    "vllm": "local runner — the window is whatever model the operator loaded",
    "ollama": "local runner — the window is whatever model the operator pulled",
    "litellm": "proxy — the window belongs to the model it is pointed at",
    "kilo-auto-balanced": "router tier — the window belongs to the model picked per request",
    "kilo-auto-frontier": "router tier — the window belongs to the model picked per request",
    "kilo-auto-free": "router tier — the window belongs to the model picked per request",
    "kiro": "aggregator — the window belongs to the model picked per request",
    "claude-code": "oauth harness, not a model endpoint",
}

NOT_APPLICABLE = {
    "openai-images": "image generation — there is no text context window",
}

RANK = {"unconfirmed": 0, "plausible": 1, "confirmed": 2}


def _norm(value: str) -> str:
    return str(value or "").replace(".", "-").lower()


def classify(name: str, entry: dict, caps: dict) -> tuple[str, str, dict | None]:
    """Return (kind, why, derived_from) for one provider."""
    if name in NOT_APPLICABLE:
        return "not_applicable", NOT_APPLICABLE[name], None
    if name in RUNTIME_DEPENDENT:
        return "runtime_dependent", RUNTIME_DEPENDENT[name], None

    index = {_norm(key): (key, fact) for key, fact in caps.items()}
    for candidate in [entry.get("model"), *(entry.get("aliases") or [])]:
        hit = index.get(_norm(candidate))
        if hit is None:
            continue
        key, fact = hit
        cap = fact.get("max_input_tokens")
        if not isinstance(cap, int):
            continue
        return (
            "derivable",
            f"the entry names {key!r}, whose cap this catalog already records",
            {"key": key, "cap": cap, "level": (fact.get("evidence") or {}).get("level", "unconfirmed")},
        )

    return "unlisted", "no consulted source publishes a window for this model", None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    providers, caps = catalog["providers"], catalog.get("model_caps", {})

    changed = 0
    for name in sorted(providers):
        entry = providers[name]
        evidence = entry.get("context_evidence") or {}
        # Only entries that nobody has decided yet. A "plausible" window is the
        # outcome of a considered, per-entry decision with its reasoning recorded
        # in the entry; overwriting that with a mechanical derivation would
        # replace a judgement with a rule and lose the reason it was made.
        if evidence.get("level") != "unconfirmed":
            continue

        kind, why, derived = classify(name, entry, caps)
        before = json.dumps(evidence, sort_keys=True)

        evidence["unknown_kind"] = kind
        evidence["unknown_reason"] = why

        if kind == "derivable" and derived:
            # A derived value can never outrank the fact it came from.
            level = "plausible" if derived["level"] == "confirmed" else derived["level"]
            evidence["level"] = level
            evidence["derived_from"] = f'model_caps["{derived["key"]}"]'
            evidence["derived_from_value"] = derived["cap"]
            if entry.get("context_window") != derived["cap"]:
                # The recorded number and the cap this entry points at disagree.
                # Both are kept: the derivation is the sourced one, and the old
                # figure is named rather than dropped, so the disagreement stays
                # visible instead of being resolved by whoever ran this last.
                evidence["superseded_value"] = entry.get("context_window")
                entry["context_window"] = derived["cap"]
                entry.setdefault("limits", {})["max_input_tokens"] = derived["cap"]

        entry["context_evidence"] = evidence
        if json.dumps(evidence, sort_keys=True) != before:
            changed += 1
        print(f"  {name:22} {kind:18} {why[:64]}")

    print(f"\n  {changed} Eintraege geaendert")
    if args.write and changed:
        CATALOG.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
        print(f"  {CATALOG} geschrieben")
    elif not args.write:
        print("  (Probelauf — nichts geschrieben; --write zum Anwenden)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
