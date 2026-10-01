"""rosetta.algorithms.geohash: encoding, decoding, masking and neighbours."""
from __future__ import annotations

import numpy as np
import pytest

from rosetta.algorithms import geohash as G


def test_known_vector():
    assert G.encode(57.64911, 10.40744, 11) == "u4pruydqqvj"


@pytest.mark.parametrize("lat,lon,precision,expected", [
    (57.64911, 10.40744, 5, "u4pru"),
    (0.0, 0.0, 5, "s0000"),
    (-90.0, -180.0, 6, "000000"),
    (90.0, 180.0, 6, "zzzzzz"),
    (48.8566, 2.3522, 6, "u09tvw"),
    (12.9716, 77.5946, 5, "tdr1v"),
])
def test_more_known_cells(lat, lon, precision, expected):
    assert G.encode(lat, lon, precision) == expected


def test_default_precision_is_seven():
    assert len(G.encode(12.9716, 77.5946)) == 7


@pytest.mark.parametrize("precision", range(1, 13))
def test_length_equals_precision_and_prefix_property_holds(precision):
    full = G.encode(57.64911, 10.40744, 12)
    assert G.encode(57.64911, 10.40744, precision) == full[:precision]


@pytest.mark.parametrize("lat,lon", [(90.0001, 0.0), (-90.0001, 0.0), (0.0, 180.0001), (0.0, -180.0001),
                                     (float("nan"), 0.0), (0.0, float("nan")), (float("inf"), 0.0)])
def test_out_of_range_coordinates_are_rejected(lat, lon):
    with pytest.raises(ValueError):
        G.encode(lat, lon)


def test_only_the_geohash_alphabet_is_used():
    rng = np.random.default_rng(1)
    for lat, lon in zip(rng.uniform(-90, 90, 200), rng.uniform(-180, 180, 200)):
        assert set(G.encode(lat, lon, 9)) <= set(G.BASE32)
    assert len(G.BASE32) == 32 and not set("ailo") & set(G.BASE32)


def test_decode_bbox_contains_the_encoded_point():
    rng = np.random.default_rng(2)
    for lat, lon in zip(rng.uniform(-90, 90, 300), rng.uniform(-180, 180, 300)):
        for precision in (1, 4, 7, 10):
            lat_lo, lat_hi, lon_lo, lon_hi = G.decode_bbox(G.encode(lat, lon, precision))
            assert lat_lo <= lat <= lat_hi
            assert lon_lo <= lon <= lon_hi


def test_decode_returns_the_centre_of_the_bbox():
    gh = "u4pruydqqvj"
    lat_lo, lat_hi, lon_lo, lon_hi = G.decode_bbox(gh)
    lat, lon = G.decode(gh)
    assert lat == pytest.approx((lat_lo + lat_hi) / 2)
    assert lon == pytest.approx((lon_lo + lon_hi) / 2)
    assert lat_lo < lat < lat_hi and lon_lo < lon < lon_hi


def test_decode_is_close_to_the_original_point():
    lat, lon = G.decode(G.encode(57.64911, 10.40744, 11))
    assert lat == pytest.approx(57.64911, abs=1e-5)
    assert lon == pytest.approx(10.40744, abs=1e-5)


def test_encode_of_decoded_centre_is_a_fixed_point():
    rng = np.random.default_rng(3)
    for lat, lon in zip(rng.uniform(-89, 89, 100), rng.uniform(-179, 179, 100)):
        gh = G.encode(lat, lon, 8)
        assert G.encode(*G.decode(gh), 8) == gh


def test_cell_shrinks_by_32_with_every_character():
    sizes = []
    for precision in range(1, 8):
        lat_lo, lat_hi, lon_lo, lon_hi = G.decode_bbox(G.encode(12.9716, 77.5946, precision))
        sizes.append((lat_hi - lat_lo) * (lon_hi - lon_lo))
    for bigger, smaller in zip(sizes, sizes[1:]):
        assert bigger / smaller == pytest.approx(32.0)


def test_empty_geohash_is_the_whole_world():
    assert G.decode_bbox("") == (-90.0, 90.0, -180.0, 180.0)


def test_decode_rejects_characters_outside_the_alphabet():
    with pytest.raises(KeyError):
        G.decode_bbox("u4a")


# ----------------------------------------------------------------------- mask
def test_mask_snaps_to_the_cell_centre():
    lat, lon, gh = G.mask(12.9716, 77.5946, 5)
    assert gh == G.encode(12.9716, 77.5946, 5)
    assert (lat, lon) == G.decode(gh)


def test_mask_gives_the_same_position_to_every_point_of_a_cell():
    a = G.mask(12.9716, 77.5946, 5)
    b = G.mask(12.9720, 77.5950, 5)
    assert a == b
    assert a[:2] != (12.9716, 77.5946)


def test_mask_error_is_bounded_by_the_cell_size():
    rng = np.random.default_rng(4)
    for lat, lon in zip(rng.uniform(-80, 80, 100), rng.uniform(-179, 179, 100)):
        mlat, mlon, gh = G.mask(lat, lon, 5)
        lat_lo, lat_hi, lon_lo, lon_hi = G.decode_bbox(gh)
        assert abs(mlat - lat) <= (lat_hi - lat_lo) / 2
        assert abs(mlon - lon) <= (lon_hi - lon_lo) / 2


def test_lower_precision_masks_more():
    _, _, fine = G.mask(12.9716, 77.5946, 6)
    _, _, coarse = G.mask(12.9716, 77.5946, 4)
    assert fine.startswith(coarse)
    assert G.CELL_KM[4][0] > G.CELL_KM[6][0]


# ---------------------------------------------------------------- encode_many
@pytest.mark.parametrize("precision", [1, 2, 5, 7, 9, 12])
def test_encode_many_agrees_with_encode_on_random_points(precision):
    rng = np.random.default_rng(100 + precision)
    lat, lon = rng.uniform(-90, 90, 2_000), rng.uniform(-180, 180, 2_000)
    many = G.encode_many(lat, lon, precision)
    assert many.shape == (2_000,)
    assert [g.decode() for g in many] == [G.encode(a, b, precision) for a, b in zip(lat, lon)]


def test_encode_many_at_the_corners_of_the_world():
    lat = np.array([-90.0, 90.0, 0.0, 90.0, -90.0])
    lon = np.array([-180.0, 180.0, 0.0, -180.0, 180.0])
    many = [g.decode() for g in G.encode_many(lat, lon, 6)]
    assert many == [G.encode(a, b, 6) for a, b in zip(lat, lon)]


def test_encode_many_accepts_lists_and_empty_input():
    assert [g.decode() for g in G.encode_many([57.64911], [10.40744], 11)] == ["u4pruydqqvj"]
    assert G.encode_many(np.array([]), np.array([]), 5).shape == (0,)


def test_encode_many_default_precision_is_five():
    assert G.encode_many(np.array([12.9716]), np.array([77.5946]))[0] == b"tdr1v"


# ------------------------------------------------------------------ neighbors
def _touches(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    lat_gap = max(a[0], b[0]) - min(a[1], b[1])
    lon_gap = max(a[2], b[2]) - min(a[3], b[3])
    return lat_gap <= 1e-9 and lon_gap <= 1e-9


def test_a_cell_away_from_the_poles_has_eight_distinct_neighbours():
    gh = G.encode(12.9716, 77.5946, 6)
    ns = G.neighbors(gh)
    assert len(ns) == 8 and len(set(ns)) == 8
    assert gh not in ns
    assert all(len(n) == len(gh) for n in ns)
    assert ns == sorted(ns)


def test_neighbours_share_an_edge_or_a_corner_with_the_cell():
    gh = G.encode(48.8566, 2.3522, 5)
    box = G.decode_bbox(gh)
    assert all(_touches(box, G.decode_bbox(n)) for n in G.neighbors(gh))


@pytest.mark.parametrize("gh", ["u4pru", "tdr1v", "s0000", "9q8yy", "r3gx2"])
def test_cell_and_neighbours_tile_a_three_by_three_block(gh):
    lat_lo, lat_hi, lon_lo, lon_hi = G.decode_bbox(gh)
    dlat, dlon = lat_hi - lat_lo, lon_hi - lon_lo
    boxes = [G.decode_bbox(n) for n in G.neighbors(gh)] + [(lat_lo, lat_hi, lon_lo, lon_hi)]
    assert min(b[0] for b in boxes) == pytest.approx(lat_lo - dlat)
    assert max(b[1] for b in boxes) == pytest.approx(lat_hi + dlat)
    assert min(b[2] for b in boxes) == pytest.approx(lon_lo - dlon)
    assert max(b[3] for b in boxes) == pytest.approx(lon_hi + dlon)
    corners = {(round((b[0] - lat_lo) / dlat), round((b[2] - lon_lo) / dlon)) for b in boxes}
    assert corners == {(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)}


def test_known_neighbours():
    assert G.neighbors("u4pru") == ["u4pre", "u4prg", "u4prs", "u4prt", "u4prv", "u4r25", "u4r2h", "u4r2j"]


def test_neighbour_relation_is_symmetric():
    gh = G.encode(-33.87, 151.21, 5)
    for n in G.neighbors(gh):
        assert gh in G.neighbors(n)


def test_neighbours_wrap_around_the_antimeridian():
    gh = G.encode(10.0, 179.999, 4)
    ns = G.neighbors(gh)
    assert len(ns) == 8
    lons = [G.decode(n)[1] for n in ns]
    assert any(lon < -170 for lon in lons), "cells east of the date line have negative longitudes"


def test_a_cell_at_the_pole_has_fewer_neighbours():
    assert len(G.neighbors(G.encode(89.999, 10.0, 4))) == 5
    assert len(G.neighbors(G.encode(-89.999, 10.0, 4))) == 5
