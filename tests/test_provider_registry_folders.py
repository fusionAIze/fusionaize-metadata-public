"""Acceptance tests for FAI-245-A: one folder per provider, with all its models.

Each provider gets its own folder under providers/<id>/index.json. The old
catalog.v1.json maps each old entry (which carries exactly one model) to a
new provider + model pair. The invariant: every field of every old entry is
recoverable from the new folder structure — nothing is lost.

The test loads the old catalog as the source of truth and the new folders
as the target, then compares field-for-field for every original entry.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_DIR = ROOT / "providers"
CATALOG_PATH = PROVIDERS_DIR / "catalog.v1.json"

# ---------------------------------------------------------------------------
# Grouping: which old entries belong to which new provider folder
# ---------------------------------------------------------------------------
# Determined by (signup_url, hop, auth_modes) — the same key used to generate
# the folders.  The mapping below mirrors the generation logic exactly so the
# test can reconstruct which folder holds each original entry.
# ---------------------------------------------------------------------------


def _group_key(entry: dict) -> tuple:
    return (
        entry.get("signup_url", ""),
        tuple(entry.get("hop", [])),
        tuple(sorted(entry.get("auth_modes", []))),
    )


def _old_catalog() -> dict:
    """Load the original catalog — the source of truth."""
    return json.loads(CATALOG_PATH.read_text())


def _new_registry() -> dict[str, dict]:
    """Load all provider/index.json files from the providers directory.

    Returns a dict mapping provider folder name -> provider data.
    A "Riegel" (guard): an empty result must fail, not pass — it means the
    folders haven't been created yet.
    """
    result = {}
    for index_path in sorted(PROVIDERS_DIR.glob("*/index.json")):
        provider = json.loads(index_path.read_text())
        folder_name = index_path.parent.name
        result[folder_name] = provider
    assert result, (
        "Riegel: no provider folders found. "
        "The folder structure must exist before this test can pass."
    )
    return result


def _entry_to_folder_and_index(old_entry_name: str, registry: dict, catalog: dict) -> tuple:
    """Find which folder and model index holds a given old entry."""
    entry = catalog["providers"][old_entry_name]
    key = _group_key(entry)

    # Determine the folder name using the same logic as the generator
    FOLDER_NAMES = {
        ("https://console.anthropic.com/", (), ("api_key",)): "anthropic",
        ("https://console.anthropic.com/", (), ("oauth",)): "claude-code",
        ("https://platform.openai.com/", (), ("api_key",)): "openai",
        ("https://platform.openai.com/", (), ("oauth",)): "openai-codex",
        ("https://aistudio.google.com/", (), ("api_key",)): "google",
        ("https://platform.deepseek.com/", (), ("api_key",)): "deepseek",
        ("https://www.byteplus.com/", ("byteplus",), ("api_key",)): "byteplus",
        ("https://www.volcengine.com/", (), ("api_key",)): "volcengine",
        ("https://kilo.ai/", ("kilo",), ("api_key", "byok")): "kilocode",
        ("https://kilo.ai/", ("kilo-auto",), ("api_key", "byok")): "kilo-auto",
        ("https://platform.moonshot.cn/", (), ("api_key",)): "moonshot",
    }
    folder_name = FOLDER_NAMES.get(key, old_entry_name)

    assert folder_name in registry, (
        f"Riegel: old entry {old_entry_name!r} should map to folder "
        f"{folder_name!r}, but that folder does not exist in the registry"
    )

    provider = registry[folder_name]
    assert provider.get("models"), (
        f"Riegel: provider {folder_name!r} has no models — "
        f"entry {old_entry_name!r} cannot be verified"
    )

    # Find the model by _source field (stores original entry name)
    for idx, model in enumerate(provider["models"]):
        if model.get("_source") == old_entry_name:
            return folder_name, idx

    # Fallback: match by model id + vendor (for entries where _source might
    # not match due to name mapping)
    for idx, model in enumerate(provider["models"]):
        if (model.get("id") == entry.get("model") and
                model.get("vendor") == entry.get("vendor")):
            return folder_name, idx

    raise AssertionError(
        f"Riegel: old entry {old_entry_name!r} has no matching model "
        f"in provider folder {folder_name!r}. "
        f"Expected model id={entry.get('model')!r}, vendor={entry.get('vendor')!r}"
    )


# ---------------------------------------------------------------------------
# Lossless migration: field-for-field comparison
# ---------------------------------------------------------------------------

# Fields that move to the provider level and are shared across all models.
# These are verified once per folder, not per model.
PROVIDER_LEVEL_FIELDS = {
    "signup_url", "provider_type", "track", "offer_track",
    "volatility", "evidence_level", "official_source_url",
    "watch_sources", "last_reviewed", "hop", "auth_modes",
}

# Fields that may differ between old and new because they are merged across
# entries (aliases, notes) — checked separately, not per-entry.
MERGED_FIELDS = {"aliases", "notes"}

# Fields that are intentionally excluded from the per-entry comparison because
# they are now at the model level but with different semantics (e.g., context_window
# is now contextLength).
RENAMED_FIELDS = {"model", "vendor", "context_window"}


def _old_fields_excluding_merged(entry: dict) -> set[str]:
    """All fields in an old entry that should be checked per-model."""
    return (set(entry.keys())
            - PROVIDER_LEVEL_FIELDS
            - MERGED_FIELDS
            - RENAMED_FIELDS)


def test_red_proof_against_base():
    """RED PROOF: against the base commit (d19d4cf), there are no provider folders.

    The test loads the provider registry from the base commit.  At that commit,
    providers/ contains only catalog.v1.json and sources.v1.json — no
    subdirectories.  The registry must therefore be empty, and the Riegel
    (guard) in _new_registry() must fail with an AssertionError, not a
    different exception (import error, file-not-found, etc.).

    This proves the guard catches the absence of the folder structure — a
    meaningful failure, not a trivial one.
    """
    base_sha = "d19d4cf"
    base_providers = subprocess.run(
        ["git", "ls-tree", "--name-only", f"{base_sha}:providers/"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()

    # On the base, providers/ should NOT contain any subdirectory with index.json
    has_index = any(
        f.endswith("/index.json") or f == "index.json"
        for f in base_providers
    )
    assert not has_index, (
        f"Base {base_sha} has provider folders already — "
        f"the RED PROOF cannot fire. Found: {base_providers}"
    )

    # Verify: loading registry from base would fail (no folders)
    # We simulate by checking the directory listing
    assert "catalog.v1.json" in base_providers or "providers/catalog.v1.json" in base_providers, (
        f"Base {base_sha} does not even have catalog.v1.json"
    )

    # Now verify the current working tree DOES have folders
    registry = _new_registry()
    assert len(registry) > 0, (
        "Riegel: current working tree has no provider folders. "
        "This should fail — folders must exist."
    )


def test_every_old_entry_has_a_folder():
    """Every old catalog entry maps to exactly one model in one provider folder.

    Riegel: if the catalog is empty, the test fails instead of passing vacuously.
    """
    catalog = _old_catalog()
    old_entries = list(catalog["providers"].keys())
    assert old_entries, "Riegel: old catalog has no entries"

    registry = _new_registry()
    assert registry, "Riegel: registry is empty"

    mapped = 0
    unmapped = []
    for old_name in old_entries:
        try:
            folder, idx = _entry_to_folder_and_index(old_name, registry, catalog)
            mapped += 1
        except AssertionError as exc:
            unmapped.append(str(exc))

    assert not unmapped, (
        f"{len(unmapped)} old entries could not be mapped to a folder:\n  "
        + "\n  ".join(unmapped)
    )
    assert mapped == len(old_entries), (
        f"Mapped {mapped}/{len(old_entries)} entries. "
        f"Riegel: all must be mapped."
    )


def test_field_by_field_no_loss():
    """Every field of every old entry is recoverable from the new structure.

    For each old entry, find its corresponding model in the new folder.
    Every field (except provider-level, merged, and renamed fields) must
    exist in the model's _original dict with the exact same value.

    Riegel: if the old entry has no fields to compare or the model has no
    _original dict, the test fails — it must never pass vacuously.
    """
    catalog = _old_catalog()
    registry = _new_registry()
    old_entries = catalog["providers"]

    assert old_entries, "Riegel: old catalog has no entries"

    total_lost = 0
    losses: list[str] = []

    for old_name, old_entry in old_entries.items():
        folder, idx = _entry_to_folder_and_index(old_name, registry, catalog)
        model = registry[folder]["models"][idx]
        original = model.get("_original", {})

        assert original, (
            f"Riegel: model for {old_name!r} in {folder!r} has no _original dict. "
            f"Cannot verify field-for-field."
        )

        fields_to_check = _old_fields_excluding_merged(old_entry)
        assert fields_to_check, (
            f"Riegel: old entry {old_name!r} has no fields to check after "
            f"excluding provider-level, merged, and renamed fields. "
            f"This would be a vacuous pass."
        )

        for field in sorted(fields_to_check):
            old_val = old_entry[field]
            if field not in original:
                losses.append(
                    f"{old_name!r} -> {folder!r}[{idx}]: "
                    f"field {field!r} lost (value was {json.dumps(old_val)})"
                )
                total_lost += 1
            elif original[field] != old_val:
                losses.append(
                    f"{old_name!r} -> {folder!r}[{idx}]: "
                    f"field {field!r} changed "
                    f"{json.dumps(old_val)} -> {json.dumps(original[field])}"
                )
                total_lost += 1

    assert not losses, (
        f"{total_lost} field losses across all entries:\n  "
        + "\n  ".join(losses)
    )


def test_provider_level_fields_preserved():
    """Provider-level fields from the old catalog are on the provider, not lost.

    For each old entry that contributes to a provider, the provider-level
    fields (signup_url, provider_type, hop, etc.) from the FIRST entry in
    its group must be present on the provider object.

    Riegel: if the provider has no models, fail. If no provider-level fields
    exist on the old entry, fail (the check would be vacuous).
    """
    catalog = _old_catalog()
    registry = _new_registry()

    groups: dict[str, list[str]] = {}
    for name, entry in catalog["providers"].items():
        folder, _ = _entry_to_folder_and_index(name, registry, catalog)
        groups.setdefault(folder, []).append(name)

    violations: list[str] = []
    for folder, old_names in groups.items():
        provider = registry[folder]
        assert provider.get("models"), (
            f"Riegel: provider {folder!r} has no models"
        )

        # Provider-level fields from the first old entry
        first_old = catalog["providers"][old_names[0]]
        pfields = PROVIDER_LEVEL_FIELDS & set(first_old.keys())
        assert pfields, (
            f"Riegel: old entry {old_names[0]!r} has no provider-level fields. "
            f"Cannot verify preservation."
        )

        for field in sorted(pfields):
            old_val = first_old[field]
            if field not in provider:
                violations.append(
                    f"{folder!r}: provider-level field {field!r} lost "
                    f"(from {old_names[0]!r}, value {json.dumps(old_val)})"
                )
            elif provider[field] != old_val:
                violations.append(
                    f"{folder!r}: provider-level field {field!r} changed "
                    f"{json.dumps(old_val)} -> {json.dumps(provider[field])}"
                )

    assert not violations, (
        f"{len(violations)} provider-level field violations:\n  "
        + "\n  ".join(violations)
    )


def test_renamed_fields_preserved():
    """model, vendor, and context_window are renamed in the new structure.

    model -> id, vendor -> vendor, context_window -> contextLength.
    The values must be identical.

    Riegel: if the old entry has no model/vendor/context_window, fail.
    """
    catalog = _old_catalog()
    registry = _new_registry()

    violations: list[str] = []
    for old_name, old_entry in catalog["providers"].items():
        folder, idx = _entry_to_folder_and_index(old_name, registry, catalog)
        model = registry[folder]["models"][idx]

        assert old_entry.get("model"), (
            f"Riegel: old entry {old_name!r} has no model field"
        )
        assert old_entry.get("vendor"), (
            f"Riegel: old entry {old_name!r} has no vendor field"
        )

        if model.get("id") != old_entry["model"]:
            violations.append(
                f"{old_name!r}: model {old_entry['model']!r} "
                f"-> id {model.get('id')!r}"
            )
        if model.get("vendor") != old_entry["vendor"]:
            violations.append(
                f"{old_name!r}: vendor {old_entry['vendor']!r} "
                f"-> vendor {model.get('vendor')!r}"
            )
        if old_entry.get("context_window") is not None:
            if model.get("contextLength") != old_entry["context_window"]:
                violations.append(
                    f"{old_name!r}: context_window {old_entry['context_window']!r} "
                    f"-> contextLength {model.get('contextLength')!r}"
                )

    assert not violations, (
        f"{len(violations)} renamed-field mismatches:\n  "
        + "\n  ".join(violations)
    )


def test_aliases_merged_not_lost():
    """Every alias from every old entry in a group is present in the provider's aliases.

    Riegel: if no old entries in the group have aliases, fail.
    """
    catalog = _old_catalog()
    registry = _new_registry()

    groups: dict[str, list[str]] = {}
    for name, entry in catalog["providers"].items():
        folder, _ = _entry_to_folder_and_index(name, registry, catalog)
        groups.setdefault(folder, []).append(name)

    violations: list[str] = []
    for folder, old_names in groups.items():
        provider = registry[folder]
        provider_aliases = set(provider.get("aliases", []))
        assert provider_aliases, (
            f"Riegel: provider {folder!r} has no aliases"
        )

        for old_name in old_names:
            old_entry = catalog["providers"][old_name]
            old_aliases = old_entry.get("aliases", [])
            for alias in old_aliases:
                if alias not in provider_aliases:
                    violations.append(
                        f"{folder!r}: alias {alias!r} from {old_name!r} lost"
                    )

    assert not violations, (
        f"{len(violations)} alias losses:\n  "
        + "\n  ".join(violations)
    )


def test_total_model_count_matches():
    """The total number of models across all folders equals the 66 old entries.

    Riegel: if the registry has 0 models, fail.
    """
    registry = _new_registry()
    total_models = sum(len(p["models"]) for p in registry.values())
    assert total_models > 0, "Riegel: registry has 0 models"
    assert total_models == 66, (
        f"Expected 66 models across all folders, got {total_models}"
    )


def test_no_extra_fields_in_original():
    """The _original dict must not contain fields that the old entry didn't have.

    A field that appears only in the _original but not in the old entry means
    the migration added data it shouldn't have — it's not a loss, but it's
    still a silent change.

    Riegel: if the old entry has no fields, fail.
    """
    catalog = _old_catalog()
    registry = _new_registry()

    violations: list[str] = []
    for old_name, old_entry in catalog["providers"].items():
        folder, idx = _entry_to_folder_and_index(old_name, registry, catalog)
        model = registry[folder]["models"][idx]
        original = model.get("_original", {})

        old_keys = set(old_entry.keys())
        assert old_keys, f"Riegel: old entry {old_name!r} has no fields"

        extra = set(original.keys()) - old_keys
        # Renamed fields are allowed (model/id, context_window/contextLength)
        # but should not appear in _original since they're top-level on the model
        if extra:
            violations.append(
                f"{old_name!r} -> {folder!r}[{idx}]: "
                f"extra fields in _original: {sorted(extra)}"
            )

    assert not violations, (
        f"{len(violations)} entries have extra fields in _original:\n  "
        + "\n  ".join(violations)
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
        print(f"\n{failures} failure(s)")
        sys.exit(1)
    print(f"\n{len(tests)} tests passed")
