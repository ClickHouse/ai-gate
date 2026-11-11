from __future__ import annotations

from aigate.gate import FAILURE, PENDING, SUCCESS, Signal, decide
from aigate.reviewers import REVIEWERS

BUGBOT = REVIEWERS["bugbot"].app_slug
BUGBOT_NAME = REVIEWERS["bugbot"].check_name


def bugbot(
    conclusion: str | None,
    *,
    name: str = BUGBOT_NAME,
    slug: str | None = BUGBOT,
    at: str | None = "2026-10-16T00:00:00Z",
) -> Signal:
    return Signal(app_slug=slug, name=name, conclusion=conclusion, completed_at=at)


def test_clean_bugbot_success_passes():
    d = decide(["bugbot"], [bugbot(SUCCESS)])
    assert d.outcome == SUCCESS
    assert d.ok


def test_no_signal_is_pending_not_failure():
    # Bugbot has not posted yet; the gate re-runs when its check completes.
    d = decide(["bugbot"], [])
    assert d.outcome == PENDING
    assert not d.ok


def test_in_progress_is_pending():
    d = decide(["bugbot"], [bugbot(PENDING)])
    assert d.outcome == PENDING


def test_neutral_does_not_pass_under_clean_strictness():
    # Bugbot concludes "neutral" when it found issues.
    d = decide(["bugbot"], [bugbot("neutral")])
    assert d.outcome == FAILURE


def test_failure_fails():
    assert decide(["bugbot"], [bugbot("failure")]).outcome == FAILURE


def test_forged_check_from_another_app_does_not_satisfy_the_gate():
    # Any workflow or app can post a green check named "Cursor Bugbot".
    # Matching on the posting app ignores it.
    forged = Signal(
        app_slug="github-actions",  # NOT Cursor's app
        name=BUGBOT_NAME,  # identical display name
        conclusion=SUCCESS,
        completed_at="2026-10-16T00:00:00Z",
    )
    d = decide(["bugbot"], [forged])
    assert d.outcome == PENDING
    assert not d.ok


def test_a_human_approval_does_not_satisfy_the_gate():
    # A human review is not a signal from Bugbot's app.
    d = decide(["bugbot"], [Signal(app_slug=None, name="review", conclusion=SUCCESS)])
    assert d.outcome == PENDING


def test_latest_signal_wins_over_a_stale_one():
    # A stale neutral followed by a newer success passes; the reverse fails.
    stale_neutral = bugbot("neutral", at="2026-10-16T00:00:00Z")
    fresh_success = bugbot(SUCCESS, at="2026-10-16T01:00:00Z")
    assert decide(["bugbot"], [stale_neutral, fresh_success]).outcome == SUCCESS
    assert decide(["bugbot"], [fresh_success, stale_neutral]).outcome == SUCCESS

    stale_success = bugbot(SUCCESS, at="2026-10-16T00:00:00Z")
    fresh_neutral = bugbot("neutral", at="2026-10-16T01:00:00Z")
    assert decide(["bugbot"], [stale_success, fresh_neutral]).outcome == FAILURE


def test_a_failure_outranks_a_pending_when_both_required():
    # One reviewer failed and one is still running: report the failure.
    r = REVIEWERS
    try:
        r["_t_pass"] = r["bugbot"].__class__(
            "_t_pass", "T-Pass", "t-pass-app", "T Pass"
        )
        r["_t_fail"] = r["bugbot"].__class__(
            "_t_fail", "T-Fail", "t-fail-app", "T Fail"
        )
        signals = [
            Signal(app_slug="t-fail-app", name="T Fail", conclusion="failure"),
            # _t_pass posts nothing yet -> would be pending on its own
        ]
        assert decide(["_t_pass", "_t_fail"], signals).outcome == FAILURE
    finally:
        r.pop("_t_pass", None)
        r.pop("_t_fail", None)


def test_summary_lists_what_passed_and_what_blocked():
    d = decide(["bugbot"], [bugbot("neutral")])
    assert "Cursor Bugbot" in d.summary
    assert "neutral" in d.summary


# --- every non-green verdict says what to do next ------------------------------


def test_findings_say_found_issues_not_neutral_jargon():
    d = decide(["bugbot"], [bugbot("neutral")])
    assert d.title == "Cursor Bugbot found issues"
    assert "found issues that are not resolved" in d.summary
    assert "**What to do:**" in d.summary
    assert "Fix the findings and push" in d.summary


def test_a_cancelled_review_says_how_to_ask_for_a_new_one():
    d = decide(["bugbot"], [bugbot("cancelled")])
    assert d.outcome == FAILURE
    assert "was cancelled" in d.summary
    assert "`bugbot run`" in d.summary
    assert "Fix the findings" not in d.summary


def test_a_missing_review_says_how_to_trigger_one():
    d = decide(["bugbot"], [])
    assert "has not posted a review" in d.summary
    assert "`bugbot run`" in d.summary
    assert "not enabled for this repository" in d.summary
