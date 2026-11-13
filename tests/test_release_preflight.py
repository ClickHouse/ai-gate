"""The release preflight: each workflow copy runs exactly a trusted action @main."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

HERE = pathlib.Path(__file__).resolve().parent


def _load():
    script = HERE.parent / "release_preflight.py"
    if not script.exists():
        pytest.skip(
            "release_preflight.py lives in the security-integrations monorepo",
            allow_module_level=True,
        )
    spec = importlib.util.spec_from_file_location("release_preflight", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rp = _load()

PRIVATE = "ClickHouse/security-integrations/.github/actions/ai-gate"
PUBLIC = "ClickHouse/ai-gate"


def _workflow(tmp_path, action: str, ref: str, comment: str = "") -> pathlib.Path:
    path = tmp_path / "ai-gate-required.yml"
    tail = f"  # {comment}" if comment else ""
    path.write_text(f"jobs:\n  gate:\n    steps:\n      - uses: {action}@{ref}{tail}\n")
    return path


def test_the_pin_is_parsed_from_the_one_uses_line(tmp_path):
    assert rp.pinned(_workflow(tmp_path, PRIVATE, "main").read_text()) == (
        PRIVATE,
        "main",
    )
    assert rp.pinned(_workflow(tmp_path, PUBLIC, "main", "a comment").read_text()) == (
        PUBLIC,
        "main",
    )
    with pytest.raises(ValueError):
        rp.pinned("jobs: {}\n")
    two = _workflow(tmp_path, PRIVATE, "main").read_text()
    with pytest.raises(ValueError):
        rp.pinned(two + two)


@pytest.mark.parametrize("action", [PRIVATE, PUBLIC])
def test_a_trusted_action_at_main_passes(tmp_path, action):
    code, message = rp.check(_workflow(tmp_path, action, "main"))
    assert code == 0 and "trust root" in message


@pytest.mark.parametrize(
    "ref",
    [
        "v0.1.7",
        "ai-gate-v0.1.7",
        "6ef147947503337378373b116158373b6e883e45",
        "dev",
        "refs/heads/main",
    ],
)
def test_anything_but_main_is_refused(tmp_path, ref):
    code, message = rp.check(_workflow(tmp_path, PRIVATE, ref))
    assert code == 1 and "the gate runs @main" in message


@pytest.mark.parametrize(
    "action",
    [
        "stranger/security-integrations/.github/actions/ai-gate",
        "ClickHouse/security-integrations/.github/actions/other",
        "ClickHouse/ai-gate-fork",
    ],
)
def test_an_untrusted_action_is_refused(tmp_path, action):
    code, message = rp.check(_workflow(tmp_path, action, "main"))
    assert code == 1 and "not one of the trusted gate actions" in message


def test_the_shipped_workflow_passes():
    code, message = rp.check(rp.DEFAULT_WORKFLOW)
    assert code == 0, message
    assert rp.pinned(rp.DEFAULT_WORKFLOW.read_text()) == (PRIVATE, "main")
