"""Per-finding disposition: pure, given the observed facts about each finding.

* Proof, not silence, closes a fix. A finding is ``ADDRESSED`` only with machine
  evidence that the fix works. The reviewer no longer flagging it does not
  count: a cosmetic change can cause that, and the recheck is itself a model.
* A dismissal is a risk acceptance, so a person must make it. It is
  ``OWNER_ACCEPTED`` only when an eligible non-author owner resolved it;
  otherwise it is ``HAND_WAVED``.
* The reviewer reports no severity, so every finding is treated as one that
  matters: none may be closed by the author alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Disposition(Enum):
    """How a single finding was (or was not) resolved."""

    ADDRESSED = "addressed"  # fixed, with machine evidence — green
    OWNER_ACCEPTED = "owner-accepted"  # refuted, accepted by an eligible owner — green
    UNRESOLVED = "unresolved"  # still open / not yet reviewed — pending
    HAND_WAVED = "hand-waved"  # dismissed without authority or evidence — red


# Dispositions that let a pull request merge.
_GREEN = frozenset({Disposition.ADDRESSED, Disposition.OWNER_ACCEPTED})


@dataclass(frozen=True)
class FindingFacts:
    """The observed facts about one reviewer finding, gathered by the caller."""

    finding_id: str
    resolved: bool
    has_machine_evidence: bool
    resolver_is_eligible_owner: bool
    resolver_is_author: bool


@dataclass(frozen=True)
class FindingVerdict:
    finding_id: str
    disposition: Disposition
    reason: str

    @property
    def is_green(self) -> bool:
        return self.disposition in _GREEN


def dispose(facts: FindingFacts) -> FindingVerdict:
    """Decide one finding's disposition from its facts.

    A proven fix wins regardless of thread state. Otherwise an unresolved
    finding is pending, and a resolved one needs an eligible non-author owner.
    """
    # 1. Proven fix, whoever resolved it.
    if facts.has_machine_evidence:
        return _v(facts, Disposition.ADDRESSED, "fix proven by machine evidence")

    # 2. Not proven and not resolved: still open.
    if not facts.resolved:
        return _v(facts, Disposition.UNRESOLVED, "no fix evidence and not resolved")

    # 3. Resolved without evidence: a dismissal.
    if facts.resolver_is_author:
        return _v(
            facts,
            Disposition.HAND_WAVED,
            "refuted by the author; a non-author owner must accept the risk",
        )
    if facts.resolver_is_eligible_owner:
        return _v(
            facts,
            Disposition.OWNER_ACCEPTED,
            "risk accepted by an eligible non-author owner",
        )
    return _v(
        facts,
        Disposition.HAND_WAVED,
        "resolver is not an eligible owner for this finding",
    )


def _v(facts: FindingFacts, d: Disposition, reason: str) -> FindingVerdict:
    return FindingVerdict(finding_id=facts.finding_id, disposition=d, reason=reason)
