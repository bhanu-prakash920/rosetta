"""The HTTP API against a world with processed traffic."""
from __future__ import annotations

from rosetta.simulator.dialects import SPEC_HELIX


def test_health_needs_no_token(api):
    r = api.get("/api/v1/system/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok" and j["checks"]["database"] == "ok" and j["vehicles"] == 3000


def test_overview_reflects_the_pipeline(api, engineer):
    ov = api.get("/api/v1/overview", headers=engineer).json()
    assert ov["totals"]["ok"] > 1000
    assert {o["oem"] for o in ov["oems"]} >= {"nordvik", "pacifica", "stellaris", "kaizen", "voltaic", "helix"}
    helix = next(o for o in ov["oems"] if o["oem"] == "helix")
    assert helix["total_ok"] == 0 and helix["reasons"]["NO_ADAPTER"] > 0
    assert ov["routing"]["nordvik"] == {"active": [1], "canary": None, "canary_pct": 0}
    assert "helix" not in ov["routing"]


def test_series_and_prometheus(api, engineer):
    s = api.get("/api/v1/series/all?seconds=60", headers=engineer).json()
    assert set(s["ok"]) >= {"nordvik", "kaizen"} and len(s["failed"]) == 60
    text = api.get("/metrics").text
    assert 'rosetta_events_total{oem="nordvik",outcome="ok"}' in text
    assert 'rosetta_dead_letters_total{oem="helix",reason="NO_ADAPTER"}' in text
    assert "rosetta_latency_milliseconds" in text
    assert "rosetta_workers_reporting" in text


def test_metrics_token_is_required_when_configured(api, monkeypatch):
    monkeypatch.setenv("ROSETTA_METRICS_TOKEN", "s3cret-scrape-token")
    assert api.get("/metrics").status_code == 401
    assert api.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = api.get("/metrics", headers={"Authorization": "Bearer s3cret-scrape-token"})
    assert ok.status_code == 200 and "rosetta_events_total" in ok.text


def test_keyset_pagination_walks_every_vehicle_once(api, manager):
    seen, cursor, pages = [], None, 0
    while True:
        q = "/api/v1/vehicles?limit=7" + (f"&cursor={cursor}" if cursor else "")
        j = api.get(q, headers=manager).json()
        seen += [v["id"] for v in j["items"]]
        pages += 1
        cursor = j["next_cursor"]
        if not cursor:
            break
        assert len(j["items"]) == 7
    assert len(seen) == len(set(seen)) > 7 and seen == sorted(seen) and pages > 1
    assert all(v["tenant_id"] == 1 for v in api.get("/api/v1/vehicles?limit=200", headers=manager).json()["items"])


def test_vehicle_views(api, manager):
    v = api.get("/api/v1/vehicles?limit=50", headers=manager).json()["items"]
    live = next(x for x in v if x.get("live"))
    vin = live["vin"]
    d = api.get(f"/api/v1/vehicles/{vin}", headers=manager).json()
    assert d["vin"] == vin and d["live"]["map_v"] >= 1 and -90 <= d["live"]["lat"] <= 90
    h = api.get(f"/api/v1/vehicles/{vin}/history?limit=50", headers=manager).json()
    assert h["count"] >= 1 and h["items"][0]["ts"] >= h["items"][-1]["ts"]
    t = api.get(f"/api/v1/vehicles/{vin}/trips", headers=manager)
    assert t.status_code == 200 and "segments" in t.json()
    assert api.get("/api/v1/vehicles/NOTAVIN", headers=manager).status_code == 404


def test_map_and_cells(api, engineer, manager):
    allv = api.get("/api/v1/map/points?limit=20000", headers=engineer).json()
    mine = api.get("/api/v1/map/points?limit=20000", headers=manager).json()
    assert allv["total_in_view"] > mine["total_in_view"] > 0
    assert len(allv["vins"]) == allv["count"] and not allv["masked"]
    box = api.get("/api/v1/map/points?south=12&north=14&west=79&east=81", headers=engineer).json()
    assert 0 < box["total_in_view"] < allv["total_in_view"]
    assert all(12 <= p[0] <= 14 and 79 <= p[1] <= 81 for p in box["points"])
    cells = api.get("/api/v1/map/cells?precision=4", headers=engineer).json()
    assert sum(c["vehicles"] for c in cells["cells"]) == cells["vehicles"] == allv["total_in_view"]


def test_dead_letter_views(api, analyst):
    g = api.get("/api/v1/dlq/groups?oem=helix", headers=analyst).json()
    assert g["totals"]["open"] == g["totals"]["count"] > 0
    fam = [f for f in g["families"] if f["mappable"]]
    # Payloads that differ only by optional fields are joined into one format family.
    # The few that were damaged in transit look different and stay apart.
    assert fam[0]["groups"] >= 3 and fam[0]["count"] > 0.99 * g["totals"]["count"]
    assert fam[0]["reasons"] == {"NO_ADAPTER": fam[0]["count"]}
    gid = g["items"][0]["id"]
    s = api.get(f"/api/v1/dlq/groups/{gid}/samples", headers=analyst).json()
    assert s["items"] and "kopf" in s["items"][0]["text"]
    assert api.get("/api/v1/dlq/groups/999999/samples", headers=analyst).status_code == 404


def test_schema_endpoint(api, analyst):
    j = api.get("/api/v1/schema/canonical", headers=analyst).json()
    assert j["json_schema"]["required"] == ["vin", "ts", "seq", "lat", "lon", "speed_kmh", "odo_km"]
    assert "unit" in j["transform_ops"] and "eval" not in j["transform_ops"]


def test_onboarding_through_the_api(api, engineer, analyst):
    """The whole story over HTTP: agent proposes, engineer tests on parked traffic, approves,
    promotes, and the parked messages come back."""
    w = api.world
    parked = api.get("/api/v1/dlq/groups?oem=helix", headers=analyst).json()["totals"]["open"]
    run = api.post("/api/v1/agent/runs", json={"oem": "helix", "engine": "workflow"}, headers=engineer)
    assert run.status_code == 201, run.text
    run = run.json()
    assert run["status"] == "awaiting_approval" and run["mapping"]["state"] == "validated"
    v = run["mapping"]["version"]
    assert [s["tool"] for s in run["steps"]][-1] == "submit_draft"

    m = api.get(f"/api/v1/mappings/helix/{v}", headers=analyst).json()
    assert m["source"] == "agent" and len(m["field_map"]) == 14
    assert m["validation_runs"][0]["passed"] == m["validation_runs"][0]["total"]
    assert all(f["rationale"] for f in m["field_map"])

    sh = api.post(f"/api/v1/mappings/helix/{v}/shadow", json={"max_records": 5000}, headers=engineer).json()
    assert sh["parked_messages_read"] > 0 and sh["success_rate"] > 0.98

    a = api.post(f"/api/v1/mappings/helix/{v}/actions/approve", json={"comment": "reviewed", "canary_pct": 100},
                 headers=engineer).json()
    assert a["state"] == "canary" and a["replay_job"]
    p = api.post(f"/api/v1/mappings/helix/{v}/actions/promote", json={}, headers=engineer).json()
    assert p["state"] == "active"
    assert api.post(f"/api/v1/mappings/helix/{v}/actions/promote", json={}, headers=engineer).status_code == 409

    w.reload()
    w.pump()
    w.metrics()
    after = api.get("/api/v1/dlq/groups?oem=helix", headers=analyst).json()["totals"]
    assert after["open"] < parked * 0.02                       # what is left is damaged in transit
    jobs = api.get("/api/v1/dlq/replay", headers=engineer).json()["items"]
    assert sum(j["republished"] for j in jobs if j["oem"] == "helix") >= parked * 0.98
    ov = api.get("/api/v1/overview", headers=engineer).json()
    assert ov["routing"]["helix"]["active"] == [v]
    assert next(o for o in ov["oems"] if o["oem"] == "helix")["total_ok"] >= parked * 0.98
    oems = {o["key"]: o for o in api.get("/api/v1/oems", headers=analyst).json()["items"]}
    assert oems["helix"]["status"] == "live"


def test_preview_shows_lineage(api, analyst):
    payload = ('{"vehicle":{"vin":"1HGCM82633A004352"},"recordedAt":"2026-09-25T10:15:02.120Z","sequence":9,'
               '"position":{"latitude":21.1702,"longitude":72.8311,"heading":90},'
               '"motion":{"speedKmh":64.2,"odometerKm":18234.7},"diagnostics":{"troubleCodes":["P0301"]},'
               '"event":"HARSH_BRAKE"}')
    j = api.post("/api/v1/mappings/nordvik/1/preview", json={"payload": payload}, headers=analyst).json()
    assert j["ok"] and j["event"]["speed_kmh"] == 64.2 and j["event"]["ts"] == 1790331302120
    assert j["event"]["dtc"] == ["P0301"] and j["event"]["evt"] == "HARSH_BRAKE"
    line = next(x for x in j["lineage"] if x["canonical"] == "speed_kmh")
    assert line["path"] == "motion.speedKmh" and line["source_value"] == 64.2
    bad = api.post("/api/v1/mappings/nordvik/1/preview", json={"payload": '{"vehicle":{}}'}, headers=analyst).json()
    assert bad["ok"] is False and bad["error"]["reason"] == "SCHEMA_MISMATCH" and bad["error"]["field"] == "vin"


def test_manual_mapping_and_diff(api, engineer, analyst):
    spec = {k: v for k, v in SPEC_HELIX.items() if k != "oem"}
    spec = {"decoder": spec["decoder"], "fields": {**spec["fields"], "speed_kmh": {"path": "fahrt.v"}}}
    r = api.post("/api/v1/mappings/helix", json={**spec, "comment": "hand written"}, headers=engineer)
    assert r.status_code == 201, r.text
    v = r.json()["version"]
    val = api.post(f"/api/v1/mappings/helix/{v}/validate", json={"label": "helix"}, headers=engineer).json()
    assert val["state"] == "draft" and val["field_accuracy"]["speed_kmh"] < 0.5 and val["failures"]
    d = api.get(f"/api/v1/mappings/helix/{v}/diff?against=1", headers=analyst).json()
    speed = next(f for f in d["fields"] if f["canonical"] == "speed_kmh")
    assert speed["change"] == "changed" and speed["after"] == {"path": "fahrt.v"}
    assert speed["before"]["transforms"] == [{"op": "unit", "quantity": "speed", "from": "mps"}]
    assert api.post(f"/api/v1/mappings/helix/{v}/actions/approve", json={}, headers=engineer).status_code == 409
    assert api.post(f"/api/v1/mappings/helix/{v}/actions/reject", json={"comment": "wrong unit"},
                    headers=engineer).json()["state"] == "rejected"


def test_simulator_control_and_scenarios(api, engineer, analyst):
    assert api.post("/api/v1/simulator/scenario/ota_drift", headers=analyst).status_code == 403
    r = api.post("/api/v1/simulator/scenario/ota_drift", headers=engineer)
    assert r.status_code == 200 and r.json()["applied"] == {"drift_pct": 35}
    assert api.post("/api/v1/simulator/scenario/nope", headers=engineer).status_code == 404
    assert api.post("/api/v1/simulator", json={"hz": 50}, headers=engineer).status_code == 422
    assert api.post("/api/v1/simulator", json={"enabled": ["tesla"]}, headers=engineer).status_code == 422
    assert api.post("/api/v1/simulator", json={"drift_pct": 0}, headers=engineer).status_code == 200


def test_a_new_source_can_be_turned_off_again(api, engineer):
    """Every scenario in the console is a toggle, so launching a maker must be undoable
    without resetting everything else the demo has set up."""
    on = api.post("/api/v1/simulator/scenario/launch_helix", headers=engineer)
    assert on.status_code == 200 and "helix" in on.json()["applied"]["enabled"]

    off = api.post("/api/v1/simulator/scenario/stop_helix", headers=engineer)
    assert off.status_code == 200
    enabled = off.json()["applied"]["enabled"]
    assert "helix" not in enabled           # the new maker stops sending
    assert "nordvik" in enabled             # and the rest of the fleet carries on
    assert "drift_pct" not in off.json()["applied"]   # nothing else is disturbed


def test_ending_one_scenario_does_not_end_another(api, engineer):
    """Ending the firmware update used to run the whole demo reset, which also dropped
    the new maker that a separate scenario had launched."""
    api.post("/api/v1/simulator/scenario/launch_helix", headers=engineer)
    api.post("/api/v1/simulator/scenario/ota_drift", headers=engineer)

    done = api.post("/api/v1/simulator/scenario/ota_reset", headers=engineer)
    assert done.status_code == 200
    applied = done.json()["applied"]
    assert applied == {"drift_pct": 0}          # only the drift is cleared
    assert "enabled" not in applied             # the new maker keeps sending


def test_every_scenario_the_console_offers_can_be_undone():
    """The console pairs each scenario with a reverse, and disables the card when there
    is none. Each reverse must undo its own scenario and leave the others alone."""
    from rosetta.api.routers.ops import SCENARIOS

    def settings(name: str) -> set[str]:
        # _enable and _disable are two ways of writing the same setting, the source list
        return {"enabled" if k in ("_enable", "_disable") else k for k in SCENARIOS[name]}

    pairs = [("launch_helix", "stop_helix"), ("ota_drift", "ota_reset"),
             ("outage", "recover"), ("shift_start", "calm"), ("pause", "resume")]
    for on, off in pairs:
        assert on in SCENARIOS and off in SCENARIOS, f"{on}/{off} missing"
        assert settings(off) == settings(on), f"{off} does not undo exactly what {on} did"
        assert SCENARIOS[off] != SCENARIOS[on], f"{off} does not change anything back"


def test_ingest_over_http(api, admin, analyst):
    body = b'{"VIN":"5PAEV1A2XTA000035","ts":1790000102.12,"msg_no":424242,"lat":13.0,"lng":80.2,"hdg":1,' \
           b'"speed_mph":10,"odometer_mi":100,"oat_f":80,"ign":"ON","dtcs":""}\n'
    h = {**admin, "X-Device-Id": "pa-http-1", "Content-Type": "application/x-ndjson"}
    r = api.post("/api/v1/ingest/pacifica", content=body * 3, headers=h)
    assert r.status_code == 202 and r.json() == {"accepted": 3, "rejected": 0}
    assert api.post("/api/v1/ingest/pacifica", content=body, headers={**analyst, "X-Device-Id": "x"}).status_code == 403
    assert api.post("/api/v1/ingest/tesla", content=body, headers=h).status_code == 404
    assert api.post("/api/v1/ingest/pacifica", content=body, headers=admin).status_code == 422   # no device id
    before = api.world.metrics()["totals"]
    api.world.pump()
    after = api.world.metrics()["totals"]
    assert after["ok"] - before["ok"] == 1 and after["duplicates"] - before["duplicates"] == 2


def test_batch_reports(api, analyst):
    q = api.get("/api/v1/batch/quality", headers=analyst).json()
    assert q["rows"] > 1000 and 20 < q["bytes_per_row"] < 200
    volt = next(s for s in q["sources"] if s["oem"] == "voltaic")
    assert volt["completeness"]["soc_pct"] == 1.0 and volt["completeness"]["fuel_pct"] == 0.0
    assert api.get("/api/v1/batch/events", headers=analyst).status_code == 200
    assert api.get("/api/v1/batch/hotspots", headers=analyst).status_code == 200


def test_errors_use_problem_json(api, engineer):
    r = api.get("/api/v1/mappings/helix/999", headers=engineer)
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    assert set(r.json()) >= {"type", "title", "status", "detail"}
    r = api.post("/api/v1/agent/runs", json={"oem": "UPPER"}, headers=engineer)
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "oem"


def test_stream_sends_overview_events(api, engineer):
    """Drive the endpoint's generator directly: a test client would wait for the end of a
    stream that, by design, never ends."""
    import asyncio

    from rosetta.api.routers import ops
    from rosetta.api.security import Principal

    async def first_frames():
        resp = await ops.stream(Principal(1, "engineer@rosetta.example", ("platform_engineer",)))
        assert resp.media_type == "text/event-stream"
        it = resp.body_iterator
        out = [await it.__anext__(), await it.__anext__()]
        await it.aclose()
        return out

    retry, frame = asyncio.run(first_frames())
    assert retry == b"retry: 2000\n\n"
    assert frame.startswith(b"event: overview\ndata: {") and frame.endswith(b"\n\n") and b'"throughput"' in frame
    assert api.get("/api/v1/stream").status_code == 401
