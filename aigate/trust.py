"""Load and validate the gate's trust policy, ``aigate/trust_policy.yaml``.

The file holds the risk-tier rules (``aigate.tiers``), the approver's
always-human floor (``aigate_approver.guardrails``) and what the approver may
do on a fork pull request (``aigate_approver.cosign``). It ships with the package
and is validated in full when first read: an unknown key, a wrong type or an
out-of-range tier raises ``TrustPolicyError``, so a policy that silently does
less than it says can never load. Every consumer imports it at start-up, so a
broken file fails the gate closed (red) instead of running with gaps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

import yaml

POLICY_FILE = "trust_policy.yaml"
SUPPORTED_VERSION = 1
TIERS = (0, 1, 2, 3)
CODEOWNER_AUTHOR = "codeowner_author"
MAINTAINER_AUTHOR = "maintainer_author"
# The deterministic checks a floor rule may name in ``unless``.
EXCEPTIONS = frozenset({CODEOWNER_AUTHOR, MAINTAINER_AUTHOR})
# ``approver.forks.mode``: whether the approver may co-sign a fork pull request.
FORKS_NEVER = "never"
FORKS_COSIGN = "cosign"
FORK_MODES = (FORKS_NEVER, FORKS_COSIGN)
VISIBILITIES = ("public", "internal", "private")


class TrustPolicyError(ValueError):
    """``trust_policy.yaml`` is malformed."""


@dataclass(frozen=True)
class TierRule:
    """Paths matching ``glob`` carry at least ``tier``."""

    glob: str
    tier: int
    why: str


@dataclass(frozen=True)
class FloorRule:
    """Paths matching ``glob`` (case-insensitively) are never bot-approved.

    ``unless`` names the checks (``EXCEPTIONS``) any one of which may clear a
    match; empty means nothing clears it.
    """

    glob: str
    why: str
    unless: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ForkPolicy:
    """``approver.forks``: what the approver may do on a fork pull request."""

    mode: str = FORKS_NEVER
    # Repository visibilities (``public``, ``internal``, ``private``) where a
    # co-sign may happen.
    visibility: frozenset[str] = frozenset()
    # Lower-cased logins that never count as a human co-signer.
    machine_accounts: frozenset[str] = frozenset()

    def cosigns(self, visibility: str | None) -> bool:
        """Whether a fork pull request in a repository of ``visibility`` may be co-signed."""
        return self.mode == FORKS_COSIGN and visibility in self.visibility


@dataclass(frozen=True)
class TrustPolicy:
    tier_rules: tuple[TierRule, ...]
    unknown_path_tier: int
    always_human: tuple[FloorRule, ...]
    blocking_risk: frozenset[str]
    maintainer_roles: frozenset[str]
    forks: ForkPolicy = ForkPolicy()


@cache
def load_trust_policy() -> TrustPolicy:
    """Return the packaged trust policy, parsed once per process."""
    text = resources.files("aigate").joinpath(POLICY_FILE).read_text("utf-8")
    return parse_trust_policy(text)


def parse_trust_policy(text: str) -> TrustPolicy:
    """Parse and fully validate a trust policy document."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TrustPolicyError(f"{POLICY_FILE} is not valid YAML: {exc}") from exc
    top = _mapping(doc, POLICY_FILE, {"version", "tiers", "approver"})
    if top.get("version") != SUPPORTED_VERSION:
        raise TrustPolicyError(
            f"{POLICY_FILE}: version must be {SUPPORTED_VERSION}, got "
            f"{top.get('version')!r}"
        )

    tiers = _mapping(top.get("tiers"), "tiers", {"unknown_path", "rules"})
    unknown = tiers.get("unknown_path")
    if unknown not in TIERS:
        raise TrustPolicyError(f"tiers.unknown_path must be one of {TIERS}")
    tier_rules = tuple(
        _tier_rule(entry, i) for i, entry in enumerate(_list(tiers, "rules", "tiers"))
    )

    approver = _mapping(
        top.get("approver"),
        "approver",
        {"always_human", "blocking_risk", "maintainer_roles", "forks"},
    )
    floor = tuple(
        _floor_rule(entry, i)
        for i, entry in enumerate(_list(approver, "always_human", "approver"))
    )
    risk = _strings(approver.get("blocking_risk"), "approver.blocking_risk")
    allowed_risk = {"low", "medium", "high", "critical"}
    if not set(risk) <= allowed_risk:
        raise TrustPolicyError(
            f"approver.blocking_risk: levels must be among {sorted(allowed_risk)}"
        )

    return TrustPolicy(
        tier_rules=tier_rules,
        unknown_path_tier=unknown,
        always_human=floor,
        blocking_risk=frozenset(level.lower() for level in risk),
        maintainer_roles=_roles(approver.get("maintainer_roles")),
        forks=_forks(approver.get("forks")),
    )


def _forks(value: object) -> ForkPolicy:
    where = "approver.forks"
    forks = _mapping(value, where, {"mode", "visibility", "machine_accounts"})
    mode = forks["mode"]
    if mode not in FORK_MODES:
        raise TrustPolicyError(f"{where}.mode must be one of {', '.join(FORK_MODES)}")
    visibility = {
        v.lower() for v in _strings(forks["visibility"], f"{where}.visibility")
    }
    if not visibility or not visibility <= set(VISIBILITIES):
        raise TrustPolicyError(
            f"{where}.visibility must name at least one of {', '.join(VISIBILITIES)}"
        )
    accounts = _strings(forks["machine_accounts"], f"{where}.machine_accounts")
    assert isinstance(mode, str)
    return ForkPolicy(
        mode, frozenset(visibility), frozenset(a.lower() for a in accounts)
    )


def _roles(value: object) -> frozenset[str]:
    where = "approver.maintainer_roles"
    roles = {r.lower() for r in _strings(value, where)}
    if not roles:
        raise TrustPolicyError(f"{where} must name at least one role")
    if not roles <= {"maintain", "admin"}:
        raise TrustPolicyError(f"{where}: only maintain and admin may be listed")
    return frozenset(roles)


def _mapping(value: object, where: str, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TrustPolicyError(f"{where} must be a mapping")
    extra = sorted(str(k) for k in set(value) - keys)
    if extra:
        raise TrustPolicyError(f"{where}: unknown key(s) {', '.join(extra)}")
    missing = sorted(keys - set(value))
    if missing:
        raise TrustPolicyError(f"{where}: missing key(s) {', '.join(missing)}")
    return value


def _list(parent: dict[str, object], key: str, where: str) -> list[object]:
    value = parent.get(key)
    if not isinstance(value, list) or not value:
        raise TrustPolicyError(f"{where}.{key} must be a non-empty list")
    return value


def _strings(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v.strip() for v in value
    ):
        raise TrustPolicyError(f"{where} must be a list of non-empty strings")
    return [v.strip() for v in value]


_GLOB_CHARS = re.compile(r"^[^\s\\]+$")


def _glob(value: object, where: str) -> str:
    if not isinstance(value, str) or not _GLOB_CHARS.match(value):
        raise TrustPolicyError(f"{where}.glob must be a non-empty glob without spaces")
    return value


def _why(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrustPolicyError(f"{where}.why must be a non-empty string")
    return value.strip()


def _tier_rule(entry: object, index: int) -> TierRule:
    where = f"tiers.rules[{index}]"
    rule = _mapping(entry, where, {"glob", "tier", "why"})
    tier = rule["tier"]
    if isinstance(tier, bool) or tier not in TIERS:
        raise TrustPolicyError(f"{where}.tier must be one of {TIERS}")
    assert isinstance(tier, int)
    return TierRule(_glob(rule["glob"], where), tier, _why(rule["why"], where))


def _floor_rule(entry: object, index: int) -> FloorRule:
    where = f"approver.always_human[{index}]"
    if not isinstance(entry, dict):
        raise TrustPolicyError(f"{where} must be a mapping")
    keys = {"glob", "why", "unless"} if "unless" in entry else {"glob", "why"}
    rule = _mapping(entry, where, keys)
    unless = (
        frozenset(_strings(rule["unless"], f"{where}.unless"))
        if "unless" in rule
        else frozenset()
    )
    if "unless" in rule and not unless:
        raise TrustPolicyError(f"{where}.unless must name at least one check")
    unknown = sorted(unless - EXCEPTIONS)
    if unknown:
        raise TrustPolicyError(
            f"{where}.unless: unknown check(s) {', '.join(unknown)}; "
            f"known: {', '.join(sorted(EXCEPTIONS))}"
        )
    return FloorRule(_glob(rule["glob"], where), _why(rule["why"], where), unless)
