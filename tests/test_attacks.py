"""End-to-end attempts to pass the gate without a real review of this pull request.

Each test drives ``cli.main(["gate"])`` with GitHub mocked at the ``gh api``
boundary, so the real parsing and decision code runs.
"""

from __future__ import annotations

import json

from aigate import cli, github

SLUG = "o/r"
HEAD = "h" * 40
OPENED = "2026-11-11T10:00:00Z"


def _run(name, conclusion, started, completed, slug="cursor"):
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": started,
        "completed_at": completed,
        "app": {"slug": slug},
    }


def _gate(
    mocker,
    tmp_path,
    check_runs,
    *,
    draft=False,
    other_prs=(),
    closed_heads=(),
    base_changed_at=None,
    search_hits=None,
):
    """Run the gate for PR #1 (opened at OPENED) and return its exit code."""
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "pull_request": {
                    "number": 1,
                    "head": {"sha": HEAD},
                    "base": {"ref": "main"},
                }
            }
        )
    )
    mocker.patch.dict(
        "os.environ", {"GITHUB_EVENT_PATH": str(event), "AIGATE_WAIT_SECONDS": "0"}
    )
    pr = {
        "number": 1,
        "state": "open",
        "draft": draft,
        "created_at": OPENED,
        "head": {"sha": HEAD},
        "base": {"ref": "main"},
        "user": {"login": "author"},
    }
    routes = {
        f"repos/{SLUG}/commits/{HEAD}/check-runs": [{"check_runs": check_runs}],
        f"repos/{SLUG}/commits/{HEAD}/pulls": [pr, *other_prs],
        f"repos/{SLUG}/pulls/1/reviews": [[]],
        f"repos/{SLUG}/pulls/1": pr,
        f"repos/{SLUG}/issues/1/timeline": [
            (
                [{"event": "base_ref_changed", "created_at": base_changed_at}]
                if base_changed_at
                else []
            )
        ],
        # The commit search also finds closed pull requests.
        "search/issues": {
            "items": (
                [{"number": 1}, *({"number": c["number"]} for c in closed_heads)]
                if search_hits is None
                else search_hits
            ),
            "total_count": (
                1 + len(closed_heads) if search_hits is None else len(search_hits)
            ),
        },
    }
    for c in closed_heads:
        routes[f"repos/{SLUG}/pulls/{c['number']}/commits"] = [
            [{"sha": sha} for sha in c.get("commits", [c["head"]["sha"]])]
        ]
        routes[f"repos/{SLUG}/pulls/{c['number']}"] = c

    def gh_json(args):
        for prefix, value in routes.items():
            if args[0].startswith(prefix):
                return value
        raise github.GitHubError(f"gh api {args[0]} failed: HTTP 404: Not Found")

    mocker.patch.object(github, "gh_json", side_effect=gh_json)
    mocker.patch.object(github, "repo_slug", return_value=SLUG)
    mocker.patch.object(github, "read_config_text", return_value=None)
    mocker.patch.object(github, "review_threads", return_value={})
    mocker.patch.object(github, "list_issue_comments", return_value=[])
    mocker.patch.object(github, "create_issue_comment")
    mocker.patch.object(github, "update_issue_comment")
    mocker.patch.object(github, "current_actor_login", return_value=None)
    mocker.patch.object(github, "post_check_run")
    return cli.main(["gate"])


def test_own_clean_review_passes(mocker, tmp_path):
    runs = [
        _run("Cursor Bugbot", "success", "2026-11-11T10:01:00Z", "2026-11-11T10:05:00Z")
    ]
    assert _gate(mocker, tmp_path, runs) == 0


def test_autofix_success_does_not_hide_bugbot_findings(mocker, tmp_path):
    # Cursor's app also posts "Cursor Bugbot Autofix", which concludes success
    # after pushing a fix, possibly to a different branch.
    runs = [
        _run(
            "Cursor Bugbot", "neutral", "2026-11-11T10:01:00Z", "2026-11-11T10:05:00Z"
        ),
        _run(
            "Cursor Bugbot Autofix",
            "success",
            "2026-11-11T10:05:00Z",
            "2026-11-11T10:09:00Z",
        ),
    ]
    assert _gate(mocker, tmp_path, runs) == 1


def test_review_from_an_earlier_pull_request_on_the_same_commit_does_not_count(
    mocker, tmp_path
):
    # The commit was reviewed in PR #2 against another base (a harmless diff),
    # before PR #1 into main was opened.
    runs = [
        _run("Cursor Bugbot", "success", "2026-11-11T09:00:00Z", "2026-11-11T09:04:00Z")
    ]
    closed = {
        "number": 2,
        "state": "closed",
        "head": {"sha": HEAD},
        "base": {"ref": "base-x"},
    }
    assert _gate(mocker, tmp_path, runs, closed_heads=[closed]) == 1
    body = github.post_check_run.call_args.args[6]
    assert "reviewed a different change" in body


def test_draft_with_only_an_earlier_review_does_not_pass(mocker, tmp_path):
    runs = [
        _run("Cursor Bugbot", "success", "2026-11-11T09:00:00Z", "2026-11-11T09:04:00Z")
    ]
    assert _gate(mocker, tmp_path, runs, draft=True) == 1


def test_a_check_named_cursor_bugbot_from_another_app_does_not_count(mocker, tmp_path):
    runs = [
        _run(
            "Cursor Bugbot",
            "success",
            "2026-11-11T10:01:00Z",
            "2026-11-11T10:02:00Z",
            slug="github-actions",
        )
    ]
    assert _gate(mocker, tmp_path, runs) == 1


def test_review_after_opening_but_for_another_pull_request_does_not_count(
    mocker, tmp_path
):
    # PR #2 (same head, harmless diff against another base) was opened, or
    # re-reviewed, after PR #1, then closed. Its clean review is newer than PR
    # #1's opening, so only the shared head gives it away.
    runs = [
        _run("Cursor Bugbot", "success", "2026-11-11T10:30:00Z", "2026-11-11T10:34:00Z")
    ]
    other = {
        "number": 2,
        "state": "closed",
        "head": {"sha": HEAD},
        "base": {"ref": "base-x"},
    }
    assert _gate(mocker, tmp_path, runs, closed_heads=[other]) == 1
    body = github.post_check_run.call_args.args[6]
    assert "also part of #2" in body


def test_review_from_before_a_base_change_does_not_count(mocker, tmp_path):
    # Reviewed clean against a harmless base, then retargeted to main.
    runs = [
        _run("Cursor Bugbot", "success", "2026-11-11T10:01:00Z", "2026-11-11T10:05:00Z")
    ]
    rc = _gate(mocker, tmp_path, runs, base_changed_at="2026-11-11T10:20:00Z")
    assert rc == 1
    assert "reviewed a different change" in github.post_check_run.call_args.args[6]


def test_review_after_a_base_change_counts(mocker, tmp_path):
    runs = [
        _run("Cursor Bugbot", "success", "2026-11-11T10:30:00Z", "2026-11-11T10:34:00Z")
    ]
    assert _gate(mocker, tmp_path, runs, base_changed_at="2026-11-11T10:20:00Z") == 0


def test_decoy_whose_head_moved_on_after_its_review_still_counts_as_shared(
    mocker, tmp_path
):
    # The decoy got its clean review on HEAD, then moved its head on. It still
    # contains HEAD, which is what gives it away.
    runs = [
        _run("Cursor Bugbot", "success", "2026-11-11T10:30:00Z", "2026-11-11T10:34:00Z")
    ]
    moved = {
        "number": 2,
        "state": "open",
        "head": {"sha": "f" * 40},
        "base": {"ref": "base-x"},
        "commits": [HEAD, "f" * 40],
    }
    assert _gate(mocker, tmp_path, runs, closed_heads=[moved]) == 1
    assert "also part of #2" in github.post_check_run.call_args.args[6]


def test_a_search_hit_that_only_mentions_the_commit_does_not_count(mocker, tmp_path):
    runs = [
        _run("Cursor Bugbot", "success", "2026-11-11T10:30:00Z", "2026-11-11T10:34:00Z")
    ]
    mention = {
        "number": 3,
        "state": "open",
        "head": {"sha": "e" * 40},
        "base": {"ref": "main"},
        "commits": ["e" * 40],
    }
    assert _gate(mocker, tmp_path, runs, closed_heads=[mention]) == 0


def test_an_earlier_success_does_not_pass_while_a_review_is_running(mocker, tmp_path):
    # This pull request's own review is still running (for example, just
    # marked ready); a finished success on the same commit must not pass now.
    runs = [
        _run(
            "Cursor Bugbot", "success", "2026-11-11T10:30:00Z", "2026-11-11T10:34:00Z"
        ),
        {
            **_run("Cursor Bugbot", None, "2026-11-11T10:40:00Z", None),
            "status": "in_progress",
        },
    ]
    assert _gate(mocker, tmp_path, runs) == 1


def test_search_that_cannot_see_this_pull_request_fails_closed(mocker, tmp_path):
    runs = [
        _run("Cursor Bugbot", "success", "2026-11-11T10:30:00Z", "2026-11-11T10:34:00Z")
    ]
    assert _gate(mocker, tmp_path, runs, search_hits=[]) == 1
