from __future__ import annotations

import pytest

from aigate.reviewers import (
    BASELINE_REVIEWERS,
    DEFAULT_REVIEWERS,
    REVIEWERS,
    ConfigError,
    Reviewer,
    load_config,
    not_auto_approve_eligible,
)


def test_bugbot_is_auto_approve_eligible():
    assert REVIEWERS["bugbot"].auto_approve_eligible is True


def test_not_auto_approve_eligible_passes_the_trusted_baseline():
    assert not_auto_approve_eligible(["bugbot"]) == []


def test_not_auto_approve_eligible_flags_a_ci_hosted_reviewer(monkeypatch):
    monkeypatch.setitem(
        REVIEWERS,
        "cihosted",
        Reviewer("cihosted", "CI Review", "github-actions", "Code Review"),
    )
    assert not_auto_approve_eligible(["bugbot", "cihosted"]) == ["cihosted"]


def test_unknown_reviewer_is_not_eligible_fail_safe():
    assert not_auto_approve_eligible(["ghost"]) == ["ghost"]


@pytest.fixture
def extra_reviewer(monkeypatch):
    """Register a throwaway second reviewer so union behaviour is testable."""
    monkeypatch.setitem(
        REVIEWERS,
        "coderabbit",
        Reviewer("coderabbit", "CodeRabbit", "coderabbitai", "CodeRabbit"),
    )
    return "coderabbit"


def test_absent_file_takes_the_default_bugbot():
    assert load_config(None) == list(DEFAULT_REVIEWERS)
    assert load_config(None) == ["bugbot"]


def test_explicit_single_reviewer():
    assert load_config("reviewers:\n  - bugbot\n") == ["bugbot"]


def test_baseline_is_always_required_even_when_not_listed(extra_reviewer):
    # Listing only another reviewer still requires the baseline, first.
    assert load_config("reviewers:\n  - coderabbit\n") == ["bugbot", "coderabbit"]


def test_baseline_not_duplicated_when_listed_explicitly(extra_reviewer):
    assert load_config("reviewers:\n  - bugbot\n  - coderabbit\n") == [
        "bugbot",
        "coderabbit",
    ]
    # order of extras is preserved after the baseline
    assert load_config("reviewers:\n  - coderabbit\n  - bugbot\n") == [
        "bugbot",
        "coderabbit",
    ]


def test_absent_file_is_exactly_the_baseline():
    assert load_config(None) == list(BASELINE_REVIEWERS)


def test_default_equals_baseline():
    assert DEFAULT_REVIEWERS == BASELINE_REVIEWERS


def test_empty_file_is_an_error_not_an_opt_out():
    # A repository cannot silently require nothing; that would be the bypass.
    with pytest.raises(ConfigError):
        load_config("")
    with pytest.raises(ConfigError):
        load_config("\n")


def test_empty_reviewers_list_is_an_error():
    with pytest.raises(ConfigError):
        load_config("reviewers: []\n")


def test_unknown_reviewer_is_rejected():
    with pytest.raises(ConfigError) as exc:
        load_config("reviewers:\n  - totallylegitbot\n")
    assert "unknown reviewer" in str(exc.value)


def test_non_string_reviewer_is_rejected():
    with pytest.raises(ConfigError):
        load_config("reviewers:\n  - 5\n")


def test_missing_reviewers_key_is_rejected():
    with pytest.raises(ConfigError):
        load_config("something: else\n")


def test_malformed_yaml_is_rejected():
    with pytest.raises(ConfigError):
        load_config("reviewers: [unclosed\n")


def test_bugbot_is_identified_by_posting_app_not_name():
    # The registry must pin an app slug; check names can be forged.
    assert REVIEWERS["bugbot"].app_slug
