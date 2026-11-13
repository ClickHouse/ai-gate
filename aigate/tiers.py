"""Capability-based risk tiering of a pull request's changed paths.

The tier sets how much human sign-off a change needs. It is derived from the
capability or trust boundary a path touches, with fail-safe defaults, rather
than from an allowlist of safe paths, which is easy to evade by moving code.
This module only computes a tier and its reasons; ``escalation`` decides what a
tier requires.

* Monotonic: the tier is the ``max`` over all signals, so nothing lowers it.
* Unknown raises: a path no rule matches is ``TIER_2``.
* Nothing ordinary is free: ordinary code and tests are ``TIER_1``, since a
  test change can weaken the checks that judge everything else. Only
  documentation-like files are ``TIER_0``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache

from .render import safe_inline

# Tiers, low to high. The numbers are ordinal; compare with ``max``.
TIER_0 = 0  # documentation-like; lowest consequence
TIER_1 = 1  # ordinary code and tests; needs review coverage, not a human by default
TIER_2 = 2  # elevated: dependencies, unknown areas; a human is involved
TIER_3 = 3  # trust root / sensitive capability; a human is always required


@dataclass(frozen=True)
class Rule:
    """One capability pattern: paths matching ``glob`` carry at least ``tier``."""

    glob: str
    tier: int
    capability: str  # human-readable reason, surfaced in the decision


# Order is for readability only; the classifier takes the max. Globs match the
# full repo-relative path with the semantics described in ``_compile``.
SENSITIVITY_MAP: tuple[Rule, ...] = (
    # --- Trust root: the things that govern the rules themselves (TIER_3) ---
    Rule(".github/workflows/**", TIER_3, "CI/CD workflow (runs with repo privileges)"),
    Rule(".github/actions/**", TIER_3, "composite action (runs with repo privileges)"),
    # Repository control plane, including CODEOWNERS and the gate's own config.
    Rule(".github/**", TIER_3, "repository control plane (.github config)"),
    # Instructions the AI reviewer reads, at any depth: a pull request that
    # edits them can steer its own review.
    Rule("**/.cursor/**", TIER_3, "AI reviewer instructions (.cursor)"),
    Rule("**/BUGBOT.md", TIER_3, "AI reviewer instructions (BUGBOT.md)"),
    Rule("**/.cursorrules", TIER_3, "AI reviewer instructions (.cursorrules)"),
    Rule("atlantis.yaml", TIER_3, "Atlantis config"),
    Rule("atlantis*/**", TIER_3, "Atlantis config"),
    Rule("**/atlantis.yaml", TIER_3, "Atlantis config"),
    Rule("**/CODEOWNERS", TIER_3, "code ownership / review routing"),
    Rule(".github/CODEOWNERS", TIER_3, "code ownership / review routing"),
    Rule("**/*.tf", TIER_3, "infrastructure as code (Terraform)"),
    Rule("**/*.tfvars", TIER_3, "infrastructure as code (Terraform vars)"),
    Rule("infra/**", TIER_3, "infrastructure as code"),
    Rule("**/Dockerfile", TIER_3, "container build (base image, runtime)"),
    # Build files run arbitrary commands. CMakeLists.txt would otherwise match
    # the ``*.txt`` documentation rule.
    Rule("**/CMakeLists.txt", TIER_3, "build system (CMake)"),
    Rule("**/*.cmake", TIER_3, "build system (CMake)"),
    Rule("**/Makefile", TIER_3, "build system (Make)"),
    Rule("**/*.mk", TIER_3, "build system (Make)"),
    # --- Sensitive capabilities (TIER_3) ---
    Rule("**/auth/**", TIER_3, "authentication / authorisation"),
    Rule("**/authz/**", TIER_3, "authorisation"),
    Rule("**/crypto/**", TIER_3, "cryptography"),
    Rule("**/*secret*", TIER_3, "secret handling"),
    Rule("**/migrations/**", TIER_3, "database migration"),
    Rule("**/*.rego", TIER_3, "policy as code (OPA)"),
    # --- Elevated (TIER_2) ---
    Rule("**/package.json", TIER_2, "dependency manifest"),
    Rule("**/package-lock.json", TIER_2, "dependency lockfile"),
    Rule("**/requirements*.txt", TIER_2, "dependency manifest"),
    Rule("**/pyproject.toml", TIER_2, "dependency manifest"),
    Rule("**/go.mod", TIER_2, "dependency manifest"),
    Rule("**/go.sum", TIER_2, "dependency lockfile"),
    Rule("**/Cargo.toml", TIER_2, "dependency manifest"),
    Rule("**/Cargo.lock", TIER_2, "dependency lockfile"),
    # --- Tests (TIER_1): a test change can weaken the review oracle ---
    Rule("**/test_*.py", TIER_1, "test (affects the review oracle)"),
    Rule("**/*_test.go", TIER_1, "test (affects the review oracle)"),
    Rule("**/tests/**", TIER_1, "test (affects the review oracle)"),
    # --- Known ordinary code areas (TIER_1): not free, but not sensitive ---
    # Extend as areas are vetted. Unmatched paths get UNKNOWN_PATH_TIER.
    Rule("src/**", TIER_1, "ordinary application code"),
    Rule("app/**", TIER_1, "ordinary application code"),
    Rule("lib/**", TIER_1, "ordinary application code"),
    Rule("pkg/**", TIER_1, "ordinary application code"),
    Rule("cmd/**", TIER_1, "ordinary application code"),
    Rule("internal/**", TIER_1, "ordinary application code"),
    Rule("services/**", TIER_1, "ordinary application code"),
    # --- Documentation-like (TIER_0) ---
    Rule("**/*.md", TIER_0, "documentation"),
    Rule("**/*.txt", TIER_0, "documentation"),
    Rule("**/LICENSE", TIER_0, "licence"),
)

# Tier of a path no rule matches. Lowering an area is a deliberate edit to
# SENSITIVITY_MAP, never a default.
UNKNOWN_PATH_TIER = TIER_2

# Tier of a path matched only by an ordinary-code rule.
ORDINARY_CODE_TIER = TIER_1


@dataclass(frozen=True)
class TierResult:
    """The computed tier and the reasons behind it."""

    tier: int
    reasons: tuple[str, ...]  # one line per capability that drove the tier

    @property
    def requires_human_always(self) -> bool:
        return self.tier >= TIER_3


@cache
def _compile(glob: str) -> re.Pattern[str]:
    """Translate a glob to an anchored regex.

    ``**/`` matches zero or more leading directories, ``**`` matches across
    directory separators, ``*`` matches within a single path segment, and ``?``
    matches one non-separator character.
    """
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        if glob[i : i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif glob[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches(path: str, glob: str) -> bool:
    return _compile(glob).match(path) is not None


def path_matches(path: str, glob: str) -> bool:
    """Match ``path`` against ``glob`` with the sensitivity map's semantics.

    Public so policy globs use the same dialect as the tier rules.
    """
    return _matches(path, glob)


def _top_level(path: str) -> str:
    norm = path.lstrip("/")
    return norm.split("/", 1)[0] if "/" in norm else norm


def classify_path(path: str) -> tuple[int, str]:
    """Return the ``(tier, reason)`` a single changed path carries.

    The highest matching tier wins; an unmatched path gets ``UNKNOWN_PATH_TIER``.
    """
    best_tier = -1
    best_reason = ""
    for rule in SENSITIVITY_MAP:
        if rule.tier > best_tier and _matches(path, rule.glob):
            best_tier, best_reason = rule.tier, rule.capability

    if best_tier >= 0:
        return best_tier, best_reason
    # The directory name is the pull request's: rendered through aigate.render
    # so it reaches the summary and the comment as a code span, never as
    # Markdown or a workflow command (round 8, Codex 2).
    return (
        UNKNOWN_PATH_TIER,
        f"unmapped area {safe_inline(_top_level(path))} (unknown raises)",
    )


def classify(
    changed_paths: list[str],
) -> TierResult:
    """Compute a pull request's risk tier from its changed paths.

    The reasons are those of every path at the final tier.
    """
    contributions: list[tuple[int, str]] = []

    for path in changed_paths:
        contributions.append(classify_path(path))

    if not contributions:
        return TierResult(tier=TIER_0, reasons=("no changes",))

    tier = max(t for t, _ in contributions)
    reasons = tuple(dict.fromkeys(r for t, r in contributions if t == tier))
    return TierResult(tier=tier, reasons=reasons)
