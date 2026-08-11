"""Bounded parsing of integer query parameters.

Every paginated endpoint used to do `min(int(request.args.get(...)), cap)`
directly, which had two holes: a non-numeric value ("?page=abc") raised
ValueError and surfaced as a 500 rather than a 400, and `min()` alone caps
only the top end — "?per_page=-1" sailed under it and reached SQLAlchemy's
.limit(-1), which SQLite reads as "no limit" and returns the whole table.
Clamping both ends in one place keeps every caller honest.
"""

from flask import request


def int_arg(name, default, minimum, maximum):
    """Read one integer query param, clamped to [minimum, maximum].

    A missing or unparseable value falls back to `default` rather than
    erroring: these are pagination knobs, and a junk ?page= is better
    answered with the first page than with a stack trace.
    """
    raw = request.args.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
    return max(minimum, min(value, maximum))
