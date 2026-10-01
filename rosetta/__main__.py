"""Command line: `python -m rosetta <command>`.

    up        seed if needed, start the pipeline and the API (one command demo)
    migrate   create the relational schema and nothing else
    seed      create the database and the synthetic 100K-vehicle world
    train     train and evaluate the field-mapping model
    agent     run the mapping agent for one source
    verify    check the audit chain
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _seed(vehicles: int) -> None:
    from .db.session import init_db, session_scope
    from .factory import load_vehicle_index, make_hot_state
    from .services import agent_service, seed

    init_db()
    with session_scope() as s:
        if seed.is_seeded(s):
            print("database already seeded")
        else:
            print(f"seeding {vehicles:,} vehicles")
            print(json.dumps(seed.seed(s, vehicles=vehicles), indent=2))
    with session_scope() as s:
        agent_service.seed_memory(s)
    vins, _ = load_vehicle_index()
    make_hot_state(vins, writable=True).close()      # create the state file before workers map it


# Read from .env by a local run. Deliberately only the model credentials: the rest of
# that file configures the Compose stack, and some of it would quietly change a local
# run if it were applied here. ROSETTA_DEMO_PASSWORD is the clearest example, where the
# shipped placeholder would replace the password the README documents.
ENV_FILE_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY",
                 "GOOGLE_API_KEY", "ROSETTA_LLM_PROVIDER", "ROSETTA_LLM_MODEL")


def _load_env_file(path: str = ".env") -> list[str]:
    """Fill in missing model credentials from .env. Returns the names that were set.

    Only the command line does this, never an imported module, so a developer's .env
    cannot reach the tests. The real environment wins: the file fills gaps, it never
    overrides what the shell already set.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        return []
    try:
        values = dotenv_values(path)
    except OSError:
        return []
    taken = []
    for key in ENV_FILE_KEYS:
        value = (values.get(key) or "").strip()
        if value and not os.environ.get(key):
            os.environ[key] = value
            taken.append(key)
    return taken


def main(argv: list[str] | None = None) -> int:
    _load_env_file()
    ap = argparse.ArgumentParser(prog="rosetta", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument("--host", default="127.0.0.1")
    up.add_argument("--port", type=int, default=8000)
    up.add_argument("--vehicles", type=int, default=int(os.environ.get("ROSETTA_VEHICLES", "100000")))
    up.add_argument("--normalizers", type=int, default=4)
    up.add_argument("--processors", type=int, default=2)
    up.add_argument("--shards", type=int, default=1)
    sub.add_parser("migrate")
    sd = sub.add_parser("seed")
    sd.add_argument("--vehicles", type=int, default=100_000)
    sub.add_parser("train")
    ag = sub.add_parser("agent")
    ag.add_argument("oem")
    ag.add_argument("--engine", default="auto", choices=["auto", "workflow", "model", "anthropic", "google"])
    sub.add_parser("verify")
    a, rest = ap.parse_known_args(argv)

    if a.cmd == "migrate":
        from .db.session import init_db

        init_db()
        print("schema ready")
    elif a.cmd == "seed":
        _seed(a.vehicles)
    elif a.cmd == "train":
        from .ml import train

        sys.argv = ["train", *rest]
        train.main()
    elif a.cmd == "agent":
        from .db.session import init_db, session_scope
        from .services import agent_service

        init_db()
        with session_scope() as s:
            run = agent_service.run_agent(s, a.oem, requested_by="cli", engine=a.engine)
            print(json.dumps({"run": run.id, "status": run.status, "engine": run.engine,
                              "summary": run.summary}, indent=2))
    elif a.cmd == "verify":
        from .db.session import init_db, session_scope
        from .services import audit

        init_db()
        with session_scope() as s:
            res = audit.verify_chain(s)
        print(json.dumps(res, indent=2))
        return 0 if res["valid"] else 1
    elif a.cmd == "up":
        os.environ.update({"ROSETTA_EMBED_PIPELINE": "1", "ROSETTA_VEHICLES": str(a.vehicles),
                           "ROSETTA_NORMALIZERS": str(a.normalizers), "ROSETTA_PROCESSORS": str(a.processors),
                           "ROSETTA_SIM_SHARDS": str(a.shards)})
        _seed(a.vehicles)
        import uvicorn

        from .services.seed import DEMO_USERS, demo_password

        print(f"\n  Rosetta is starting on http://{a.host}:{a.port}")
        print(f"  demo accounts (password: {demo_password()})")
        for email, _n, role, _t in DEMO_USERS:
            if role != "service":
                print(f"    {email:32s} {role}")
        print()
        uvicorn.run("rosetta.api.main:app", host=a.host, port=a.port, log_level="warning",
                    timeout_graceful_shutdown=5)   # open event streams must not hold shutdown
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
