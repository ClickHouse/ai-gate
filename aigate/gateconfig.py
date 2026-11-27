"""Load the per-repository gate config, ``.github/ai-gate.yml``.

Every key is optional:

    reviewers: [bugbot]          # extra AI reviewers to require
    policy:                      # tiers, escalation paths, dismissal sign-off
      escalation_paths:
        - paths: ["auth/**"]
          reviewers: "@org/security"
      security_dismissal_reviewers: "@org/security"
    approver:                    # tightens the approver's central policy
      forks: never               # never co-sign a fork pull request here
      risk: block                # a declared High/Critical risk needs a person,
                                 # even on a maintainer's pull request

With no file, the gate requires the default reviewer and applies no policy.
The file is read from the base branch, and a malformed file fails closed. This
module splits the document; ``reviewers.load_config`` and
``policy.load_policy`` validate each section, ``approver_fork_mode`` and
``approver_risk_mode`` the ``approver:`` one (read only by the approver; the gate validates it so a typo
fails closed here too).
"""

from __future__ import annotations

import yaml

from .policy import GatePolicy, load_policy
from .reviewers import ConfigError, load_config
from .trust import FORK_MODES, RISK_MODES

GATE_CONFIG_PATH = ".github/ai-gate.yml"

_TOP_KEYS = frozenset({"reviewers", "policy", "approver"})
_APPROVER_KEYS = frozenset({"forks", "risk"})


def load_gate_config(
    text: str | None, slug: str | None = None
) -> tuple[list[str], GatePolicy | None]:
    """Resolve ``(required_reviewers, policy)`` from ``.github/ai-gate.yml``.

    ``text`` is the file from the base branch, or ``None`` when absent (the
    defaults). An empty file or an unknown top-level key is an error, so a
    config that silently does nothing cannot pass. ``slug`` is the repository,
    for the central reviewer allow-list (``reviewers.load_config``).
    """
    if text is None:
        return load_config(None, slug), load_policy(None)

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{GATE_CONFIG_PATH} is not valid YAML: {exc}") from exc

    if doc is None:
        raise ConfigError(
            f"{GATE_CONFIG_PATH} is empty. Remove the file for the defaults "
            f"(Cursor Bugbot, no policy), or configure the gate."
        )
    if not isinstance(doc, dict):
        raise ConfigError(f"{GATE_CONFIG_PATH} must be a mapping.")

    unknown = sorted(set(doc) - _TOP_KEYS)
    if unknown:
        raise ConfigError(
            f"{GATE_CONFIG_PATH}: unknown top-level key(s) "
            f"{', '.join(map(repr, unknown))}. Known keys: 'reviewers', 'policy', 'approver'. "
            f"(Policy keys like 'escalation_paths' go under 'policy:'.)"
        )

    # Re-serialise each section for the loader that validates it.
    reviewers_text = (
        yaml.safe_dump({"reviewers": doc["reviewers"]}) if "reviewers" in doc else None
    )

    # An empty `policy:` is likely a half-finished edit, so reject it rather
    # than silently run without a policy. `policy: {}` opts in with no rules.
    if "policy" in doc:
        if doc["policy"] is None:
            raise ConfigError(
                f"{GATE_CONFIG_PATH}: 'policy:' is present but empty. Remove the key "
                f"for the simple gate, or configure it (see ai-gate.example.yml)."
            )
        policy_text: str | None = yaml.safe_dump(doc["policy"])
    else:
        policy_text = None

    _approver_section(doc)
    return load_config(reviewers_text, slug), load_policy(policy_text)


def approver_fork_mode(text: str | None) -> str | None:
    """Return the repository's ``approver: forks:`` setting, or ``None``.

    The repository can only tighten the central ``approver.forks.mode``
    (``aigate.trust``); the approver takes the stricter of the two. Raises
    ``ConfigError`` on a malformed file, like ``load_gate_config``.
    """
    if text is None:
        return None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{GATE_CONFIG_PATH} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        return None
    section = _approver_section(doc)
    return None if section is None else section.get("forks")


def approver_risk_mode(text: str | None) -> str | None:
    """Return the repository's ``approver: risk:`` setting, or ``None``.

    ``block`` takes away the central ``approver.blocking_risk_unless`` checks
    (``aigate.trust``) here: a declared High/Critical risk then needs a person
    whoever opened the pull request. Raises ``ConfigError`` on a malformed
    file, like ``load_gate_config``.
    """
    if text is None:
        return None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{GATE_CONFIG_PATH} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        return None
    section = _approver_section(doc)
    return None if section is None else section.get("risk")


def _approver_section(doc: dict) -> dict | None:
    if "approver" not in doc:
        return None
    section = doc["approver"]
    where = f"{GATE_CONFIG_PATH}: 'approver'"
    if not isinstance(section, dict) or not section:
        raise ConfigError(f"{where} must be a non-empty mapping.")
    unknown = sorted(set(section) - _APPROVER_KEYS)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {', '.join(map(repr, unknown))}. "
            f"Known keys: {', '.join(sorted(_APPROVER_KEYS))}."
        )
    if "forks" in section and section["forks"] not in FORK_MODES:
        raise ConfigError(f"{where}.forks must be one of {', '.join(FORK_MODES)}.")
    if "risk" in section and section["risk"] not in RISK_MODES:
        raise ConfigError(f"{where}.risk must be one of {', '.join(RISK_MODES)}.")
    return section
