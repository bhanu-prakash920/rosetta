"""rosetta.ml.synth: synthetic dialects with known answers, for training and evaluation."""
from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from rosetta.domain.canonical import EVENT_TYPES
from rosetta.domain.transforms import compile_transform
from rosetta.domain.vin import is_valid_vin
from rosetta.engine.decoders import flatten
from rosetta.ml import synth as S
from rosetta.ml.labels import IGNORE, LABEL_INDEX, field_of, transforms_of
from rosetta.ml.profile import name_tokens


@pytest.fixture(scope="module")
def recording() -> S.Recording:
    return S.record(seed=5, region=1, vehicles=120, ticks=12, keep=20)


def dialects(recording: S.Recording, n: int, seed: int = 1, names=None, anonymous: bool = False):
    rng = np.random.default_rng(seed)
    names = names or S._clean(S.TRAIN_NAMES, S.demo_tokens())
    return [S.make_dialect(recording, rng, names, anonymous=anonymous) for _ in range(n)]


# ----------------------------------------------------------------- vocabulary
def overlap() -> dict[str, set[str]]:
    out = {}
    for field in S.TRAIN_NAMES:
        shared = {name_tokens(n) for n in S.TRAIN_NAMES[field]} & {name_tokens(n) for n in S.TEST_NAMES[field]}
        if shared:
            out[field] = shared
    return out


KNOWN_LEAKS = {"ts": {"recorded at"}, "ambient": {"env temp"}, "dtc": {"diag codes"}}


def test_train_and_test_names_never_overlap():
    assert overlap() == {}


def test_train_and_test_names_overlap_only_in_the_known_names():
    found = overlap()
    assert set(found) <= set(KNOWN_LEAKS)
    assert all(found[field] <= KNOWN_LEAKS[field] for field in found)


def test_both_vocabularies_cover_the_same_fields():
    assert set(S.TRAIN_NAMES) == set(S.TEST_NAMES) == set(S.GROUPS)
    assert all(len(v) >= 4 for v in S.TEST_NAMES.values())


def test_demo_tokens_come_from_helix_and_the_drifted_pacifica():
    toks = S.demo_tokens()
    assert {"fin", "zeit", "breite", "laenge", "strecke", "zuendung", "fehler"} <= toks, "helix"
    assert {"speed", "odometer"} <= toks, "the two renamed pacifica fields"
    assert "vin" not in toks and "mph" not in toks
    assert all(t == t.lower() for t in toks)


def test_clean_removes_names_that_share_a_token_with_the_demo_cases():
    cleaned = S._clean(S.TRAIN_NAMES, S.demo_tokens())
    banned = S.demo_tokens()
    for field, names in cleaned.items():
        assert names, field
        for n in names:
            assert not set(name_tokens(n).split()) & banned, n
    assert "speed_kmh" in S.TRAIN_NAMES["speed"] and "speed_kmh" not in cleaned["speed"]
    assert "odometerKm" not in cleaned["odo"]


def test_clean_keeps_one_name_when_everything_is_banned():
    assert S._clean({"x": ["alpha_one", "alpha_two"]}, {"alpha"}) == {"x": ["alpha_one"]}


def test_test_names_stay_clear_of_the_demo_tokens_too():
    banned = S.demo_tokens()
    leaked = [n for names in S.TEST_NAMES.values() for n in names if set(name_tokens(n).split()) & banned]
    assert leaked == []


def test_unit_factors_agree_with_the_engine():
    for unit, factor in S._FACTOR.items():
        quantity = next((q for q, table in __import__("rosetta.domain.transforms", fromlist=["UNITS"]).UNITS.items()
                         if unit in table and q != "time"), None)
        if quantity is None:
            assert unit == "centideg"
            continue
        back = compile_transform({"op": "unit", "quantity": quantity, "from": unit})(100.0 * factor)
        assert back == pytest.approx(100.0), unit


@pytest.mark.parametrize("name,style,expected", [
    ("fuel_level", "snake", "fuel_level"), ("fuelLevel", "snake", "fuel_level"),
    ("fuel_level", "camel", "fuelLevel"), ("fuel_level", "pascal", "FuelLevel"),
    ("fuel_level", "upper", "FUEL_LEVEL"), ("fuel_level", "kebab", "fuel-level"),
    ("vin", "camel", "vin"), ("", "camel", ""), ("__", "upper", "__"),
])
def test_style(name, style, expected):
    assert S._style(name, style) == expected


# ------------------------------------------------------------------- recording
def test_recording_shape(recording):
    assert recording.devices == 20
    assert len(recording.truth) == 12
    assert all(len(t["vin"]) == 20 for t in recording.truth)
    assert all(is_valid_vin(v) for v in recording.truth[0]["vin"])


def test_recording_follows_the_same_vehicles_over_time(recording):
    first = recording.truth[0]
    for k, t in enumerate(recording.truth):
        assert t["vin"] == first["vin"] and t["i"] == first["i"]
        assert t["seq"] == [s + k for s in first["seq"]], "one message per vehicle per tick"
    ts = [t["ts"][0] for t in recording.truth]
    assert ts == sorted(ts) and len(set(ts)) == 12
    assert all(900 <= b - a <= 1100 for a, b in zip(ts, ts[1:])), "about one second apart, with jitter"


def test_recording_mixes_moving_and_parked_vehicles(recording):
    ignition = recording.truth[0]["ignition"]
    assert 0 < sum(ignition) < len(ignition)
    assert sum(ignition) >= 12, "mostly moving, so the physics is visible"


def test_recording_is_deterministic():
    a = S.record(seed=9, region=2, vehicles=80, ticks=5, keep=10)
    b = S.record(seed=9, region=2, vehicles=80, ticks=5, keep=10)
    assert a.truth == b.truth


def test_recording_uses_the_cities_of_its_region():
    for region, (lat_lo, lat_hi) in ((0, (12, 30)), (3, (-25, 6)), (7, (58, 66))):
        rec = S.record(seed=1, region=region, vehicles=60, ticks=2, keep=10)
        lats = rec.truth[0]["lat"]
        assert all(lat_lo - 1 <= v <= lat_hi + 1 for v in lats), region
    assert S.record(seed=1, region=8, vehicles=60, ticks=1, keep=5).truth[0]["lat"] == \
        S.record(seed=1, region=0, vehicles=60, ticks=1, keep=5).truth[0]["lat"], "regions wrap around"


# --------------------------------------------------------------------- dialect
def test_dialect_has_one_device_per_message(recording):
    for d in dialects(recording, 5):
        assert len(d.messages) == len(d.devices) > 0.9 * 12 * 20 * 0.9
        assert len(d.messages) <= 12 * 20
        assert set(d.devices) <= set(recording.truth[0]["device_id"])


def test_every_label_is_known_and_every_field_is_labelled(recording):
    for d in dialects(recording, 25):
        assert set(d.labels.values()) <= set(LABEL_INDEX)
        paths = set()
        for m in d.messages:
            paths.update(flatten(m))
        assert paths <= set(d.labels), "nothing in the data is without an answer"
        mapped = [field_of(lab) for lab in d.labels.values() if lab != IGNORE]
        assert len(mapped) == len(set(mapped)), "a canonical field is never mapped twice"
        assert {"vin", "ts", "seq", "lat", "lon", "speed_kmh", "odo_km"} <= set(mapped)
        assert 3 <= list(d.labels.values()).count(IGNORE) <= 8


def test_latitude_and_longitude_share_a_unit(recording):
    for d in dialects(recording, 25):
        units = {lab.split("|")[0]: lab.split("|")[1] for lab in d.labels.values() if lab.startswith(("lat|", "lon|"))}
        assert units["lat"] == units["lon"]


def read(message: dict[str, Any], path: str) -> Any:
    return flatten(message).get(path)


def test_labels_are_the_correct_answers(recording):
    """Applying the transforms of a label to the raw value gives back the truth."""
    truth_by_key = {}
    for t in recording.truth:
        for j in range(len(t["vin"])):
            truth_by_key[(t["vin"][j], t["seq"][j])] = {k: t[k][j] for k in t}
    tol = {"lat": 1e-6, "lon": 1e-6, "heading_deg": 0.01, "speed_kmh": 0.02, "odo_km": 0.06, "soc_pct": 0.06,
           "fuel_pct": 0.06, "ambient_c": 0.06}
    checked = set()
    for d in dialects(recording, 30, seed=3):
        by_field = {field_of(lab): (path, lab) for path, lab in d.labels.items() if lab != IGNORE}
        for m in d.messages[:40]:
            vin = read(m, by_field["vin"][0])
            seq = int(read(m, by_field["seq"][0]))
            want = truth_by_key[(vin, seq)]
            for field, (path, label) in by_field.items():
                raw = read(m, path)
                if raw is None or raw == "" or field in ("evt", "vin", "seq"):
                    continue
                value = raw
                for t in transforms_of(label):
                    value = compile_transform(t)(value)
                if field in tol:
                    assert float(value) == pytest.approx(want[field], abs=tol[field]), (label, raw)
                elif field == "ts":
                    assert abs(value - want["ts"]) <= 1, (label, raw)
                else:
                    assert value == want[field], (label, raw)
                checked.add(label)
    assert len(checked) >= 25, "most of the label space was exercised"


def test_event_codes_decode_to_canonical_events(recording):
    with_events = 0
    for d in dialects(recording, 30, seed=4):
        assert set(d.evt_codes.values()) <= set(EVENT_TYPES)
        evt_path = next((p for p, lab in d.labels.items() if lab == "evt"), None)
        if evt_path is None:
            continue
        with_events += 1
        seen = {read(m, evt_path) for m in d.messages} - {None}
        assert seen <= set(d.evt_codes), "every code in the data has an answer"
    assert with_events >= 20


def test_every_event_has_a_code_of_its_own(recording):
    for d in dialects(recording, 30, seed=4):
        assert sorted(d.evt_codes.values()) == sorted(EVENT_TYPES)


def test_anonymous_dialect_has_no_names_to_read(recording):
    for d in dialects(recording, 8, anonymous=True):
        assert all(p[0] == "c" and p[1:].isdigit() for p in d.labels)
        assert all("." not in p for p in d.labels), "flat"
        numeric_label = next(p for p, lab in d.labels.items() if lab.startswith("odo|"))
        assert all(isinstance(m[numeric_label], str) for m in d.messages), "numbers travel as text"


def test_paths_are_unique_within_a_dialect(recording):
    for d in dialects(recording, 40, seed=6):
        assert len(d.labels) == len(set(d.labels))


def test_dialects_differ_from_each_other(recording):
    ds = dialects(recording, 12, seed=8)
    assert len({tuple(sorted(d.labels)) for d in ds}) == 12
    assert len({tuple(sorted(d.labels.values())) for d in ds}) > 6


def test_same_seed_gives_the_same_dialect(recording):
    a, b = dialects(recording, 3, seed=21), dialects(recording, 3, seed=21)
    assert [d.labels for d in a] == [d.labels for d in b]
    assert [d.messages for d in a] == [d.messages for d in b]


def test_distractors_cover_every_alias():
    assert set(S.DISTRACTOR_ALIASES) == set(S._distractors(np.random.default_rng(0),
                                                           {"vin": ["a"], "speed_kmh": [10.0], "i": [3], "lat": [12.0],
                                                            "odo_km": [100.0]}, tick=0))


# ---------------------------------------------------------------------- corpus
def test_corpus(monkeypatch):
    lines = []
    real_record = S.record
    monkeypatch.setattr(S, "record", lambda seed, region: real_record(seed, region, vehicles=60, ticks=6, keep=8))
    corpus = S.make_corpus(100, seed=2, test=False, log=lines.append)
    assert len(corpus) == 100
    assert lines == ["  generated 100/100 dialects"]
    banned = S.demo_tokens()
    for d in corpus:
        for path, label in d.labels.items():
            if label != IGNORE and not path[1:].isdigit():
                assert not set(name_tokens(path.split(".")[-1]).split()) & banned, path


def test_test_corpus_uses_only_test_names(monkeypatch):
    real_record = S.record
    monkeypatch.setattr(S, "record", lambda seed, region: real_record(seed, region, vehicles=60, ticks=6, keep=8))
    allowed = {name_tokens(n) for names in S.TEST_NAMES.values() for n in names}
    named = 0
    for d in S.make_corpus(12, seed=3, test=True):
        for path, label in d.labels.items():
            if label == IGNORE or path[1:].isdigit():
                continue
            leaf = name_tokens(path.split(".")[-1])
            assert leaf in allowed or leaf.rstrip("2") in allowed, path
            named += 1
    assert named > 50


def test_corpus_is_deterministic(monkeypatch):
    real_record = S.record
    monkeypatch.setattr(S, "record", lambda seed, region: real_record(seed, region, vehicles=60, ticks=4, keep=6))
    a, b = S.make_corpus(5, seed=4, test=True), S.make_corpus(5, seed=4, test=True)
    assert [d.labels for d in a] == [d.labels for d in b]
    assert [d.labels for d in a] != [d.labels for d in S.make_corpus(5, seed=5, test=True)]
