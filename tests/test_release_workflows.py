"""The release workflows: the production deploy and the public sync run only
on main with the triggering sha, in their environments, with provenance and
rollback; the action prints its own revision. Pinned here (skipped in the
standalone public copy, which has none of these files)."""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[4]
DEPLOY = ROOT / ".github" / "workflows" / "ai-gate-approver-deploy.yml"
SYNC = ROOT / ".github" / "workflows" / "ai-gate-public-sync.yml"
ACTION = ROOT / ".github" / "actions" / "ai-gate" / "action.yml"
if not (DEPLOY.exists() and SYNC.exists()):
    pytest.skip("the release workflows live in the monorepo", allow_module_level=True)


def _load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


def _dump(obj: object) -> str:
    return yaml.safe_dump(obj, width=10**6)


def _runs(job: dict) -> str:
    """The job's shell, verbatim (a YAML dump would escape the quotes)."""
    return "\n".join(str(step.get("run") or "") for step in job.get("steps", []))


def _secrets_in(obj: object) -> set[str]:
    return set(re.findall(r"secrets\.([A-Z_]+)", _dump(obj)))


def _checkouts(job: dict) -> list[dict]:
    return [
        s
        for s in job.get("steps", [])
        if str(s.get("uses", "")).startswith("actions/checkout@")
    ]


def _token_steps(job: dict) -> list[dict]:
    return [
        s
        for s in job.get("steps", [])
        if str(s.get("uses", "")).startswith("actions/create-github-app-token@")
    ]


def test_the_deploy_workflow_runs_on_main_in_its_environment_with_provenance_and_rollback():
    wf = _load(DEPLOY)
    build, deploy = wf["jobs"]["build"], wf["jobs"]["deploy"]
    assert set(wf["jobs"]) == {"build", "deploy"}
    # The shared ECR push role trusts `ref:refs/heads/*` subjects, so the
    # build has NO environment; only the deploy job runs in the environment
    # the deploy role trusts.
    assert "environment" not in build
    assert deploy["environment"] == "ai-gate-approver-deploy"
    assert deploy["needs"] == "build"
    for job in (build, deploy):
        assert job["if"] == "${{ github.ref == 'refs/heads/main' }}"
    (checkout,) = _checkouts(build)
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert not _checkouts(deploy)
    built = _runs(build)
    assert '[[ "$(git rev-parse HEAD)" == "$GITHUB_SHA" ]]' in built
    assert (
        'tag="${GITHUB_SHA:0:12}"' in built
        and '--build-arg "APPROVER_BUILD=$tag"' in built
    )
    # Only the tag crosses jobs (an account id in an output may be withheld).
    assert build["outputs"] == {"tag": "${{ steps.image.outputs.tag }}"}
    job = deploy
    shell = _runs(deploy)
    # Receiver first, worker with the SAME digest, one retry, rollback.
    assert "before_receiver=$(aws lambda get-function" in shell
    assert (
        'update-function-code --function-name "$WORKER" --image-uri "$digest"' in shell
    )
    assert "for attempt in 1 2" in shell
    # Success means the worker RUNS the digest, not just "status Successful",
    # which a failed update call leaves untouched from the old image.
    assert (
        '--function-name "$WORKER" --query \'Code.ResolvedImageUri\' --output text)" == "$digest"'
        in shell
    )
    assert '--function-name "$RECEIVER" --image-uri "$before_receiver"' in shell
    assert wf["concurrency"] == {
        "group": "ai-gate-approver-deploy",
        "cancel-in-progress": False,
    }
    assert not _secrets_in(wf)

    def roles(j: dict) -> list[str]:
        return [
            s["with"]["role-to-assume"]
            for s in j["steps"]
            if str(s.get("uses", "")).startswith(
                "aws-actions/configure-aws-credentials@"
            )
        ]

    assert roles(build) == ["arn:aws:iam::576599896960:role/GitHubOpenIdConnect"]
    assert roles(deploy) == ["arn:aws:iam::576599896960:role/ai-gate-approver-deploy"]


def test_the_deploy_triggers_on_everything_the_image_installs():
    """The image vendors infra/github/ai-gate and `pip install`s it, which
    reads pyproject.toml; a packaging or dependency bump there must ship."""
    # PyYAML reads the bare `on:` key as boolean True.
    paths = _load(DEPLOY)[True]["push"]["paths"]
    assert "ai-gate-approver/**" in paths
    assert "infra/github/ai-gate/aigate/**" in paths
    assert "infra/github/ai-gate/pyproject.toml" in paths
    assert "COPY ai-gate-src/" in (ROOT / "ai-gate-approver" / "Dockerfile").read_text()


def test_the_sync_workflow_runs_on_main_with_the_triggering_sha_and_names_the_rules_that_refuse():
    wf = _load(SYNC)
    (job,) = wf["jobs"].values()
    assert job["if"] == "${{ github.ref == 'refs/heads/main' }}"
    (checkout,) = _checkouts(job)
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    shell = _runs(job)
    assert '[[ "$(git rev-parse HEAD)" == "$GITHUB_SHA" ]]' in shell
    assert "--unified" in shell and "```diff" in shell
    assert "push to ai-gate main refused by repository rules" in shell
    assert "sync from security-integrations@${GITHUB_SHA}" in shell
    assert wf["concurrency"] == {
        "group": "ai-gate-public-sync",
        "cancel-in-progress": False,
    }


def test_the_action_prints_its_own_revision_first():
    action = _load(ACTION)
    first = action["runs"]["steps"][0]
    assert first["name"] == "ai-gate action revision"
    assert first["env"]["ACTION_REF"] == "${{ github.action_ref }}"
    assert "ai-gate action" in first["run"]


def test_every_app_token_mint_names_its_repository_and_explicit_permissions():
    (job,) = _load(SYNC)["jobs"].values()
    (mint,) = _token_steps(job)
    assert mint["with"]["repositories"] == "ai-gate"
    assert mint["with"]["owner"] == "${{ github.repository_owner }}"
    assert {k for k in mint["with"] if k.startswith("permission-")} == {
        "permission-contents",
        "permission-workflows",
        "permission-metadata",
    }
    for deploy_job in _load(DEPLOY)["jobs"].values():
        assert _token_steps(deploy_job) == []  # OIDC only, no App
