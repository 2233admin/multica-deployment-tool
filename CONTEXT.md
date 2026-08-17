# Fleet deployment context

## Canonical terms

- **Fleet**: the set of execution nodes whose installation and lifecycle are owned by AGX.
- **Node**: one machine with an AGX installation and the local tools it is allowed to run.
- **AGX**: the fleet authority. It owns node identity, installed Bundle, capabilities, desired state, receipts, health, upgrade, rollback, and diagnostics.
- **Multica**: the task board and task transport. It owns workspaces, Issues, task assignment, human-visible progress, and execution summaries; it is not the source of truth for fleet lifecycle.
- **Deployment tool**: the operator-facing installer and reconciler. It deploys the Multica server, bootstraps nodes, and verifies the cross-repository path; it does not reimplement AGX lifecycle logic.
- **Connector**: the versioned boundary between AGX and Multica. It uses the official Multica CLI and structured output, never Multica private HTTP or database tables.
- **Deployment contract**: the versioned, secret-free desired-state document used by the deployment tool to coordinate Multica and AGX versions, nodes, workspaces, and environments.

## Source of truth

AGX is authoritative for fleet state. Multica is authoritative for task-board state. GitHub/Gitea is authoritative for source and review state. The deployment contract is authoritative only for the operator's desired installation state; it must not overwrite runtime receipts or task history.

## Non-goals

- Multica does not SSH into nodes or execute arbitrary deployment scripts.
- AGX does not own the Multica server database or daily task-board UX.
- The deployment tool does not copy credentials between nodes or manufacture Multica records.
