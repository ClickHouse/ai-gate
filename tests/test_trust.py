"""The trust policy file: it loads, it is the tier map, and anything malformed
is refused rather than half-applied."""

from __future__ import annotations

import copy
from importlib import resources

import pytest
import yaml

from aigate import tiers
from aigate.trust import (
    CODEOWNER_AUTHOR,
    FORKS_COSIGN,
    MAINTAINER_AUTHOR,
    POLICY_FILE,
    TrustPolicyError,
    load_trust_policy,
    parse_trust_policy,
)


def _doc() -> dict[str, object]:
    text = resources.files("aigate").joinpath(POLICY_FILE).read_text("utf-8")
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict)
    return doc


def _parse(doc: object) -> None:
    parse_trust_policy(yaml.safe_dump(doc))


def test_the_packaged_policy_loads() -> None:
    policy = load_trust_policy()
    assert policy.unknown_path_tier == 2
    assert policy.blocking_risk == {"high", "critical"}
    # A maintainer's or code owner's own pull request clears a declared risk.
    assert policy.blocking_risk_unless == {MAINTAINER_AUTHOR, CODEOWNER_AUTHOR}
    assert policy.maintainer_roles == {"maintain", "admin"}
    # Co-signing fork pull requests is on for public repositories only.
    assert policy.forks.mode == FORKS_COSIGN
    assert policy.forks.visibility == {"public"}
    assert policy.forks.machine_accounts == frozenset()
    assert policy.forks.cosigns("public")
    assert not policy.forks.cosigns("private")
    assert not policy.forks.cosigns("internal")
    assert not policy.forks.cosigns(None)


def test_the_tier_map_is_the_policy_file() -> None:
    policy = load_trust_policy()
    assert [(r.glob, r.tier, r.capability) for r in tiers.SENSITIVITY_MAP] == [
        (r.glob, r.tier, r.why) for r in policy.tier_rules
    ]
    assert policy.unknown_path_tier == tiers.UNKNOWN_PATH_TIER


# A path per behaviour the hardcoded map had, so a policy edit that moves one
# of them is a visible test change, not a silent one.
@pytest.mark.parametrize(
    "path,tier",
    [
        (".github/workflows/ci.yml", 3),
        (".github/actions/x/action.yml", 3),
        (".github/dependabot.yml", 3),
        ("a/.cursor/rules", 3),
        ("x/BUGBOT.md", 3),
        ("atlantis.yaml", 3),
        ("docs/CODEOWNERS", 3),
        ("modules/vpc/main.tf", 3),
        ("infra/anything", 3),
        ("svc/Dockerfile", 3),
        ("CMakeLists.txt", 3),
        ("svc/auth/login.py", 3),
        ("db/migrations/001.sql", 3),
        ("config/app_secret.yaml", 3),
        ("policy/x.rego", 3),
        ("web/package.json", 2),
        ("go.sum", 2),
        ("unmapped/thing.py", 2),
        ("pkg/tests/test_x.py", 1),
        ("src/main.rs", 1),
        ("docs/guide.md", 0),
        ("LICENSE", 0),
    ],
)
def test_paths_keep_their_tier(path: str, tier: int) -> None:
    assert tiers.classify_path(path)[0] == tier


def test_only_workflows_and_actions_can_be_cleared() -> None:
    allowed = {r.glob: r.unless for r in load_trust_policy().always_human if r.unless}
    assert allowed == {
        ".github/workflows/**": frozenset({CODEOWNER_AUTHOR, MAINTAINER_AUTHOR}),
        ".github/actions/**": frozenset({CODEOWNER_AUTHOR, MAINTAINER_AUTHOR}),
    }


def _with(path: list[object], value: object) -> dict[str, object]:
    doc = copy.deepcopy(_doc())
    node: object = doc
    for key in path[:-1]:
        node = node[key]  # type: ignore[index]
    node[path[-1]] = value  # type: ignore[index]
    return doc


@pytest.mark.parametrize(
    "path,value",
    [
        (["version"], 2),
        (["extra"], 1),
        (["tiers", "unknown_path"], 5),
        (["tiers", "rules"], []),
        (["tiers", "rules", 0, "tier"], 4),
        (["tiers", "rules", 0, "tier"], True),
        (["tiers", "rules", 0, "glob"], "has space"),
        (["tiers", "rules", 0, "why"], ""),
        (["tiers", "rules", 0, "extra"], "x"),
        (["approver", "always_human", 0, "unless"], ["anything_else"]),
        (["approver", "always_human", 0, "unless"], []),
        (["approver", "always_human", 0, "unless"], "codeowner_author"),
        (["approver", "always_human", 0, "unless"], ["unprivileged_workflow"]),
        (["approver", "blocking_risk"], ["severe"]),
        (["approver", "blocking_risk_unless"], ["anything_else"]),
        (["approver", "blocking_risk_unless"], "maintainer_author"),
        (["approver", "blocking_risk_unless"], [""]),
        (["approver", "maintainer_roles"], ["write"]),
        (["approver", "maintainer_roles"], []),
        (["approver", "forks"], "cosign"),
        (["approver", "forks", "mode"], "always"),
        (["approver", "forks", "mode"], True),
        (["approver", "forks", "machine_accounts"], "ci-bot"),
        (["approver", "forks", "machine_accounts"], [""]),
        (["approver", "forks", "extra"], 1),
        (["approver", "forks", "visibility"], []),
        (["approver", "forks", "visibility"], ["secret"]),
        (["approver", "forks", "visibility"], "public"),
    ],
)
def test_a_malformed_policy_is_refused(path: list[object], value: object) -> None:
    with pytest.raises(TrustPolicyError):
        _parse(_with(path, value))


@pytest.mark.parametrize(
    "path",
    [
        ["tiers"],
        ["approver"],
        ["tiers", "rules", 0, "why"],
        ["approver", "forks"],
        ["approver", "forks", "machine_accounts"],
        ["approver", "forks", "visibility"],
    ],
)
def test_a_missing_section_is_refused(path: list[object]) -> None:
    doc = copy.deepcopy(_doc())
    node: object = doc
    for key in path[:-1]:
        node = node[key]  # type: ignore[index]
    del node[path[-1]]  # type: ignore[attr-defined]
    with pytest.raises(TrustPolicyError):
        _parse(doc)


def test_not_yaml_is_refused() -> None:
    with pytest.raises(TrustPolicyError):
        parse_trust_policy("tiers: [")


def test_machine_accounts_are_case_insensitive() -> None:
    doc = _with(
        ["approver", "forks"],
        {"mode": "cosign", "visibility": ["public"], "machine_accounts": ["CI-Bot"]},
    )
    policy = parse_trust_policy(yaml.safe_dump(doc))
    assert policy.forks.mode == "cosign"
    assert policy.forks.machine_accounts == {"ci-bot"}


def test_blocking_risk_unless_must_be_stated() -> None:
    # An empty list says "nothing clears a risk"; a missing key is an error.
    doc = copy.deepcopy(_doc())
    del doc["approver"]["blocking_risk_unless"]  # type: ignore[index, attr-defined]
    with pytest.raises(TrustPolicyError):
        _parse(doc)
    assert (
        parse_trust_policy(
            yaml.safe_dump(_with(["approver", "blocking_risk_unless"], []))
        ).blocking_risk_unless
        == frozenset()
    )
