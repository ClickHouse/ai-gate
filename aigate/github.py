"""GitHub I/O for the gate, through the ``gh`` CLI.

Every GitHub call goes through here, so the decision modules (``gate``,
``escalation``, ``ledger``, ``tiers``) stay free of I/O. In Actions, ``gh``
authenticates with ``GITHUB_TOKEN``.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from dataclasses import dataclass

from .gate import PENDING, SUCCESS, Signal

# Where GitHub looks for CODEOWNERS, in its own precedence order.
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


class GitHubError(RuntimeError):
    pass


class GitHubRefusalError(GitHubError):
    """A deliberate fail-closed refusal of what GitHub returned, not a failed read.

    For example, a file list at the API's listing cap. Retrying returns the same
    result, so callers treat it as a verdict rather than a transient error.
    """


@dataclass(frozen=True)
class PullRef:
    number: int
    head_sha: str
    base_ref: str
    # When the pull request was opened (ISO 8601), if the source carried it.
    created_at: str | None = None


# Every gh call gets a timeout, so a hung gh fails this run instead of hanging
# the required workflow until the job times out.
GH_TIMEOUT_SECONDS = 60


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, timeout=GH_TIMEOUT_SECONDS, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(
            f"{' '.join(cmd[:3])} timed out after {GH_TIMEOUT_SECONDS}s"
        ) from exc


def gh_json(args: list[str]) -> object:
    """Run ``gh api <args>`` and parse stdout as JSON."""
    proc = _run(
        ["gh", "api", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitHubError(f"gh api {' '.join(args)} failed: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise GitHubError(f"gh api {' '.join(args)} returned non-JSON: {exc}") from exc


def read_event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise GitHubError(
            "GITHUB_EVENT_PATH is not set; this runs inside GitHub Actions."
        )
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise GitHubError("Event payload is not a JSON object.")
    return data


def repo_slug() -> str:
    slug = os.environ.get("GITHUB_REPOSITORY")
    if not slug:
        raise GitHubError("GITHUB_REPOSITORY is not set.")
    return slug


def resolve_pull(event: dict, slug: str) -> PullRef:
    """Find the pull request, head SHA and base branch for any trigger.

    ``check_run`` and ``status`` events (for a caller workflow triggered by
    them) do not always carry the pull request, so fall back to a lookup by
    commit.
    """
    pr = event.get("pull_request")
    if isinstance(pr, dict):
        return PullRef(
            pr["number"], pr["head"]["sha"], pr["base"]["ref"], pr.get("created_at")
        )

    check_run = event.get("check_run")
    if isinstance(check_run, dict):
        prs = check_run.get("pull_requests") or []
        if prs:
            p = prs[0]
            return PullRef(p["number"], p["head"]["sha"], p["base"]["ref"])
        head_sha = check_run.get("head_sha")
        if head_sha:
            return _pull_for_sha(slug, head_sha)

    sha = event.get("sha")  # status event
    if sha:
        return _pull_for_sha(slug, sha)

    raise GitHubError("Could not find a pull request for this event.")


def _pull_for_sha(slug: str, sha: str) -> PullRef:
    data = gh_json([f"repos/{slug}/commits/{sha}/pulls"])
    if not isinstance(data, list) or not data:
        raise GitHubError(f"No open pull request found for commit {sha}.")
    p = data[0]
    return PullRef(p["number"], p["head"]["sha"], p["base"]["ref"], p.get("created_at"))


def open_pull_refs_for_sha(slug: str, sha: str) -> list[PullRef]:
    """Return every open pull request whose head commit is ``sha``.

    A check run attaches to a commit, so GitHub shows it on every pull request
    with that head. A second pull request from the same commit into a base with
    weaker config could otherwise produce a green that also applies to the real
    one. Callers take the strictest verdict across all of them.
    """
    data = gh_json([f"repos/{slug}/commits/{sha}/pulls"])
    out: list[PullRef] = []
    if isinstance(data, list):
        for p in data:
            if not isinstance(p, dict) or p.get("state") != "open":
                continue
            head = (p.get("head") or {}).get("sha")
            base = (p.get("base") or {}).get("ref")
            if head == sha and base is not None:
                out.append(PullRef(p["number"], head, base, p.get("created_at")))
    return out


# More candidates than this from the commit search fails closed rather than
# reading every one.
_MAX_SHARED_HEAD_CANDIDATES = 20
# GitHub's pull request commits endpoint lists at most this many commits.
_PULL_COMMITS_CAP = 250
# How long a shared-commit answer is reused while the gate polls.
_SHARED_COMMIT_TTL_SECONDS = 120
_shared_commit_cache: dict[tuple[str, str, int], tuple[float, list[int]]] = {}


class NotIndexedYetError(GitHubError):
    """The commit search does not list this pull request yet."""


def other_pulls_containing(slug: str, sha: str, number: int) -> list[int]:
    """Return every other pull request, open or closed, that contains ``sha``.

    A check run belongs to a commit, and a clean review leaves nothing on the
    pull request it reviewed. Bugbot reviews a pull request's head, so any
    pull request that ever had ``sha`` as its head still contains it, even
    after later pushes; one stacked on top of this branch does not.
    ``commits/{sha}/pulls`` lists open and merged pull requests; the commit
    search also finds closed ones. The search must list ``number`` itself,
    which shows the token can see pull requests and the index has caught up.
    """
    key = (slug, sha, number)
    cached = _shared_commit_cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < _SHARED_COMMIT_TTL_SECONDS:
        return cached[1]

    found: set[int] = set()
    listed = gh_json([f"repos/{slug}/commits/{sha}/pulls"])
    for p in listed if isinstance(listed, list) else []:
        if isinstance(p, dict) and isinstance(p.get("number"), int):
            found.add(p["number"])
    data = gh_json(
        [
            "search/issues",
            "--method",
            "GET",
            "-f",
            f"q=repo:{slug} is:pr {sha}",
            "-f",
            "per_page=100",
        ]
    )
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not isinstance(data, dict):
        raise GitHubError(f"searching for pull requests on {sha}: unexpected response")
    if data.get("incomplete_results") or data.get("total_count", 0) > len(items):
        raise GitHubError(f"searching for pull requests on {sha}: incomplete results")
    hits = {int(i["number"]) for i in items if isinstance(i, dict) and "number" in i}
    if number not in hits:
        raise NotIndexedYetError(
            f"the commit search does not list #{number} for {sha[:8]} yet"
        )
    candidates = hits - found - {number}
    if len(candidates) > _MAX_SHARED_HEAD_CANDIDATES:
        raise GitHubError(
            f"{len(candidates)} pull requests mention {sha}; too many to check"
        )
    # A search hit may only mention the SHA in text; confirm it contains it.
    for n in sorted(candidates):
        commits = _gh_list(f"repos/{slug}/pulls/{n}/commits?per_page=100")
        if len(commits) >= _PULL_COMMITS_CAP:
            raise GitHubError(f"#{n} has too many commits to check for {sha[:8]}")
        if any(isinstance(c, dict) and c.get("sha") == sha for c in commits):
            found.add(n)
    found.discard(number)
    result = sorted(found)
    _shared_commit_cache[key] = (time.monotonic(), result)
    return result


def last_base_change(slug: str, number: int) -> str | None:
    """Return when ``number``'s base branch was last changed, or ``None``."""
    pages = gh_json([f"repos/{slug}/issues/{number}/timeline", "--paginate", "--slurp"])
    latest: str | None = None
    for page in pages if isinstance(pages, list) else []:
        for event in page if isinstance(page, list) else []:
            if isinstance(event, dict) and event.get("event") == "base_ref_changed":
                at = event.get("created_at")
                if isinstance(at, str) and (latest is None or at > latest):
                    latest = at
    return latest


def pull_ref(slug: str, pr_number: int) -> PullRef:
    """Look a pull request up by number (for commands run outside an event)."""
    data = gh_json([f"repos/{slug}/pulls/{pr_number}"])
    if not isinstance(data, dict):
        raise GitHubError(f"Pull request {slug}#{pr_number} not found.")
    return PullRef(
        data["number"],
        data["head"]["sha"],
        data["base"]["ref"],
        data.get("created_at"),
    )


def _gh_list(path: str) -> list:
    """Return every page of a list endpoint, flattened."""
    data = gh_json([path, "--paginate", "--slurp"])
    out: list = []
    if isinstance(data, list):
        for page in data:
            if isinstance(page, list):
                out.extend(page)
    return out


def read_config_text(slug: str, base_ref: str, path: str) -> str | None:
    """Fetch a file from the base branch, or ``None`` if it is absent.

    Reading from ``base_ref`` rather than the head means the pull request being
    judged cannot edit the configuration that judges it.
    """
    # `gh api` switches to POST when any `-f` field is passed, and a POST here
    # 404s, which would read as "file absent" and silently drop the config.
    # `--method GET` sends `ref` as a query parameter instead.
    proc = _run(
        [
            "gh",
            "api",
            f"repos/{slug}/contents/{path}",
            "--method",
            "GET",
            "-f",
            f"ref={base_ref}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if "404" in proc.stderr or "Not Found" in proc.stderr:
            return None
        raise GitHubError(
            f"reading {path} from {base_ref} failed: {proc.stderr.strip()}"
        )
    try:
        payload = json.loads(proc.stdout)
        content = payload["content"] if isinstance(payload, dict) else None
        if not isinstance(content, str):
            raise ValueError("not a file")
        return base64.b64decode(content).decode("utf-8")
    except (ValueError, KeyError) as exc:
        # A directory, a symlink or a garbled response: fail closed rather than
        # crash before the red check is posted.
        raise GitHubError(
            f"reading {path} from {base_ref}: unexpected response ({exc})"
        ) from exc


def read_codeowners(slug: str, base_ref: str) -> str | None:
    """Fetch CODEOWNERS from the base branch, or ``None`` if the repo has none.

    Read from the base so a pull request cannot make its author an owner.
    """
    for path in CODEOWNERS_PATHS:
        text = read_config_text(slug, base_ref, path)
        if text is not None:
            return text
    return None


def team_members(org: str, team_slug: str) -> frozenset[str]:
    """Return the lower-cased logins of a team's members, including child teams.

    Needs a token that can read org membership. The default ``GITHUB_TOKEN``
    cannot, so the error propagates and the gate fails closed rather than
    treating the team as empty.
    """
    members = _gh_list(f"orgs/{org}/teams/{team_slug}/members")
    return frozenset(
        m["login"].lower() for m in members if isinstance(m, dict) and m.get("login")
    )


class TeamMemberships:
    """Resolve and cache which named teams a login belongs to.

    Only teams a requirement names are fetched.
    """

    def __init__(self) -> None:
        self._cache: dict[str, frozenset[str]] = {}

    def members(self, handle: str) -> frozenset[str]:
        """Return the members of ``org/team`` (no leading ``@``)."""
        key = handle.lower()
        if key not in self._cache:
            org, team = handle.split("/", 1)
            self._cache[key] = team_members(org, team)
        return self._cache[key]

    def teams_of(self, login: str, handles: frozenset[str]) -> frozenset[str]:
        """Return the subset of ``handles`` (``org/team``) ``login`` belongs to."""
        return frozenset(h for h in handles if login.lower() in self.members(h))


def pull_reviews(slug: str, pr_number: int) -> list[dict]:
    """Return every review on a pull request."""
    return [
        r
        for r in _gh_list(f"repos/{slug}/pulls/{pr_number}/reviews")
        if isinstance(r, dict)
    ]


def pull_author(slug: str, pr_number: int) -> str | None:
    """Login of the user who opened the pull request, or ``None`` if absent."""
    data = gh_json([f"repos/{slug}/pulls/{pr_number}"])
    user = (data or {}).get("user") if isinstance(data, dict) else None
    return (user or {}).get("login")


def repo_role(slug: str, login: str) -> str | None:
    """Return ``login``'s base permission (``admin``, ``write``, ``read``, ``none``).

    Uses the legacy ``permission`` field rather than ``role_name``, because it
    folds ``maintain``, ``triage`` and custom roles into their base role.
    Returns ``None`` when GitHub reports no permission.
    """
    data = gh_json([f"repos/{slug}/collaborators/{login}/permission"])
    return data.get("permission") if isinstance(data, dict) else None


def _normalise_check_run(conclusion: str | None, status: str | None) -> str | None:
    if status != "completed":
        return PENDING
    if conclusion == SUCCESS:
        return SUCCESS
    return conclusion  # neutral, failure, cancelled, ...: all "not clean"


def signals_for(slug: str, sha: str) -> list[Signal]:
    """Return every check run on ``sha``, with the app that posted it."""
    # Without --slurp, --paginate concatenates one JSON object per page, which
    # is invalid JSON past the first page.
    data = gh_json([f"repos/{slug}/commits/{sha}/check-runs", "--paginate", "--slurp"])
    pages = data if isinstance(data, list) else [data]
    runs: list[dict] = []
    for page in pages:
        if isinstance(page, dict):
            runs.extend(page.get("check_runs") or [])
    out: list[Signal] = []
    for run in runs:
        app = run.get("app") or {}
        out.append(
            Signal(
                app_slug=app.get("slug"),
                name=run.get("name", ""),
                conclusion=_normalise_check_run(
                    run.get("conclusion"), run.get("status")
                ),
                completed_at=run.get("completed_at"),
                started_at=run.get("started_at"),
            )
        )
    return out


# The "list pull request files" endpoint silently truncates at this many files.
# Fail closed at the cap so a sensitive path cannot hide behind padding files.
_FILES_API_CAP = 3000


def changed_paths(slug: str, pr_number: int) -> list[str]:
    """Return the paths a pull request changes, for risk tiering.

    An unreadable, truncated or malformed response fails closed rather than
    returning a short list that would tier too low. Renames return both the new
    and the previous name, so renaming a sensitive file does not escape the tier.
    """
    data = gh_json([f"repos/{slug}/pulls/{pr_number}/files", "--paginate", "--slurp"])
    if not isinstance(data, list):
        raise GitHubError(
            f"unexpected response listing files for {slug}#{pr_number}: "
            f"expected a list of pages, got {type(data).__name__}"
        )
    paths: list[str] = []
    for page in data:
        if not isinstance(page, list):
            raise GitHubError(
                f"unexpected page shape listing files for {slug}#{pr_number}: "
                f"got {type(page).__name__}"
            )
        for f in page:
            if not isinstance(f, dict):
                continue
            if "filename" in f:
                paths.append(f["filename"])
            if f.get("previous_filename"):
                paths.append(f["previous_filename"])
    if len(paths) >= _FILES_API_CAP:
        raise GitHubRefusalError(
            f"{slug}#{pr_number} changes {len(paths)}+ files, at or past GitHub's "
            f"{_FILES_API_CAP}-file listing cap; the file list may be truncated, "
            f"so the risk tier cannot be trusted. Failing closed."
        )
    return paths


def review_threads(slug: str, pr_number: int) -> dict:
    """Fetch a pull request's review threads via GraphQL, as the raw payload.

    ``threads.parse_review_threads`` parses it. ``pageInfo`` lets a caller tell
    a truncated list from a complete one.
    """
    owner, name = slug.split("/", 1)
    query = """
    query($owner:String!, $name:String!, $pr:Int!) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          author { login }
          reviewThreads(first: 100) {
            pageInfo { hasNextPage }
            nodes {
              id
              isResolved
              isOutdated
              path
              resolvedBy { login }
              comments(first: 1) { nodes { url author { login } } }
            }
          }
        }
      }
    }
    """
    proc = _run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"pr={pr_number}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitHubError(f"gh api graphql failed: {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise GitHubError(f"gh api graphql returned non-JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GitHubError("GraphQL payload is not a JSON object.")
    return data


def post_check_run(
    slug: str,
    head_sha: str,
    name: str,
    status: str,
    conclusion: str | None,
    title: str,
    summary: str,
) -> None:
    """Post the gate's verdict as a check run on ``head_sha``.

    A run triggered by a reviewer's ``check_run`` does not update the pull
    request's own job status, but a check run on the head SHA does, so this is
    what lets the gate turn green once the review lands. ``status`` is
    ``completed`` (with a ``conclusion``) or ``in_progress``. Needs
    ``checks: write``.
    """
    args = [
        "--method",
        "POST",
        f"repos/{slug}/check-runs",
        "-f",
        f"name={name}",
        "-f",
        f"head_sha={head_sha}",
        "-f",
        f"status={status}",
        "-f",
        f"output[title]={title}",
        "-f",
        f"output[summary]={summary[:60000]}",
    ]
    if conclusion is not None:
        args += ["-f", f"conclusion={conclusion}"]
    proc = _run(["gh", "api", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitHubError(
            f"gh api POST repos/{slug}/check-runs failed: {proc.stderr.strip()}"
        )


def current_actor_login() -> str | None:
    """Return the authenticated login, or ``None`` when it cannot be read.

    ``GITHUB_TOKEN`` and App installation tokens fail ``/user``, so ``None`` is
    expected for them. Callers then match on ``user.type == "Bot"``.
    """
    try:
        data = gh_json(["user"])
    except GitHubError:
        return None
    return data.get("login") if isinstance(data, dict) else None


def list_issue_comments(slug: str, number: int) -> list[dict]:
    """Return every comment on pull request or issue ``number``."""
    return [
        c
        for c in _gh_list(f"repos/{slug}/issues/{number}/comments?per_page=100")
        if isinstance(c, dict)
    ]


def create_issue_comment(slug: str, number: int, body: str) -> None:
    """Post a comment on pull request or issue ``number``.

    Needs ``pull-requests: write``. The calling workflow never checks out the
    pull request's code, so this permission cannot be used to forge a verdict.
    """
    proc = _run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{slug}/issues/{number}/comments",
            "-f",
            f"body={body[:60000]}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitHubError(
            f"gh api POST repos/{slug}/issues/{number}/comments failed: "
            f"{proc.stderr.strip()}"
        )


def update_issue_comment(slug: str, comment_id: int, body: str) -> None:
    """Edit an existing comment in place. Needs ``pull-requests: write``."""
    proc = _run(
        [
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{slug}/issues/comments/{comment_id}",
            "-f",
            f"body={body[:60000]}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitHubError(
            f"gh api PATCH repos/{slug}/issues/comments/{comment_id} failed: "
            f"{proc.stderr.strip()}"
        )


# Markers meaning "this commit or pull request does not exist", as opposed to a
# failed read (rate limit, auth, 5xx). Matched against stderr only: the
# formatted error also embeds the SHA and number, which can contain "404" by
# chance. 422 is excluded because it is a generic validation failure.
_MISSING_COMMIT = ("404", "Not Found", "No commit found")


def _is_missing(exc: Exception) -> bool:
    message = str(exc)
    # gh_json formats "gh api <args> failed: <stderr>"; check only the stderr.
    # For an unrecognised shape, fall back to the whole message.
    _, _, stderr = message.partition(" failed: ")
    haystack = stderr if stderr else message
    return any(marker in haystack for marker in _MISSING_COMMIT)


def pull_detail(slug: str, pr_number: int) -> dict | None:
    """Return the raw pull request object, or ``None`` if it doesn't exist.

    Unlike ``pull_ref``, this keeps the full payload (for example ``draft``
    and ``created_at``).
    """
    try:
        data = gh_json([f"repos/{slug}/pulls/{pr_number}"])
    except GitHubError as exc:
        if _is_missing(exc):
            return None
        raise
    return data if isinstance(data, dict) else None
