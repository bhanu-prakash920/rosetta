"""rosetta.engine.decoders: bytes in, Python objects out, for four wire formats."""
from __future__ import annotations

import orjson
import pytest

from rosetta.domain.errors import DECODE_ERROR, NormalizeError, SpecError
from rosetta.engine import decoders as D
from rosetta.simulator.dialects import KAIZEN_DESCRIPTOR_B64

KAIZEN_CFG = {"type": "protobuf", "message": "kaizen.v1.Telemetry", "descriptor_b64": KAIZEN_DESCRIPTOR_B64}


def decode_error(decoder, payload: bytes) -> NormalizeError:
    with pytest.raises(NormalizeError) as info:
        decoder(payload)
    assert info.value.reason == DECODE_ERROR
    return info.value


# ----------------------------------------------------------------- parse_path
@pytest.mark.parametrize("path,steps", [
    ("speed", ("speed",)),
    ("a.b[2].c", ("a", "b", 2, "c")),
    ("position.latitude", ("position", "latitude")),
    ("signals[0]", ("signals", 0)),
    ("m[1][2]", ("m", 1, 2)),
    ("[0]", (0,)),
    ("a[-1]", ("a", -1)),
    ("with space", ("with space",)),
    ("kebab-name.snake_name", ("kebab-name", "snake_name")),
])
def test_parse_path(path, steps):
    assert D.parse_path(path) == steps


@pytest.mark.parametrize("path", ["", None, 5, ["a"], "a..b", ".a", "a.", ".", "a[1", "a[x]", "a[]", "a[1]b",
                                  "a[1]]", "a[1.5]", "a[ 1]", "a[-]", "a[x", "x" * 201])
def test_parse_path_rejects_malformed_paths(path):
    with pytest.raises(SpecError):
        D.parse_path(path)


def test_parse_path_accepts_the_maximum_length():
    assert D.parse_path("x" * 200) == ("x" * 200,)


@pytest.mark.parametrize("path", ["a[", "[", "a.b["])
def test_parse_path_rejects_a_dangling_bracket(path):
    with pytest.raises(SpecError):
        D.parse_path(path)


@pytest.mark.parametrize("path", ["a[--1]", "a[²]"])
def test_parse_path_reports_every_bad_index_as_spec_error(path):
    with pytest.raises(SpecError):
        D.parse_path(path)


# ---------------------------------------------------------------- make_getter
def test_getter_reads_top_level_and_nested_values():
    obj = {"speed": 12, "pos": {"lat": 1.5, "fix": [{"q": 9}, {"q": 7}]}}
    assert D.make_getter("speed")(obj) == 12
    assert D.make_getter("pos.lat")(obj) == 1.5
    assert D.make_getter("pos.fix[1].q")(obj) == 7
    assert D.make_getter("pos.fix[-1].q")(obj) == 7


@pytest.mark.parametrize("path", ["missing", "pos.missing", "pos.lat.deeper", "pos.fix[5]", "speed[0]", "speed.x",
                                  "pos[0]"])
def test_getter_returns_none_when_the_value_is_absent(path):
    obj = {"speed": 12, "pos": {"lat": 1.5, "fix": [{"q": 9}]}}
    assert D.make_getter(path)(obj) is None


@pytest.mark.parametrize("obj", [None, 5, "text", [], True])
def test_getter_returns_none_for_objects_of_the_wrong_type(obj):
    assert D.make_getter("a")(obj) is None
    assert D.make_getter("a.b")(obj) is None


def test_getter_returns_falsy_values_as_they_are():
    obj = {"a": 0, "b": "", "c": False, "d": {"e": []}}
    assert D.make_getter("a")(obj) == 0
    assert D.make_getter("b")(obj) == ""
    assert D.make_getter("c")(obj) is False
    assert D.make_getter("d.e")(obj) == []


def test_getter_rejects_a_bad_path_when_it_is_built():
    with pytest.raises(SpecError):
        D.make_getter("a..b")


# -------------------------------------------------------------------- flatten
def test_flatten_nested_objects_and_lists_of_objects():
    obj = {"a": {"b": [{"c": 1}, {"c": 2}]}, "d": [1, 2], "e": [], "f": "x"}
    assert D.flatten(obj) == {"a.b[0].c": 1, "a.b[1].c": 2, "d": [1, 2], "e": [], "f": "x"}


def test_flatten_paths_can_be_read_back_with_a_getter():
    obj = {"meta": {"vin": "V"}, "signals": [{"k": "a", "v": 1}, {"k": "b", "v": 2}]}
    for path, leaf in D.flatten(obj).items():
        assert D.make_getter(path)(obj) == leaf


def test_flatten_looks_at_the_first_eight_items_of_a_list():
    flat = D.flatten({"rows": [{"v": i} for i in range(20)]})
    assert sorted(flat) == sorted(f"rows[{i}].v" for i in range(8))


def test_flatten_stops_at_depth_eight():
    deep = cur = {}
    for _ in range(12):
        cur["k"] = {}
        cur = cur["k"]
    cur["leaf"] = 1
    assert D.flatten(deep) == {}
    shallow = {"a": {"b": {"c": {"d": 1}}}}
    assert D.flatten(shallow) == {"a.b.c.d": 1}


def test_flatten_scalar_and_prefix():
    assert D.flatten(5) == {"": 5}
    assert D.flatten({"x": 1}, prefix="root") == {"root.x": 1}


def test_flatten_mixed_list_is_a_leaf():
    assert D.flatten({"m": [{"a": 1}, 2]}) == {"m": [{"a": 1}, 2]}


# ----------------------------------------------------------------------- JSON
def test_json_decoder():
    dec = D.build_decoder({"type": "json"})
    assert dec(b'{"a": 1, "b": {"c": [1, 2]}}') == {"a": 1, "b": {"c": [1, 2]}}


@pytest.mark.parametrize("payload,expected", [(b"null", None), (b"[]", []), (b"{}", {}), (b"12", 12), (b'"x"', "x")])
def test_json_decoder_returns_any_json_value(payload, expected):
    assert D.build_decoder({"type": "json"})(payload) == expected


@pytest.mark.parametrize("payload", [b"", b"{", b'{"a": 1', b"{'a': 1}", b"\xff\xfe", b"STL|2|x", b'{"a": NaN}',
                                     b'{"a": 1} trailing'])
def test_json_decoder_reports_malformed_input(payload):
    err = decode_error(D.build_decoder({"type": "json"}), payload)
    assert err.field == ""
    assert err.detail


def test_json_decode_error_detail_is_bounded():
    err = decode_error(D.build_decoder({"type": "json"}), b"{" + b"x" * 5000)
    assert len(err.detail) <= 120


PIVOT = {"type": "json", "pivot": {"path": "signals", "key": "k", "value": "v", "into": "sig"}}


def test_json_pivot_turns_a_signal_array_into_an_object():
    payload = orjson.dumps({"meta": {"vin": "V1"}, "signals": [{"k": "speed", "v": 12.5}, {"k": "codes", "v": ["P0420"]}]})
    obj = D.build_decoder(PIVOT)(payload)
    assert obj["sig"] == {"speed": 12.5, "codes": ["P0420"]}
    assert obj["meta"] == {"vin": "V1"}
    assert D.make_getter("sig.speed")(obj) == 12.5


def test_json_pivot_with_a_nested_array_path():
    cfg = {"type": "json", "pivot": {"path": "data.items", "key": "name", "value": "val", "into": "flat"}}
    payload = orjson.dumps({"data": {"items": [{"name": "a", "val": 1}]}})
    assert D.build_decoder(cfg)(payload)["flat"] == {"a": 1}


def test_json_pivot_with_an_empty_array():
    assert D.build_decoder(PIVOT)(b'{"signals": []}')["sig"] == {}


def test_json_pivot_last_value_wins_for_a_repeated_name():
    payload = b'{"signals": [{"k": "a", "v": 1}, {"k": "a", "v": 2}]}'
    assert D.build_decoder(PIVOT)(payload)["sig"] == {"a": 2}


@pytest.mark.parametrize("payload", [
    b'{"meta": {}}',                                  # array missing
    b'{"signals": 5}',                                # not an array
    b'{"signals": [{"k": "a"}]}',                     # value missing
    b'{"signals": [{"v": 1}]}',                       # name missing
    b'{"signals": ["a", "b"]}',                       # items are not objects
    b'{"signals": [{"k": ["x"], "v": 1}]}',           # unhashable name
    b"null", b"[]", b"12", b"", b"{",
])
def test_json_pivot_reports_malformed_input(payload):
    err = decode_error(D.build_decoder(PIVOT), payload)
    assert err.field == "signals"
    assert len(err.detail) <= 120


@pytest.mark.parametrize("pivot", [
    {}, {"path": "signals", "key": "k", "value": "v"}, {"path": "signals", "key": "k", "value": "v", "into": ""},
    {"path": "signals", "key": 1, "value": "v", "into": "sig"}, {"path": None, "key": "k", "value": "v", "into": "s"},
    {"path": "a..b", "key": "k", "value": "v", "into": "sig"},
])
def test_json_pivot_configuration_is_validated(pivot):
    with pytest.raises(SpecError):
        D.build_decoder({"type": "json", "pivot": pivot})


@pytest.mark.parametrize("pivot", ["signals", ["signals"], 5])
def test_json_pivot_of_the_wrong_type_is_a_spec_error(pivot):
    with pytest.raises(SpecError):
        D.build_decoder({"type": "json", "pivot": pivot})


# ------------------------------------------------------------------ delimited
STL = {"type": "delimited", "delimiter": "|", "columns": ["tag", "vin", "speed", "codes"]}


def test_delimited_decoder():
    assert D.build_decoder(STL)(b"STL|V1|12.5|P0301 P0420") == \
        {"tag": "STL", "vin": "V1", "speed": "12.5", "codes": "P0301 P0420"}


def test_delimited_keeps_empty_columns_as_empty_strings():
    assert D.build_decoder(STL)(b"STL|V1||") == {"tag": "STL", "vin": "V1", "speed": "", "codes": ""}


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r", b"\n\n"])
def test_delimited_strips_the_line_ending(ending):
    assert D.build_decoder(STL)(b"STL|V1|1|x" + ending)["codes"] == "x"


@pytest.mark.parametrize("payload,got", [(b"STL|V1|12.5", 3), (b"STL|V1|12.5|x|extra", 5), (b"", 1), (b"STL", 1),
                                         (b"STL,V1,12.5,x", 1)])
def test_delimited_wrong_column_count(payload, got):
    err = decode_error(D.build_decoder(STL), payload)
    assert err.detail == f"expected 4 columns, got {got}"


def test_delimited_invalid_utf8():
    decode_error(D.build_decoder(STL), b"STL|\xff\xfe|1|x")


def test_delimited_with_a_multi_character_delimiter():
    cfg = {"type": "delimited", "delimiter": "::", "columns": ["a", "b"]}
    assert D.build_decoder(cfg)(b"1::2") == {"a": "1", "b": "2"}


@pytest.mark.parametrize("patch", [
    {"delimiter": ""}, {"delimiter": "12345"}, {"delimiter": None}, {"delimiter": 1},
    {"columns": []}, {"columns": None}, {"columns": "a,b"}, {"columns": ["a", ""]}, {"columns": ["a", 1]},
    {"columns": [f"c{i}" for i in range(129)]},
])
def test_delimited_configuration_is_validated(patch):
    with pytest.raises(SpecError):
        D.build_decoder({**STL, **patch})


def test_delimited_accepts_128_columns():
    cfg = {"type": "delimited", "delimiter": ",", "columns": [f"c{i}" for i in range(128)]}
    obj = D.build_decoder(cfg)(",".join(str(i) for i in range(128)).encode())
    assert obj["c127"] == "127"


# ------------------------------------------------------------------------- kv
def test_kv_decoder_defaults():
    assert D.build_decoder({"type": "kv"})(b"vin=V1;speed=12.5;codes=P0301,P0420") == \
        {"vin": "V1", "speed": "12.5", "codes": "P0301,P0420"}


def test_kv_custom_separators():
    cfg = {"type": "kv", "pair_sep": "&", "kv_sep": ":"}
    assert D.build_decoder(cfg)(b"a:1&b:2") == {"a": "1", "b": "2"}


def test_kv_skips_empty_pairs_and_trims_keys():
    assert D.build_decoder({"type": "kv"})(b" a =1;;b=2;\n") == {"a": "1", "b": "2"}


def test_kv_value_may_contain_the_separator_and_may_be_empty():
    assert D.build_decoder({"type": "kv"})(b"expr=a=b;empty=") == {"expr": "a=b", "empty": ""}


def test_kv_empty_payload_is_an_empty_object():
    assert D.build_decoder({"type": "kv"})(b"") == {}


@pytest.mark.parametrize("payload", [b"a=1;broken;b=2", b"justtext", b"a=1;\xff\xfe=2"])
def test_kv_reports_malformed_input(payload):
    err = decode_error(D.build_decoder({"type": "kv"}), payload)
    assert err.field == "" and len(err.detail) <= 120


@pytest.mark.parametrize("patch", [{"pair_sep": ""}, {"pair_sep": "12345"}, {"kv_sep": ""}, {"kv_sep": None},
                                   {"pair_sep": 5}])
def test_kv_configuration_is_validated(patch):
    with pytest.raises(SpecError):
        D.build_decoder({"type": "kv", **patch})


# ------------------------------------------------------------------- protobuf
def test_protobuf_round_trip():
    cls = D.build_message_class(KAIZEN_DESCRIPTOR_B64, "kaizen.v1.Telemetry")
    msg = cls(vin="1HGCM82633A004352", time_us=1_790_000_000_123_000, counter=42, speed_centi_kmh=5423,
              odometer_hm=182_345, ambient_deci_c=-35, ignition_on=True, dtc=["P0301", "U0100"], event=3)
    msg.pos.lat_e7 = 129_716_000
    msg.pos.lon_e7 = -775_946_000
    msg.pos.heading_cdeg = 18_100
    msg.soc_permille = 765
    obj = D.build_decoder(KAIZEN_CFG)(msg.SerializeToString())
    assert obj == {
        "vin": "1HGCM82633A004352", "time_us": 1_790_000_000_123_000, "counter": 42,
        "pos": {"lat_e7": 129_716_000, "lon_e7": -775_946_000, "heading_cdeg": 18_100},
        "speed_centi_kmh": 5423, "odometer_hm": 182_345, "soc_permille": 765, "fuel_permille": None,
        "ambient_deci_c": -35, "ignition_on": True, "dtc": ["P0301", "U0100"], "event": 3,
    }


def test_protobuf_optional_zero_is_distinguished_from_absent():
    cls = D.build_message_class(KAIZEN_DESCRIPTOR_B64, "kaizen.v1.Telemetry")
    msg = cls(vin="V")
    msg.soc_permille = 0
    obj = D.build_decoder(KAIZEN_CFG)(msg.SerializeToString())
    assert obj["soc_permille"] == 0
    assert obj["fuel_permille"] is None


def test_protobuf_empty_payload_is_a_message_of_defaults():
    obj = D.build_decoder(KAIZEN_CFG)(b"")
    assert obj["vin"] == "" and obj["pos"] is None and obj["dtc"] == [] and obj["soc_permille"] is None


def test_protobuf_result_is_plain_python():
    cls = D.build_message_class(KAIZEN_DESCRIPTOR_B64, "kaizen.v1.Telemetry")
    obj = D.build_decoder(KAIZEN_CFG)(cls(vin="V", dtc=["P0301"]).SerializeToString())
    assert type(obj) is dict and type(obj["dtc"]) is list
    assert orjson.loads(orjson.dumps(obj)) == obj


@pytest.mark.parametrize("payload", [b"\xff\xff\xff", b"\x0a\x05abc", b'{"vin": "V"}', b"STL|2|x"])
def test_protobuf_reports_malformed_input(payload):
    err = decode_error(D.build_decoder(KAIZEN_CFG), payload)
    assert len(err.detail) <= 120


def test_protobuf_nested_message_type_can_be_decoded_on_its_own():
    cls = D.build_message_class(KAIZEN_DESCRIPTOR_B64, "kaizen.v1.Position")
    dec = D.build_decoder({**KAIZEN_CFG, "message": "kaizen.v1.Position"})
    assert dec(cls(lat_e7=1, lon_e7=2, heading_cdeg=3).SerializeToString()) == \
        {"lat_e7": 1, "lon_e7": 2, "heading_cdeg": 3}


@pytest.mark.parametrize("cfg", [
    {"type": "protobuf"},
    {"type": "protobuf", "message": "kaizen.v1.Telemetry"},
    {"type": "protobuf", "descriptor_b64": KAIZEN_DESCRIPTOR_B64},
    {"type": "protobuf", "message": 5, "descriptor_b64": KAIZEN_DESCRIPTOR_B64},
    {"type": "protobuf", "message": "kaizen.v1.Telemetry", "descriptor_b64": b"bytes"},
    {"type": "protobuf", "message": "kaizen.v1.Nope", "descriptor_b64": KAIZEN_DESCRIPTOR_B64},
    {"type": "protobuf", "message": "kaizen.v1.Telemetry", "descriptor_b64": "!!! not base64 !!!"},
    {"type": "protobuf", "message": "kaizen.v1.Telemetry", "descriptor_b64": "aGVsbG8gd29ybGQ="},
    {"type": "protobuf", "message": "kaizen.v1.Telemetry", "descriptor_b64": "A" * 200_001},
])
def test_protobuf_configuration_is_validated(cfg):
    with pytest.raises(SpecError):
        D.build_decoder(cfg)


def test_build_message_class_reports_a_bad_descriptor():
    with pytest.raises(SpecError, match="bad protobuf descriptor"):
        D.build_message_class("aGVsbG8=", "kaizen.v1.Telemetry")


# -------------------------------------------------------------- build_decoder
@pytest.mark.parametrize("cfg", [None, "json", [], {}, {"type": "xml"}, {"type": "JSON"}, {"type": None},
                                 {"kind": "json"}])
def test_unknown_decoder_type_is_rejected(cfg):
    with pytest.raises(SpecError, match="decoder.type"):
        D.build_decoder(cfg)


@pytest.mark.parametrize("kind", [["json"], {"name": "json"}])
def test_unhashable_decoder_type_is_a_spec_error(kind):
    with pytest.raises(SpecError):
        D.build_decoder({"type": kind})


def test_decoder_types_constant_matches_the_builders():
    assert set(D.DECODER_TYPES) == {"json", "delimited", "kv", "protobuf"}
    for kind in ("json", "kv"):
        assert callable(D.build_decoder({"type": kind}))


def test_payload_limit_is_64_kib():
    assert D.MAX_PAYLOAD_BYTES == 64 * 1024
