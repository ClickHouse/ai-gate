"""Tests for the escalation-policy loader (the `policy:` block of .github/ai-gate.yml)."""

from __future__ import annotations

import pytest

from aigate.policy import EscalationRule, GatePolicy, PolicyError, load_policy

VALID = """
escalation_paths:
  - paths: ["auth/**", "infra/**", "**/*secret*"]
    reviewers: "@example-org/prodsec"
  - paths: ["billing/**"]
    reviewers: "@alice"
security_dismissal_reviewers: "@example-org/prodsec"
"""


def test_absent_file_means_no_policy_not_an_error() -> None:
    # A repository without a policy keeps the simple gate.
    assert load_policy(None) is None


def test_valid_policy_parses() -> None:
    policy = load_policy(VALID)
    assert policy is not None
    assert policy.security_dismissal_reviewers == "@example-org/prodsec"
    assert policy.escalation_paths == (
        EscalationRule(
            paths=("auth/**", "infra/**", "**/*secret*"),
            reviewers="@example-org/prodsec",
        ),
        EscalationRule(paths=("billing/**",), reviewers="@alice"),
    )


def test_escalation_reviewers_for_path_matches_globs() -> None:
    policy = load_policy(VALID)
    assert policy is not None
    assert policy.escalation_reviewers_for("auth/login.py") == ("@example-org/prodsec",)
    assert policy.escalation_reviewers_for("svc/k8s/secrets.yaml") == (
        "@example-org/prodsec",
    )
    assert policy.escalation_reviewers_for("billing/invoice.py") == ("@alice",)
    assert policy.escalation_reviewers_for("docs/readme.md") == ()


def test_team_handles_lists_only_teams() -> None:
    policy = load_policy(VALID)
    assert policy is not None
    assert policy.team_handles() == frozenset({"example-org/prodsec"})


def test_mapping_with_no_keys_is_an_empty_policy() -> None:
    # Opting in with nothing to escalate still turns on the stricter gate.
    assert load_policy("{}\n") == GatePolicy()


def test_empty_file_fails_closed() -> None:
    with pytest.raises(PolicyError):
        load_policy("")
    with pytest.raises(PolicyError):
        load_policy("\n")


def test_malformed_yaml_fails_closed() -> None:
    with pytest.raises(PolicyError):
        load_policy("escalation_paths: [unclosed\n")


def test_non_mapping_fails_closed() -> None:
    with pytest.raises(PolicyError):
        load_policy("- just\n- a list\n")


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(PolicyError) as exc:
        load_policy("security_dismissal_reviewer: '@a'\n")  # typo
    assert "unknown key" in str(exc.value)


def test_entry_with_unknown_key_is_rejected() -> None:
    with pytest.raises(PolicyError):
        load_policy(
            "escalation_paths:\n  - paths: ['a/**']\n    reviewers: '@a'\n    extra: 1\n"
        )


def test_entry_must_have_non_empty_paths() -> None:
    with pytest.raises(PolicyError):
        load_policy("escalation_paths:\n  - paths: []\n    reviewers: '@a'\n")
    with pytest.raises(PolicyError):
        load_policy("escalation_paths:\n  - reviewers: '@a'\n")
    with pytest.raises(PolicyError):
        load_policy("escalation_paths:\n  - paths: ['']\n    reviewers: '@a'\n")


@pytest.mark.parametrize(
    "handle",
    ["alice", "@", "@example-org/", "@a/b/c", "@Example Org", "", 5, ["@a"]],
)
def test_reviewer_handle_must_be_team_or_user(handle: object) -> None:
    import yaml

    text = yaml.safe_dump(
        {"escalation_paths": [{"paths": ["a/**"], "reviewers": handle}]}
    )
    with pytest.raises(PolicyError):
        load_policy(text)
    with pytest.raises(PolicyError):
        load_policy(yaml.safe_dump({"security_dismissal_reviewers": handle}))


def test_escalation_paths_must_be_a_list_of_mappings() -> None:
    with pytest.raises(PolicyError):
        load_policy("escalation_paths: 'auth/**'\n")
    with pytest.raises(PolicyError):
        load_policy("escalation_paths:\n  - 'auth/**'\n")
