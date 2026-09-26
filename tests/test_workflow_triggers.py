"""Tests for workflow trigger branch correctness.

Every GitHub Actions workflow that triggers on push or pull_request to specific
branches must reference a branch that actually exists in the repository.
A trigger targeting a non-existent branch is dead code that never runs, and
a silent no-op workflow is worse than none — it creates the illusion of CI
coverage while actually providing none.

RED PROOF (FAI-247-G): on bcd6ff6 (HEAD before this lane) all three in-scope
workflows reference 'master', which does not exist.  The test_every_workflow_
triggers_on_a_real_branch test MUST fail.

v1.0:
  - Parses .github/workflows/*.yml (excluding catalog-refresh.yml)
  - Extracts branches from on.push.branches and on.pull_request.branches
  - Verifies every listed branch resolves via git show-ref --verify
  - Fails when the branch list is empty (guards against silent pass)
"""

import json
import shutil
import subprocess
import tempfile

import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# Workflows excluded from trigger-branch checking.
# catalog-refresh.yml only triggers on schedule + workflow_dispatch,
# so push/PR branch lists are not applicable.
EXCLUDED = {"catalog-refresh.yml"}


def _workflow_paths():
    """Return all .yml workflow paths except excluded ones."""
    paths = sorted(WORKFLOWS_DIR.glob("*.yml"))
    return [p for p in paths if p.name not in EXCLUDED]


def _branch_exists(name: str) -> bool:
    """Check if a branch exists locally or as origin/<name> via show-ref.

    Uses git show-ref --verify which is strict: it checks for an exact
    ref match and returns non-zero if the ref does not exist.
    """
    for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
        result = subprocess.run(
            ["git", "show-ref", "--verify", ref],
            capture_output=True, text=True, cwd=ROOT,
        )
        if result.returncode == 0:
            return True
    return False


def _get_branches(workflow: dict) -> list[str]:
    """Extract branch lists from on.push.branches and on.pull_request.branches.

    PyYAML (YAML 1.1) parses the bare key ``on:`` as the boolean ``True``,
    so we try both the string key ``"on"`` and the boolean key ``True``.
    """
    branches: list[str] = []
    on = workflow.get("on") or workflow.get(True) or {}
    if isinstance(on, str):
        # Single event string, e.g. on: push — no branch filter
        return []
    if isinstance(on, list):
        # List of event strings — no branch filter
        return []
    for event in ("push", "pull_request"):
        event_config = on.get(event, {})
        if isinstance(event_config, dict):
            event_branches = event_config.get("branches", [])
            if isinstance(event_branches, list):
                branches.extend(event_branches)
    return branches


def test_every_workflow_triggers_on_a_real_branch():
    """Every push/PR branch trigger must name an existing branch.

    On the current HEAD (bcd6ff6) this MUST fail because all three in-scope
    workflows reference 'master', which does not exist as a local or remote
    branch.  The repository uses 'main'.

    Riegel: an empty branch list MUST also fail (a workflow with no branches
    cannot be a valid trigger and would silently pass the check).
    """
    workflows = _workflow_paths()
    assert workflows, f"no workflow files found in {WORKFLOWS_DIR}"

    violations = []
    for path in workflows:
        parsed = yaml.safe_load(path.read_text())
        branches = _get_branches(parsed)
        if not branches:
            violations.append(
                f"{path.name}: push/pull_request branch list is empty — "
                "a workflow with no branches never runs"
            )
            continue
        for branch in branches:
            if not _branch_exists(branch):
                violations.append(
                    f"{path.name}: branch {branch!r} does not exist "
                    "(local or origin/<name>)"
                )

    assert not violations, (
        "Workflow trigger branch violations:\n  " + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# FAI-247-G — CI's "Validate catalog schema" step must actually validate
# ---------------------------------------------------------------------------
# Criterion 2: the test must prove that the SCHEDULED STEP validates — not
# that jsonschema validates.  The approach:
#
#   1. Parse ci.yml, find step "Validate catalog schema"
#   2. Static check: no "skipping", no "optional", calls a real validator
#   3. Dynamic check: run the step's command AS-WRITTEN against the real
#      catalog (must pass) and against a deliberately broken fixture (must
#      fail)
#   4. Riegel: the test MUST fail if the step is missing or its command is
#      empty
#
# RED PROOF: on bcd6ff6 the step echoes "skipping for now" and exits 0.
# The static check catches "skipping" and the test fails with a real
# assertion.  The RED PROOF is demonstrated by running this test against
# bcd6ff6's ci.yml — see the report for the output.
# ---------------------------------------------------------------------------

SCHEMA_PATH = ROOT / "schemas" / "provider-catalog.v1.schema.json"
CATALOG_PATH = ROOT / "providers" / "catalog.v1.json"


def _ci_step_run(name: str) -> str | None:
    """Extract the ``run`` script of a ci.yml step by its name."""
    path = WORKFLOWS_DIR / "ci.yml"
    parsed = yaml.safe_load(path.read_text())
    for step in parsed.get("jobs", {}).get("validate", {}).get("steps", []):
        if step.get("name") == name:
            return step.get("run")
    return None


def test_ci_validate_catalog_schema_step_actually_validates():
    """The 'Validate catalog schema' step in ci.yml must actually check.

    Static checks (Riegel):
      - step exists
      - run script is non-empty
      - no 'skipping' or 'optional' keywords
      - calls a real JSON Schema validator (jsonschema / Draft202012Validator)

    Dynamic checks:
      - run the step's command against the real catalog  -> exit 0
      - run the step's command against a broken fixture  -> exit != 0 with ERROR

    RED PROOF: against bcd6ff6 all static checks fire because the step was
    a no-op that echoed "skipping for now" and always exited 0.
    """
    run = _ci_step_run("Validate catalog schema")
    # Riegel: step must exist
    assert run is not None, (
        "ci.yml must have a step named 'Validate catalog schema'"
    )
    # Riegel: command must not be empty
    assert run.strip(), (
        "Validate catalog schema step must have a non-empty run script"
    )

    run_lower = run.lower()
    # Static: no skipping or optional keywords
    assert "skipping" not in run_lower, (
        "Step contains 'skipping' — it is a no-op, not actual validation"
    )
    assert "optional" not in run_lower, (
        "Step is marked 'optional' — failures would be silently ignored"
    )
    # Static: must call a real JSON Schema validator
    assert "Draft202012Validator" in run or "jsonschema" in run, (
        "Step must call a real JSON Schema validator "
        "(Draft202012Validator or jsonschema)"
    )

    # Dynamic: run the step's command against the real catalog (must pass)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "schemas").mkdir()
        (tmp / "providers").mkdir()
        shutil.copy2(SCHEMA_PATH, tmp / "schemas" / "provider-catalog.v1.schema.json")
        shutil.copy2(CATALOG_PATH, tmp / "providers" / "catalog.v1.json")

        result = subprocess.run(
            ["bash", "-c", run],
            capture_output=True, text=True, cwd=tmp,
        )
        assert result.returncode == 0, (
            f"Real catalog must pass validation, but got exit code "
            f"{result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
        assert "no errors" in result.stdout.lower(), (
            f"Expected success message, got:\n{result.stdout}"
        )

    # Dynamic: run the step's command against a deliberately broken fixture
    # (must fail with a non-zero exit code and ERROR output)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "schemas").mkdir()
        (tmp / "providers").mkdir()
        shutil.copy2(SCHEMA_PATH, tmp / "schemas" / "provider-catalog.v1.schema.json")

        broken = tmp / "providers" / "catalog.v1.json"
        broken.write_text(json.dumps({
            "schema_version": "fusionaize-provider-catalog/v1.4",
            "providers": "not-an-object",
        }))

        result = subprocess.run(
            ["bash", "-c", run],
            capture_output=True, text=True, cwd=tmp,
        )
        assert result.returncode != 0, (
            f"Broken catalog must fail validation, but exited 0:\n{result.stdout}"
        )
        assert "ERROR" in result.stdout, (
            f"Broken catalog must produce ERROR output, got:\n{result.stdout}"
        )


def test_ci_workflow_uses_main():
    """CI workflow must trigger on 'main', not 'master'."""
    path = WORKFLOWS_DIR / "ci.yml"
    parsed = yaml.safe_load(path.read_text())
    branches = _get_branches(parsed)
    assert "master" not in branches, \
        f"ci.yml still references 'master' in branches: {branches}"
    assert "main" in branches, \
        f"ci.yml does not reference 'main' in branches: {branches}"


def test_codeql_workflow_uses_main():
    """CodeQL workflow must trigger on 'main', not 'master'."""
    path = WORKFLOWS_DIR / "codeql.yml"
    parsed = yaml.safe_load(path.read_text())
    branches = _get_branches(parsed)
    assert "master" not in branches, \
        f"codeql.yml still references 'master' in branches: {branches}"
    assert "main" in branches, \
        f"codeql.yml does not reference 'main' in branches: {branches}"


def test_security_workflow_uses_main():
    """Security workflow must trigger on 'main', not 'master'."""
    path = WORKFLOWS_DIR / "security.yml"
    parsed = yaml.safe_load(path.read_text())
    branches = _get_branches(parsed)
    assert "master" not in branches, \
        f"security.yml still references 'master' in branches: {branches}"
    assert "main" in branches, \
        f"security.yml does not reference 'main' in branches: {branches}"
