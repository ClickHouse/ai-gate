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

The gate waits up to `wait-seconds` for the reviewer, then passes or fails.
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

Without a `policy:`, a clean AI review passes. With one, dismissed findings,
escalation paths and [high-risk files](aigate/tiers.py) also need a human
approval. See [`ai-gate.example.yml`](ai-gate.example.yml) for every key.

## How it decides

- Only the reviewer's own GitHub App check counts, never a check name alone.
- The review must be for this pull request: not from before it was opened or
  retargeted, and not shared with another pull request on the same commit.
- Config and `CODEOWNERS` are read from the base branch.
- If the AI review is missing or not clean, an approval of the head commit from
  someone with write access (not the author) passes instead.
- Any error fails closed.

## Development

```bash
uv sync
uv run pytest -q
uv run ruff check . && uv run black --check . && uv run mypy aigate --ignore-missing-imports
```

## License

[Apache 2.0](LICENSE). Report vulnerabilities as described in [SECURITY.md](SECURITY.md).
