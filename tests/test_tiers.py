"""Tests for the capability-based tier classifier.

Pins the three safety properties: max wins, unknown areas raise, and nothing
ordinary is free.
"""

from __future__ import annotations

from aigate.tiers import (
    ORDINARY_CODE_TIER,
    TIER_0,
    TIER_1,
    TIER_2,
    TIER_3,
    UNKNOWN_PATH_TIER,
    classify,
    classify_path,
)


def test_trust_root_paths_are_tier_3() -> None:
    for path in (
        ".github/workflows/ci.yml",
        "infra/github/rulesets/main.tf",
        ".github/CODEOWNERS",
        "services/api/auth/login.py",
        "policy/security/github_organization_ruleset.rego",
    ):
        tier, _ = classify_path(path)
        assert tier == TIER_3, path


def test_dependency_manifests_are_tier_2() -> None:
    for path in ("app/package.json", "svc/pyproject.toml", "go.sum"):
        tier, _ = classify_path(path)
        assert tier == TIER_2, path


def test_tests_are_tier_1_not_zero() -> None:
    # A test change can weaken the checks, so it is never TIER_0.
    tier, _ = classify_path("app/tests/test_pricing.py")
    assert tier == TIER_1


def test_documentation_is_tier_0() -> None:
    tier, _ = classify_path("docs/guide.md")
    assert tier == TIER_0


def test_ordinary_code_area_is_tier_1() -> None:
    # A plain file in a mapped ordinary area is TIER_1, not free.
    tier, _ = classify_path("services/api/handler.py")
    assert tier == ORDINARY_CODE_TIER == TIER_1


def test_sensitive_rule_beats_ordinary_area() -> None:
    # services/** is ordinary (1), but an auth path under it is TIER_3; max wins.
    tier, _ = classify_path("services/api/auth/token.py")
    assert tier == TIER_3


def test_unknown_top_level_area_raises_to_tier_2() -> None:
    tier, reason = classify_path("somebrandnewservice/handler.py")
    assert tier == UNKNOWN_PATH_TIER == TIER_2
    assert "unknown raises" in reason


def test_classify_takes_the_max_over_paths() -> None:
    result = classify(
        [
            "docs/readme.md",  # TIER_0
            "app/tests/test_x.py",  # TIER_1
            "services/auth/token.py",  # TIER_3
        ]
    )
    assert result.tier == TIER_3
    assert result.requires_human_always


def test_no_changes_is_tier_0() -> None:
    result = classify([])
    assert result.tier == TIER_0


def test_reasons_are_recorded_for_the_winning_tier() -> None:
    result = classify(["services/auth/token.py", "docs/x.md"])
    assert result.tier == TIER_3
    assert result.reasons  # non-empty, auditable
    assert all(isinstance(r, str) for r in result.reasons)


def test_cmakelists_is_build_system_not_documentation() -> None:
    # CMakeLists.txt must not fall to the ``*.txt`` documentation rule.
    for path in ("CMakeLists.txt", "src/CMakeLists.txt", "cmake/toolchain.cmake"):
        tier, _ = classify_path(path)
        assert tier == TIER_3, path


def test_plain_text_file_is_still_documentation() -> None:
    # Ordinary .txt docs stay TIER_0.
    tier, _ = classify_path("docs/notes.txt")
    assert tier == TIER_0


def test_ai_reviewer_instructions_always_need_a_human():
    # A pull request that edits the reviewer's instructions can steer its own
    # review, at any depth.
    for path in (
        ".cursor/rules/x.md",
        "pkg/.cursor/BUGBOT.md",
        "BUGBOT.md",
        "src/BUGBOT.md",
        ".cursorrules",
    ):
        tier, _ = classify_path(path)
        assert tier == TIER_3, path
