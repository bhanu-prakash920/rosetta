# ADR 0001: Mappings are data, compiled at run time, never code

Status: accepted

## Context

Every car maker sends telemetry in its own format. The usual way to support a new
one is an adapter class per maker: write code, review it, release it, restart the
consumers. That makes every new maker, and every firmware update that renames a
field, a software release, with a window where messages cannot be read.

We also want an AI agent to propose adapters. An agent that writes code which then
runs inside the pipeline is a remote-code-execution path with a language model in
the middle.

## Options considered

1. **Adapter class per maker.** Simple, fast, familiar. A release per change. The
   agent could only open a pull request.
2. **Adapter as a script (Python, Lua, JSONata) stored in a database.** No release,
   but the script can do anything the interpreter allows. Sandboxing is hard to get
   right and hard to prove.
3. **Mapping as declarative data**: a wire decoder, and for each canonical field a
   source path and a short list of transforms chosen from a fixed whitelist
   (`unit`, `scale`, `enum`, `iso8601`, `dtc_extract` and ten more).

## Decision

Option 3. A mapping spec is JSON. `rosetta/engine/compiler.py` validates it and
builds closures from the whitelist in `rosetta/domain/transforms.py`. For speed,
`rosetta/engine/codegen.py` then specialises the spec into one straight-line Python
function. Nothing from the spec is pasted into that source as text: numbers are
checked to be finite and embedded with `repr`, strings are embedded with `repr`,
and every non-trivial transform stays a call to the whitelisted closure. A
differential test runs both paths on every dialect, on corrupted payloads and on
odd inputs, and requires identical results.

## Consequences

- A new maker or a changed format is a registry change, not a release. Workers
  reload the routing table between two batches (ADR 0004).
- The agent picks encodings from an enumeration. It cannot express anything
  outside the whitelist, so what it proposes cannot run arbitrary code.
- Some formats will need a transform we do not have yet. Adding one is a code
  change, reviewed once, and then available to every mapping.
- The specialised function made the normaliser about 1.6 times faster than the
  interpreter (measured: 64,700 to 103,000 events per second on one core for a
  flat JSON dialect). The interpreter stays as the reference and the fallback.
