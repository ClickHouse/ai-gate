"""The registry of accepted AI reviewers and each repository's required set.

A repository may add reviewers under ``reviewers:`` in ``.github/ai-gate.yml``,
read from the base branch. ``BASELINE_REVIEWERS`` is always required and cannot
be removed by the repository: only a central allow-list
(``REVIEWER_ALLOWLIST_FILE``, held next to this module by the operator of the
gate, not by the repository) may let a named repository select another
reviewer in its place. See "Requirements for a reviewer the approver trusts"
in the README.

A reviewer is identified by the GitHub App that posts its check run
(``app_slug``). Check names are free text: any workflow or installed app can
post a green check called "Cursor Bugbot", so a name alone never matches. The
name then picks the review check out of the app's checks (see
``gate.is_reviewer_run``).
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml


class ConfigError(ValueError):
    """The repository's ai-gate.yml is malformed or names an unknown reviewer."""


# Apps whose check runs any workflow in the repository can post, so a check
# "from" them proves nothing about who reviewed: never a reviewer identity.
# `github-actions` is the GITHUB_TOKEN; a bespoke reviewer must be its own
# GitHub App with its own slug, added here through a security-reviewed change.
FORBIDDEN_REVIEWER_APP_SLUGS = frozenset({"github-actions"})


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

    Granted centrally, never by a repository: for a built-in reviewer here, for
    an allow-listed one in its ``REVIEWER_ALLOWLIST_FILE`` entry.

    True only for a reviewer that posts from an external app whose credentials a
    pull request cannot use. A reviewer that runs in the repository's own CI
    (posting with ``GITHUB_TOKEN`` or a shared bot) can be forged by the author:
    it may still be required, but should not trigger an automatic approval.
    Defaults to False, so a new reviewer is never eligible by accident.
    """

    app_id: int | None = None
    """The numeric id of the GitHub App that posts the check, when pinned.

    Matched together with ``app_slug`` when set; required for an allow-listed
    reviewer, so a renamed or re-registered app with the same slug never
    matches. ``None`` matches on the slug alone (the built-in reviewers).
    """

    has_threads: bool = True
    """Whether the reviewer posts its findings as review threads.

    A conclusion-only reviewer (``False``) is judged from its check's
    conclusion alone: it has no thread ledger, so a non-clean verdict is
    cleared only by a re-review or a human approval, never by resolving or
    accepting a finding.
    """

    thread_login: str | None = None
    """The bot login that opens the reviewer's finding threads, if it has any."""

    floor_globs: tuple[str, ...] = field(default=())
    """The reviewer's own instruction and configuration files.

    Changing one always needs a human approval (the approver's floor), so a
    pull request cannot rewrite the reviewer's rules and ride its verdict.
    """

    def __post_init__(self) -> None:
        if self.app_slug in FORBIDDEN_REVIEWER_APP_SLUGS:
            raise ValueError(
                f"reviewer {self.key!r}: app slug {self.app_slug!r} is not a "
                f"reviewer identity (any workflow can post as it)"
            )


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
        thread_login="cursor[bot]",
    ),
    # Example of another reviewer. One that runs in the repository's own CI
    # must use auto_approve_eligible=False.
    # "coderabbit": Reviewer("coderabbit", "CodeRabbit", "coderabbitai",
    #     "CodeRabbit", auto_approve_eligible=True),
}


#: The central allow-list: reviewers registered outside this module and the
#: repositories that may select one of them instead of the baseline. It lives
#: next to this module, is changed only through a reviewed change to the
#: gate's own repository, and is absent from copies that ship without it (then
#: nothing is allow-listed and the baseline is always required).
REVIEWER_ALLOWLIST_FILE = pathlib.Path(__file__).with_name("reviewer_allowlist.yaml")

#: Repository (``owner/name``, lower-case) -> reviewer keys it may select in
#: place of ``BASELINE_REVIEWERS``. Filled from ``REVIEWER_ALLOWLIST_FILE``.
BASELINE_REPLACEMENTS: dict[str, frozenset[str]] = {}

_ALLOWLIST_REVIEWER_KEYS = frozenset(
    {
        "label",
        "app_slug",
        "app_id",
        "check_name",
        "auto_approve_eligible",
        "has_threads",
        "floor_globs",
    }
)


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a repeated mapping key.

    PyYAML keeps the last of two equal keys, so a stray second
    ``auto_approve_eligible: true`` would win silently.
    """


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)  # type: ignore[no-untyped-call]
        if key in seen:
            raise ConfigError(f"duplicate key {key!r} in the reviewer allow-list")
        seen.add(key)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def parse_allowlist(
    text: str, known: dict[str, Reviewer]
) -> tuple[dict[str, Reviewer], dict[str, frozenset[str]]]:
    """Parse the allow-list into ``(extra reviewers, baseline replacements)``.

    Strict: any unknown key, missing field or wrong type raises ``ConfigError``,
    so a malformed allow-list never silently allows or drops anything.
    """
    where = REVIEWER_ALLOWLIST_FILE.name
    try:
        doc = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{where} is not valid YAML: {exc}") from exc
    if doc is None:
        return {}, {}
    if not isinstance(doc, dict) or set(doc) - {"reviewers", "repositories"}:
        raise ConfigError(
            f"{where} must be a mapping of 'reviewers' and 'repositories'."
        )

    for section in ("reviewers", "repositories"):
        if section in doc and not isinstance(doc[section], dict):
            raise ConfigError(f"{where}: '{section}' must be a mapping.")
    extra: dict[str, Reviewer] = {}
    for key, entry in (doc.get("reviewers") or {}).items():
        if not isinstance(key, str) or key in known:
            raise ConfigError(f"{where}: reviewer {key!r} is not a new key.")
        if not isinstance(entry, dict) or set(entry) - _ALLOWLIST_REVIEWER_KEYS:
            raise ConfigError(f"{where}: reviewer {key!r} has unknown fields.")
        try:
            label, slug, name = entry["label"], entry["app_slug"], entry["check_name"]
            app_id = entry["app_id"]
        except KeyError as exc:
            raise ConfigError(f"{where}: reviewer {key!r} needs {exc}.") from exc
        eligible = entry.get("auto_approve_eligible", False)
        threads = entry.get("has_threads", False)
        globs = entry.get("floor_globs", [])
        if not all(isinstance(v, str) and v for v in (label, slug, name)):
            raise ConfigError(f"{where}: reviewer {key!r} has an empty name field.")
        if not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0:
            raise ConfigError(f"{where}: reviewer {key!r} needs a numeric app_id.")
        if not isinstance(eligible, bool) or not isinstance(threads, bool):
            raise ConfigError(f"{where}: reviewer {key!r} has a non-boolean flag.")
        if threads:
            # The thread ledger knows one reviewer's comment format; another
            # reviewer's threads would be judged by rules written for it.
            raise ConfigError(
                f"{where}: reviewer {key!r}: only conclusion-only reviewers can "
                f"be allow-listed (has_threads must be false)."
            )
        if not isinstance(globs, list) or not all(
            isinstance(g, str) and g for g in globs
        ):
            raise ConfigError(
                f"{where}: reviewer {key!r}: floor_globs must be strings."
            )
        try:
            extra[key] = Reviewer(
                key=key,
                label=label,
                app_slug=slug,
                check_name=name,
                auto_approve_eligible=eligible,
                app_id=app_id,
                has_threads=False,
                floor_globs=tuple(globs),
            )
        except ValueError as exc:
            raise ConfigError(f"{where}: {exc}") from exc

    allowed = {**known, **extra}
    replacements: dict[str, frozenset[str]] = {}
    for repo, entry in (doc.get("repositories") or {}).items():
        if not isinstance(repo, str) or repo.count("/") != 1:
            raise ConfigError(f"{where}: repository {repo!r} must be 'owner/name'.")
        if not isinstance(entry, dict) or set(entry) != {"replaces_baseline_with"}:
            raise ConfigError(
                f"{where}: repository {repo!r} must set only 'replaces_baseline_with'."
            )
        keys = entry["replaces_baseline_with"]
        if (
            not isinstance(keys, list)
            or not keys
            or not all(isinstance(k, str) and k in allowed for k in keys)
        ):
            raise ConfigError(
                f"{where}: repository {repo!r}: replaces_baseline_with must list "
                f"known reviewers."
            )
        # Lookups are case-insensitive, so two keys differing only in case
        # would silently overwrite each other.
        if repo.lower() in replacements:
            raise ConfigError(
                f"{where}: repository {repo!r} is listed more than once "
                f"(repository names are case-insensitive)."
            )
        replacements[repo.lower()] = frozenset(keys)
    return extra, replacements


def _load_allowlist() -> None:
    if not REVIEWER_ALLOWLIST_FILE.exists():
        return
    extra, replacements = parse_allowlist(
        REVIEWER_ALLOWLIST_FILE.read_text(encoding="utf-8"), REVIEWERS
    )
    REVIEWERS.update(extra)
    BASELINE_REPLACEMENTS.update(replacements)


def thread_reviewer_login(keys: list[str]) -> str | None:
    """The finding-thread login of the first required reviewer that has threads.

    ``None`` when every required reviewer is conclusion-only: then no thread is
    a finding, whoever opened it.
    """
    for key in keys:
        rv = REVIEWERS.get(key)
        if rv is not None and rv.has_threads and rv.thread_login:
            return rv.thread_login
    return None


def reviewer_floor_globs(keys: list[str] | None = None) -> list[tuple[str, str]]:
    """``(glob, why)`` for the instruction files of ``keys`` (default: every reviewer)."""
    out: list[tuple[str, str]] = []
    for key in keys if keys is not None else list(REVIEWERS):
        rv = REVIEWERS.get(key)
        if rv is None:
            continue
        out.extend((g, f"{rv.label} reviewer instructions") for g in rv.floor_globs)
    return out


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

_load_allowlist()


def load_config(text: str | None, slug: str | None = None) -> list[str]:
    """Resolve the reviewer keys this repository requires.

    ``text`` is the ``reviewers:`` section of ai-gate.yml from the base branch,
    or ``None`` when absent, which means the default. An empty or reviewer-less
    section is an error, not an opt-out. ``slug`` (``owner/name``) is the
    repository: only one in ``BASELINE_REPLACEMENTS`` may list one of its
    allowed reviewers instead of the baseline, and then the baseline is not
    added. A repository without ``slug`` or not on the list always gets the
    baseline.
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

    # Listed reviewers add to the baseline; they replace it only where the
    # central allow-list says this repository may, and only when it selects one
    # of the allowed reviewers.
    allowed = BASELINE_REPLACEMENTS.get((slug or "").lower(), frozenset())
    if allowed & set(keys):
        return list(dict.fromkeys(keys))
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
