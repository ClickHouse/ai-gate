# AI review gate

A GitHub Action that blocks merge until a pull request has a clean review from an
AI reviewer ([Cursor Bugbot](https://cursor.com/bugbot) by default). While the
gate is red, it keeps one comment on the pull request saying why and what to do.

## Usage

```yaml
# .github/workflows/ai-gate.yml
name: AI review gate
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions:
  contents: read
  checks: write
  pull-requests: write
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: ClickHouse/ai-gate@v0.1.6
        with:
          wait-seconds: "900"
```

The gate waits up to `wait-seconds` for the reviewer. If no review arrives in
time, it fails.
Use `mode: shadow` to see the verdict without blocking.

To enforce it so that a pull request cannot change or skip it, require
[`.github/workflows/ai-gate-required.yml`](.github/workflows/ai-gate-required.yml)
through an organisation ruleset (**Require workflows to pass before merging**),
together with **Require approval of the most recent reviewable push**.

## Configuration

Optional, in `.github/ai-gate.yml` on the base branch:

```yaml
reviewers: [bugbot]
policy:
  escalation_paths:
    - paths: ["auth/**", "infra/**"]
      reviewers: "@your-org/security"
  security_dismissal_reviewers: "@your-org/security"
```

Listed reviewers are added to Bugbot. An operator can let a named repository
select another registered reviewer instead, through an optional allow-list
file next to the package (`aigate/reviewer_allowlist.yaml`, read by
`aigate.reviewers`); without that file Bugbot is always required. Such a
reviewer is pinned by App slug and App id, judged on its check's conclusion
alone (only `success` is clean), and is not eligible for auto-approval unless
the allow-list says so.

Without a `policy:`, a clean AI review passes. With one, dismissed findings,
escalation paths and [high-risk files](aigate/tiers.py) also need a human
approval. See [`ai-gate.example.yml`](ai-gate.example.yml) for every key.

An `approver:` block is read only by an auto-approver built on this package,
and can only make it stricter: `forks: never` keeps fork pull requests for
people even where the approver may co-sign a maintainer's approval of one
(`approver.forks` in [`aigate/trust_policy.yaml`](aigate/trust_policy.yaml)),
and `risk: block` keeps a person on every pull request a reviewer rates
High/Critical risk, even a maintainer's or a code owner's own
(`approver.blocking_risk_unless`).

## How it decides

- Only the reviewer's own GitHub App check counts, never a check name alone.
- The review must be for this pull request: not from before it was opened or
  retargeted, and not shared with another pull request on the same commit.
- Config and `CODEOWNERS` are read from the base branch.
- If the AI review is missing or not clean, an approval of the head commit from
  someone with write access (not the author) passes instead.
- A finding someone thinks is wrong is accepted when a maintainer of the
  repository (`maintain` or `admin` role, the pull request author included)
  replies in its thread and resolves it. A bare resolve, or one by someone
  with less access, is not. When every finding on the head commit is accepted
  and none is open, the gate passes.
- Findings stick to the commit: a later clean review of the same commit does
  not clear what an earlier review of it reported. Every review run on the
  commit is read, and the findings' threads must all still be there (a deleted
  one keeps the gate red). A new push starts fresh.
- Any error, or a review history it cannot read with certainty, fails closed.

## Development

```bash
uv sync
uv run pytest -q
uv run ruff check . && uv run black --check . && uv run mypy aigate --ignore-missing-imports
```

## License

[Apache 2.0](LICENSE). Report vulnerabilities as described in [SECURITY.md](SECURITY.md).
