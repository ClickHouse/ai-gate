"""``other_pulls_containing``: which other pull requests a head commit is part of.

langfuse/langfuse#20804 (2026-11-25) was refused as "also part of #20794"
although #20794 is stacked ON TOP of it: GitHub's ``commits/{sha}/pulls``
also lists a pull request whose base branch ends at the commit. Only one that
contains the commit (as its head, or in its own commit list) counts.
"""

from __future__ import annotations

import pytest

from aigate import github

# conftest.py stubs it for every test; these test the real one.
other_pulls_containing = github.other_pulls_containing
SLUG = "langfuse/langfuse"
SHA = "c" * 40


def _pr(number: int, head: str, base_sha: str = "0" * 40) -> dict:
    return {"number": number, "head": {"sha": head}, "base": {"sha": base_sha}}


@pytest.fixture(autouse=True)
def _no_cache():
    github._shared_commit_cache.clear()
    github._shared_commit_reasons.clear()
    yield
    github._shared_commit_cache.clear()
    github._shared_commit_reasons.clear()


def _wire(mocker, associated: list, hits: list, commits: dict):
    def gh_json(args):
        if args[0].endswith("/pulls") and "commits/" in args[0]:
            return associated
        if args[0] == "search/issues":
            return {
                "items": [{"number": n} for n in hits],
                "total_count": len(hits),
                "incomplete_results": False,
            }
        raise AssertionError(args)

    mocker.patch.object(github, "gh_json", side_effect=gh_json)
    mocker.patch.object(
        github,
        "_gh_list",
        side_effect=lambda path: [
            {"sha": s} for s in commits[int(path.split("/pulls/")[1].split("/")[0])]
        ],
    )


def test_a_pr_stacked_on_top_is_not_counted(mocker):
    # #20794's base branch ends at SHA; its own commits do not include it.
    _wire(
        mocker,
        associated=[_pr(20804, SHA), _pr(20794, "d" * 40, base_sha=SHA)],
        hits=[20804],
        commits={20794: ["d" * 40, "e" * 40]},
    )
    assert other_pulls_containing(SLUG, SHA, 20804) == []


def test_another_pr_with_the_same_head_is_counted(mocker):
    _wire(
        mocker,
        associated=[_pr(20804, SHA), _pr(30000, SHA)],
        hits=[20804],
        commits={},
    )
    assert other_pulls_containing(SLUG, SHA, 20804) == [30000]
    assert github.shared_commit_reasons(SLUG, SHA, 20804) == {30000: github.SAME_HEAD}


def test_a_pr_that_once_had_the_commit_as_head_is_counted(mocker):
    # Pushed past since: the commit is still in its commit list.
    _wire(
        mocker,
        associated=[_pr(20804, SHA), _pr(30001, "f" * 40)],
        hits=[20804],
        commits={30001: [SHA, "f" * 40]},
    )
    assert other_pulls_containing(SLUG, SHA, 20804) == [30001]
    assert github.shared_commit_reasons(SLUG, SHA, 20804) == {30001: github.CONTAINS}


class TestTheMessage:
    def _summary(self, others, reasons):
        from aigate import cli

        return cli._shared_commit_summary(others, reasons)

    def test_a_same_head_duplicate_says_so_and_offers_closing_it(self):
        text = self._summary([30000], {30000: github.SAME_HEAD})
        assert "#30000 has this exact commit as head" in text
        assert "If #30000 duplicates this pull request, close it" in text

    def test_a_pushed_past_pr_asks_for_a_new_commit_not_a_close(self):
        text = self._summary([30001], {30001: github.CONTAINS})
        assert "it was its head at some point" in text
        assert "close" not in text.split("**What to do:**")[1].split("Or ask")[0]

    def test_every_message_says_a_stack_needs_nothing_closed(self):
        for reasons in ({1: github.SAME_HEAD}, {1: github.CONTAINS}, {}):
            assert "nothing needs closing for a stack" in self._summary([1], reasons)

    def test_without_reasons_it_still_names_the_prs(self):
        assert "also part of #7" in self._summary([7], {})
