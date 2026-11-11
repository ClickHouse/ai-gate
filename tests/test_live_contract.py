"""Contract tests against GitHub's real API shape rather than hand-written mocks.

GraphQL returns the ``cursor[bot]`` reviewer as ``cursor`` under
``author.login``, which mocks can easily get wrong.

* ``test_live_fixture_*`` parse ``fixtures/review_threads_live.json``, captured
  from a real pull request, with no network.
* ``test_live_smoke`` calls the real API only when ``AIGATE_LIVE_PR`` is set:
      AIGATE_LIVE_PR=owner/repo#123 pytest -k live_smoke
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aigate import github
from aigate.cli import DEFAULT_REVIEWER_LOGIN
from aigate.gate import is_reviewer_run
from aigate.reviewers import REVIEWERS
from aigate.threads import _same_actor, parse_review_threads

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "review_threads_live.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_live_fixture_reviewer_threads_are_recognised() -> None:
    # Two Cursor threads with author.login "cursor" match "cursor[bot]".
    pr_author, findings = parse_review_threads(_load(), DEFAULT_REVIEWER_LOGIN)
    assert pr_author == "pr-author"
    assert len(findings) == 2
    assert {f.path for f in findings} == {
        "src/app.py",
        "src/util.py",
    }


def test_live_fixture_documents_the_within_payload_login_inconsistency() -> None:
    # In one payload the same bot is "cursor" under author.login and
    # "cursor[bot]" under resolvedBy.login; both must match.
    node = _load()["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]
    author_login = node["comments"]["nodes"][0]["author"]["login"]
    resolver_login = node["resolvedBy"]["login"]
    assert author_login == "cursor"
    assert resolver_login == "cursor[bot]"
    assert _same_actor(author_login, DEFAULT_REVIEWER_LOGIN)
    assert _same_actor(resolver_login, DEFAULT_REVIEWER_LOGIN)


@pytest.mark.skipif(
    not os.environ.get("AIGATE_LIVE_PR"),
    reason="set AIGATE_LIVE_PR=owner/name#number (and gh auth) to run the live check",
)
def test_live_smoke_gate_actually_sees_reviewer_threads() -> None:
    # Every reviewer thread in the live payload must survive parsing.
    slug, _, num = os.environ["AIGATE_LIVE_PR"].partition("#")
    raw = github.review_threads(slug, int(num))
    nodes = (
        raw.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    expected = [
        n
        for n in nodes
        if _same_actor(
            ((n.get("comments") or {}).get("nodes") or [{}])[0]
            .get("author", {})
            .get("login", ""),
            DEFAULT_REVIEWER_LOGIN,
        )
    ]
    _, findings = parse_review_threads(raw, DEFAULT_REVIEWER_LOGIN)
    assert len(findings) == len(expected), (
        f"{len(expected)} reviewer threads in the live payload but the parser "
        f"kept {len(findings)} — the reviewer-login contract has drifted."
    )


def test_live_check_runs_fixture_parses_to_signals(mocker):
    # Pins the real check-runs shape the gate relies on: the app slug, the
    # check name, and started_at for tying a review to this pull request.
    payload = json.loads((FIXTURES / "check_runs_live.json").read_text())
    mocker.patch.object(github, "gh_json", return_value=[payload])
    signals = github.signals_for("o/r", "0" * 40)
    bugbot = [s for s in signals if is_reviewer_run(s, REVIEWERS["bugbot"])]
    assert len(bugbot) == 1
    assert bugbot[0].conclusion == "success"
    assert bugbot[0].started_at and bugbot[0].started_at.endswith("Z")
    assert bugbot[0].started_at <= (bugbot[0].completed_at or "")
    assert not any(
        is_reviewer_run(s, REVIEWERS["bugbot"]) for s in signals if s.name == "test"
    )
