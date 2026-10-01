# Demo script — 9 minutes

Spoken lines in quotes. Everything else is a cue, kept short on purpose.
About 1,070 words over nine minutes: roughly 120 a minute, which is unhurried.
The rest of the time is clicking and waiting, so take the pauses.

Say the lines your own way if that comes out easier. Nothing needs quoting exactly.

## Setup

1. **Clean world**, so Helix is still unknown: stop the app, `mv data data.before-demo`,
   `make run`, wait **2 minutes** so the charts have history.
2. **Pick the engine.** Your `.env` has a Gemini key, so Auto uses the model.
   A free key can hit its per-minute limit and hand back to the deterministic
   workflow mid-run, so **rehearse the agent step once**. If the model finishes,
   show **Model**. If it is flaky, show **Deterministic** and just say a model can
   drive the same tools. Reset again after rehearsing (step 1): the scenario's off
   switch stops Helix sending, but the parked messages and any approved mapping stay.
3. **Two windows** on http://127.0.0.1:8765, password `rosetta-demo-2026`:
   A as `engineer@rosetta.example`, B (private) as `analyst@rosetta.example`.
4. **Tabs ready:** `docs/diagrams/c4_containers.png`, `docs/diagrams/bench_burst.png`,
   the README Results table. Generate the images first:
   `.venv/bin/python scripts/draw_diagrams.py` and `scripts/draw_charts.py`.
5. 1080p, notifications off, browser at 110%.

---

## 0:00 — The problem

*Landing page, top.*

> "Hi, I'm Bhanu Prakash Kusha. This is Rosetta.
>
> Connected cars send data all the time. Speed, location, battery, fault codes.
>
> The problem? Every car maker sends it in a different format."

*Scroll to "the same moment, written six ways".*

> "Here's one single moment. A car brakes hard, at 64 kilometres an hour.
>
> Six makers describe it six different ways.
> Different names. Different units. One sends binary. One writes in German.
>
> So today, a new maker means new code. A firmware update means new code.
> And while that code ships, you lose data."

## 0:45 — The idea

*Scroll to "one event, whatever the source", then to Park, Study, Prove, Approve, Replay.*

> "Rosetta turns every format into one standard event.
>
> And when a maker it has never seen starts sending, nothing breaks.
> It **parks** the messages. An AI agent **studies** them.
> It **proves** the mapping on known answers. A person **approves** it.
> Then the parked messages are **replayed**.
>
> No new code. No restart. Nothing lost.
> Let me show you."

## 1:15 — Live pipeline

*Sign in as engineer → Live.*

> "This is running live, on one laptop.
> A hundred thousand simulated vehicles. About a hundred thousand events a second.
> Five different makers."

*Point at "Ingest to dashboard".*

> "This is how long a message takes to reach the dashboard.
> The target was two seconds. We're at about a quarter of a second."

*Point at "Duplicates dropped".*

> "And I break things on purpose.
> Duplicates. Messages out of order. Damaged messages.
> You can watch the duplicates get caught and dropped. Nothing is counted twice."

## 2:00 — A new maker appears

*"Make something happen" → "A new maker starts sending". Wait for "parked, no mapping".*

> "Now watch this. A new maker, Helix Mobility, starts sending.
> Rosetta has never seen this format before.
>
> And look. Nothing is dropped.
> Every message it can't read is parked, with a reason. No adapter."

*Dead letters → open the Helix group and a payload.*

> "Here are the parked messages, grouped by format. Let's open one.
>
> The field names are in German. `fahrt.v`.
> Right now, nothing in the system knows what that means."

## 2:45 — The agent learns the format

The core of the demo. Take your time.

*"Ask the agent for a mapping". Point at the engine toggle.*

> "This is the Mapping Studio. And this is the interesting part.
>
> The agent has seven tools. And there are two ways to drive them.
>
> **Deterministic** runs them in a fixed order. No AI. Same answer every time.
>
> **Model** lets a language model decide what to do next, and explain why.
> Gemini, Claude, whichever key you give it."

*Run the agent. While it works:*

> "And one thing matters here.
> It can't write code. It can't approve its own work. It can only suggest."

*Open "Profile every field".*

> "Step one, it collects the parked messages. Step two, it profiles every field.
>
> And this is my favourite part. It uses **physics**.
> It finds latitude and longitude just from how the car moves.
>
> Then it notices something. This field, `fahrt.v`, is always the GPS speed
> times zero point two seven eight.
> And that number is exactly how you convert kilometres an hour into metres per second.
>
> So this field is speed. In metres per second.
> It doesn't matter what it's called."

*Point at "Propose a mapping".*

> "Next, a machine-learning model suggests a match for every field.
> Then the Hungarian algorithm makes sure no two fields claim the same slot."

*Point at "Test on known answers".*

> "Then it tests itself.
> I keep a set of messages where the right answer is already known.
>
> And here's the trick. Half of those are hidden from the agent.
> So it can't memorise them. It has to actually get them right.
>
> It passes all of them. Including the half it never saw.
> And every step is written to the audit log. Input and output."

## 4:30 — A person approves

*"Try on parked traffic".*

> "Before anyone approves it, I can try it on the real parked messages.
> Nothing gets written. It just shows what would happen."

*Approve, type a reason, confirm. Then go to Live.*

> "Only a person can approve this. If the agent tries, the system refuses it.
>
> Within a second, the workers pick up the new mapping.
> Helix appears in the chart. The parked messages replay. The parked count drops.
>
> And nothing restarted. That's a new car maker onboarded, with zero downtime."

## 5:15 — Firmware update, safe rollout

*Live → "Firmware update".*

> "Now a harder one. Pacifica pushes a firmware update.
> It renames fields and changes the units. But only on 35 percent of the cars.
>
> So the same maker is now sending two different formats at once."

*Run the agent again, approve, open the Pacifica Sources page.*

> "The agent proposes version two. And when I approve it, it doesn't go to everyone.
>
> It goes out as a **canary**, to a small share of cars.
> Version one keeps serving the rest. Both run side by side.
>
> If it looks good, I promote it. If not, one click rolls it back."

*Short on time? Skip the second agent run. Show the drift on Live, explain the canary from Sources.*

## 6:00 — It survives failures

*Live → Processes. Kill a normaliser.*

> "So what happens when a server dies? Let's find out.
>
> I'll kill a worker outright, the way a real machine fails.
> The queue grows. The supervisor restarts it from its last checkpoint.
> And the queue drains."

*Show `bench_burst.png`.*

> "And I tested this properly.
> I killed four processes under full load. Nothing was lost. Nothing was stored twice.
>
> And in a traffic burst, the backlog grew to one point four million messages.
> It drained in 25 seconds. Still nothing lost."

## 6:45 — Fleet, privacy, audit

*Fleet → a vehicle with an alert.*

> "This is what a fleet operator sees.
> Every vehicle on one map, in one format, no matter which maker it came from.
>
> And alerts are raised automatically. Harsh braking. Low battery. New fault codes."

*Window B (analyst) → Fleet.*

> "Now the same page, as an analyst.
> The locations are blurred to about five kilometres. The vehicle numbers are shortened.
>
> Same data. Different role. Different view."

*Window A → Compliance, filter to Agent.*

> "Every read of fleet data, and every agent action, is logged here.
>
> And it's a hash chain. Each entry carries the fingerprint of the one before it.
> So if anyone edits a row, the chain breaks right there.
> Tampering shows up."

## 7:40 — Results

*Insights → "The field-mapping model, against a baseline".*

> "So how good is the AI, really?
>
> On formats it has never seen before, it maps **99.1 percent** of the fields correctly.
> Plain name matching gets **35.8 percent**."

*README Results table.*

> "And every number here is measured. Each one has its evidence file in the repo.
>
> A hundred thousand events a second, sustained.
> Under 400 milliseconds to the dashboard, even at the 99th percentile.
> Zero events lost, out of more than ten million.
> A ten-minute soak test: 31 million events, no restarts.
> Over 2,700 automated tests, 93 percent coverage, all green in CI.
> And the security scans pass, including OWASP ZAP against the running API."

*Architecture diagram.*

> "And quickly, under the hood.
>
> MQTT from the cars. Kafka as the queue. Python services doing the translation.
> Redis, PostgreSQL and Parquet for storage. A React dashboard.
> Prometheus and Grafana for monitoring.
>
> It all runs in Docker, and the Kubernetes and AWS files are in the repo."

## 8:35 — Close

*Landing page, or a closing slide with your name.*

> "Let me be honest about the limits.
> This is simulated data, on one laptop. Real car feeds would be messier than this.
> And that is exactly why every mapping has to pass the golden set, and a person.
>
> So, that's Rosetta.
> One event from every car maker.
> And a new maker joins in minutes. No code, no downtime, nothing lost.
>
> Thank you."

---

## If it goes wrong

| Problem | What to do |
|---|---|
| Model run slow or rate limited | Say: "on a free key it falls back to the deterministic engine, and records why." That *is* a feature. Or switch to Deterministic and rerun. |
| Helix already mapped | You didn't reset. Stop the app, `mv data data.old2`, `make run`, wait 2 minutes. |
| Charts empty | Just started. Wait 2 minutes. |
| Running long | Cut the second agent run at 5:15, then shorten 6:45. Never cut 2:00 to 4:30: that is the idea. |

## Numbers and evidence

| Claim | File |
|---|---|
| 100,572 sent/s, 99,364 translated/s; p50 129 ms, p95 258 ms, p99 379 ms; 0 of 10,140,200 lost | `bench_steady.json` |
| Burst: 1.4 M backlog drained 25 s after, 0 lost | `bench_burst.json` |
| Soak: 30.9 M events in 10 min, 0 restarts | `bench_soak.json` |
| 4 processes SIGKILLed, 0 lost, 0 stored twice | `chaos.json` |
| 99.1% vs 35.8% field mapping | `ml_field_mapper.json` |
| 2,768 tests, 93.5% coverage | `coverage.json` |

All in `docs/evidence/`. Regenerate them if you change code before recording: the
README numbers are typed by hand and go stale.
