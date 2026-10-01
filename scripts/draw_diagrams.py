"""Draw the architecture diagrams as PNG for the Solution Document (docs/diagrams/).

The Markdown documents keep Mermaid sources of the same diagrams; these images
exist because a Word/PDF document cannot render Mermaid.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "docs" / "diagrams"
INK, FLAME, CLAY, STONE, PAPER, SKY, LEAF, AMBER = "#232428", "#FF5623", "#E99A62", "#ECE9E4", "#FCFCF4", "#5C88EE", "#67C56B", "#FFBF33"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9})


class D:
    def __init__(self, w: float, h: float, title: str = ""):
        self.fig, self.ax = plt.subplots(figsize=(w, h), dpi=200)
        self.ax.set_xlim(0, w * 10)
        self.ax.set_ylim(0, h * 10)
        self.ax.axis("off")
        self.fig.patch.set_facecolor(PAPER)
        self.w, self.h = w * 10, h * 10
        self.boxes: dict[str, tuple[float, float, float, float]] = {}
        if title:
            self.ax.text(1, self.h - 2.5, title, fontsize=12, fontweight="bold", color=INK, va="center")

    def box(self, key, x, y, w, h, label, fill=STONE, fg=INK, sub="", shape="round", bold=True):
        style = "round,pad=0.02,rounding_size=1.2" if shape == "round" else "square,pad=0.02"
        self.ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=style, fc=fill, ec=INK if fill == PAPER else fill, lw=1.1))
        if sub:
            self.ax.text(x + w / 2, y + h / 2 + 1.1, label, ha="center", va="center", color=fg, fontsize=9,
                         fontweight="bold" if bold else "normal")
            self.ax.text(x + w / 2, y + h / 2 - 1.4, sub, ha="center", va="center", color=fg, fontsize=7, alpha=0.85)
        else:
            self.ax.text(x + w / 2, y + h / 2, label, ha="center", va="center", color=fg, fontsize=9,
                         fontweight="bold" if bold else "normal")
        self.boxes[key] = (x, y, w, h)

    def store(self, key, x, y, w, h, label, sub="", fill=PAPER):
        self.box(key, x, y, w, h, label, fill=fill, sub=sub)
        self.ax.plot([x + 0.6, x + w - 0.6], [y + h - 1.2, y + h - 1.2], color=INK, lw=0.6)

    def group(self, x, y, w, h, label):
        self.ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=1.5", fc="none",
                                         ec="#9a9a9a", lw=0.8, ls=(0, (4, 3))))
        self.ax.text(x + 1, y + h - 1.4, label, fontsize=7.5, color="#6b6b6b", va="center", fontweight="bold")

    def _anchor(self, key, side):
        x, y, w, h = self.boxes[key]
        return {"l": (x, y + h / 2), "r": (x + w, y + h / 2), "t": (x + w / 2, y + h), "b": (x + w / 2, y),
                "c": (x + w / 2, y + h / 2)}[side]

    def arrow(self, a, b, label="", sa="r", sb="l", color=INK, dashed=False, rad=0.0, lpos=0.5, loff=(0, 1.2)):
        p, q = self._anchor(a, sa), self._anchor(b, sb)
        self.ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=9, color=color, lw=1.0,
                                          ls="--" if dashed else "-", connectionstyle=f"arc3,rad={rad}"))
        if label:
            mx, my = p[0] + (q[0] - p[0]) * lpos + loff[0], p[1] + (q[1] - p[1]) * lpos + loff[1]
            self.ax.text(mx, my, label, ha="center", va="center", fontsize=6.8, color=color,
                         bbox=dict(fc=PAPER, ec="none", pad=0.6))

    def save(self, name):
        OUT.mkdir(parents=True, exist_ok=True)
        self.fig.savefig(OUT / name, bbox_inches="tight", facecolor=PAPER, pad_inches=0.15)
        plt.close(self.fig)


def context():
    d = D(10, 4.6, "System context (C4 level 1)")
    d.box("veh", 2, 20, 17, 9, "Vehicles and OEM clouds", sub="six makers, six dialects", fill=CLAY)
    d.box("users", 2, 5, 17, 9, "People", sub="engineer, analyst, fleet manager", fill=CLAY)
    d.box("ros", 38, 10, 24, 18, "Rosetta", sub="normalise, park, learn, replay", fill=INK, fg=PAPER)
    d.box("cust", 80, 26, 18, 8, "Customer systems", sub="fleets, lenders, dealers")
    d.box("lake", 80, 15, 18, 8, "Warehouse or lake", sub="Snowflake, Spark, DuckDB")
    d.box("llm", 80, 4, 18, 8, "model API", sub="optional, for the agent")
    d.box("idp", 44, 36, 12, 6, "Identity provider", sub="OIDC, optional")
    d.arrow("veh", "ros", "MQTT + mTLS, HTTPS", color=FLAME)
    d.arrow("users", "ros", "browser, OAuth2 bearer")
    d.arrow("ros", "cust", "canonical events, alerts")
    d.arrow("ros", "lake", "Parquet")
    d.arrow("ros", "llm", "tool-use requests", dashed=True)
    d.arrow("ros", "idp", "tokens", sa="t", sb="b", dashed=True)
    d.save("c4_context.png")


def containers():
    d = D(12, 7.2, "Containers (C4 level 2)")
    d.group(1, 36, 26, 30, "Edge")
    d.box("sim", 3, 55, 22, 7, "simulator", sub="100,000 vehicles", fill=CLAY)
    d.box("mq", 3, 46, 22, 6, "EMQX", sub="MQTT, mTLS, QoS 1")
    d.box("gw", 3, 38, 22, 6, "gateway", sub="stamp, back-pressure")
    d.store("raw", 33, 38, 18, 9, "telemetry.raw", sub="32 partitions, key = device")
    d.box("norm", 56, 38, 25, 9, "normaliser x N", sub="decode, map, validate, dedup", fill=INK, fg=PAPER)
    d.store("can", 84, 44, 21, 8, "telemetry.canonical", sub="key = VIN")
    d.store("dlq", 84, 31, 21, 8, "telemetry.dlq", sub="parked, reason coded")
    d.box("proc", 108, 44, 12, 8, "processor", sub="x M", fill=INK, fg=PAPER)
    d.store("hot", 100, 60, 19, 7, "Redis", sub="latest state per vehicle")
    d.store("arc", 78, 60, 19, 7, "S3 / MinIO", sub="Parquet dt=/hour=")
    d.store("ts", 56, 60, 19, 7, "TimescaleDB", sub="optional hypertable")
    d.box("dw", 85, 18, 18, 7, "dead-letter worker", sub="index, sample, replay")
    d.store("pg", 55, 6, 26, 10, "PostgreSQL", sub="3NF core, registry, audit, pgvector")
    d.box("agent", 90, 5, 22, 7, "mapping agent", sub="7 tools, a person approves", fill=FLAME, fg=INK)
    d.box("api", 22, 6, 24, 9, "API + web console", sub="FastAPI, React, OAuth2", fill=INK, fg=PAPER)
    d.box("prom", 1, 20, 16, 6, "Prometheus", sub="/metrics -> Grafana")
    d.arrow("sim", "mq", "", sa="b", sb="t")
    d.arrow("mq", "gw", "shared sub", sa="b", sb="t", loff=(6, 0))
    d.arrow("gw", "raw", "produce", color=FLAME)
    d.arrow("raw", "norm", "", color=FLAME)
    d.arrow("norm", "can", "", color=FLAME, sb="l")
    d.arrow("norm", "dlq", "unreadable", sb="l", loff=(0, -1.5))
    d.arrow("can", "proc", "", color=FLAME)
    d.arrow("proc", "hot", "", sa="t", sb="b")
    d.arrow("proc", "arc", "", sa="t", sb="b", rad=0.0)
    d.arrow("proc", "ts", "", sa="t", sb="b", rad=0.0, dashed=True)
    d.arrow("dlq", "dw", "", sa="b", sb="t")
    d.arrow("dw", "raw", "replay", sa="l", sb="b", rad=-0.25, color=FLAME)
    d.arrow("dw", "pg", "groups, samples", sa="b", sb="r", rad=0.1)
    d.arrow("agent", "pg", "draft", sa="l", sb="r", loff=(0, 1.4))
    d.arrow("pg", "norm", "registry epoch", sa="t", sb="b", dashed=True)
    d.arrow("api", "pg", "", sa="r", sb="l")
    d.arrow("api", "prom", "", sa="l", sb="b", rad=-0.2)
    d.ax.text(30, 22, "all arrows into a store go through a port:\nlocal adapters (file log, SQLite, mmap, disk)\nor production adapters (Kafka, PostgreSQL, Redis, S3)",
              fontsize=7, color="#555", va="center")
    d.save("c4_containers.png")


def deployment():
    d = D(11, 5.4, "Deployment on Kubernetes (Helm chart, AWS via Terraform)")
    d.group(1, 8, 64, 40, "namespace rosetta, pods spread across nodes and zones")
    d.box("ing", 3, 36, 14, 6, "Ingress", sub="TLS")
    d.box("api", 20, 36, 18, 6, "api x2..6", sub="HPA on CPU", fill=INK, fg=PAPER)
    d.box("gw", 42, 36, 20, 6, "gateway x2", sub="MQTT shared subscription")
    d.box("norm", 3, 24, 22, 7, "normaliser x4..16", sub="HPA, emptyDir checkpoint", fill=INK, fg=PAPER)
    d.box("proc", 28, 24, 16, 7, "processor x2..8", fill=INK, fg=PAPER)
    d.box("dlq", 47, 24, 15, 7, "dlq x1")
    d.box("seed", 3, 11, 22, 7, "seed Job", sub="pre-install hook: migrate, SQL, seed")
    d.box("pdb", 28, 11, 34, 7, "PodDisruptionBudgets, NetworkPolicy,", sub="non-root, read-only root FS, no capabilities")
    d.store("msk", 72, 38, 26, 7, "Kafka (MSK)", sub="TLS, 3 brokers, RF 3")
    d.store("rds", 72, 28, 26, 7, "PostgreSQL (RDS)", sub="Multi-AZ, KMS encrypted")
    d.store("ec", 72, 18, 26, 7, "Redis (ElastiCache)", sub="TLS, auth token")
    d.store("s3", 72, 8, 26, 7, "S3", sub="SSE-KMS, lifecycle to Glacier IR")
    for k, t in (("norm", "msk"), ("proc", "ec"), ("proc", "s3"), ("api", "rds")):
        d.arrow(k, t, "", sb="l", dashed=True, color="#777")
    d.arrow("ing", "api", "")
    d.save("deployment.png")


def layers():
    d = D(10, 4.2, "Inside the normaliser: dependencies point inwards")
    d.box("shell", 2, 30, 96, 7, "Process shell: pipeline/normalizer_worker.py (poll, commit, checkpoint, metrics, hot reload)", fill=STONE, bold=False)
    d.box("eng", 2, 20, 62, 7, "Engine: compiler, codegen, router, normaliser, dedup", fill=INK, fg=PAPER, bold=False)
    d.box("alg", 66, 20, 32, 7, "Algorithms: Bloom, replay window, ...", fill=INK, fg=PAPER, bold=False)
    d.box("dom", 2, 10, 62, 7, "Domain: canonical event, transforms, VIN, DTC, errors", fill=FLAME, bold=False)
    d.box("port", 66, 10, 32, 7, "Ports: Broker, HotState, Archive", fill=CLAY, bold=False)
    d.box("adp", 66, 1, 32, 7, "Adapters: Kafka / log, Redis / mmap, S3 / disk", fill=STONE, bold=False)
    d.arrow("shell", "eng", "", sa="b", sb="t")
    d.arrow("eng", "dom", "", sa="b", sb="t")
    d.arrow("eng", "alg", "", sa="r", sb="l")
    d.arrow("shell", "port", "", sa="b", sb="t", rad=0.0)
    d.arrow("adp", "port", "implements", sa="t", sb="b", dashed=True)
    d.save("layers.png")


def er():
    d = D(12, 6.6, "Relational core, Third Normal Form (26 tables, main ones shown)")
    ent = {
        "tenant": (2, 48, "tenant", "id, name, kind"), "sub": (2, 36, "subscription", "tenant_id, plan, quota, valid_from/to"),
        "user": (2, 24, "app_user", "tenant_id?, email, password_hash"), "role": (2, 12, "role / user_role", "name; (user_id, role_id)"),
        "fleet": (26, 48, "fleet", "tenant_id, name, home_city"), "veh": (50, 48, "vehicle", "vin*, device_id*, oem_id, fleet_id"),
        "driver": (26, 30, "driver", "tenant_id, name?, email?, erased_at"), "vda": (50, 30, "vehicle_driver_assignment", "vehicle_id, driver_id, valid_from/to"),
        "trip": (74, 48, "trip", "vehicle_id, driver_id?, start/end, km"), "alert": (74, 36, "alert", "vehicle_id, kind, ts  unique"),
        "oem": (50, 12, "oem", "key*, name, wmi, status"), "mv": (74, 20, "mapping_version", "oem_id, version, state, spec"),
        "mf": (98, 28, "mapping_field", "canonical, path, transforms"), "ma": (98, 16, "mapping_action", "action, from, to, actor"),
        "gc": (74, 4, "golden_case", "oem_id, payload, expected"), "audit": (26, 4, "audit_log", "actor, action, prev_hash, hash"),
        "run": (98, 4, "agent_run / agent_step", "tool, input, output, ms"),
    }
    for k, (x, y, t, s) in ent.items():
        d.box(k, x, y, 21, 8, t, sub=s, fill=INK if k in ("veh", "mv") else STONE, fg=PAPER if k in ("veh", "mv") else INK)
    rel = [("tenant", "sub", "b", "t"), ("tenant", "fleet", "r", "l"), ("sub", "user", "b", "t"), ("user", "role", "b", "t"),
           ("fleet", "veh", "r", "l"), ("tenant", "driver", "r", "t"), ("veh", "vda", "b", "t"), ("driver", "vda", "r", "l"),
           ("veh", "trip", "r", "l"), ("veh", "alert", "r", "l"), ("oem", "veh", "t", "b"), ("oem", "mv", "r", "l"),
           ("mv", "mf", "r", "l"), ("mv", "ma", "r", "l"), ("oem", "gc", "r", "l"), ("run", "mv", "t", "r")]
    for a, b, sa, sb in rel:
        d.arrow(a, b, "", sa=sa, sb=sb, color="#555")
    d.ax.text(2, 1.5, "arrows point from the one side to the many side.  * unique.  ? nullable (erasure keeps the row, clears the person)",
              fontsize=7, color="#555")
    d.save("er.png")


def seq_onboarding():
    d = D(11, 6.2, "Sequence: a new maker is onboarded without downtime")
    cols = ["vehicle", "gateway", "normaliser", "dead letters", "agent", "engineer", "registry"]
    xs = [6 + i * 16 for i in range(len(cols))]
    for x, c in zip(xs, cols):
        d.ax.add_patch(FancyBboxPatch((x - 6, 54), 12, 4, boxstyle="round,pad=0.02,rounding_size=0.8", fc=INK, ec=INK))
        d.ax.text(x, 56, c, ha="center", va="center", color=PAPER, fontsize=8, fontweight="bold")
        d.ax.plot([x, x], [2, 54], color="#aaa", lw=0.8, ls=(0, (3, 3)))
    steps = [(0, 1, "helix payload (unknown)"), (1, 2, "raw topic"), (2, 3, "NO_ADAPTER, parked"),
             (5, 4, "run the agent"), (4, 3, "sample, profile, physics"), (4, 6, "draft v1 (validated on golden set)"),
             (5, 6, "shadow run, then approve"), (6, 2, "epoch+1: reload between batches"),
             (6, 3, "queue replay"), (3, 1, "replay parked messages"), (1, 2, "raw topic"), (2, 2, "translated, flagged replayed")]
    y = 50
    for a, b, t in steps:
        col = FLAME if "approve" in t or "reload" in t else INK
        if a == b:
            d.ax.annotate("", xy=(xs[a] + 0.3, y - 2), xytext=(xs[a] + 0.3, y), arrowprops=dict(arrowstyle="-|>", color=col,
                          connectionstyle="arc3,rad=-1.2", lw=1))
            d.ax.text(xs[a] + 3, y - 1, t, fontsize=7, color=col, va="center")
        else:
            d.ax.annotate("", xy=(xs[b], y), xytext=(xs[a], y), arrowprops=dict(arrowstyle="-|>", color=col, lw=1))
            d.ax.text((xs[a] + xs[b]) / 2, y + 1.1, t, fontsize=7, color=col, ha="center")
        y -= 4
    d.save("seq_onboarding.png")


def seq_failure():
    d = D(11, 5.4, "Sequence (failure path): a processor dies after writing a file, before committing")
    cols = ["canonical topic", "processor A", "archive (Parquet)", "processor B (restart)"]
    xs = [10 + i * 26 for i in range(len(cols))]
    for x, c in zip(xs, cols):
        d.ax.add_patch(FancyBboxPatch((x - 9, 46), 18, 4, boxstyle="round,pad=0.02,rounding_size=0.8", fc=INK, ec=INK))
        d.ax.text(x, 48, c, ha="center", va="center", color=PAPER, fontsize=8, fontweight="bold")
        d.ax.plot([x, x], [2, 46], color="#aaa", lw=0.8, ls=(0, (3, 3)))
    steps = [(0, 1, "poll 60,000 events at offsets 100..160k"), (1, 2, "write part-p0.parquet with offsets {p3: 160k} in metadata"),
             (1, 1, "SIGKILL before commit (committed offset still 100k)"), (3, 2, "on start: read offsets from newest own file"),
             (3, 0, "seek p3 to 160k (not 100k)"), (0, 3, "continue from 160k: nothing stored twice")]
    y = 42
    for a, b, t in steps:
        col = FLAME if "SIGKILL" in t else INK
        if a == b:
            d.ax.text(xs[a] + 1.5, y, "x  " + t, fontsize=7.2, color=col, va="center", fontweight="bold")
        else:
            d.ax.annotate("", xy=(xs[b], y), xytext=(xs[a], y), arrowprops=dict(arrowstyle="-|>", color=col, lw=1))
            d.ax.text((xs[a] + xs[b]) / 2, y + 1.2, t, fontsize=7, color=col, ha="center")
        y -= 6.5
    d.save("seq_failure.png")


if __name__ == "__main__":
    for f in (context, containers, deployment, layers, er, seq_onboarding, seq_failure):
        f()
    print("wrote", sorted(p.name for p in OUT.glob("*.png")))
