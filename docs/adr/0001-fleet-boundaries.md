# ADR 0001: Keep fleet lifecycle in AGX and expose one operator workflow

Status: accepted

## Decision

Keep `multica-deployment-tool`, `agx`, and Multica in separate repositories, but expose one operator workflow from `multica-deployment-tool`:

```text
fleet plan → fleet apply → fleet verify
```

The deployment tool coordinates infrastructure and invokes versioned AGX and Multica CLI contracts. AGX remains the authority for fleet lifecycle. Multica remains a task board and task transport. The AGX–Multica connector is an explicit, versioned adapter and is not implemented through private Multica HTTP or database access.

The first contract is intentionally one-node and end-to-end. Multi-node scheduling, GitHub automation, and automatic rollback are extensions after the happy path is proven.

## Why

- Merging repositories would make AGX lifecycle changes depend on the Multica server release cycle.
- Letting Multica directly control nodes would duplicate AGX's fleet authority and leak deployment credentials into the task system.
- Leaving the relationship to ad-hoc shell commands creates the current operational confusion and makes a repeatable installation impossible.

## Consequences

- The operator gets one installation entry point while the repositories remain replaceable.
- The deployment tool needs a secret-free deployment contract and idempotent `plan/apply/verify` phases.
- AGX must eventually expose a structured connector surface for receiving a Multica task and returning a deployment result.
- A local Multica fork is allowed and is built/deployed by the deployment tool, but the connector still targets its versioned official CLI contract rather than fork internals.
