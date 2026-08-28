"""Shared numeric tolerance.

Every price crossing a boundary - parsed from CSV, read from the API, compared,
or rendered - goes through money(). Float arithmetic otherwise leaks artifacts
like 4.790000000000001 into comparisons and onto the operator's screen.
"""

TOLERANCE = 0.001


def money(value):
    """Round to cents. None passes through."""
    return None if value is None else round(float(value), 2)


def same(a, b):
    """True only when both are real numbers within one tenth of a cent.

    None is never equal to anything, including another None: a missing list
    price and a list price of 0.0 are different facts, and callers rely on
    that distinction.
    """
    return a is not None and b is not None and abs(a - b) <= TOLERANCE
