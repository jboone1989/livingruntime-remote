# Migration provenance

LivingRuntime Remote was split into this standalone repository from the private pi-remote development repository.

Initial standalone snapshot:

- source repository: pi-remote
- source branch: main
- source commit: 4b0eb51
- product version: 0.4.4
- snapshot date: 2026-09-21

The standalone repository intentionally keeps only the reusable Remote foundation:

- plugin/runtime
- Connector and installers
- public relay
- bounded MCP gateway
- Remote tests and release workflows

No pi-remote autonomous-session, planner, checkpoint, workflow, or distributed-worker code is included.
