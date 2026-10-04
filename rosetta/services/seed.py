"""Seed the relational core with a synthetic 100,000-vehicle world.

All data is generated. Names are combined from short word lists, e-mail
addresses use the reserved `.example` domain, and VINs use fictional
manufacturer prefixes. Nothing here describes a real person or vehicle.

The vehicles are created from the same seeded `Fleet` the simulator uses, so
every VIN the stream produces exists in the database.
"""
from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from ..db.models import (
    AppUser,
    Driver,
    GoldenCase,
    Oem,
    RegistryEpoch,
    Role,
    Subscription,
    Tenant,
    UserRole,
    Vehicle,
    VehicleDriverAssignment,
)
from ..db.models import (
    Fleet as FleetRow,
)
from ..simulator.dialects import DIALECTS, expected_events
from ..simulator.fleet import BEV, CITIES, EVT_CODE, OEM_INDEX, OEMS, PHEV, Fleet
from . import registry
from .passwords import hash_password

ROLES = {
    "admin": "Full control of the platform, users and compliance requests",
    "platform_engineer": "Reviews, approves, promotes and rolls back mappings",
    "analyst": "Read-only. Sees masked locations and no personal data",
    "fleet_manager": "Sees the vehicles of one tenant, with precise location",
    "service": "Machine account for pipeline services and the AI agent",
}

DEMO_USERS = (
    # email, name, role, tenant index (None = platform staff)
    ("admin@rosetta.example", "Asha Raman", "admin", None),
    ("engineer@rosetta.example", "Dev Kapoor", "platform_engineer", None),
    ("analyst@rosetta.example", "Mira Nair", "analyst", None),
    ("manager@northwind.example", "Kiran Rao", "fleet_manager", 0),
    ("manager@bluepeak.example", "Sana Iyer", "fleet_manager", 1),
    ("agent@rosetta.example", "Mapping Agent", "service", None),
)

_T1 = ("Northwind", "Bluepeak", "Redwood", "Summit", "Harbor", "Cobalt", "Juniper", "Atlas", "Meridian",
       "Pioneer", "Sterling", "Vertex", "Lumen", "Granite", "Orchid", "Falcon", "Cedar", "Monsoon",
       "Coral", "Saffron")
_T2 = ("Logistics", "Rentals", "Mobility", "Freight", "Leasing", "Couriers", "Transit", "Finance")
_FIRST = ("Aarav", "Diya", "Kabir", "Meera", "Rohan", "Isha", "Vikram", "Anika", "Arjun", "Tara",
          "Nikhil", "Zoya", "Rahul", "Leela", "Farhan", "Nandini", "Imran", "Pooja", "Sameer", "Kavya")
_LAST = ("Menon", "Shah", "Reddy", "Bose", "Pillai", "Gill", "Khan", "Desai", "Verma", "Joshi",
         "Nambiar", "Chopra", "Dutta", "Hegde", "Saxena", "Thomas", "Fernandes", "Mehta", "Rao", "Sethi")
_PT = {0: "ICE", BEV: "BEV", PHEV: "PHEV"}


def demo_password() -> str:
    """Password of the demo accounts. Override with ROSETTA_DEMO_PASSWORD."""
    return os.environ.get("ROSETTA_DEMO_PASSWORD", "rosetta-demo-2026")


def is_seeded(s: Session) -> bool:
    return (s.execute(select(func.count()).select_from(Oem)).scalar() or 0) > 0


def seed(s: Session, vehicles: int = 100_000, seed_value: int = 7, fleets: int = 250,
         tenants: int = 40, golden_per_dialect: int = 200, fleet: Fleet | None = None,
         log=print) -> dict[str, Any]:
    if is_seeded(s):
        return {"skipped": True}
    rng = np.random.default_rng(seed_value + 1000)
    now = datetime.now(UTC)

    roles = {name: Role(name=name, description=desc) for name, desc in ROLES.items()}
    s.add_all(roles.values())

    tenant_rows = []
    for i in range(tenants):
        name = f"{_T1[i % len(_T1)]} {_T2[(i // len(_T1) + i) % len(_T2)]}"
        kind = "lender" if name.endswith("Finance") else "fleet"
        tenant_rows.append(Tenant(name=name if i < len(_T1) else f"{name} {i}", kind=kind))
    s.add_all(tenant_rows)
    s.flush()

    for t in tenant_rows:
        plan = ("starter", "growth", "enterprise")[t.id % 3]
        s.add(Subscription(tenant_id=t.id, plan=plan, status="active",
                           vehicle_quota={"starter": 500, "growth": 5000, "enterprise": 100000}[plan],
                           price_cents={"starter": 49_900, "growth": 249_900, "enterprise": 1_499_900}[plan],
                           valid_from=now - timedelta(days=200), valid_to=now + timedelta(days=165)))

    fleet_rows = []
    for i in range(fleets):
        t = tenant_rows[i % tenants]
        city = CITIES[i % len(CITIES)][0]
        fleet_rows.append(FleetRow(tenant_id=t.id, name=f"{city} depot {i // tenants + 1}", home_city=city))
    s.add_all(fleet_rows)

    oem_rows = {}
    for prof in OEMS:
        d = DIALECTS[prof.key]
        oem_rows[prof.key] = Oem(key=prof.key, name=prof.name, wmi=prof.wmi,
                                 wire_format=d.spec["decoder"]["type"],
                                 status="live" if d.seeded else "onboarding")
    s.add_all(oem_rows.values())
    s.flush()

    pw = hash_password(demo_password())
    for email, name, role, ti in DEMO_USERS:
        u = AppUser(email=email, display_name=name, password_hash=pw,
                    tenant_id=None if ti is None else tenant_rows[ti].id)
        s.add(u)
        s.flush()
        s.add(UserRole(user_id=u.id, role_id=roles[role].id))
    s.flush()

    # ---- vehicles, from the same seeded fleet the simulator drives
    sim = fleet or Fleet(vehicles, seed=seed_value, fleets=fleets)
    oem_id = [oem_rows[o.key].id for o in OEMS]
    fleet_id = [f.id for f in fleet_rows]
    years = rng.integers(2019, 2027, sim.n)
    rows = [{"vin": sim.vin[i], "device_id": sim.device_id[i], "oem_id": oem_id[int(sim.oem[i])],
             "fleet_id": fleet_id[int(sim.fleet_id[i])], "powertrain": _PT[int(sim.powertrain[i])],
             "model_year": int(years[i]), "created_at": now} for i in range(sim.n)]
    for a in range(0, len(rows), 10_000):
        s.execute(insert(Vehicle), rows[a:a + 10_000])
    log(f"  vehicles: {len(rows):,}")

    # ---- drivers: one for every fifth vehicle, inside the vehicle's tenant
    tenant_of_fleet = {f.id: f.tenant_id for f in fleet_rows}
    vid = dict(s.execute(select(Vehicle.vin, Vehicle.id)).all())
    drivers, pairs = [], []
    for i in range(0, sim.n, 5):
        fn, ln = _FIRST[int(rng.integers(len(_FIRST)))], _LAST[int(rng.integers(len(_LAST)))]
        tid = tenant_of_fleet[fleet_id[int(sim.fleet_id[i])]]
        drivers.append({"tenant_id": tid, "full_name": f"{fn} {ln}",
                        "email": f"{fn}.{ln}.{i}@drivers.example".lower(),
                        "phone": f"+91-55501-{i % 100000:05d}",
                        "license_hash": hashlib.sha256(f"DL-{seed_value}-{i}".encode()).hexdigest(),
                        "created_at": now})
        pairs.append(sim.vin[i])
    for a in range(0, len(drivers), 10_000):
        s.execute(insert(Driver), drivers[a:a + 10_000])
    did = [r[0] for r in s.execute(select(Driver.id).order_by(Driver.id))]
    assign = [{"vehicle_id": vid[v], "driver_id": d, "valid_from": now - timedelta(days=30)}
              for v, d in zip(pairs, did)]
    for a in range(0, len(assign), 10_000):
        s.execute(insert(VehicleDriverAssignment), assign[a:a + 10_000])
    log(f"  drivers: {len(drivers):,}")

    # ---- mappings the platform already knows, and golden cases for every dialect
    s.add(RegistryEpoch(id=1, epoch=0))
    s.flush()
    for _key, d in DIALECTS.items():
        if d.seeded:
            mv = registry.create_version(s, d.oem, d.spec, source="seed", actor="seed", actor_kind="system",
                                         state="active", comment="known dialect at platform start")
            mv.activated_at = now

    for k in range(12):
        sim.step(1.0, 1_790_000_000_000 + k * 1000)
    idx_all = sim.emitting("all")
    golden = 0
    n_evt = len(EVT_CODE)
    for key, d in DIALECTS.items():
        sel = idx_all[sim.oem[idx_all] == OEM_INDEX[d.oem]]
        # A certification kit covers every case on purpose. Four vehicles per event
        # type come first, so both halves of the set (development and hold-out)
        # contain every event code. The rest is a varied sample: moving and parked,
        # with and without trouble codes.
        moving = sel[sim.speed[sel] > 5]
        ev_pick = moving[: n_evt * 4] if len(moving) >= n_evt * 4 else sel[: n_evt * 4]
        saved = sim.evt[ev_pick].copy()
        sim.evt[ev_pick] = np.repeat(np.arange(1, n_evt + 1), 4)[: len(ev_pick)]
        rest_pool = np.setdiff1d(sel, ev_pick)
        score = (sim.speed[rest_pool] > 1).astype(int) + np.array([1 if int(i) in sim.dtc else 0 for i in rest_pool])
        order = np.argsort(-score, kind="stable")
        rest_n = min(max(0, golden_per_dialect - len(ev_pick)), len(rest_pool))   # small fleets: take what exists
        varied = rest_pool[order[: rest_n // 2]]
        left = np.setdiff1d(rest_pool, varied)
        random_part = rng.choice(left, size=min(len(left), rest_n - len(varied)), replace=False)
        pick = np.concatenate([ev_pick, varied, random_part])
        t = sim.truth(pick)
        sim.evt[ev_pick] = saved
        payloads = d.encode(t)
        for p, e in zip(payloads, expected_events(t, d.oem)):
            s.add(GoldenCase(oem_id=oem_rows[d.oem].id, label=key, payload=p, expected=e))
            golden += 1
    registry.bump_epoch(s)
    s.flush()
    log(f"  golden cases: {golden:,}")
    return {"skipped": False, "vehicles": len(rows), "drivers": len(drivers), "fleets": fleets,
            "tenants": tenants, "golden_cases": golden, "oems": len(OEMS)}
