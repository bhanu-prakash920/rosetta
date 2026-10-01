"""Reason codes for events that could not be normalised.

Every dead-lettered message carries exactly one of these, so the dashboard,
the agent and the replay job can group failures without parsing free text.
"""
from __future__ import annotations

NO_ADAPTER = "NO_ADAPTER"            # no active mapping for this OEM source
DECODE_ERROR = "DECODE_ERROR"        # bytes could not be parsed in the declared wire format
SCHEMA_MISMATCH = "SCHEMA_MISMATCH"  # a required source field is missing: the OEM changed format
TRANSFORM_ERROR = "TRANSFORM_ERROR"  # a value could not be converted
OVERSIZE = "OVERSIZE"                # payload larger than the intake limit
INVALID = "INVALID"                  # decoded and mapped, but failed canonical validation

ALL_REASONS = (NO_ADAPTER, DECODE_ERROR, SCHEMA_MISMATCH, TRANSFORM_ERROR, OVERSIZE, INVALID)


class NormalizeError(Exception):
    """Raised by an adapter. Carries a reason code, the field and a short detail."""

    __slots__ = ("reason", "field", "detail")

    def __init__(self, reason: str, field: str = "", detail: str = "") -> None:
        super().__init__(f"{reason}:{field}:{detail}")
        self.reason = reason
        self.field = field
        self.detail = detail


class SpecError(ValueError):
    """A mapping spec is malformed or asks for something that is not allowed."""
