# Demo video script (5 minutes)

Record at 1080p. Start `make run` a minute before recording so the charts have
history. Sign in as `engineer@rosetta.example`. Keep a second browser window
signed in as `analyst@rosetta.example` for the masking shot.

| Time | Screen | Say (short, plain) |
|---|---|---|
| 0:00 | Landing page, "the same moment, written six ways" | "One vehicle brakes hard at 64 km/h. Six car makers describe that moment in six different formats and units. Every new maker, and every firmware update that renames a field, normally means new code, a release, and messages lost while it ships." |
| 0:30 | Scroll to "one event, whatever the source" | "Rosetta translates every dialect into one canonical event, and a new maker joins without a restart or lost data." |
| 1:00 | Live page | "100,000 simulated vehicles, about 100,000 events a second, five makers. Duplicates, reordering and damaged messages are injected on purpose." Point at the p95 latency card. |
| 1:20 | Click "A new maker starts sending" | "Helix Mobility comes online. The platform has never seen its format." Wait until the Right-now card says "parked, no mapping". "Nothing is dropped. The messages are parked with a reason." |
| 1:45 | Dead letters page | "Thousands of messages, grouped into one format family." Open a group, show a sample payload in German field names. |
| 2:05 | Mapping studio, Helix, Run the agent | "The agent samples what could not be read, profiles each field, and checks it against physics." Open "Profile every field": "`fahrt.v` is always 0.278 times the GPS speed. That is metres per second, whatever it is called." |
| 2:30 | Scroll to the field map | "14 fields, each with confidence and a reason. It passes 200 of 200 known answers, and half of those it never saw." |
| 2:40 | Click "Try on parked traffic" | "A dry run on the real parked messages. Nothing is written." |
| 2:45 | Approve, reason, confirm | "Only a person can do this. The agent is refused by the registry itself." |
| 2:50 | Live page | "Within a second the workers load it between two batches. Helix appears. The parked messages are replayed. No process restarted." |
| 3:00 | Click "Firmware update" | "35% of Pacifica vehicles now send a new format. The agent proposes version 2; I release it to 25% of vehicles; version 1 keeps serving the rest." Show the source page with v1 live and v2 canary. |
| 3:40 | Live page, Processes, kill normaliser-0 | "Kill a worker, the way a machine dies. The queue grows, the supervisor restarts it from its checkpoint, the queue drains. The chaos test proves nothing is lost or stored twice." |
| 4:00 | Burst chart from the document or `docs/diagrams/bench_burst.png` | "A burst: the backlog grows to 1.4 million and drains, nothing lost." |
| 4:15 | Compliance, audit trail | "Every read and every agent action, in a hash chain. Edit a row and the chain breaks at that row." Filter to Agent. |
| 4:25 | Analyst window, Fleet | "An analyst sees cells of about 5 km, not positions." |
| 4:35 | README results table | "100,000 events a second, p99 under half a second, 99.1% against 35.8% for the baseline, 2,700 tests." |
| 4:50 | Team slide | Names and roles. |

Keep the video under 5:00: rehearse once with a timer.
