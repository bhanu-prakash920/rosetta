"""Charts of the benchmark evidence for the Solution Document (docs/diagrams/)."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EV, OUT = ROOT / "docs" / "evidence", ROOT / "docs" / "diagrams"
INK, FLAME, CLAY, STONE, PAPER, SKY, LEAF = "#232428", "#FF5623", "#E99A62", "#ECE9E4", "#FCFCF4", "#5C88EE", "#2d7a31"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8.5, "axes.edgecolor": "#999", "axes.facecolor": PAPER,
                     "figure.facecolor": PAPER, "axes.spines.top": False, "axes.spines.right": False})


def load(name):
    p = EV / f"bench_{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def pipeline(name, title, burst=False):
    r = load(name)
    if not r:
        return
    s = r["samples"]
    t = [x["t"] for x in s]
    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(8, 6.2), sharex=True, dpi=200)
    a1.plot(t, [x["sent_per_s"] / 1000 for x in s], color=CLAY, lw=1.2, label="sent by the simulator")
    a1.plot(t, [x["ok_per_s"] / 1000 for x in s], color=INK, lw=1.4, label="translated")
    a1.set_ylabel("thousand events / s")
    a1.legend(frameon=False, loc="upper left", fontsize=7)
    a1.set_title(title, loc="left", fontweight="bold", fontsize=10)
    a2.plot(t, [x["e2e_p50"] for x in s], color=SKY, lw=1.1, label="p50")
    a2.plot(t, [x["e2e_p95"] for x in s], color=INK, lw=1.2, label="p95")
    a2.plot(t, [x["e2e_p99"] for x in s], color=FLAME, lw=1.2, label="p99")
    a2.axhline(2000, color=FLAME, lw=0.8, ls="--")
    a2.text(t[0], 2050, "target 2,000 ms", color=FLAME, fontsize=7, va="bottom")
    a2.set_ylabel("ingest to dashboard, ms")
    a2.legend(frameon=False, loc="upper right", ncol=3)
    a3.fill_between(t, [x["lag_normalizer"] / 1000 for x in s], color=CLAY, alpha=0.8, label="waiting for the normaliser")
    a3.fill_between(t, [x["lag_processor"] / 1000 for x in s], color=INK, alpha=0.5, label="waiting for the processor")
    a3.set_ylabel("backlog, thousand events")
    a3.set_xlabel("seconds")
    a3.legend(frameon=False, loc="upper right")
    if burst and r["config"]["burst_hz"]:
        b0 = r["config"]["seconds"] / 4
        for a in (a1, a2, a3):
            a.axvspan(b0, b0 + r["config"]["burst_seconds"], color=FLAME, alpha=0.08)
    fig.tight_layout()
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"bench_{name}.png", facecolor=PAPER)
    plt.close(fig)


def memory():
    r = load("soak")
    if not r or not r.get("memory_samples"):
        return
    m = r["memory_samples"]
    names = [k for k in m[0] if k != "t"]
    fig, ax = plt.subplots(figsize=(8, 3), dpi=200)
    for n in names:
        ax.plot([x["t"] / 60 for x in m], [x.get(n) for x in m], lw=1.1, label=n)
    ax.set_xlabel("minutes")
    ax.set_ylabel("resident memory, MB")
    ax.set_title("Soak: memory of every process", loc="left", fontweight="bold", fontsize=10)
    ax.legend(frameon=False, fontsize=6.5, ncol=4, loc="lower right")
    fig.tight_layout()
    fig.savefig(OUT / "bench_soak_memory.png", facecolor=PAPER)
    plt.close(fig)


def model():
    r = json.loads((EV / "ml_field_mapper.json").read_text())
    labels = ["field and unit", "field", "unit, when field right", "whole format right"]
    mv = [r["model_scores"]["exact_label_accuracy"], r["model_scores"]["field_accuracy"],
          r["model_scores"]["unit_accuracy_when_field_right"], r["whole_dialect"]["model_fully_correct"]]
    bv = [r["baseline_scores"]["exact_label_accuracy"], r["baseline_scores"]["field_accuracy"],
          r["baseline_scores"]["unit_accuracy_when_field_right"], r["whole_dialect"]["baseline_fully_correct"]]
    fig, ax = plt.subplots(figsize=(8, 2.8), dpi=200)
    y = range(len(labels))
    ax.barh([i + 0.2 for i in y], [v * 100 for v in mv], height=0.38, color=FLAME, label="model")
    ax.barh([i - 0.2 for i in y], [v * 100 for v in bv], height=0.38, color="#9a9a9a", label="name-matching baseline")
    for i, (a, b) in enumerate(zip(mv, bv)):
        ax.text(a * 100 + 1, i + 0.2, f"{a:.1%}", va="center", fontsize=7.5)
        ax.text(b * 100 + 1, i - 0.2, f"{b:.1%}", va="center", fontsize=7.5, color="#555")
    ax.set_yticks(list(y), labels)
    ax.set_xlim(0, 110)
    ax.set_xlabel("percent correct on 160 dialects with names never seen in training")
    ax.invert_yaxis()
    ax.legend(frameon=False, loc="lower right")
    fig.tight_layout()
    fig.savefig(OUT / "ml_vs_baseline.png", facecolor=PAPER)
    plt.close(fig)


if __name__ == "__main__":
    pipeline("steady", "Steady state: 100,000 vehicles, one event each per second")
    pipeline("burst", "Burst: 3x requested (shaded), then catch-up", burst=True)
    pipeline("soak", "Soak: 50,000 events per second")
    memory()
    model()
    print(sorted(p.name for p in OUT.glob("*.png")))
