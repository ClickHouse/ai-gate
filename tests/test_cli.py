from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml

from aigate import cli, github
from aigate.gate import FAILURE, PENDING, SUCCESS, Decision, Signal
from aigate.reviewers import REVIEWERS

# When every test pull request was opened: before any reviewer run.
OPENED = "2000-01-01T00:00:00Z"

BUGBOT = REVIEWERS["bugbot"].app_slug


def test_resolve_pull_from_pull_request_event():
    event = {
        "pull_request": {
            "number": 7,
            "head": {"sha": "abc"},
            "base": {"ref": "main"},
            "created_at": OPENED,
        }
    }
    pr = github.resolve_pull(event, "o/r")
    assert (pr.number, pr.head_sha, pr.base_ref) == (7, "abc", "main")


def test_resolve_pull_from_check_run_event_with_pr_attached():
    event = {
        "check_run": {
            "head_sha": "def",
            "pull_requests": [
                {"number": 9, "head": {"sha": "def"}, "base": {"ref": "release/1"}}
            ],
        }
    }
    pr = github.resolve_pull(event, "o/r")
    assert (pr.number, pr.head_sha, pr.base_ref) == (9, "def", "release/1")


def test_resolve_pull_from_status_event_looks_up_by_sha(mocker):
    mocker.patch.object(
        github,
        "gh_json",
        return_value=[
            {
                "number": 11,
                "head": {"sha": "f00"},
                "base": {"ref": "main"},
                "created_at": OPENED,
            }
        ],
    )
    pr = github.resolve_pull({"sha": "f00"}, "o/r")
    assert pr.number == 11


def test_resolve_pull_raises_when_nothing_matches():
    with pytest.raises(github.GitHubError):
        github.resolve_pull({}, "o/r")


def test_read_config_text_returns_none_on_404(mocker):
    proc = mocker.Mock(returncode=1, stdout="", stderr="HTTP 404: Not Found")
    mocker.patch.object(github.subprocess, "run", return_value=proc)
    assert github.read_config_text("o/r", "main", ".github/ai-gate.yml") is None


def test_read_config_text_decodes_and_reads_from_base_ref(mocker):
    body = base64.b64encode(b"reviewers:\n  - bugbot\n").decode()
    proc = mocker.Mock(returncode=0, stdout=f'{{"content":"{body}"}}', stderr="")
    run = mocker.patch.object(github.subprocess, "run", return_value=proc)
    text = github.read_config_text("o/r", "trunk", ".github/ai-gate.yml")
    assert text is not None and "bugbot" in text
    argv = run.call_args.args[0]
    # Must have asked for the base ref, never the PR head.
    assert "ref=trunk" in argv
    # Must force GET: without --method GET, `gh api -f` POSTs, the contents
    # endpoint 404s, and the gate silently treats every config file as absent.
    assert argv[argv.index("--method") + 1] == "GET"


def _patch_event(mocker, slug="o/r", base="main", head="abc"):
    mocker.patch.object(github, "repo_slug", return_value=slug)
    mocker.patch.object(
        github,
        "read_event",
        return_value={
            "pull_request": {
                "number": 1,
                "head": {"sha": head},
                "base": {"ref": base},
                "created_at": OPENED,
            }
        },
    )
    # One pull request on this head SHA, so the same-head fold is a no-op.
    mocker.patch.object(
        github,
        "open_pull_refs_for_sha",
        return_value=[github.PullRef(1, head, base, OPENED)],
    )
    # A red gate lists the reviewer's unresolved findings; none by default.
    mocker.patch.object(github, "review_threads", return_value={})
    # The live PR (re-runs replay the original event): same head, not a draft.
    mocker.patch.object(
        github, "pull_detail", return_value={"head": {"sha": head}, "draft": False}
    )
    # Stub the PR comment I/O. No existing comment by default, and no actor
    # login (the github.token / App-token case).
    mocker.patch.object(github, "list_issue_comments", return_value=[])
    mocker.patch.object(github, "create_issue_comment")
    mocker.patch.object(github, "update_issue_comment")
    mocker.patch.object(github, "current_actor_login", return_value=None)
    # No reviews by default, so the human fallback applies only when a test
    # sets one up.
    mocker.patch.object(github, "pull_reviews", return_value=[])
    mocker.patch.object(github, "pull_author", return_value="author")
    mocker.patch.object(github, "repo_role", return_value="write")
    # Return the check-run stub so a test can assert what was posted.
    return mocker.patch.object(github, "post_check_run")


def test_cmd_gate_green_when_bugbot_clean(mocker):
    _patch_event(mocker)
    mocker.patch.object(
        github, "read_config_text", return_value=None
    )  # default: bugbot
    mocker.patch.object(
        github,
        "signals_for",
        return_value=[
            Signal(app_slug=BUGBOT, name="Cursor Bugbot", conclusion=SUCCESS)
        ],
    )
    assert cli.main(["gate"]) == 0


def test_cmd_gate_red_on_forged_check(mocker):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(
        github,
        "signals_for",
        return_value=[
            Signal(app_slug="github-actions", name="Cursor Bugbot", conclusion=SUCCESS)
        ],
    )
    assert cli.main(["gate"]) == 1


def test_cmd_gate_fails_closed_on_bad_config(mocker):
    post = _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value="reviewers: []\n")
    # signals_for should not even matter; config error fails closed.
    assert cli.main(["gate"]) == 1
    # Even a config error posts a red gate check, not just a failed job.
    assert post.called
    assert post.call_args.args[3] == "completed" and post.call_args.args[4] == "failure"


def test_cmd_gate_posts_green_check_on_clean_review(mocker):
    # A clean review posts a completed/success gate check on the head SHA.
    post = _patch_event(mocker, head="deadbeef")
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(
        github,
        "signals_for",
        return_value=[
            Signal(app_slug=BUGBOT, name="Cursor Bugbot", conclusion=SUCCESS)
        ],
    )
    assert cli.main(["gate"]) == 0
    assert post.called
    _slug, head_sha, name, status, conclusion = post.call_args.args[:5]
    assert name == cli.CHECK_RUN_NAME
    assert head_sha == "deadbeef"
    assert status == "completed" and conclusion == "success"


def test_cmd_gate_posts_in_progress_check_when_waiting(mocker):
    # No review yet -> PENDING -> in_progress with no conclusion.
    post = _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=[])
    assert cli.main(["gate"]) == 1
    assert post.call_args.args[3] == "in_progress" and post.call_args.args[4] is None


def test_cmd_gate_comments_why_a_human_is_required(mocker):
    # A blocked PR explains why on the conversation, not only in a check.
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=[])  # PENDING -> red
    create = github.create_issue_comment
    assert cli.main(["gate"]) == 1
    assert create.called
    slug, number, body = create.call_args.args
    assert (slug, number) == ("o/r", 1)
    assert cli.GATE_COMMENT_MARKER in body
    assert "Waiting for AI review" in body


def test_cmd_gate_skips_comment_on_a_clean_pass_never_blocked(mocker):
    # A PR that was never blocked gets no comment.
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(
        github,
        "signals_for",
        return_value=[
            Signal(app_slug=BUGBOT, name="Cursor Bugbot", conclusion=SUCCESS)
        ],
    )
    assert cli.main(["gate"]) == 0
    assert not github.create_issue_comment.called
    assert not github.update_issue_comment.called


def test_cmd_gate_edits_the_existing_comment_in_place_once_green(mocker):
    # Once the gate clears, the existing comment is edited, not duplicated.
    _patch_event(mocker)
    mocker.patch.object(
        github,
        "list_issue_comments",
        return_value=[
            {
                "id": 555,
                "body": f"{cli.GATE_COMMENT_MARKER}\nold reason",
                "user": {"login": "github-actions[bot]", "type": "Bot"},
            }
        ],
    )
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(
        github,
        "signals_for",
        return_value=[
            Signal(app_slug=BUGBOT, name="Cursor Bugbot", conclusion=SUCCESS)
        ],
    )
    assert cli.main(["gate"]) == 0
    assert not github.create_issue_comment.called
    update = github.update_issue_comment
    assert update.called
    _slug, comment_id, body = update.call_args.args
    assert comment_id == 555
    assert cli.GATE_COMMENT_MARKER in body


def test_cmd_gate_ignores_a_human_planted_marker_comment(mocker):
    # A user comment containing the marker is not the gate's comment, or an
    # author could plant one and hijack which comment gets edited.
    _patch_event(mocker)
    mocker.patch.object(
        github,
        "list_issue_comments",
        return_value=[
            {
                "id": 999,
                "body": f"{cli.GATE_COMMENT_MARKER}\nplanted by the PR author",
                "user": {"login": "some-author", "type": "User"},
            }
        ],
    )
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=[])  # PENDING -> red
    assert cli.main(["gate"]) == 1
    assert not github.update_issue_comment.called
    assert github.create_issue_comment.called


def test_cmd_gate_matches_its_own_comment_when_run_with_a_pat(mocker):
    # A PAT posts as a User, not a Bot; its comment is still matched by login.
    _patch_event(mocker)
    mocker.patch.object(github, "current_actor_login", return_value="aigate-bot-pat")
    mocker.patch.object(
        github,
        "list_issue_comments",
        return_value=[
            {
                "id": 777,
                "body": f"{cli.GATE_COMMENT_MARKER}\nold reason",
                "user": {"login": "aigate-bot-pat", "type": "User"},
            }
        ],
    )
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=[])  # PENDING -> red
    assert cli.main(["gate"]) == 1
    assert not github.create_issue_comment.called
    update = github.update_issue_comment
    assert update.called
    _slug, comment_id, _body = update.call_args.args
    assert comment_id == 777


def test_cmd_gate_falls_back_to_create_when_the_update_fails(mocker):
    # If the existing comment cannot be edited (e.g. it was deleted), post a
    # new one rather than drop the reason.
    _patch_event(mocker)
    mocker.patch.object(
        github,
        "list_issue_comments",
        return_value=[
            {
                "id": 555,
                "body": f"{cli.GATE_COMMENT_MARKER}\nold reason",
                "user": {"login": "github-actions[bot]", "type": "Bot"},
            }
        ],
    )
    mocker.patch.object(
        github, "update_issue_comment", side_effect=github.GitHubError("404")
    )
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=[])  # PENDING -> red
    assert cli.main(["gate"]) == 1
    assert github.update_issue_comment.called
    assert github.create_issue_comment.called


# The action sits at the root of the standalone repository, or under
# .github/actions/ai-gate/ where the package is vendored into a larger one.
_ROOT = Path(__file__).resolve().parents[1]
ACTION = next(
    p
    for p in (
        _ROOT / "action.yml",
        _ROOT.parents[2] / ".github/actions/ai-gate/action.yml",
    )
    if p.exists()
)


def test_action_mode_defaults_to_enforce_and_is_never_interpolated_into_shell():
    action = yaml.safe_load(ACTION.read_text())
    assert action["inputs"]["mode"]["default"] == "enforce"
    step = next(
        s for s in action["runs"]["steps"] if s.get("name") == "Evaluate the gate"
    )
    # The input reaches the script only through env: `${{ inputs.mode }}` inside
    # `run:` would let a caller-supplied value inject shell.
    assert "inputs.mode" not in step["run"]
    assert step["env"]["AIGATE_MODE"] == "${{ inputs.mode }}"
    # wait-seconds likewise goes through env, and defaults to "0" (no wait).
    assert action["inputs"]["wait-seconds"]["default"] == "0"
    assert "inputs.wait-seconds" not in step["run"]
    assert step["env"]["AIGATE_WAIT_SECONDS"] == "${{ inputs.wait-seconds }}"
    # Only the two known modes run the gate; anything else fails the step.
    assert "enforce) exec python -P -m aigate gate ;;" in step["run"]
    assert "shadow)  exec python -P -m aigate gate --shadow ;;" in step["run"]
    assert "exit 1" in step["run"]


def test_gate_folds_across_same_head_prs_and_takes_the_strictest(mocker):
    # Two open PRs share a head: one into main (red), one into a weak base
    # (green). The check is per commit, so the strictest verdict must win.
    from aigate.gate import Decision

    post = _patch_event(mocker, head="c0ffee")
    mocker.patch.object(
        github,
        "open_pull_refs_for_sha",
        return_value=[
            github.PullRef(1, "c0ffee", "main", OPENED),
            github.PullRef(2, "c0ffee", "attacker-base", OPENED),
        ],
    )

    def fake_eval(slug, pull, reviewer_login):
        if pull.base_ref == "main":
            return Decision(
                outcome="failure", title="red", summary="main not satisfied"
            )
        return Decision(outcome=SUCCESS, title="green", summary="weak base green")

    mocker.patch.object(cli, "evaluate", side_effect=fake_eval)
    assert cli.main(["gate"]) == 1
    assert post.call_args.args[3] == "completed" and post.call_args.args[4] == "failure"
    # The posted summary says it was judged across more than one PR.
    assert "across 2 open pull requests" in post.call_args.args[6]


def test_gate_is_green_only_when_every_same_head_pr_is_green(mocker):
    from aigate.gate import Decision

    post = _patch_event(mocker, head="c0ffee")
    mocker.patch.object(
        github,
        "open_pull_refs_for_sha",
        return_value=[
            github.PullRef(1, "c0ffee", "main", OPENED),
            github.PullRef(2, "c0ffee", "release/1", OPENED),
        ],
    )
    mocker.patch.object(
        cli,
        "evaluate",
        side_effect=lambda *a, **k: Decision(outcome=SUCCESS, title="ok", summary="ok"),
    )
    assert cli.main(["gate"]) == 0
    assert post.call_args.args[4] == "success"


def test_changed_paths_includes_rename_sources(mocker):
    # Both paths of a rename count, so renaming a sensitive file does not dodge
    # the risk tier.
    page = [
        {"filename": "utils/helper.py"},
        {"filename": "src/new.py", "previous_filename": "src/auth/token.py"},
    ]
    mocker.patch.object(github, "gh_json", return_value=[page])
    paths = github.changed_paths("o/r", 7)
    assert "src/auth/token.py" in paths
    assert "src/new.py" in paths


# --- in-run wait: a ruleset-required workflow is not re-triggered when the
# reviewer finishes, so the gate waits within the run.


def _pending(title="waiting"):
    return Decision(outcome=PENDING, title=title, summary="reviewer in progress")


def _success(title="clean"):
    return Decision(outcome=SUCCESS, title=title, summary="reviewer passed")


def test_gate_waits_then_greens_when_reviewer_lands(mocker):
    _patch_event(mocker)
    mocker.patch.dict(
        "os.environ", {"AIGATE_WAIT_SECONDS": "40", "AIGATE_POLL_SECONDS": "20"}
    )
    sleep = mocker.patch.object(cli.time, "sleep")
    # First evaluation is PENDING (Bugbot not done); it concludes on the next poll.
    ev = mocker.patch.object(
        cli, "_evaluate_head", side_effect=[_pending(), _success()]
    )
    assert cli.main(["gate"]) == 0
    assert sleep.call_count == 1  # one poll was enough
    assert ev.call_count == 2  # initial + one re-evaluation


def test_gate_wait_times_out_stays_red_with_bounded_polls(mocker):
    _patch_event(mocker)
    mocker.patch.dict(
        "os.environ", {"AIGATE_WAIT_SECONDS": "40", "AIGATE_POLL_SECONDS": "20"}
    )
    sleep = mocker.patch.object(cli.time, "sleep")
    # Reviewer never finishes within the window -> fail closed (red), never hang.
    ev = mocker.patch.object(cli, "_evaluate_head", return_value=_pending())
    assert cli.main(["gate"]) == 1
    assert sleep.call_count == 2  # 40s / 20s poll = exactly two naps, then stop
    assert ev.call_count == 3  # initial + two re-evaluations


def test_gate_without_wait_env_evaluates_once(mocker):
    _patch_event(mocker)
    # No AIGATE_WAIT_SECONDS: evaluate once, never sleep, PENDING is red.
    mocker.patch.dict("os.environ", {"AIGATE_WAIT_SECONDS": ""}, clear=False)
    sleep = mocker.patch.object(cli.time, "sleep")
    ev = mocker.patch.object(cli, "_evaluate_head", return_value=_pending())
    assert cli.main(["gate"]) == 1
    assert sleep.call_count == 0
    assert ev.call_count == 1


def _retryable_failure():
    # Red now, but an approval could still land this run (missing, satisfiable).
    return Decision(
        outcome=FAILURE, title="needs approval", summary="x", retryable=True
    )


def _terminal_failure():
    # Red and will not change without a push (reviewer failed / unacceptable).
    return Decision(outcome=FAILURE, title="red", summary="x", retryable=False)


def test_gate_waits_for_a_late_human_approval(mocker):
    _patch_event(mocker)
    mocker.patch.dict(
        "os.environ", {"AIGATE_WAIT_SECONDS": "40", "AIGATE_POLL_SECONDS": "20"}
    )
    sleep = mocker.patch.object(cli.time, "sleep")
    ev = mocker.patch.object(
        cli, "_evaluate_head", side_effect=[_retryable_failure(), _success()]
    )
    assert cli.main(["gate"]) == 0  # the approval landed during the wait
    assert sleep.call_count == 1
    assert ev.call_count == 2


def test_gate_does_not_wait_on_a_terminal_failure(mocker):
    _patch_event(mocker)
    mocker.patch.dict(
        "os.environ", {"AIGATE_WAIT_SECONDS": "40", "AIGATE_POLL_SECONDS": "20"}
    )
    sleep = mocker.patch.object(cli.time, "sleep")
    ev = mocker.patch.object(cli, "_evaluate_head", return_value=_terminal_failure())
    assert cli.main(["gate"]) == 1
    assert sleep.call_count == 0  # terminal -> no spin
    assert ev.call_count == 1


def _approval(
    login, sha="abc", state="APPROVED", kind="User", at="2026-11-10T12:00:00Z"
):
    return {
        "id": 1,
        "user": {"login": login, "type": kind},
        "state": state,
        "commit_id": sha,
        "submitted_at": at,
    }


def _bugbot(conclusion):
    return [Signal(app_slug=BUGBOT, name="Cursor Bugbot", conclusion=conclusion)]


@pytest.mark.parametrize("ai", [None, FAILURE, PENDING])
def test_human_approval_stands_in_for_a_missing_or_red_ai_review(mocker, ai):
    post = _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(ai) if ai else [])
    mocker.patch.object(github, "pull_reviews", return_value=[_approval("reviewer")])
    assert cli.main(["gate"]) == 0
    assert post.call_args.args[4] == "success"


@pytest.mark.parametrize("ai", [None, FAILURE, PENDING])
def test_human_fallback_success_still_posts_a_comment_with_no_prior_one(mocker, ai):
    # A human-fallback pass always comments, so it does not look like a clean
    # AI review.
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(ai) if ai else [])
    mocker.patch.object(github, "pull_reviews", return_value=[_approval("reviewer")])
    create = mocker.patch.object(github, "create_issue_comment")
    assert cli.main(["gate"]) == 0
    assert create.call_count == 1
    body = create.call_args.args[2]
    assert "standing in for the AI review" in body
    assert "reviewer" in body


def test_human_fallback_comment_names_its_head_and_warns_it_can_go_stale(mocker):
    # Nothing re-runs the gate when the AI review concludes later, so the
    # comment names its commit and says it can go stale.
    _patch_event(mocker, head="c0ffee")
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(FAILURE))
    mocker.patch.object(
        github, "pull_reviews", return_value=[_approval("reviewer", sha="c0ffee")]
    )
    assert cli.main(["gate"]) == 0
    body = _gate_comment()
    assert "c0ffee" in body
    assert "does not re-run on its own" in body


def test_ordinary_failure_comment_has_no_stale_footer(mocker):
    # The stale-comment footer is only for the fallback case.
    post = _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(FAILURE))
    assert cli.main(["gate"]) == 1
    body = _gate_comment()
    assert "does not re-run on its own" not in body
    assert post.call_args.args[4] == "failure"


def test_ordinary_clean_pass_still_gets_no_comment(mocker):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(SUCCESS))
    create = mocker.patch.object(github, "create_issue_comment")
    assert cli.main(["gate"]) == 0
    assert create.call_count == 0


def test_policy_success_after_human_fallback_still_names_the_fallback(mocker):
    # _enforce replaces the whole Decision and knows nothing about the human
    # fallback. The final comment must keep both the fallback's explanation
    # and the policy's outcome.
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(FAILURE))
    mocker.patch.object(github, "pull_reviews", return_value=[_approval("reviewer")])
    mocker.patch.object(cli, "load_gate_config", return_value=(["bugbot"], object()))
    mocker.patch.object(
        cli,
        "_enforce",
        return_value=Decision(
            outcome=SUCCESS,
            title="AI review passed; human sign-off satisfied",
            summary="escalation requirements met",
        ),
    )
    assert cli.main(["gate"]) == 0
    body = _gate_comment()
    assert "standing in for the AI review" in body
    assert "escalation requirements met" in body
    # The policy's "AI review passed" title would contradict the fallback.
    assert "AI review passed" not in body


@pytest.mark.parametrize(
    "review, role",
    [
        (_approval("author"), "admin"),  # the author's own approval
        (_approval("reviewer", sha="old"), "write"),  # approves a stale commit
        (_approval("reviewer", kind="Bot"), "write"),  # a bot, not a human
        (_approval("reviewer", state="COMMENTED"), "write"),  # not an approval
        (_approval("reviewer"), "triage"),  # cannot merge, so cannot stand in
        (_approval("reviewer"), "read"),
        (_approval("reviewer"), None),
    ],
)
def test_human_fallback_rejects_unqualified_approvals(mocker, review, role):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(FAILURE))
    mocker.patch.object(github, "pull_reviews", return_value=[review])
    mocker.patch.object(github, "repo_role", return_value=role)
    assert cli.main(["gate"]) == 1


def test_human_fallback_counts_only_the_latest_stance(mocker):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(FAILURE))
    mocker.patch.object(
        github,
        "pull_reviews",
        return_value=[
            _approval("reviewer", at="2026-11-10T12:00:00Z"),
            _approval("reviewer", state="CHANGES_REQUESTED", at="2026-11-10T12:05:00Z"),
        ],
    )
    assert cli.main(["gate"]) == 1


def test_human_fallback_fails_closed_when_the_author_is_unknown(mocker):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(FAILURE))
    mocker.patch.object(github, "pull_reviews", return_value=[_approval("reviewer")])
    mocker.patch.object(
        github, "pull_author", side_effect=github.GitHubError("unreadable")
    )
    assert cli.main(["gate"]) == 1


def test_human_fallback_keeps_the_ai_verdict_when_a_role_is_unreadable(mocker):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(FAILURE))
    mocker.patch.object(github, "pull_reviews", return_value=[_approval("reviewer")])
    mocker.patch.object(github, "repo_role", side_effect=github.GitHubError("403"))
    assert cli.main(["gate"]) == 1


def test_red_gate_says_a_human_approval_would_satisfy_it(mocker):
    post = _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(FAILURE))
    assert cli.main(["gate"]) == 1
    assert "write access who is not the PR author" in post.call_args.args[6]


def test_evaluate_without_fallback_ignores_human_approval(mocker):
    # human_fallback=False judges on the AI review alone.
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(FAILURE))
    mocker.patch.object(github, "pull_reviews", return_value=[_approval("reviewer")])
    pull = github.PullRef(1, "abc", "main", OPENED)
    assert cli.evaluate("o/r", pull, "cursor", human_fallback=False).outcome == FAILURE
    assert cli.evaluate("o/r", pull, "cursor").outcome == SUCCESS


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"permission": "write", "role_name": "maintain"}, "write"),
        ({"permission": "write", "role_name": "release-manager"}, "write"),
        ({"permission": "read", "role_name": "triage"}, "read"),
        ({"permission": "admin", "role_name": "admin"}, "admin"),
    ],
)
def test_repo_role_reads_the_base_permission_not_the_role_name(
    mocker, payload, expected
):
    # role_name can be a custom role; permission is always a base role.
    mocker.patch.object(github, "gh_json", return_value=payload)
    assert github.repo_role("o/r", "someone") == expected


# --- the gate comment points the author at the exact next step -----------------


def _gate_comment():
    return github.create_issue_comment.call_args.args[2]  # type: ignore[attr-defined]


def test_red_gate_links_each_unresolved_finding(mocker):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot("neutral"))

    def thread(path, url, resolved):
        return {
            "id": path,
            "isResolved": resolved,
            "isOutdated": False,
            "path": path,
            "resolvedBy": None,
            "comments": {"nodes": [{"url": url, "author": {"login": "cursor"}}]},
        }

    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "author": {"login": "author"},
                    "reviewThreads": {
                        "nodes": [
                            thread("src/a.rs", "https://x/1", False),
                            thread("src/b.rs", "https://x/2", True),
                        ]
                    },
                }
            }
        }
    }
    mocker.patch.object(github, "review_threads", return_value=payload)
    assert cli.main(["gate"]) == 1
    body = _gate_comment()
    assert "Unresolved findings (1):" in body
    assert "[src/a.rs](https://x/1)" in body
    assert "src/b.rs" not in body
    # The list sits above the steps, not after them.
    assert body.index("Unresolved findings") < body.index("**What to do:**")


def test_unreadable_threads_keep_the_verdict(mocker):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot("neutral"))
    mocker.patch.object(
        github, "review_threads", side_effect=github.GitHubError("graphql")
    )
    assert cli.main(["gate"]) == 1
    assert "Unresolved findings" not in _gate_comment()


def test_red_gate_links_the_rerun_button(mocker, monkeypatch):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot("neutral"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    assert cli.main(["gate"]) == 1
    assert "[Re-run the gate](https://github.com/o/r/actions/runs/42)" in (
        _gate_comment()
    )


def test_timed_out_wait_says_it_stopped_waiting(mocker, monkeypatch):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(PENDING))
    mocker.patch.object(cli.time, "sleep")
    monkeypatch.setenv("AIGATE_WAIT_SECONDS", "900")
    monkeypatch.setenv("AIGATE_POLL_SECONDS", "900")
    assert cli.main(["gate"]) == 1
    assert "stopped waiting after 15 minutes" in _gate_comment()


def test_config_error_says_fix_it_on_the_base_branch(mocker):
    _patch_event(mocker, base="main")
    mocker.patch.object(github, "read_config_text", return_value="reviewers: []\n")
    assert cli.main(["gate"]) == 1
    body = _gate_comment()
    assert "on `main` in its own pull request" in body
    assert "could not decide" in body


def test_draft_fails_at_once_and_says_mark_it_ready(mocker, monkeypatch):
    _patch_event(mocker)
    github.pull_detail.return_value["draft"] = True  # type: ignore[attr-defined]
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=[])
    sleep = mocker.patch.object(cli.time, "sleep")
    monkeypatch.setenv("AIGATE_WAIT_SECONDS", "900")
    assert cli.main(["gate"]) == 1
    sleep.assert_not_called()
    body = _gate_comment()
    assert "Draft pull request" in body
    assert "Ready for review" in body
    assert "stopped waiting" not in body


def test_draft_that_bugbot_did_review_is_judged_normally(mocker):
    _patch_event(mocker)
    github.pull_detail.return_value["draft"] = True  # type: ignore[attr-defined]
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot("neutral"))
    assert cli.main(["gate"]) == 1
    assert "Cursor Bugbot found issues" in _gate_comment()


def test_rerun_after_marking_ready_reads_the_live_draft_state(mocker, monkeypatch):
    # A re-run replays the original event, still saying draft: true.
    _patch_event(mocker)
    github.read_event.return_value["pull_request"]["draft"] = True  # type: ignore[attr-defined]
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(
        github, "signals_for", side_effect=[_bugbot(PENDING), _bugbot(SUCCESS)]
    )
    mocker.patch.object(cli.time, "sleep")
    monkeypatch.setenv("AIGATE_WAIT_SECONDS", "60")
    assert cli.main(["gate"]) == 0


def test_a_push_mid_wait_stops_the_old_run_and_posts_nothing(mocker, monkeypatch):
    post = _patch_event(mocker, head="old")
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(PENDING))
    sleep = mocker.patch.object(cli.time, "sleep")
    github.pull_detail.return_value = {"head": {"sha": "new"}, "draft": False}  # type: ignore[attr-defined]
    monkeypatch.setenv("AIGATE_WAIT_SECONDS", "900")
    assert cli.main(["gate"]) == 1
    assert sleep.call_count == 1  # stopped at the first poll, not after 15 min
    post.assert_not_called()
    github.create_issue_comment.assert_not_called()  # type: ignore[attr-defined]


def test_a_policy_refusal_is_not_called_a_github_problem(mocker):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", side_effect=lambda s, r, p: None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(SUCCESS))
    mocker.patch.object(cli, "load_gate_config", return_value=(["bugbot"], object()))
    mocker.patch.object(
        cli, "_enforce", side_effect=github.GitHubRefusalError("more than 100 threads")
    )
    assert cli.main(["gate"]) == 1
    body = _gate_comment()
    assert "cannot be judged automatically" in body
    assert "not your change" not in body
    # Re-running hits the same refusal, so it must not promise that it helps.
    assert "then re-run" not in body
    assert "repository administrator" in body


def test_all_threads_resolved_but_still_neutral_says_ask_for_a_fresh_review(mocker):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot("neutral"))
    node = {
        "id": "t",
        "isResolved": True,
        "isOutdated": False,
        "path": "a.rs",
        "resolvedBy": None,
        "comments": {"nodes": [{"url": "u", "author": {"login": "cursor"}}]},
    }
    mocker.patch.object(
        github,
        "review_threads",
        return_value={
            "data": {
                "repository": {
                    "pullRequest": {
                        "author": {"login": "a"},
                        "reviewThreads": {"nodes": [node]},
                    }
                }
            }
        },
    )
    assert cli.main(["gate"]) == 1
    body = _gate_comment()
    assert "threads are resolved" in body
    assert "Fix the findings" not in body  # no contradicting step


def test_unreadable_live_pr_mid_wait_keeps_waiting(mocker, monkeypatch):
    # An unreadable PR is not "pushed past": fail toward judging, not quitting.
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(
        github, "signals_for", side_effect=[_bugbot(PENDING), _bugbot(SUCCESS)]
    )
    mocker.patch.object(cli.time, "sleep")
    mocker.patch.object(github, "pull_detail", side_effect=github.GitHubError("502"))
    monkeypatch.setenv("AIGATE_WAIT_SECONDS", "60")
    assert cli.main(["gate"]) == 0


def test_a_draft_bugbot_is_already_reviewing_is_waited_for(mocker, monkeypatch):
    # e.g. someone posted `bugbot run` on the draft: don't abort that review.
    _patch_event(mocker)
    github.pull_detail.return_value["draft"] = True  # type: ignore[attr-defined]
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(
        github,
        "signals_for",
        side_effect=[_bugbot(PENDING), _bugbot(PENDING), _bugbot(SUCCESS)],
    )
    mocker.patch.object(cli.time, "sleep")
    monkeypatch.setenv("AIGATE_WAIT_SECONDS", "60")
    assert cli.main(["gate"]) == 0


def test_a_policy_refusal_offers_no_rerun_link(mocker, monkeypatch):
    _patch_event(mocker)
    mocker.patch.object(github, "read_config_text", side_effect=lambda s, r, p: None)
    mocker.patch.object(github, "signals_for", return_value=_bugbot(SUCCESS))
    mocker.patch.object(cli, "load_gate_config", return_value=(["bugbot"], object()))
    mocker.patch.object(
        cli, "_enforce", side_effect=github.GitHubRefusalError("more than 100 threads")
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    assert cli.main(["gate"]) == 1
    body = _gate_comment()
    assert "Re-run the gate" not in body
    assert "repository administrator" in body


def test_a_config_error_still_offers_the_rerun_link(mocker, monkeypatch):
    _patch_event(mocker, base="main")
    mocker.patch.object(github, "read_config_text", return_value="reviewers: []\n")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    assert cli.main(["gate"]) == 1
    assert "Re-run the gate" in _gate_comment()
