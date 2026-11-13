from __future__ import annotations

import pytest

from aigate import github

# Modules that exercise these lookups themselves, at the gh boundary.
_REAL_LOOKUPS = {"test_github", "test_attacks"}


@pytest.fixture(autouse=True)
def _review_is_this_pull_requests(request, mocker):
    """By default no base change and no other pull request on the head commit."""
    if request.module.__name__.rsplit(".", 1)[-1] in _REAL_LOOKUPS:
        return
    mocker.patch.object(github, "last_base_change", return_value=None)
    mocker.patch.object(github, "other_pulls_containing", return_value=[])


@pytest.fixture(autouse=True)
def _fresh_shared_commit_cache():
    github._shared_commit_cache.clear()


@pytest.fixture(autouse=True)
def _fresh_changed_paths_cache():
    github._changed_paths_cache.clear()
