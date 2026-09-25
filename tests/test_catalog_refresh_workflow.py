"""The scheduled refresh proposes, it does not decide.

FAI-245-E's criteria are behavioural: a run opens a pull request with a diff, a
run without a source change produces nothing, and a change with routing effect
is marked. A workflow file cannot be unit-tested end to end, but those three
properties are readable from it — and without a test they are one "simplify
this" away from becoming a silent push to main.
"""

from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "catalog-refresh.yml"


def _workflow() -> dict:
    assert WORKFLOW.exists(), f"{WORKFLOW.name} is missing — the scheduled refresh has no workflow"
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> list[dict]:
    wf = _workflow()
    steps = [s for job in wf.get("jobs", {}).values() for s in job.get("steps", []) or []]
    assert steps, "the workflow has no steps — nothing to check"
    return steps


def test_the_workflow_runs_on_a_schedule_and_by_hand():
    """Scheduled so it keeps up, manual so it can be triggered when it matters."""
    on = _workflow().get("on") or _workflow().get(True)  # PyYAML reads bare `on:` as True
    assert "schedule" in on, "a catalog that only refreshes when someone remembers is the state we are leaving"
    assert "workflow_dispatch" in on, "must be triggerable by hand"


def test_it_opens_a_pull_request_and_never_pushes_to_the_default_branch():
    """A generated catalog is a proposal with a diff, not a fact someone wakes up to."""
    body = WORKFLOW.read_text()
    assert "gh pr create" in body or "create-pull-request" in body, "the run must open a pull request"
    for forbidden in ("git push origin main", "git push origin HEAD:main"):
        assert forbidden not in body, f"the run must not push to the default branch: {forbidden!r}"


def test_every_writing_step_is_gated_on_an_actual_change():
    """A run without a source change produces no PR and no noise."""
    body = WORKFLOW.read_text()
    assert "git diff --quiet" in body, "the run must detect whether anything changed at all"
    writing = [s for s in _steps() if "gh pr create" in str(s.get("run", "")) or "git push" in str(s.get("run", ""))]
    assert writing, "no writing step found — this test would pass on an empty workflow"
    for step in writing:
        assert "changed == 'true'" in str(step.get("if", "")), (
            f"writing step {step.get('name')!r} runs unconditionally; an unchanged run must stay silent"
        )
