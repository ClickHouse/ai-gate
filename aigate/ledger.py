"""Per-finding disposition: pure, given the observed facts about each finding.

* Proof, not silence, closes a fix. A finding is ``ADDRESSED`` only with machine
  evidence that the fix works. The reviewer no longer flagging it does not
  count: a cosmetic change can cause that, and the recheck is itself a model.
* A dismissal is a risk acceptance, so a person must make it. It is
  ``OWNER_ACCEPTED`` when an eligible non-author owner resolved it, or when a
  maintainer of the repository (``maintain`` or ``admin`` role, the author
  included) replied in the thread and resolved it; otherwise it is
  ``HAND_WAVED``.
* The reviewer reports no severity, so every finding is treated as one that
  matters: an author without the maintainer role cannot close one alone, and a
  maintainer must say why (a reply) rather than only resolve.
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
    # The resolver has the maintain or admin role and replied in the thread.
    resolver_is_maintainer_with_reply: bool = False


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
    finding is pending, and a resolved one needs a maintainer who replied, or
    an eligible non-author owner.
    """
    # 1. Proven fix, whoever resolved it.
    if facts.has_machine_evidence:
        return _v(facts, Disposition.ADDRESSED, "fix proven by machine evidence")

    # 2. Not proven and not resolved: still open.
    if not facts.resolved:
        return _v(facts, Disposition.UNRESOLVED, "no fix evidence and not resolved")

    # 3. Resolved without evidence: a dismissal.
    if facts.resolver_is_maintainer_with_reply:
        return _v(
            facts,
            Disposition.OWNER_ACCEPTED,
            "risk accepted by a maintainer, with a reply",
        )
    if facts.resolver_is_author:
        return _v(
            facts,
            Disposition.HAND_WAVED,
            "resolved by the author, who is not a maintainer; a maintainer must "
            "reply and resolve it, or a non-author owner must accept the risk",
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
