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
