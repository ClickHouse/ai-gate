"""A repository selecting a reviewer in place of the baseline, by central allow-list.

The allow-list itself is the operator's file (``reviewers.REVIEWER_ALLOWLIST_FILE``);
these tests use throwaway names so they hold for any copy of the gate.
"""

from __future__ import annotations

import pytest

from aigate import reviewers, threads
from aigate.gate import (
    FAILURE,
    PENDING,
    SUCCESS,
    Signal,
    all_required_green,
    decide,
    is_reviewer_run,
)
from aigate.gateconfig import load_gate_config
from aigate.reviewers import ConfigError, load_config, parse_allowlist

ALLOWLIST = """\
reviewers:
  inhouse:
    label: In-house Review
    app_slug: example-review-app
    app_id: 4242
    check_name: "CI / Code Review"
    auto_approve_eligible: false
    has_threads: false
    floor_globs: ["ci/prompts/**"]
repositories:
  Example/Selected:
    replaces_baseline_with: [inhouse]
"""


@pytest.fixture
def allowlisted(monkeypatch):
    extra, replacements = parse_allowlist(ALLOWLIST, dict(reviewers.REVIEWERS))
    for key, rv in extra.items():
        monkeypatch.setitem(reviewers.REVIEWERS, key, rv)
    for repo, keys in replacements.items():
        monkeypatch.setitem(reviewers.BASELINE_REPLACEMENTS, repo, keys)
    return extra["inhouse"]


def _run(rv, conclusion=SUCCESS, *, app_id=None, at="2026-01-01T00:01:00Z"):
    return Signal(
        app_slug=rv.app_slug,
        name=rv.check_name,
        conclusion=conclusion,
        completed_at=at,
        started_at="2026-01-01T00:00:00Z",
        app_id=rv.app_id if app_id is None else app_id,
    )


class TestSelection:
    def test_an_allowlisted_repository_selecting_it_drops_the_baseline(
        self, allowlisted
    ):
        text = "reviewers: [inhouse]\n"
        assert load_config(text, "Example/Selected") == ["inhouse"]
        assert load_config(text, "example/selected") == ["inhouse"]

    def test_any_other_repository_keeps_the_baseline(self, allowlisted):
        text = "reviewers: [inhouse]\n"
        assert load_config(text, "Example/Other") == ["bugbot", "inhouse"]
        assert load_config(text) == ["bugbot", "inhouse"]

    def test_the_repository_file_alone_cannot_drop_the_baseline(self, allowlisted):
        # Listing only the baseline-free set is what selects; on the list but
        # without selecting, the default stays.
        assert load_config(None, "Example/Selected") == ["bugbot"]
        assert load_config("reviewers: [bugbot]\n", "Example/Selected") == ["bugbot"]

    def test_selecting_it_alongside_the_baseline_keeps_both(self, allowlisted):
        text = "reviewers: [bugbot, inhouse]\n"
        assert load_config(text, "Example/Selected") == ["bugbot", "inhouse"]

    def test_the_gate_config_passes_the_repository_through(self, allowlisted):
        required, policy = load_gate_config(
            "reviewers: [inhouse]\n", "Example/Selected"
        )
        assert required == ["inhouse"] and policy is None


class TestTheAllowlistIsStrict:
    @pytest.mark.parametrize(
        "text",
        [
            "reviewers: {x: {label: X, app_slug: a, check_name: c}}\n",  # no app_id
            "reviewers: {x: {label: X, app_slug: a, app_id: '1', check_name: c}}\n",
            "reviewers: {x: {label: X, app_slug: a, app_id: true, check_name: c}}\n",
            "reviewers: {x: {label: X, app_slug: github-actions, app_id: 1, check_name: c}}\n",
            "reviewers: {x: {label: X, app_slug: a, app_id: 1, check_name: c, has_threads: true}}\n",
            "reviewers: {x: {label: X, app_slug: a, app_id: 1, check_name: c, extra: 1}}\n",
            "reviewers: {bugbot: {label: X, app_slug: a, app_id: 1, check_name: c}}\n",
            "repositories: {Example/Selected: {replaces_baseline_with: [ghost]}}\n",
            "repositories: {Example/Selected: {replaces_baseline_with: []}}\n",
            "repositories: {noslash: {replaces_baseline_with: [bugbot]}}\n",
            "repositories: {Example/Selected: {replaces_baseline_with: [bugbot], x: 1}}\n",
            "other: 1\n",
            "[unclosed\n",
            "reviewers: []\n",
            "repositories: [x]\n",
            "reviewers: {x: {label: X, app_slug: a, app_id: 1, check_name: c, floor_globs: false}}\n",
            "reviewers: {x: {label: X, app_slug: a, app_id: 1, check_name: c, floor_globs: 0}}\n",
            "reviewers: {x: {label: X, app_slug: a, app_id: 1, check_name: c, floor_globs: {}}}\n",
            "reviewers: {x: {label: X, app_slug: a, app_id: 1, check_name: c, floor_globs: ''}}\n",
            "reviewers: {x: {label: X, app_slug: a, app_id: 1, check_name: c, floor_globs: [a, '']}}\n",
            "reviewers: {x: {label: X, app_slug: a, app_id: 1, check_name: c, auto_approve_eligible: 1}}\n",
            "reviewers:\n  x:\n    label: X\n    app_slug: a\n    app_id: 1\n    check_name: c\n"
            "    auto_approve_eligible: false\n    auto_approve_eligible: true\n",
        ],
    )
    def test_a_malformed_allowlist_fails_closed(self, text):
        with pytest.raises(ConfigError):
            parse_allowlist(text, dict(reviewers.REVIEWERS))

    def test_eligibility_defaults_to_off(self):
        extra, _ = parse_allowlist(
            "reviewers: {x: {label: X, app_slug: a, app_id: 1, check_name: c}}\n",
            dict(reviewers.REVIEWERS),
        )
        assert extra["x"].auto_approve_eligible is False
        assert extra["x"].has_threads is False


class TestIdentity:
    def test_slug_and_name_without_the_pinned_app_id_do_not_match(self, allowlisted):
        rv = allowlisted
        assert is_reviewer_run(_run(rv), rv)
        assert not is_reviewer_run(_run(rv, app_id=9999), rv)
        no_id = Signal(app_slug=rv.app_slug, name=rv.check_name, conclusion=SUCCESS)
        assert not is_reviewer_run(no_id, rv)

    def test_a_built_in_reviewer_without_a_pin_still_matches_by_slug(self):
        bugbot = reviewers.REVIEWERS["bugbot"]
        run = Signal(app_slug="cursor", name="Cursor Bugbot", conclusion=SUCCESS)
        assert bugbot.app_id is None and is_reviewer_run(run, bugbot)


class TestVerdict:
    def test_success_alone_is_clean(self, allowlisted):
        rv = allowlisted
        assert decide(["inhouse"], [_run(rv)]).outcome == SUCCESS
        ok, _ = all_required_green(["inhouse"], [_run(rv)])
        assert ok

    @pytest.mark.parametrize(
        "conclusion",
        [
            "neutral",
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
            "skipped",
            "stale",
        ],
    )
    def test_every_other_conclusion_is_not_clean(self, allowlisted, conclusion):
        rv = allowlisted
        assert decide(["inhouse"], [_run(rv, conclusion)]).outcome != SUCCESS
        ok, waiting = all_required_green(["inhouse"], [_run(rv, conclusion)])
        assert not ok and waiting

    def test_missing_or_running_is_waiting(self, allowlisted):
        rv = allowlisted
        assert decide(["inhouse"], []).outcome == PENDING
        assert decide(["inhouse"], [_run(rv, PENDING)]).outcome == PENDING

    def test_a_bugbot_run_does_not_stand_in_for_the_selected_reviewer(
        self, allowlisted
    ):
        bugbot = Signal(app_slug="cursor", name="Cursor Bugbot", conclusion=SUCCESS)
        assert decide(["inhouse"], [bugbot]).outcome == PENDING
        assert (
            decide(["inhouse"], [bugbot, _run(allowlisted, "failure")]).outcome
            == FAILURE
        )


class TestNoThreadLedger:
    def test_a_conclusion_only_set_has_no_finding_login(self, allowlisted):
        assert reviewers.thread_reviewer_login(["inhouse"]) is None
        assert reviewers.thread_reviewer_login(["bugbot", "inhouse"]) == "cursor[bot]"

    def test_an_empty_login_keeps_no_thread(self):
        data = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "author": {"login": "dev"},
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "T1",
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "path": "a.py",
                                    "comments": {
                                        "nodes": [{"author": {"login": "cursor"}}]
                                    },
                                }
                            ]
                        },
                    }
                }
            }
        }
        author, findings = threads.parse_review_threads(data, "")
        assert author == "dev" and findings == []


def test_its_instruction_files_are_named_for_the_floor(allowlisted):
    assert reviewers.reviewer_floor_globs(["inhouse"]) == [
        ("ci/prompts/**", "In-house Review reviewer instructions")
    ]
    assert reviewers.reviewer_floor_globs(["bugbot"]) == []


class TestTheGateOnASelectingRepository:
    """``cli.evaluate`` end to end, every GitHub read stubbed."""

    HEAD = "deadbeef"
    SLUG = "Example/Selected"

    def _wire(self, mocker, rv, *, conclusion=SUCCESS, policy=True):
        from aigate import cli, github

        text = "reviewers: [inhouse]\n"
        if policy:
            text += "policy:\n  escalation_paths: []\n"
        mocker.patch.object(github, "read_config_text", return_value=text)
        mocker.patch.object(
            github,
            "signals_for",
            return_value=[
                _run(rv, conclusion),
                # Bugbot reviewed too and left an open finding: it is not this
                # repository's reviewer, so neither its check nor its thread
                # decides anything here.
                Signal(app_slug="cursor", name="Cursor Bugbot", conclusion="neutral"),
            ],
        )
        mocker.patch.object(
            github,
            "open_pull_refs_for_sha",
            return_value=[github.PullRef(1, self.HEAD, "main", "2000-01-01T00:00:00Z")],
        )
        mocker.patch.object(github, "changed_paths", return_value=["src/a.py"])
        mocker.patch.object(github, "pull_reviews", return_value=[])
        mocker.patch.object(github, "pull_author", return_value="dev")
        mocker.patch.object(github, "repo_role", return_value="write")
        mocker.patch.object(
            github,
            "review_threads",
            return_value={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "author": {"login": "dev"},
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "isResolved": False,
                                        "isOutdated": False,
                                        "path": "src/a.py",
                                        "comments": {
                                            "nodes": [{"author": {"login": "cursor"}}]
                                        },
                                    }
                                ],
                            },
                        }
                    }
                }
            },
        )
        mocker.patch.object(cli, "_tied_to_this_pull", side_effect=lambda *a: a[-1])
        return cli, github.PullRef(1, self.HEAD, "main", "2000-01-01T00:00:00Z")

    def test_its_clean_review_passes_even_with_a_bugbot_finding_open(
        self, mocker, allowlisted
    ):
        cli, pull = self._wire(mocker, allowlisted)
        assert cli.evaluate(self.SLUG, pull, "cursor[bot]").outcome == SUCCESS

    def test_its_non_clean_review_is_red_and_no_thread_can_clear_it(
        self, mocker, allowlisted
    ):
        cli, pull = self._wire(mocker, allowlisted, conclusion="failure", policy=False)
        decision = cli.evaluate(self.SLUG, pull, "cursor[bot]")
        assert decision.outcome == FAILURE
        assert "In-house Review" in decision.summary

    def test_another_repository_with_the_same_file_still_needs_bugbot(
        self, mocker, allowlisted
    ):
        cli, pull = self._wire(mocker, allowlisted, policy=False)
        decision = cli.evaluate("Example/Other", pull, "cursor[bot]")
        assert decision.outcome != SUCCESS

    def test_a_clean_review_is_not_held_up_by_threads_it_does_not_have(
        self, mocker, allowlisted
    ):
        # More than 100 unrelated threads, and a thread read that would fail:
        # neither is read, because no required reviewer posts threads.
        from aigate import github

        cli, pull = self._wire(mocker, allowlisted)
        mocker.patch.object(
            github, "review_threads", side_effect=AssertionError("threads read")
        )
        mocker.patch.object(
            github, "read_codeowners", side_effect=AssertionError("codeowners read")
        )
        assert cli.evaluate(self.SLUG, pull, "cursor[bot]").outcome == SUCCESS
