"""Load the per-repository gate config, ``.github/ai-gate.yml``.

Every key is optional:

    reviewers: [bugbot]          # extra AI reviewers to require
    policy:                      # tiers, escalation paths, dismissal sign-off
      escalation_paths:
        - paths: ["auth/**"]
          reviewers: "@org/security"
      security_dismissal_reviewers: "@org/security"

With no file, the gate requires the default reviewer and applies no policy.
The file is read from the base branch, and a malformed file fails closed. This
module splits the document; ``reviewers.load_config`` and
``policy.load_policy`` validate each section.
"""

from __future__ import annotations

import yaml

from .policy import GatePolicy, load_policy
from .reviewers import ConfigError, load_config

GATE_CONFIG_PATH = ".github/ai-gate.yml"

_TOP_KEYS = frozenset({"reviewers", "policy"})


def load_gate_config(text: str | None) -> tuple[list[str], GatePolicy | None]:
    """Resolve ``(required_reviewers, policy)`` from ``.github/ai-gate.yml``.

    ``text`` is the file from the base branch, or ``None`` when absent (the
    defaults). An empty file or an unknown top-level key is an error, so a
    config that silently does nothing cannot pass.
    """
    if text is None:
        return load_config(None), load_policy(None)

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
            f"{', '.join(map(repr, unknown))}. Known keys: 'reviewers', 'policy'. "
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

    return load_config(reviewers_text), load_policy(policy_text)
