"""Tests for the pure CODEOWNERS parser and owner-eligibility check."""

from __future__ import annotations

from aigate.codeowners import (
    is_eligible_resolver,
    owners_for_path,
    parse,
)

SAMPLE = """
# comment
*                       @example-org/eng
/infra/                 @example-org/security
*.tf                    @example-org/security @alice
docs/                   @bob
services/auth/**        @example-org/appsec
"""


def test_parse_skips_comments_and_blanks() -> None:
    rules = parse(SAMPLE)
    assert len(rules) == 5
    assert rules[0].pattern == "*"
    assert rules[0].owners == ("@example-org/eng",)


def test_last_matching_rule_wins() -> None:
    rules = parse(SAMPLE)
    # matches "*" and "*.tf"; the later *.tf wins.
    owners = owners_for_path(rules, "main.tf")
    assert owners == ("@example-org/security", "@alice")


def test_anchored_directory_pattern() -> None:
    rules = parse(SAMPLE)
    # A non-.tf file under /infra/ matches "*" then "/infra/"; the latter wins.
    assert owners_for_path(rules, "infra/github/config.yaml") == (
        "@example-org/security",
    )


def test_later_extension_rule_overrides_an_earlier_dir_rule() -> None:
    # GitHub semantics: *.tf comes after /infra/, so it wins for .tf files.
    rules = parse(SAMPLE)
    assert owners_for_path(rules, "infra/github/main.tf") == (
        "@example-org/security",
        "@alice",
    )


def test_double_star_crosses_directories() -> None:
    rules = parse(SAMPLE)
    assert owners_for_path(rules, "services/auth/jwt/verify.py") == (
        "@example-org/appsec",
    )


def test_unmatched_path_gets_catch_all() -> None:
    rules = parse(SAMPLE)
    assert owners_for_path(rules, "README") == ("@example-org/eng",)


def test_direct_user_owner_is_eligible() -> None:
    assert is_eligible_resolver(
        ("@alice", "@example-org/security"), "alice", frozenset()
    )


def test_team_owner_requires_membership() -> None:
    owners = ("@example-org/security",)
    assert not is_eligible_resolver(owners, "carol", frozenset())
    assert is_eligible_resolver(owners, "carol", frozenset({"example-org/security"}))


def test_eligibility_is_case_insensitive() -> None:
    assert is_eligible_resolver(("@Alice",), "alice", frozenset())
    assert is_eligible_resolver(
        ("@Example-Org/Security",), "x", frozenset({"example-org/security"})
    )


def test_non_owner_is_not_eligible() -> None:
    assert not is_eligible_resolver(("@alice",), "mallory", frozenset())
