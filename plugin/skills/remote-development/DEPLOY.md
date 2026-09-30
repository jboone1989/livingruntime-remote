# Deploy workflow

Deploy only after the relevant focused tests pass and the intended diff has been inspected. Do not run an unrelated full regression suite by default; expand testing only when the changed surface or failure evidence requires it.

Preserve unrelated changes. Commit only the intended working-tree state, integrate onto `main` as appropriate for the repository, and push without force.

For an allowlisted systemd-backed project, restart the service only after the pushed commit is ready. Verify service state and recent logs after restart. For relay/runtime deployments, verify the deployed health/version endpoint or another authoritative production receipt rather than assuming a successful copy means production is healthy.

Treat deployment failures as first-class failures. Surface and repair the broken deployment path instead of silently falling back to an older build.

Report the pushed commit SHA and production verification evidence.
