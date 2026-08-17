# Fleet deployment v1 specification

Status: draft pending issue-tracker publication

## Problem Statement

The operator currently has to understand three separate systems to deploy a working fleet: the Multica self-hosted server, AGX node installations, and the local Multica CLI/daemon that connects a node to a workspace. Each system can appear healthy while the end-to-end path is broken. A web page can load while no runtime is online; AGX can be configured while no task can reach it; a task can be assigned while the deployment command and its credentials are unavailable on the target node.

The repositories are intentionally decoupled, but their deployment relationship is currently implicit. Operators need one repeatable workflow that proves the complete path without merging the repositories, copying secrets between machines, or making Multica the authority for AGX fleet state.

## Solution

Add one operator workflow to the deployment tool:

```text
fleet plan → fleet apply → fleet verify
```

The workflow consumes a versioned, secret-free deployment contract. It deploys or upgrades the selected Multica server, installs and initializes the selected AGX Bundle on one or more nodes, connects the official Multica CLI/daemon to the declared profile and workspace, and verifies a disposable task through the AGX–Multica connector.

AGX remains the authority for node identity, fleet state, installed Bundle, receipts, health, upgrade, rollback, and diagnostics. Multica remains the task board and task transport. The deployment tool coordinates these systems but does not reimplement AGX lifecycle behavior. A local Multica fork is supported as a pinned build input; the connector targets the versioned official CLI contract, not fork-private HTTP or database interfaces.

## User Stories

1. As a fleet operator, I want one command family for the server, node, and verification workflow, so that I do not have to remember which repository owns each installation step.
2. As a fleet operator, I want a read-only plan before mutation, so that I can see the versions, nodes, workspace, and actions that will be used.
3. As a fleet operator, I want the plan to fail before mutation when a required version, URL, node selector, or local tool is invalid, so that a partial deployment is not caused by a typo.
4. As a fleet operator, I want to deploy our pinned Multica fork from a source revision, so that the task and runtime behavior is reproducible and not tied to a mutable `latest` image.
5. As a fleet operator, I want to use a prebuilt Multica image when a source build is unnecessary, so that routine upgrades remain fast.
6. As a fleet operator, I want the deployment tool to preserve the Multica database and application secrets during upgrades, so that an image change does not force a new login or destroy task history.
7. As a fleet operator, I want the deployment tool to install a pinned AGX Bundle on a selected node, so that every node has a known fleet capability set.
8. As a fleet operator, I want AGX initialization to remain idempotent, so that retrying an interrupted installation does not create duplicate repositories or overwrite user-owned resources.
9. As a fleet operator, I want a node to be connected to an explicit Multica profile and workspace, so that it cannot silently execute work for the wrong environment.
10. As a fleet operator, I want device and runtime names to be stable and configurable, so that the Multica board can distinguish nodes without using hostnames as an accidental identity system.
11. As a fleet operator, I want the workflow to verify both Multica authentication and daemon status, so that a reachable web page is not mistaken for an online runtime.
12. As a fleet operator, I want the workflow to verify that AGX reports the expected installation and Bundle, so that a connected but stale node is not accepted.
13. As a fleet operator, I want a disposable task to exercise the complete Multica-to-AGX path, so that the first successful deployment proves real task delivery rather than configuration presence.
14. As a fleet operator, I want the disposable task result to include a deployment ID, node identity, action, health result, and redacted summary, so that I can diagnose which boundary failed.
15. As a fleet operator, I want verification to refuse the `verified` state when external evidence is missing, so that documentation, mocks, or partial local state cannot create a false success.
16. As a fleet operator, I want a failed apply to identify the last completed phase and the safe retry command, so that recovery does not require reconstructing the original conversation.
17. As a fleet operator, I want a repeated apply of the same contract to be a no-op where state already matches, so that routine maintenance does not cause unnecessary restarts.
18. As a fleet operator, I want node credentials, model credentials, SSH keys, and database secrets to remain on their owning machine, so that the task board cannot become a credential distribution system.
19. As a fleet operator, I want Multica task summaries to be redacted, so that logs and results do not expose private node state or secrets.
20. As a fleet operator, I want the workflow to support a LAN or private overlay URL, so that the first end-to-end test does not require a public GitHub webhook.
21. As a fleet operator, I want GitHub App integration to be an optional later phase, so that repository event delivery does not block proving the local task path.
22. As a fleet operator, I want the same contract to describe more than one node after the first node works, so that fleet expansion is data entry rather than a new deployment design.
23. As an AGX maintainer, I want the connector to use the official Multica CLI and structured output, so that AGX is not coupled to Multica server internals.
24. As an AGX maintainer, I want connector failures to preserve AGX's own lifecycle state, so that a temporary Multica outage does not corrupt node installation receipts.
25. As a Multica maintainer, I want the deployment tool to own image construction and deployment, so that server changes can be released without importing AGX's Go implementation.
26. As a Multica maintainer, I want the connector contract to be versioned, so that a Multica fork change can be tested and rejected before it reaches a fleet node.
27. As a project maintainer, I want a task to identify repository, revision, environment, action, and target selector, so that an AGX deployment is reproducible and reviewable.
28. As a project maintainer, I want the result to link the Multica task to the AGX deployment receipt, so that human review can follow the operation across both systems.
29. As a security reviewer, I want the contract validator to reject secret-shaped fields, so that operators do not accidentally commit credentials to the deployment repository.
30. As a release maintainer, I want the distribution package to contain the contract and boundary decisions, so that a new operator receives the same operating model as the code.

## Implementation Decisions

- The user-facing seam is the `fleet plan/apply/verify` command family. Internal Multica, AGX, and connector operations are adapters behind this seam.
- The deployment contract is versioned and secret-free. It contains the Multica server/profile/workspace, the Multica image or source revision, the AGX Bundle version, node descriptors, and project/environment selectors.
- The first supported contract contains one node and one disposable project task. The schema may accept a node list, but multi-node scheduling is not required for v1 verification.
- `plan` is read-only. It validates contract shape, URL origins, version identifiers, node selectors, local prerequisites, and compatibility between the selected Multica/AGX connector versions.
- `apply` is ordered and idempotent: deploy or upgrade Multica, install the AGX Bundle, initialize AGX, connect the Multica CLI/daemon, then run connector preflight.
- The deployment tool may build a local Multica fork through the existing source-build path. The fork must be identified by an immutable source revision or immutable image tag; mutable `latest` references are rejected for a verified deployment.
- AGX remains responsible for its own installation receipts, ownership checks, Bundle integrity, lifecycle status, upgrade, rollback, and uninstall semantics.
- Multica remains responsible for workspaces, Issues, task assignment, task transport, human-visible progress, and redacted execution summaries.
- The connector is a versioned adapter that invokes the official Multica CLI with structured arguments, captures exit codes and JSON output, validates the expected schema, applies timeouts, and redacts sensitive fields.
- The connector does not call Multica private HTTP endpoints, query its database, parse human-readable CLI output, or write fake task/runtime records.
- Node credentials stay on the node. The deployment contract and Multica task contain references and selectors, not SSH keys, API keys, database passwords, or model credentials.
- `verify` may use a disposable task and a disposable project/environment. It must collect evidence from both AGX and Multica before reporting `verified`.
- Verification states are distinct: `planned`, `applying`, `configured`, `verifying`, `verified`, `failed`, and `blocked`. `configured` means local installation state only; it is not end-to-end success.
- A Multica server being healthy is necessary but insufficient. A node being AGX-configured is necessary but insufficient. A runtime being online is necessary but insufficient until a task reaches AGX and returns a structured result.
- GitHub App, public HTTPS callbacks, multi-node scheduling, automatic rollback, and rich UI changes are follow-up capabilities. They must not be required to pass the first local end-to-end path.
- The distribution package includes the glossary, ADR, and contract so that the operator workflow and repository boundaries travel with the executable tooling.

## Testing Decisions

- The primary test seam is the external `fleet` command behavior. Tests should assert plan/apply/verify outputs, exit codes, phase ordering, redaction, and state transitions, not private helper implementation.
- Plan tests use malformed and valid secret-free contracts and assert that invalid contracts produce no mutation calls.
- Apply tests use fake Multica, AGX, and connector adapters to assert ordering, idempotent retry behavior, failure phase reporting, and preservation of credentials.
- Verify tests require evidence from both systems and assert that missing health, missing runtime, connector timeout, malformed JSON, or task failure cannot produce `verified`.
- Contract tests validate version compatibility, immutable Multica source identity, node selector behavior, and rejection of secret-shaped fields.
- Integration tests use a disposable Multica workspace, one online runtime, one disposable project task, and a test AGX installation. They capture redacted evidence, not credentials.
- The existing deployment tool tests remain the prior art for configuration validation, Compose contract checks, secret exclusion, and source-build behavior.
- The existing AGX contract and CLI tests remain the prior art for Bundle integrity, idempotence, ownership-safe cleanup, structured output, and lifecycle receipts.
- A real integration run must verify both sides: the Multica task/runtime evidence and the AGX deployment receipt. A mock or documentation-only result cannot close the integration gate.

## Out of Scope

- Merging the deployment tool, AGX, and Multica source repositories.
- Making Multica the source of truth for fleet lifecycle or allowing Multica backend to SSH into nodes.
- Implementing a new Multica private API solely for the connector.
- Automatic installation of arbitrary project dependencies or model credentials on every node.
- Full multi-node scheduling and placement policy before the one-node path is verified.
- GitHub App setup, public webhook exposure, and PR automation as prerequisites for the first local task.
- Automatic rollback based only on a Multica task status without AGX-owned deployment evidence.
- A new web UI for fleet lifecycle in the first version.
- Claiming `verified` from local Bundle presence, a running daemon, a passing mock, or a healthy `/health` endpoint alone.

## Further Notes

The first implementation should be run against one known NAS, one disposable workspace, one AGX node, and one disposable task. Once that path passes, the same contract can be extended with additional nodes and environments without changing ownership boundaries.

The repository issue workflow is active. Portable deployment and the later desktop pairing phase are tracked in issue #3, which retains the `ready-for-agent` label.
