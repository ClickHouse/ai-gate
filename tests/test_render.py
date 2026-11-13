"""PR-controlled text cannot carry workflow commands or break the comment's
Markdown (Opus round 6, R6-7)."""

from __future__ import annotations

from aigate.escalation import _escalation_requirements
from aigate.policy import EscalationRule, GatePolicy
from aigate.render import safe_inline, stdout_safe


def test_stdout_safe_neutralises_workflow_commands_and_control_characters():
    hostile = "src/x.py\n::error title=pwned::hi\r%0A\x1b[31m"
    out = stdout_safe(hostile)
    assert "\n" not in out and "\r" not in out and "\x1b" not in out
    assert "::" not in out
    assert out == "src/x.py%0A: :error title=pwned: :hi%0D%250A[31m"


def test_safe_inline_is_a_code_span_that_backticks_cannot_close():
    assert safe_inline("src/app.py") == "`src/app.py`"
    assert safe_inline("a`b") == "``a`b``"
    assert safe_inline("``x``") == "``` ``x`` ```"
    assert safe_inline("`lead") == "`` `lead ``"
    # A link or heading in a path stays literal inside the span.
    assert safe_inline("[click](https://evil)") == "`[click](https://evil)`"


def test_escalation_reasons_render_paths_safely():
    policy = GatePolicy(
        escalation_paths=(
            EscalationRule(paths=("infra/**",), reviewers="@ClickHouse/security"),
        )
    )
    reqs = _escalation_requirements(
        policy, [], ["infra/a::warning::x%.tf", "infra/b`c.tf"]
    )
    # One requirement per approver: the first matching path's reason is kept.
    (req,) = reqs
    assert "::" not in req.reason and "\n" not in req.reason
    assert req.reason.startswith("`infra/a: :warning: :x%25.tf`")
    (req,) = _escalation_requirements(policy, [], ["infra/b`c.tf"])
    assert req.reason.startswith("``infra/b`c.tf``")


def test_every_path_sink_in_the_decision_summary_is_rendered_safely():
    """Round 7 (Codex 10): the finding lines, the still-open and the dismissed
    lines of the summary carry the PR's path through aigate.render, so the
    full summary holds no raw newline, no workflow command and no Markdown
    the path could inject (a link, a closed code span)."""
    from aigate.escalation import evaluate
    from aigate.ledger import Disposition, FindingVerdict
    from aigate.threads import ObservedFinding
    from aigate.tiers import TIER_1, TierResult

    hostile = "src/x.py\n::warning title=pwned::[click](https://evil)`"
    finding = ObservedFinding(
        thread_id="T1",
        path=hostile,
        is_resolved=False,
        is_outdated=False,
        resolved_by_login=None,
        author_login="cursor",
    )
    dismissed = ObservedFinding(
        thread_id="T2",
        path=hostile,
        is_resolved=True,
        is_outdated=False,
        resolved_by_login="someone",
        author_login="cursor",
    )
    tier = TierResult(tier=TIER_1, reasons=("ordinary application code",))
    open_ = FindingVerdict("T1", Disposition.UNRESOLVED, "no fix evidence")
    waved = FindingVerdict("T2", Disposition.HAND_WAVED, "resolver is not an owner")
    # A problem (the dismissal) and a still-open finding are reported in
    # different summaries, so both shapes are rendered.
    summaries = {
        "waiting": evaluate(
            policy=GatePolicy(),
            tier_result=tier,
            findings=[finding],
            verdicts=[open_],
            approvals=[],
            changed_paths=[hostile],
        ).summary,
        "problem": evaluate(
            policy=GatePolicy(),
            tier_result=tier,
            findings=[dismissed],
            verdicts=[waved],
            approvals=[],
            changed_paths=[hostile],
        ).summary,
    }
    rendered = safe_inline(hostile)
    for text in summaries.values():
        assert "\r" not in text and "::" not in text
        assert not any(line.startswith("::") for line in text.splitlines())
        # The raw path never appears; its safe rendering does, at every sink.
        assert hostile not in text
        assert rendered in text
        assert "[click](https://evil)" not in text.replace(rendered, "")

    def sinks(kind: str) -> list[str]:
        return [line for line in summaries[kind].splitlines() if rendered in line]

    assert any("finding still open" in line for line in sinks("waiting"))
    assert any(line.startswith("- [unresolved]") for line in sinks("waiting"))
    assert any("finding dismissed" in line for line in sinks("problem"))
    assert any(line.startswith("- [dismissed]") for line in sinks("problem"))


def test_an_unmapped_area_name_from_real_classification_is_rendered_safely():
    """Round 8 (Codex 2): the unknown top-level directory of a changed path is
    the pull request's; `classify` carries it into the tier reason, which the
    summary (and so the comment) prints. Driven by the real classifier."""
    from aigate.escalation import evaluate
    from aigate.tiers import UNKNOWN_PATH_TIER, classify

    area = "**APPROVED — merge now**\n::warning::x`"
    hostile = [f"{area}/payload.unknown", f"{area}/other.bin"]
    tier = classify(hostile)
    assert tier.tier == UNKNOWN_PATH_TIER
    (reason,) = tier.reasons
    assert reason == f"unmapped area {safe_inline(area)} (unknown raises)"
    # Neutralised, and the bold survives only as literal text inside the span.
    assert "\n" not in reason and "::" not in reason
    assert reason.startswith("unmapped area ``") and reason.count("``") == 2
    decision = evaluate(
        policy=GatePolicy(),
        tier_result=tier,
        findings=[],
        verdicts=[],
        approvals=[],
        changed_paths=hostile,
    )
    first = decision.summary.splitlines()[0]
    assert first == f"Risk tier {tier.tier}: {reason}"
    assert area not in decision.summary and safe_inline(area) in decision.summary
    assert not any(line.startswith("::") for line in decision.summary.splitlines())
