# Demo video script (about 9 minutes)

Target length 9:00, which leaves a minute of slack under the 10-minute limit.
Speak slowly: the spoken lines below come to about 1,150 words, roughly 130 a minute.

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
3. Open two browser windows on http://127.0.0.1:8765:
   - Window A: signed in as `engineer@rosetta.example`
   - Window B (private window): signed in as `analyst@rosetta.example`
   - Password for both: `rosetta-demo-2026`
4. Have these ready in tabs: `docs/diagrams/architecture.png` (or the architecture
   section of the solution PDF), `docs/diagrams/bench_burst.png`, the README "Results" table.
5. Record at 1080p, close notifications, zoom the browser to 110% so text is readable.

## The script

Format: **[time] Screen → what to click.** Then *what to say*.

---

### Part 1: The problem (0:00 to 0:45)

**[0:00] Landing page, top.**

> "Hi, I'm Bhanu Prakash Kusha, and this is Rosetta.
> Connected cars send data all the time: speed, location, battery, fault codes.
> The problem is that every car maker sends it in its own format."

**[0:15] Scroll to "the same moment, written six ways".**

> "Here is one moment: a car braking hard at 64 kilometres an hour.
> Six makers describe it six different ways. Different field names, different units,
> one even in binary Protobuf, one with German field names.
> Today, every new maker, or every firmware update that renames a field, means
> writing new code, a release, and losing data while it ships."

### Part 2: Our solution in one line (0:45 to 1:15)

**[0:45] Scroll to "one event, whatever the source", then to Park, Study, Prove, Approve, Replay.**

> "Rosetta translates every format into one standard event.
> And when a maker it has never seen starts sending, it doesn't crash or drop data.
> It **parks** the messages, an AI agent **studies** them, **proves** its mapping on
> known answers, a human **approves** it, and the parked messages are **replayed**.
> No new code, no restart, no lost data. Let me show you live."

### Part 3: The live pipeline (1:15 to 2:00)

**[1:15] Click "Sign in" (engineer) → Live page.**

> "This is a live system on one laptop. 100,000 simulated vehicles, about
> 100,000 events every second, from five makers."

**Point at "Ingest to dashboard".**

> "This is the time from a message arriving to it showing on the dashboard.
> The target was under 2 seconds. We're at around a quarter of a second."

**Point at "Duplicates dropped" and "Throughput by source".**

> "We inject problems on purpose: duplicate messages, messages out of order,
> damaged messages. Duplicates are caught and dropped, so nothing is counted twice."

### Part 4: A new car maker appears (2:00 to 2:45)

**[2:00] Under "Make something happen", click "A new maker starts sending".**

> "Now a new maker, Helix Mobility, starts sending data. Rosetta has never seen its format."

**Wait until the "What needs attention" card shows Helix, "parked, no mapping".**

> "Look: nothing is dropped. Every unreadable message is parked, with a reason:
> no adapter."

**[2:20] Click "Dead letters" in the menu. Open the Helix group and a sample payload.**

> "These are the parked messages, grouped by format. Here's one.
> German field names like `fahrt.v`, and nothing here knows what any of them mean yet."

### Part 5: The AI agent learns the format (2:45 to 4:30)

This is the most important part. Take your time.

**[2:45] Click "Ask the agent for a mapping" (or Mapping studio → Helix).**

> "This is the Mapping Studio. Here's the clever part."

**Point at the engine toggle: Auto / Deterministic / Model.**

> "The agent has a fixed set of seven tools. There are two ways to drive them.
> **Deterministic** runs them in a fixed order, no AI, same result every time.
> **Model** lets a language model, like Gemini or Claude, decide which tool to use next
> and explain its reasoning. Auto uses a model when an API key is set."

**Select your engine, click "Run the agent". While it runs:**

> "It can't write code, and it can't approve its own work. It can only propose."

**[3:20] When it finishes, walk down the steps. Open "Profile every field".**

> "Step one, collect the parked messages. Step two, profile every field.
> And here's my favourite part. It uses **physics**.
> It works out latitude and longitude from how the car moves.
> And it notices `fahrt.v` is always 0.278 times the GPS speed.
> 0.278 is the conversion from kilometres an hour to metres per second.
> So this is speed, in metres per second, whatever the field is called."

**[3:50] Point at "Propose a mapping".**

> "Then a machine-learning classifier proposes a match for every field,
> and the Hungarian algorithm makes sure no two fields claim the same slot."

**[4:00] Point at "Test on known answers" and "Submit for review".**

> "Then it tests itself on a golden set: messages where the right answer is already known.
> The trick: half of those answers are hidden from the agent. So it can't just memorise them.
> It passes all of them, including the half it never saw.
> And every single step, input and output, is recorded in the audit log."

### Part 6: A human approves, zero downtime (4:30 to 5:15)

**[4:30] Scroll down to the draft version, click "Try on parked traffic".**

> "Before anyone approves, it can be dry-run on the real parked messages.
> Nothing is written. It just shows what would happen."

**[4:45] Click "Approve", type a reason, confirm.**

> "Only a person can approve. If the agent tries, the registry itself refuses."

**[4:55] Go to Live.**

> "Within a second, the workers load the new mapping between two batches.
> Helix appears in the chart, the parked messages are replayed, and the dead-letter
> count falls. No process restarted. That's zero downtime onboarding."

### Part 7: Firmware update and safe rollout (5:15 to 6:00)

**[5:15] Live → click "Firmware update".**

> "Now a harder case. 35 percent of Pacifica cars get a firmware update that renames
> fields and switches units. Only some of the fleet changes."

**Click "Review the new format", run the agent again, approve. Open the Pacifica Sources page.**

> "The agent proposes version 2. When I approve it, it doesn't go to everyone.
> It's released as a **canary**, to a share of vehicles, while version 1 keeps serving the rest.
> If it's good, I promote it. If not, one click rolls it back."

*(If you are short on time, skip running the agent here: start the scenario, show the
drift appear on Live, and just explain the canary with the Sources page.)*

### Part 8: It survives failures (6:00 to 6:45)

**[6:00] Live → "Processes" card. Hover a normaliser, kill it.**

> "What if a server crashes? I'll kill a worker, the way a machine dies.
> The queue grows. The supervisor restarts it from its last checkpoint. The queue drains."

**[6:20] Show `bench_burst.png`.**

> "We tested this properly. We killed four processes under full load: zero events lost,
> zero stored twice. And in a traffic burst, the backlog grew to 1.4 million messages
> and drained in 25 seconds, with nothing lost."

### Part 9: Fleet, privacy and audit (6:45 to 7:40)

**[6:45] Click "Fleet". Click a vehicle with an alert.**

> "This is what a fleet operator sees: every vehicle on the map, in one format,
> whatever its maker. Alerts like harsh braking, low battery and new fault codes are
> raised automatically."

**[7:00] Switch to Window B (analyst) → Fleet.**

> "Same page, as an analyst. Locations are blurred to about 5 kilometres,
> and VINs are shortened. Different roles see different data."

**[7:15] Window A → "Compliance". Filter the entries to Agent.**

> "Every read of fleet data and every agent action is logged here, in a hash chain:
> each entry carries the fingerprint of the one before it. If anyone edits a row,
> the chain breaks at that row, so tampering shows."

### Part 10: Results (7:40 to 8:35)

**[7:40] Click "Insights" → "The field-mapping model, against a baseline".**

> "How good is the AI? On formats with names it has never seen, the model maps
> **99.1 percent** of fields correctly. Simple name matching gets **35.8 percent**."

**[7:55] Show the README Results table.**

> "And the numbers, all measured, each with its evidence file in the repo:
> - **100,000 events a second**, sustained.
> - **Under 400 milliseconds** to the dashboard, even at the 99th percentile.
> - **Zero events lost** out of over 10 million.
> - A 10-minute soak test: 31 million events, no restarts.
> - Over **2,700 automated tests**, 93 percent code coverage, all green in CI.
> - Security scans, including OWASP ZAP against the running API, pass."

**[8:20] Show the architecture diagram.**

> "Under the hood: MQTT for the cars, Kafka as the message queue, Python services
> for translation, Redis, PostgreSQL and Parquet for storage, a React dashboard,
> and Prometheus and Grafana for monitoring. It runs in Docker, with
> Kubernetes and AWS deployment files."

### Part 11: Close (8:35 to 9:00)

**[8:35] Back to the landing page, or a closing slide with your name.**

> "To be honest about limits: everything here is simulated data on one laptop,
> and real car feeds would be messier. That's exactly why every mapping has to pass
> the golden set and a human.
>
> So that's Rosetta: one event from every car maker, and a new maker joins in minutes,
> without code, without downtime and without losing data. Thank you."

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
| 2,764 tests, 93.4% coverage | `docs/evidence/coverage.json` |

If you change any code before recording, regenerate these first, because the
numbers in the README are typed in by hand and can go stale.
