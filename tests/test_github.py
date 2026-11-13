from __future__ import annotations

import subprocess

import pytest

from aigate import github


def _realistic_error(args_str, stderr):
    # Mirrors gh_json's exact message shape: "gh api <args> failed: <stderr>".
    return github.GitHubError(f"gh api {args_str} failed: {stderr}")


def test_is_missing_matches_a_real_404_in_stderr():
    exc = _realistic_error("repos/o/r/pulls/1", "HTTP 404: Not Found")
    assert github._is_missing(exc)


def test_is_missing_does_not_misfire_on_a_404_only_in_the_request_path():
    # The number is in the args, not stderr; a rate limit is not "missing".
    exc = _realistic_error("repos/o/r/pulls/404", "HTTP 403: rate limit exceeded")
    assert not github._is_missing(exc)


def test_is_missing_does_not_misfire_on_a_sha_containing_404():
    exc = _realistic_error(
        "repos/o/r/commits/a40400000000.../pulls",
        "HTTP 403: API rate limit exceeded",
    )
    assert not github._is_missing(exc)


def test_is_missing_rejects_422_as_a_marker():
    # 422 is a generic validation failure, not evidence the commit is missing.
    exc = _realistic_error("repos/o/r/compare/a...b", "HTTP 422: Validation Failed")
    assert not github._is_missing(exc)


def test_is_missing_falls_back_to_whole_string_if_unrecognised_shape():
    exc = RuntimeError("something else entirely: 404 Not Found")
    assert github._is_missing(exc)


def test_pull_detail_returns_none_on_missing_pr(mocker):
    mocker.patch.object(
        github,
        "gh_json",
        side_effect=_realistic_error("repos/o/r/pulls/999", "HTTP 404: Not Found"),
    )
    assert github.pull_detail("o/r", 999) is None


def test_pull_detail_propagates_a_real_failure(mocker):
    mocker.patch.object(
        github,
        "gh_json",
        side_effect=_realistic_error("repos/o/r/pulls/999", "HTTP 500: internal error"),
    )
    try:
        github.pull_detail("o/r", 999)
        raised = False
    except github.GitHubError:
        raised = True
    assert raised


def _proc(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_read_config_text_fails_closed_on_a_directory(mocker):
    # A directory lists entries instead of returning a file's content.
    mocker.patch.object(github.subprocess, "run", return_value=_proc('[{"name": "x"}]'))
    with pytest.raises(github.GitHubError):
        github.read_config_text("o/r", "main", ".github/ai-gate.yml")


def test_read_config_text_fails_closed_on_garbage(mocker):
    mocker.patch.object(github.subprocess, "run", return_value=_proc("<html>"))
    with pytest.raises(github.GitHubError):
        github.read_config_text("o/r", "main", ".github/ai-gate.yml")


def test_a_hung_gh_call_becomes_a_github_error(mocker):
    mocker.patch.object(
        github.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["gh"], github.GH_TIMEOUT_SECONDS),
    )
    with pytest.raises(github.GitHubError, match="timed out"):
        github.gh_json(["repos/o/r"])


def test_changed_paths_is_read_once_per_pull_request(mocker):
    # The policy and the approver's floor both need it in one run.
    gh = mocker.patch.object(github, "gh_json", return_value=[[{"filename": "a.py"}]])
    assert github.changed_paths("o/r", 7) == ["a.py"]
    assert github.changed_paths("o/r", 7) == ["a.py"]
    assert gh.call_count == 1
    github.changed_paths("o/r", 8)
    assert gh.call_count == 2


def test_changed_paths_cache_never_hides_a_refusal_or_failure(mocker):
    gh = mocker.patch.object(
        github, "gh_json", side_effect=_realistic_error("x", "HTTP 502")
    )
    for _ in range(2):
        with pytest.raises(github.GitHubError):
            github.changed_paths("o/r", 7)
    assert gh.call_count == 2


def test_team_members_403_is_a_refusal_not_a_transient_failure(mocker):
    mocker.patch.object(
        github,
        "_gh_list",
        side_effect=_realistic_error(
            "orgs/o/teams/t/members", "HTTP 403: Resource not accessible by integration"
        ),
    )
    with pytest.raises(github.GitHubRefusalError, match="read:org"):
        github.team_members("o", "t")


def test_team_members_other_failures_stay_transient(mocker):
    mocker.patch.object(
        github,
        "_gh_list",
        side_effect=_realistic_error("orgs/o/teams/t/members", "HTTP 502: Bad Gateway"),
    )
    with pytest.raises(github.GitHubError) as info:
        github.team_members("o", "t")
    assert not isinstance(info.value, github.GitHubRefusalError)


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: API rate limit exceeded for installation ID 123. (HTTP 403)",
        "gh: You have exceeded a secondary rate limit. Please wait a few minutes "
        "before you try again. (HTTP 403)",
        "HTTP 403: API Rate Limit Exceeded",
        "gh: You have triggered an abuse detection mechanism and have been "
        "temporarily blocked from content creation. (HTTP 403)",
        "gh: Secondary rate: too many requests (HTTP 403)",
    ],
)
def test_a_rate_limited_403_is_transient_not_a_refusal(mocker, stderr):
    # A retry is exactly what clears a rate limit; calling it a refusal would
    # make the gate (and the approver) treat a busy hour as a verdict.
    exc = _realistic_error("orgs/o/teams/t/members --paginate", stderr)
    assert not github._is_forbidden(exc)
    mocker.patch.object(github, "_gh_list", side_effect=exc)
    with pytest.raises(github.GitHubError) as info:
        github.team_members("o", "t")
    assert not isinstance(info.value, github.GitHubRefusalError)


def test_a_permission_403_is_still_a_refusal():
    for stderr in (
        "HTTP 403: Resource not accessible by integration",
        "gh: Must have admin rights to Repository. (HTTP 403)",
        "HTTP 403: Forbidden",
    ):
        assert github._is_forbidden(_realistic_error("orgs/o/teams/t/members", stderr))
