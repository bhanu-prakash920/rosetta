"""rosetta.domain.dtc: finding OBD-II trouble codes in whatever shape an OEM sends them."""
from __future__ import annotations

import pytest

from rosetta.domain import dtc as D


@pytest.mark.parametrize("code", ["P0301", "C0035", "B1342", "U0100", "P0A80", "P3FFF", "P0000"])
def test_valid_codes(code):
    assert D.is_valid_dtc(code) is True


@pytest.mark.parametrize("code", ["p0301", "P4301", "X0301", "P030", "P03011", "P03G1", "", " P0301", "P0301 ",
                                  None, 301, ["P0301"], b"P0301"])
def test_invalid_codes(code):
    assert D.is_valid_dtc(code) is False


@pytest.mark.parametrize("raw", [None, "", [], (), "no codes here", 12345, {}])
def test_nothing_to_extract(raw):
    assert D.extract_dtcs(raw) == []


def test_extract_from_list():
    assert D.extract_dtcs(["P0301", "U0100"]) == ["P0301", "U0100"]


def test_extract_from_tuple_and_single_item_set():
    assert D.extract_dtcs(("C0035", "B1342")) == ["C0035", "B1342"]
    assert D.extract_dtcs({"P0420"}) == ["P0420"]


@pytest.mark.parametrize("raw", ["P0301,P0420", "P0301, P0420", "P0301;P0420", "P0301 P0420", "P0301|P0420",
                                 "P0301\nP0420", "[P0301][P0420]"])
def test_extract_from_separated_strings(raw):
    assert D.extract_dtcs(raw) == ["P0301", "P0420"]


def test_lower_case_is_normalised_to_upper():
    assert D.extract_dtcs("p0301,u0100") == ["P0301", "U0100"]
    assert D.extract_dtcs(["p0a80"]) == ["P0A80"]


def test_extract_from_free_text():
    text = "Engine misfire detected (code P0301), lost comms with ECM: U0100."
    assert D.extract_dtcs(text) == ["P0301", "U0100"]


def test_duplicates_are_removed_keeping_first_seen_order():
    assert D.extract_dtcs("U0100 P0301 U0100 p0301 C0035") == ["U0100", "P0301", "C0035"]


@pytest.mark.parametrize("raw", ["xP0420", "XP0420", "P04201", "P0420A", "AP0420B", "1P0420", "ABCP0420"])
def test_code_embedded_in_a_longer_token_is_not_extracted(raw):
    assert D.extract_dtcs(raw) == []


def test_punctuation_next_to_a_code_does_not_hide_it():
    assert D.extract_dtcs("-P0420-") == ["P0420"]
    assert D.extract_dtcs("code:P0420.") == ["P0420"]


@pytest.mark.parametrize("raw", ["P4420", "Z0420", "P042", "P04G0"])
def test_lookalikes_are_not_codes(raw):
    assert D.extract_dtcs(raw) == []


def test_list_with_non_string_items():
    assert D.extract_dtcs(["P0301", 42, None, "U0100"]) == ["P0301", "U0100"]


def test_codes_in_adjacent_list_items_do_not_merge():
    # Items are joined with a space, so two codes never fuse into one token.
    assert D.extract_dtcs(["P0301", "P0420"]) == ["P0301", "P0420"]


@pytest.mark.parametrize("code,system", [("P0301", "powertrain"), ("C0035", "chassis"),
                                         ("B1342", "body"), ("U0100", "network")])
def test_describe_reports_the_system(code, system):
    assert D.describe(code)["system"] == system
    assert D.describe(code)["code"] == code


@pytest.mark.parametrize("code,scope", [("P0301", "generic"), ("P2301", "generic"),
                                        ("P1301", "manufacturer"), ("P3301", "manufacturer")])
def test_describe_reports_the_scope(code, scope):
    assert D.describe(code)["scope"] == scope


@pytest.mark.parametrize("code", ["p0301", "nope", "", None])
def test_describe_rejects_invalid_codes(code):
    with pytest.raises(ValueError):
        D.describe(code)
