"""Tests for the single-file loader (``.github/ai-gate.yml``)."""

from __future__ import annotations

import pytest

from aigate.gateconfig import load_gate_config
from aigate.reviewers import ConfigError


def test_absent_file_is_default_reviewer_and_no_policy() -> None:
    reviewers, policy = load_gate_config(None)
    assert reviewers == ["bugbot"]
    assert policy is None


def test_reviewers_only_leaves_the_simple_gate() -> None:
    reviewers, policy = load_gate_config("reviewers: [bugbot]\n")
    assert reviewers == ["bugbot"]
    assert policy is None


def test_policy_block_opts_into_the_strict_gate() -> None:
    reviewers, policy = load_gate_config(
        "policy:\n"
        "  escalation_paths:\n"
        '    - paths: ["auth/**"]\n'
        '      reviewers: "@example-org/prodsec"\n'
    )
    assert reviewers == ["bugbot"]  # default reviewer still applies
    assert policy is not None
    assert policy.escalation_reviewers_for("auth/login.py") == ("@example-org/prodsec",)


def test_empty_policy_map_opts_in_with_no_rules() -> None:
    _, policy = load_gate_config("policy: {}\n")
    assert policy is not None
    assert policy.escalation_paths == ()


def test_present_but_null_policy_is_an_error_not_a_silent_simple_gate() -> None:
    # A half-written `policy:` fails closed rather than dropping the policy.
    with pytest.raises(ConfigError):
        load_gate_config("policy:\n")


def test_unknown_top_level_key_is_rejected() -> None:
    # A policy key written at the top level instead of under `policy:`.
    with pytest.raises(ConfigError):
        load_gate_config("escalation_paths: []\n")


def test_empty_file_fails_closed() -> None:
    with pytest.raises(ConfigError):
        load_gate_config("")


def test_unknown_reviewer_fails_closed() -> None:
    with pytest.raises(ConfigError):
        load_gate_config("reviewers: [totallylegitbot]\n")
