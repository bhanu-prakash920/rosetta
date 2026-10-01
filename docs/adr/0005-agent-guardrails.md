# ADR 0005: The agent proposes, people decide

Status: accepted

## Context

The problem statement asks for an agent that answers or acts on fleet data, with
guardrails and an audit trail. Our agent studies unreadable messages and proposes
a mapping. A wrong mapping is dangerous in a quiet way: speed in mph read as km/h
produces plausible numbers that are 38 percent too low.

## Decision

- **Two engines, one toolbox.** A deterministic workflow (always available, fully
  reproducible) and a model-driven tool-use loop (when credentials exist). Both call
  the same seven tools in `rosetta/agent/toolbox.py`, so the model has no power the
  workflow lacks.
- **What the agent may do:** sample dead letters, profile fields, search the
  mapping memory, ask the classifier for a proposal, learn event codes from
  labelled examples, test a candidate on the development half of the golden set,
  and submit one draft per run.
- **What it cannot do:** write transforms (it picks encodings from an
  enumeration), see the hold-out half of the golden set, or change what is live.
  Approve, promote, retire and reject are refused for non-human actors by the
  registry itself, and the refusal is audited. Only `system` may roll back, which
  is the canary guard.
- **Evidence before trust.** A draft becomes `validated` only when it reproduces
  at least 99 percent of the golden cases, including the half it never saw.
- **Untrusted input.** Field names and values come from vehicles. They reach the
  model only inside tool results, as JSON. Whatever the model concludes still has
  to pass the golden set and a person. A test sends an agent that "obeys" injected
  text and tries tools it does not have: the calls fail, are recorded, and nothing
  live changes.
- **Record.** Every tool call is stored with input, output and duration, and
  written to the hash-chained audit log.
- **Provider is a detail.** Anthropic and Google are both wired
  (`rosetta/agent/llm_agent.py`), chosen by whichever key is present and overridable
  with `ROSETTA_LLM_PROVIDER`. Each gets the same system prompt, the same strict tool
  schemas and no forced tool choice, and returns the same result shape, so nothing
  downstream knows which ran. Any refusal or API error falls back to the deterministic
  workflow with the reason recorded on the run. Adding a provider means one adapter,
  not a change to the tools.

## Consequences

- The agent is useful without a model, and better with one where statistics run out.
- The model-driven loop is tested against a stub client per provider, each checking
  that provider's request shape. Neither has been run against a live API in this
  repository's evidence.
