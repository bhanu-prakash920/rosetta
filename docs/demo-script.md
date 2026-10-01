# Demo video script (about 9 minutes)

Target length 9:00, which leaves a minute of slack under the 10-minute limit.

The spoken lines come to about 1,070 words. Spread over nine minutes that is roughly
120 a minute, which is an unhurried pace, and the rest of the time is clicking and
waiting. So you can take the pauses: they are built into the budget.

The lines are written to be said, not read. Short sentences, one idea each, and a
blank line wherever it is natural to breathe. Say them in your own words if that comes
out easier; nothing here needs to be quoted exactly.

## Before you record

1. **Start from a clean world**, so Helix Mobility is still unknown:
   stop the app, `mv data data.before-demo` (keeps the old data, nothing is lost),
   then `make run`. It reseeds 100,000 vehicles. Wait **2 minutes** before recording
   so the charts have history.
2. **Pick the agent engine.** Your `.env` has a `GEMINI_API_KEY`, so **Auto = the model**.
   On a free-tier key the model can hit its per-minute limit and hand back to the
   deterministic workflow mid-run. **Rehearse the whole agent step once.** If the model
   finishes, show **Model**. If it is flaky, pick **Deterministic** (fast, always
   finishes) and just *say* that a model can drive the same tools.
   After rehearsing, reset again (step 1) so Helix is unknown for the real take.
   The scenario's own off switch stops Helix sending, which is enough to rerun the
   earlier parts, but it leaves the parked messages and any approved mapping in
   place. Only step 1 makes the platform forget Helix completely.
3. Open two browser windows on http://127.0.0.1:8765:
   - Window A: signed in as `engineer@rosetta.example`
   - Window B (private window): signed in as `analyst@rosetta.example`
   - Password for both: `rosetta-demo-2026`
4. Have these ready in tabs: `docs/diagrams/c4_containers.png` (the architecture:
   containers, stores and protocols), `docs/diagrams/bench_burst.png`, and the README
   "Results" table. Both images are build outputs, so generate them first with
   `.venv/bin/python scripts/draw_diagrams.py` and `.venv/bin/python scripts/draw_charts.py`.
5. Record at 1080p, close notifications, zoom the browser to 110% so text is readable.

## The script

Format: **[time] Screen → what to click.** Then *what to say*.

---

### Part 1: The problem (0:00 to 0:45)

**[0:00] Landing page, top.**

> "Hi, I'm Bhanu Prakash Kusha. This is Rosetta.
>
> Connected cars send data all the time. Speed, location, battery, fault codes.
>
> The problem? Every car maker sends it in a different format."

**[0:15] Scroll to "the same moment, written six ways".**

> "Here's one single moment. A car brakes hard, at 64 kilometres an hour.
>
> Six makers describe it six different ways.
> Different names. Different units. One sends binary. One writes in German.
>
> So today, a new maker means new code. A firmware update means new code.
> And while that code ships, you lose data."

### Part 2: Our solution in one line (0:45 to 1:15)

**[0:45] Scroll to "one event, whatever the source", then to Park, Study, Prove, Approve, Replay.**

> "Rosetta turns every format into one standard event.
>
> And when a maker it has never seen starts sending, nothing breaks.
> It **parks** the messages. An AI agent **studies** them.
> It **proves** the mapping on known answers. A person **approves** it.
> Then the parked messages are **replayed**.
>
> No new code. No restart. Nothing lost.
> Let me show you."

### Part 3: The live pipeline (1:15 to 2:00)

**[1:15] Click "Sign in" (engineer) → Live page.**

> "This is running live, on one laptop.
> A hundred thousand simulated vehicles. About a hundred thousand events a second.
> Five different makers."

**Point at "Ingest to dashboard".**

> "This is how long a message takes to reach the dashboard.
> The target was two seconds. We're at about a quarter of a second."

**Point at "Duplicates dropped" and "Throughput by source".**

> "And I break things on purpose.
> Duplicates. Messages out of order. Damaged messages.
> You can watch the duplicates get caught and dropped. Nothing is counted twice."

### Part 4: A new car maker appears (2:00 to 2:45)

**[2:00] Under "Make something happen", click "A new maker starts sending".**

> "Now watch this. A new maker, Helix Mobility, starts sending.
> Rosetta has never seen this format before."

**Wait until the "What needs attention" card shows Helix, "parked, no mapping".**

> "And look. Nothing is dropped.
> Every message it can't read is parked, with a reason. No adapter."

**[2:20] Click "Dead letters" in the menu. Open the Helix group and a sample payload.**

> "Here are the parked messages, grouped by format. Let's open one.
>
> The field names are in German. `fahrt.v`.
> Right now, nothing in the system knows what that means."

### Part 5: The AI agent learns the format (2:45 to 4:30)

This is the most important part. Take your time.

**[2:45] Click "Ask the agent for a mapping" (or Mapping studio → Helix).**

> "This is the Mapping Studio. And this is the interesting part."

**Point at the engine toggle: Auto / Deterministic / Model.**

> "The agent has seven tools. And there are two ways to drive them.
>
> **Deterministic** runs them in a fixed order. No AI. Same answer every time.
>
> **Model** lets a language model decide what to do next, and explain why.
> Gemini, Claude, whichever key you give it."

**Select your engine, click "Run the agent". While it runs:**

> "And one thing matters here.
> It can't write code. It can't approve its own work. It can only suggest."

**[3:20] When it finishes, walk down the steps. Open "Profile every field".**

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

**[3:50] Point at "Propose a mapping".**

> "Next, a machine-learning model suggests a match for every field.
> And the Hungarian algorithm makes sure no two fields claim the same slot."

**[4:00] Point at "Test on known answers" and "Submit for review".**

> "Then it tests itself.
> I keep a set of messages where the right answer is already known.
>
> And here's the trick. Half of those are hidden from the agent.
> So it can't memorise them. It has to actually get them right.
>
> It passes all of them. Including the half it never saw.
> And every step is written to the audit log. Input and output."

### Part 6: A human approves, zero downtime (4:30 to 5:15)

**[4:30] Scroll down to the draft version, click "Try on parked traffic".**

> "Before anyone approves it, I can try it on the real parked messages.
> Nothing gets written. It just shows what would happen."

**[4:45] Click "Approve", type a reason, confirm.**

> "Only a person can approve this. If the agent tries, the system refuses it."

**[4:55] Go to Live.**

> "Within a second, the workers pick up the new mapping.
>
> Helix appears in the chart. The parked messages replay. The parked count drops.
>
> And nothing restarted. That's a new car maker onboarded, with zero downtime."

### Part 7: Firmware update and safe rollout (5:15 to 6:00)

**[5:15] Live → click "Firmware update".**

> "Now a harder one. Pacifica pushes a firmware update.
> It renames fields and changes the units. But only on 35 percent of the cars.
>
> So the same maker is now sending two different formats at once."

**Click "Review the new format", run the agent again, approve. Open the Pacifica Sources page.**

> "The agent proposes version two. And when I approve it, it doesn't go to everyone.
>
> It goes out as a **canary**, to a small share of cars.
> Version one keeps serving the rest. Both run side by side.
>
> If it looks good, I promote it. If not, one click rolls it back."

*(If you are short on time, skip running the agent here: start the scenario, show the
drift appear on Live, and just explain the canary with the Sources page.)*

### Part 8: It survives failures (6:00 to 6:45)

**[6:00] Live → "Processes" card. Hover a normaliser, kill it.**

> "So what happens when a server dies? Let's find out.
>
> I'll kill a worker outright, the way a real machine fails.
> The queue grows. The supervisor restarts it from its last checkpoint.
> And the queue drains."

**[6:20] Show `docs/diagrams/bench_burst.png`.**

> "And I tested this properly.
> I killed four processes under full load. Nothing was lost. Nothing was stored twice.
>
> And in a traffic burst, the backlog grew to one point four million messages.
> It drained in 25 seconds. Still nothing lost."

### Part 9: Fleet, privacy and audit (6:45 to 7:40)

**[6:45] Click "Fleet". Click a vehicle with an alert.**

> "This is what a fleet operator sees.
> Every vehicle on one map, in one format, no matter which maker it came from.
>
> And alerts are raised automatically. Harsh braking. Low battery. New fault codes."

**[7:00] Switch to Window B (analyst) → Fleet.**

> "Now the same page, as an analyst.
> The locations are blurred to about five kilometres. The vehicle numbers are shortened.
>
> Same data. Different role. Different view."

**[7:15] Window A → "Compliance". Filter the entries to Agent.**

> "Every read of fleet data, and every agent action, is logged here.
>
> And it's a hash chain. Each entry carries the fingerprint of the one before it.
> So if anyone edits a row, the chain breaks right there.
> Tampering shows up."

### Part 10: Results (7:40 to 8:35)

**[7:40] Click "Insights" → "The field-mapping model, against a baseline".**

> "So how good is the AI, really?
>
> On formats it has never seen before, it maps **99.1 percent** of the fields correctly.
> Plain name matching gets **35.8 percent**."

**[7:55] Show the README Results table.**

> "And every number here is measured. Each one has its evidence file in the repo.
>
> A hundred thousand events a second, sustained.
> Under 400 milliseconds to the dashboard, even at the 99th percentile.
> Zero events lost, out of more than ten million.
> A ten-minute soak test: 31 million events, no restarts.
> Over 2,700 automated tests, 93 percent coverage, all green in CI.
> And the security scans pass, including OWASP ZAP against the running API."

**[8:20] Show the architecture diagram (`docs/diagrams/c4_containers.png`).**

> "And quickly, under the hood.
>
> MQTT from the cars. Kafka as the queue. Python services doing the translation.
> Redis, PostgreSQL and Parquet for storage. A React dashboard.
> Prometheus and Grafana for monitoring.
>
> It all runs in Docker, and the Kubernetes and AWS files are in the repo."

### Part 11: Close (8:35 to 9:00)

**[8:35] Back to the landing page, or a closing slide with your name.**

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

## If something goes wrong while recording

| Problem | What to do |
|---|---|
| The model run is slow or rate limited | Say: "on a free key it falls back to the deterministic engine, and records why". That *is* a feature. Or switch to Deterministic and run again. |
| Helix already has a mapping | You didn't reset. Stop the app, `mv data data.old2`, `make run`, wait 2 minutes. |
| Charts look empty | The app was just started. Wait 2 minutes before recording. |
| Running long | Cut Part 7's second agent run (see the note in Part 7), then shorten Part 9. Never cut Parts 4 to 6: they are the core of the idea. |

## Numbers you can quote (and where they come from)

| Claim | Evidence file |
|---|---|
| 100,572 sent/s, 99,364 translated/s; p50 129 ms, p95 258 ms, p99 379 ms; 0 of 10,140,200 lost | `docs/evidence/bench_steady.json` |
| Burst: 1.4 M backlog drained 25 s after, 0 lost | `docs/evidence/bench_burst.json` |
| Soak: 30.9 M events in 10 min, 0 restarts | `docs/evidence/bench_soak.json` |
| 4 processes SIGKILLed, 0 lost, 0 stored twice | `docs/evidence/chaos.json` |
| 99.1% vs 35.8% field mapping | `docs/evidence/ml_field_mapper.json` |
| 2,768 tests, 93.5% coverage | `docs/evidence/coverage.json` |

If you change any code before recording, regenerate these first, because the
numbers in the README are typed in by hand and can go stale.
