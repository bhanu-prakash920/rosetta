"""rosetta.domain.errors: the reason codes and the exception that carries them."""
from __future__ import annotations

import pytest

from rosetta.domain import errors as E


def test_reason_codes_are_stable_strings():
    assert E.ALL_REASONS == ("NO_ADAPTER", "DECODE_ERROR", "SCHEMA_MISMATCH", "TRANSFORM_ERROR",
                             "OVERSIZE", "INVALID")


def test_reason_codes_are_unique():
    assert len(set(E.ALL_REASONS)) == len(E.ALL_REASONS)


def test_each_constant_equals_its_own_name():
    for name in E.ALL_REASONS:
        assert getattr(E, name) == name


def test_normalize_error_carries_reason_field_and_detail():
    err = E.NormalizeError(E.TRANSFORM_ERROR, "speed_kmh", "not a number")
    assert (err.reason, err.field, err.detail) == ("TRANSFORM_ERROR", "speed_kmh", "not a number")
    assert str(err) == "TRANSFORM_ERROR:speed_kmh:not a number"


def test_normalize_error_defaults_to_empty_field_and_detail():
    err = E.NormalizeError(E.OVERSIZE)
    assert (err.field, err.detail) == ("", "")
    assert str(err) == "OVERSIZE::"


def test_normalize_error_can_be_raised_and_caught_as_exception():
    with pytest.raises(E.NormalizeError) as info:
        raise E.NormalizeError(E.INVALID, "vin", "INVALID_VIN")
    assert info.value.reason == E.INVALID
    assert isinstance(info.value, Exception)


def test_normalize_error_is_not_a_spec_error():
    assert not issubclass(E.NormalizeError, E.SpecError)
    assert not issubclass(E.SpecError, E.NormalizeError)


def test_spec_error_is_a_value_error():
    assert issubclass(E.SpecError, ValueError)
    with pytest.raises(ValueError, match="bad spec"):
        raise E.SpecError("bad spec")
