"""rosetta.agent.formats: look at raw bytes and decide how to decode them."""
from __future__ import annotations

import orjson
import pytest

from rosetta.agent.formats import UnsupportedFormat, decode_all, detect
from rosetta.engine.decoders import build_decoder
from rosetta.simulator.dialects import DIALECTS


def payloads(truth: dict[str, list], key: str, n: int = 60) -> list[bytes]:
    return DIALECTS[key].encode(truth)[:n]


# --------------------------------------------------------------------- detect
def test_nothing_to_look_at():
    with pytest.raises(UnsupportedFormat, match="no sample"):
        detect([])


@pytest.mark.parametrize("key", ["nordvik", "pacifica", "pacifica_v2", "helix"])
def test_plain_json_is_detected(key, truth):
    found = detect(payloads(truth, key))
    assert found["decoder"] == {"type": "json"}
    assert found["evidence"] == "60/60 samples parse as JSON objects"


def test_signal_array_is_detected_and_pivoted(truth):
    found = detect(payloads(truth, "voltaic"))
    assert found["decoder"] == {"type": "json", "pivot": {"path": "signals", "key": "k", "value": "v", "into": "sig"}}
    assert "signal array at 'signals' pivoted by 'k'" in found["evidence"]
    assert found["decoder"] == DIALECTS["voltaic"].spec["decoder"], "the agent finds what the engineers wrote"


def test_signal_array_with_value_first():
    msgs = [orjson.dumps({"id": i, "data": [{"val": 1.5, "name": "speed"}, {"val": 2, "name": "soc"},
                                           {"val": "x", "name": "gear"}]}) for i in range(10)]
    assert detect(msgs)["decoder"]["pivot"] == {"path": "data", "key": "name", "value": "val", "into": "sig"}


@pytest.mark.parametrize("array", [
    [{"k": "a", "v": 1}, {"k": "b", "v": 2}],                                    # too short to be sure
    [{"k": "a", "v": 1}, {"k": "a", "v": 2}, {"k": "a", "v": 3}],                # names repeat
    [{"k": 1, "v": 1}, {"k": 2, "v": 2}, {"k": 3, "v": 3}],                      # no text names
    [{"k": "a", "v": 1, "u": "x"}, {"k": "b", "v": 2, "u": "x"}, {"k": "c", "v": 3, "u": "x"}],
    [{"k": "a", "v": 1}, {"k": "b", "w": 2}, {"k": "c", "v": 3}],                # different keys
    ["a", "b", "c"], [1, 2, 3],
])
def test_other_arrays_are_not_mistaken_for_signals(array):
    msgs = [orjson.dumps({"id": i, "items": array}) for i in range(10)]
    assert detect(msgs)["decoder"] == {"type": "json"}


def test_delimited_text_is_detected_with_its_column_count(truth):
    found = detect(payloads(truth, "stellaris"))
    assert found["decoder"] == {"type": "delimited", "delimiter": "|", "columns": [f"c{i}" for i in range(16)]}
    assert found["evidence"] == "60/60 samples have 16 columns separated by '|'"


@pytest.mark.parametrize("delimiter", ["|", ";", ",", "\t"])
def test_each_supported_delimiter(delimiter):
    msgs = [delimiter.join(["TAG", f"V{i}", "12.5", str(i), "ON"]).encode() for i in range(20)]
    found = detect(msgs)["decoder"]
    assert (found["type"], found["delimiter"], len(found["columns"])) == ("delimited", delimiter, 5)


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_text_with_a_line_ending_is_still_text(ending):
    msgs = [f"a|b|c|{i}{ending}".encode() for i in range(10)]
    found = detect(msgs)["decoder"]
    assert found["type"] == "delimited" and len(found["columns"]) == 4
    assert build_decoder(found)(msgs[0]) == {"c0": "a", "c1": "b", "c2": "c", "c3": "0"}


@pytest.mark.parametrize("sep", [";", "&", ","])
def test_key_value_text_is_detected(sep):
    msgs = [sep.join([f"vin=V{i}", "spd=12.5", "odo=100", "ign=1", "soc=80"]).encode() for i in range(20)]
    found = detect(msgs)
    assert found["decoder"] == {"type": "kv", "pair_sep": sep, "kv_sep": "="}
    assert found["evidence"] == f"key=value pairs separated by {sep!r}"


def test_protobuf_needs_the_descriptor_from_the_oem(truth):
    with pytest.raises(UnsupportedFormat, match="binary"):
        detect(payloads(truth, "kaizen"))


def test_protobuf_decodes_with_a_descriptor_the_oem_registered(truth):
    cfg = DIALECTS["kaizen"].spec["decoder"]
    found = detect(payloads(truth, "kaizen"), [("kaizen mapping v1", cfg)])
    assert found["decoder"] == cfg
    assert "registered in kaizen mapping v1" in found["evidence"]


def test_a_registered_schema_that_does_not_fit_is_not_used(truth):
    other = {"type": "protobuf", "message": "kaizen.v1.Position",
             "descriptor_b64": DIALECTS["kaizen"].spec["decoder"]["descriptor_b64"]}
    broken = {"type": "protobuf", "message": "nope.Missing", "descriptor_b64": "AAAA"}
    with pytest.raises(UnsupportedFormat, match="no registered schema"):
        detect([b"\xff\xfe\xfd\x00\x01" * 8] * 20, [("a", broken), ("b", other)])


@pytest.mark.parametrize("msgs", [[b"\x00\x01\x02\xff"] * 10, [b"hello"] * 10, [b"a|b"] * 10, [b"x=1"] * 10,
                                  [b"[1, 2, 3]"] * 10])
def test_anything_else_is_unsupported(msgs):
    with pytest.raises(UnsupportedFormat):
        detect(msgs)


def test_a_few_damaged_samples_do_not_change_the_verdict(truth):
    good = payloads(truth, "nordvik", 50)
    noisy = good[:45] + [p[: len(p) // 2] for p in good[45:]] + [b"\xff\xfe"] * 3
    found = detect(noisy)
    assert found["decoder"] == {"type": "json"}
    assert found["evidence"].startswith("45/53 ")


def test_too_many_damaged_samples(truth):
    good = payloads(truth, "stellaris", 20)
    with pytest.raises(UnsupportedFormat):
        detect(good[:10] + [b"\x00\xff\x00\xff"] * 10)


def test_only_the_first_two_hundred_samples_are_inspected(truth):
    good = payloads(truth, "nordvik", 50) * 4
    found = detect(good + [b"\xff"] * 500)
    assert found["evidence"].startswith("200/200 ")


@pytest.mark.parametrize("key", ["nordvik", "pacifica", "stellaris", "voltaic", "helix"])
def test_detected_decoder_is_accepted_by_the_engine(key, truth):
    found = detect(payloads(truth, key))
    decoder = build_decoder(found["decoder"])
    assert isinstance(decoder(payloads(truth, key)[0]), dict)


# ----------------------------------------------------------------- decode_all
def test_decode_all_returns_objects_with_their_devices(truth):
    p = payloads(truth, "nordvik", 10)
    devices = truth["device_id"][:10]
    objs, devs, bad = decode_all({"type": "json"}, p, devices)
    assert (len(objs), devs, bad) == (10, devices, 0)
    assert [o["vehicle"]["vin"] for o in objs] == truth["vin"][:10]


def test_decode_all_skips_what_cannot_be_decoded(truth):
    p = payloads(truth, "nordvik", 6)
    p[1] = b"{broken"
    p[4] = b"[1, 2]"                                      # valid JSON, but not an object
    devices = [f"d{i}" for i in range(6)]
    objs, devs, bad = decode_all({"type": "json"}, p, devices)
    assert bad == 2
    assert devs == ["d0", "d2", "d3", "d5"], "devices stay aligned with their messages"
    assert len(objs) == 4


def test_decode_all_drops_the_raw_signal_array_after_pivoting(truth):
    cfg = detect(payloads(truth, "voltaic"))["decoder"]
    objs, _, bad = decode_all(cfg, payloads(truth, "voltaic", 5), truth["device_id"][:5])
    assert bad == 0
    for obj in objs:
        assert set(obj) == {"meta", "sig"}
        assert {"gnss_lat", "gnss_lon", "veh_speed", "veh_odo"} <= set(obj["sig"])


def test_decode_all_with_delimited_text(truth):
    cfg = detect(payloads(truth, "stellaris"))["decoder"]
    objs, devs, bad = decode_all(cfg, payloads(truth, "stellaris", 5) + [b"too|short"], truth["device_id"][:6])
    assert (len(objs), bad) == (5, 1)
    assert [o["c2"] for o in objs] == truth["vin"][:5]


def test_decode_all_with_protobuf_once_the_descriptor_is_known(truth):
    cfg = DIALECTS["kaizen"].spec["decoder"]
    objs, devs, bad = decode_all(cfg, payloads(truth, "kaizen", 5), truth["device_id"][:5])
    assert bad == 0 and [o["vin"] for o in objs] == truth["vin"][:5]


def test_decode_all_with_nothing():
    assert decode_all({"type": "json"}, [], []) == ([], [], 0)
