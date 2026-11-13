"""Render pull-request-controlled text safely into stdout, the step summary and
the PR comment.

File paths come from the pull request. The gate prints them on stdout (where a
line starting with ``::`` is a GitHub Actions workflow command), writes them to
the step summary and posts them in its Markdown comment. Under
``pull_request_target`` that comment is posted on fork pull requests too, so
an outside contributor's path reaches the bot's comment.

``safe_inline`` renders such text as a Markdown code span whose delimiter is
longer than any backtick run inside it, with control characters and the
workflow-command introducer neutralised the way GitHub documents for command
values (``%`` -> ``%25``, ``\\r`` -> ``%0D``, ``\\n`` -> ``%0A``); ``::`` becomes
``: :`` so no printed line can start a command. Social engineering, not
privilege, is what this stops (Opus round 6, R6-7).
"""

from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def stdout_safe(text: str) -> str:
    """Neutralise workflow-command syntax and control characters in ``text``."""
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    text = text.replace("::", ": :")
    return _CONTROL.sub("", text)


def safe_inline(text: str) -> str:
    """``text`` as a Markdown code span that cannot break out, stdout-safe too."""
    text = stdout_safe(text)
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"
