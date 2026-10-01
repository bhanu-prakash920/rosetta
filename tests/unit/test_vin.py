"""rosetta.domain.vin: shape and check digit of vehicle identification numbers."""
from __future__ import annotations

import pytest

from rosetta.domain import vin as V

KNOWN_VALID = "1HGCM82633A004352"


def test_known_valid_vin_is_accepted():
    assert V.is_valid_vin(KNOWN_VALID) is True


def test_all_ones_vin_is_the_classic_valid_example():
    assert V.is_valid_vin("1" * 17) is True


def test_check_digit_of_known_vin():
    assert V.check_digit(KNOWN_VALID) == "3"


def test_check_digit_ignores_position_nine():
    altered = KNOWN_VALID[:8] + "9" + KNOWN_VALID[9:]
    assert V.check_digit(altered) == V.check_digit(KNOWN_VALID)


@pytest.mark.parametrize("digit", [d for d in "0123456789X" if d != "3"])
def test_every_wrong_check_digit_is_rejected(digit):
    assert V.is_valid_vin(KNOWN_VALID[:8] + digit + KNOWN_VALID[9:]) is False


@pytest.mark.parametrize("letter", ["I", "O", "Q"])
@pytest.mark.parametrize("position", [0, 5, 8, 16])
def test_letters_i_o_q_are_rejected_anywhere(letter, position):
    candidate = KNOWN_VALID[:position] + letter + KNOWN_VALID[position + 1:]
    assert V.is_valid_vin(candidate) is False


@pytest.mark.parametrize("candidate", ["", "1HGCM82633A00435", "1HGCM82633A0043522", KNOWN_VALID + "\n",
                                       " " + KNOWN_VALID[1:]])
def test_wrong_length_or_whitespace_is_rejected(candidate):
    assert V.is_valid_vin(candidate) is False


@pytest.mark.parametrize("candidate", [None, 12345678901234567, 1.5, KNOWN_VALID.encode(), [KNOWN_VALID],
                                       {"vin": KNOWN_VALID}, True])
def test_non_string_input_is_rejected_without_raising(candidate):
    assert V.is_valid_vin(candidate) is False


def test_lower_case_is_rejected():
    assert V.is_valid_vin(KNOWN_VALID.lower()) is False


@pytest.mark.parametrize("candidate", ["1HGCM82633A00435-", "1HGCM82633A00435é", "1HGCM 2633A004352"])
def test_characters_outside_the_alphabet_are_rejected(candidate):
    assert V.is_valid_vin(candidate) is False


def test_single_character_change_breaks_the_checksum():
    # 4 -> 5 at a position with a non-zero weight changes the weighted sum.
    assert V.is_valid_vin("1HGCM82633A005352") is False


def test_with_check_digit_repairs_a_broken_vin():
    broken = KNOWN_VALID[:8] + "0" + KNOWN_VALID[9:]
    assert V.with_check_digit(broken) == KNOWN_VALID


def test_with_check_digit_is_idempotent():
    assert V.with_check_digit(V.with_check_digit(KNOWN_VALID)) == KNOWN_VALID


def test_check_digit_x_is_produced_and_accepted():
    with_x = [v for v in (V.make_vin("7NV", s) for s in range(200)) if v[8] == "X"]
    assert with_x, "remainder 10 must occur within 200 consecutive serials"
    assert all(V.is_valid_vin(v) for v in with_x)


@pytest.mark.parametrize("wmi", ["7NV", "5PA", "3ST", "JKZ", "8VT", "WHX"])
@pytest.mark.parametrize("descriptor", ["EV1A2", "GT4B7"])
def test_make_vin_always_produces_valid_vins(wmi, descriptor):
    for serial in list(range(0, 300)) + [999_999, 123_456]:
        vin = V.make_vin(wmi, serial, descriptor=descriptor)
        assert len(vin) == 17
        assert vin.startswith(wmi + descriptor)
        assert V.is_valid_vin(vin), vin


def test_make_vin_embeds_the_serial_and_is_unique_per_serial():
    vins = {V.make_vin("7NV", s) for s in range(1000)}
    assert len(vins) == 1000
    assert V.make_vin("7NV", 42).endswith("000042")


def test_make_vin_wraps_serials_above_six_digits():
    assert V.make_vin("7NV", 1_000_007) == V.make_vin("7NV", 7)


@pytest.mark.parametrize("kwargs", [{"wmi": "7N", "serial": 1}, {"wmi": "7NVX", "serial": 1},
                                    {"wmi": "7NV", "serial": 1, "descriptor": "EV1"}])
def test_make_vin_rejects_wrong_part_lengths(kwargs):
    with pytest.raises(ValueError):
        V.make_vin(**kwargs)


def test_alphabet_has_no_forbidden_letters():
    assert len(V.VIN_ALPHABET) == 33
    assert not set("IOQ") & set(V.VIN_ALPHABET)
