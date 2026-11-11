"""The registry of accepted AI reviewers and each repository's required set.

A repository may add reviewers under ``reviewers:`` in ``.github/ai-gate.yml``,
read from the base branch. ``BASELINE_REVIEWERS`` is always required and cannot
be removed.

A reviewer is identified by the GitHub App that posts its check run
(``app_slug``). Check names are free text: any workflow or installed app can
post a green check called "Cursor Bugbot", so a name alone never matches. The
name then picks the review check out of the app's checks (see
``gate.is_reviewer_run``).
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml


class ConfigError(ValueError):
    """The repository's ai-gate.yml is malformed or names an unknown reviewer."""


@dataclass(frozen=True)
class Reviewer:
    key: str
    """The value written under ``reviewers:`` in ai-gate.yml, e.g. ``bugbot``."""

    label: str
    """Human name for messages, e.g. ``Cursor Bugbot``."""

    app_slug: str
    """The slug of the GitHub App that posts this reviewer's check run.

    This is what the gate matches on: a check counts only if this app posted
    it. Confirm the slug with ``python -m aigate identify --pr <n>``. A wrong
    slug matches nothing, so the gate stays red rather than passing.
    """

    check_name: str
    """The name of the reviewer's review check, among the checks its app posts.

    Matched together with ``app_slug``. If the vendor renames the check, the gate
    stays pending until this is updated.
    """

    auto_approve_eligible: bool = False
    """Whether a consumer may auto-approve on this reviewer's verdict.

    True only for a reviewer that posts from an external app whose credentials a
    pull request cannot use. A reviewer that runs in the repository's own CI
    (posting with ``GITHUB_TOKEN`` or a shared bot) can be forged by the author:
    it may still be required, but should not trigger an automatic approval.
    Defaults to False, so a new reviewer is never eligible by accident.
    """


# Reviewers a repository may require. The set is closed on purpose: an unknown
# name is a typo or an unvetted tool, and fails config validation.
REVIEWERS: dict[str, Reviewer] = {
    "bugbot": Reviewer(
        key="bugbot",
        label="Cursor Bugbot",
        # Verify with `python -m aigate identify --pr <n>` against a live run.
        app_slug="cursor",
        check_name="Cursor Bugbot",
        # Posts from Cursor's own app, which a pull request cannot post as.
        auto_approve_eligible=True,
    ),
    # Example of another reviewer. One that runs in the repository's own CI
    # must use auto_approve_eligible=False.
    # "coderabbit": Reviewer("coderabbit", "CodeRabbit", "coderabbitai",
    #     "CodeRabbit", auto_approve_eligible=True),
}


def not_auto_approve_eligible(keys: list[str]) -> list[str]:
    """Return the reviewer keys whose verdict the pull request author could forge.

    A consumer should not auto-approve when this is non-empty. Unknown keys are
    treated as not eligible.
    """
    return [
        key
        for key in keys
        if not (key in REVIEWERS and REVIEWERS[key].auto_approve_eligible)
    ]


#: Reviewers always required, whatever ai-gate.yml says. A repository may add
#: reviewers but not remove these. Bugbot posts from Cursor's own GitHub App,
#: which no workflow in the target repository can impersonate.
BASELINE_REVIEWERS: tuple[str, ...] = ("bugbot",)

#: Reviewers required when ai-gate.yml has no ``reviewers:`` key or no file.
DEFAULT_REVIEWERS: tuple[str, ...] = BASELINE_REVIEWERS

# Named in error messages: the file the author edits.
CONFIG_PATH = ".github/ai-gate.yml"


def load_config(text: str | None) -> list[str]:
    """Resolve the reviewer keys this repository requires.

    ``text`` is the ``reviewers:`` section of ai-gate.yml from the base branch,
    or ``None`` when absent, which means the default. An empty or reviewer-less
    section is an error, not an opt-out.
    """
    if text is None:
        return list(DEFAULT_REVIEWERS)

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{CONFIG_PATH} is not valid YAML: {exc}") from exc

    if doc is None:
        raise ConfigError(
            f"{CONFIG_PATH} is empty. Remove the file to take the default "
            f"({', '.join(DEFAULT_REVIEWERS)}), or list the reviewer(s) to require."
        )
    if not isinstance(doc, dict) or "reviewers" not in doc:
        raise ConfigError(f"{CONFIG_PATH} must be a mapping with a 'reviewers' list.")

    reviewers = doc["reviewers"]
    if not isinstance(reviewers, list) or not reviewers:
        raise ConfigError(
            f"{CONFIG_PATH}: 'reviewers' must be a non-empty list. A repository "
            f"cannot opt out of review by listing none."
        )

    keys: list[str] = []
    for item in reviewers:
        if not isinstance(item, str):
            raise ConfigError(
                f"{CONFIG_PATH}: reviewer entries must be strings, got {item!r}."
            )
        if item not in REVIEWERS:
            known = ", ".join(sorted(REVIEWERS))
            raise ConfigError(
                f"{CONFIG_PATH}: unknown reviewer {item!r}. Known reviewers: {known}. "
                f"Add it to aigate/reviewers.py (with its confirmed app slug) first."
            )
        keys.append(item)

    # Listed reviewers add to the baseline; they never replace it.
    return _with_baseline(keys)


def _with_baseline(keys: list[str]) -> list[str]:
    """Return BASELINE_REVIEWERS followed by ``keys``, de-duplicated, order kept."""
    ordered = list(BASELINE_REVIEWERS) + keys
    seen: set[str] = set()
    result: list[str] = []
    for key in ordered:
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result
