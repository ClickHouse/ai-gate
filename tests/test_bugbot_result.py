"""Reading what a Cursor Bugbot run did from its last result line."""

from __future__ import annotations

import pytest

from aigate.gate import BugbotResult, bugbot_result

# Live summaries (output.summary of Cursor Bugbot check runs), trimmed to the
# progress lines and the result line.
LIVE = {
    "found one": (
        "Bugbot Analysis Progress (8m 5s elapsed)\n\n"
        "✅ Gathered PR context (3s)\n✅ Completed bug detection (7m 59s)\n"
        "✅ Posted analysis results (4s)\n\n"
        "**Final Result:** Bugbot completed review and found 1 potential issue.\n\n"
        "Request ID: a1c06836-07ff-4fc9-8142-273ca524882c\n",
        BugbotResult("found", 1),
    ),
    "found two": (
        "**Final Result:** Bugbot completed review and found 2 potential issues.\n",
        BugbotResult("found", 2),
    ),
    "found one, three carried": (
        "**Final Result:** Bugbot completed review and found 1 potential issue. "
        "3 previously reported issues remain unresolved.\n",
        BugbotResult("found", 1),
    ),
    "none new, one carried": (
        "**Final Result:** Bugbot completed review - no issues found. "
        "1 previously reported issue remain unresolved.\n",
        BugbotResult("none"),
    ),
    "cancelled, base changed": (
        "Bugbot Analysis Progress (6m 51s elapsed)\n\n"
        "✅ Gathered PR context (16s)\n✅ ❌ Run was cancelled (1s)\n\n"
        "**Result:** Bugbot run cancelled because the base branch changed and the "
        "reviewed diff is no longer current\n\n"
        "Request ID: 105194cd-8c67-4152-b65f-b681d22e93e4\n",
        BugbotResult("cancelled"),
    ),
    "cancelled, request cancelled": (
        "**Result:** Bugbot run cancelled because the review request was cancelled\n",
        BugbotResult("cancelled"),
    ),
}


@pytest.mark.parametrize("name", list(LIVE))
def test_the_live_summaries_are_read(name):
    summary, expected = LIVE[name]
    assert bugbot_result(summary) == expected


@pytest.mark.parametrize(
    "summary",
    [
        # A result line split over two lines does not parse as one.
        "**Final Result:** Bugbot completed review -\nno issues found.\n",
        "**Final Result:**\nBugbot completed review - no issues found.\n",
        "**Result:** Bugbot run cancelled because the base branch changed and\n"
        "the reviewed diff is no longer current\n",
        # Clean wording with more on the same line is not the clean line.
        "**Final Result:** Bugbot failed; no issues found.\n",
        "**Final Result:** Bugbot completed review - no issues found. Bugbot "
        "failed to post 2 issues.\n",
        "**Result:** Bugbot run cancelled because the review request was "
        "cancelled; found 0 potential issues\n",
        # The wording without its result label, or under the other label.
        "Bugbot completed review - no issues found.\n",
        "**Result:** Bugbot completed review - no issues found.\n",
        "**Final Result:** Bugbot run cancelled because the review request was "
        "cancelled\n",
        # Nothing at all.
        "",
        None,
    ],
)
def test_anything_else_is_not_understood(summary):
    assert bugbot_result(summary).kind == "unknown"


def test_only_the_last_result_line_counts():
    # An earlier line cannot stand for the run's result, either way.
    undercount = (
        "**Final Result:** Bugbot completed review and found 0 potential issues.\n"
        "**Final Result:** Bugbot completed review and found 2 potential issues.\n"
    )
    assert bugbot_result(undercount) == BugbotResult("found", 2)
    clean_then_error = (
        "**Final Result:** Bugbot completed review - no issues found.\n"
        "**Final Result:** Bugbot failed to complete the review.\n"
    )
    assert bugbot_result(clean_then_error).kind == "unknown"
    cancel_then_review = (
        "**Result:** Bugbot run cancelled because the review request was cancelled\n"
        "**Final Result:** Bugbot completed review and found 1 potential issue.\n"
    )
    assert bugbot_result(cancel_then_review) == BugbotResult("found", 1)


def test_a_result_mentioned_mid_line_is_not_a_result_line():
    summary = (
        "note: **Final Result:** Bugbot completed review - no issues found.\n"
        "**Final Result:** Bugbot completed review and found 1 potential issue.\n"
    )
    assert bugbot_result(summary) == BugbotResult("found", 1)
