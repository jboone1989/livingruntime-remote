# Contributing

Thanks for helping improve LivingRuntime Remote.

The project is intentionally small and security-sensitive. A change that adds power to a remote agent should also explain the boundary that keeps that power bounded.

## Good first contributions

Useful contributions include:

- connector portability and installation fixes;
- documentation and examples;
- reproducible bug reports;
- tests for path, host, approval, OAuth, pairing, or credential boundaries;
- observability improvements that do not expose secret material;
- integrations that preserve the existing capability model;
- reliability fixes for durable jobs, receipts, watchers, and recovery.

## Before opening a pull request

For ordinary fixes:

1. reproduce the problem;
2. keep the change scoped;
3. add or update tests;
4. run the relevant test suite;
5. describe observable behavior before and after the change.

For a new remote capability or a security-boundary change, also include:

- what new authority is introduced;
- which actor receives it;
- how scope is represented;
- how the action is audited;
- how the permission is revoked;
- what must fail closed;
- whether any secret value can enter model-visible context.

## Design principles

Please preserve these defaults:

- bounded capability over unrestricted shell;
- explicit host/project routing over implicit fallback;
- durable state over chat-memory-only state;
- terminal receipts over inferred success;
- approval before new authority;
- credential references/leases over raw secret values;
- visible blocked/failure state over pretending progress;
- user-owned compute without requiring whole-repository upload to a hosted IDE.

## Tests

Install development dependencies as documented under `plugin/`, then run the plugin test suite:

```bash
python -m unittest discover -s plugin/tests -v
```

Run the public manifest self-check:

```bash
python plugin/scripts/selfcheck.py
```

If your change affects the relay or gateway, run the corresponding tests in those directories as well.

## Pull requests

A useful PR description states:

- the user-visible problem;
- the smallest behavioral change that solves it;
- tests run;
- security/boundary impact;
- any migration or deployment requirement.

Avoid combining unrelated refactors with a capability change.

## Vulnerabilities

Please do not report security vulnerabilities in public issues. See [SECURITY.md](SECURITY.md).
