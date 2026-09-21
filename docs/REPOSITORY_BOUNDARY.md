# Repository boundary

## LivingRuntime Remote

This repository owns the reusable remote foundation:

- MCP plugin/runtime
- bounded gateway
- public relay
- OAuth and pairing
- Connector installers
- cross-platform release artifacts
- Remote-specific tests and documentation

## pi-remote

pi-remote is a separate private product. It may consume LivingRuntime Remote as infrastructure, but LivingRuntime Remote must not depend on pi-remote code, release assets, private APIs, or repository visibility.

Dependency direction:

LivingRuntime Remote <- pi-remote

not the reverse.

## Release rule

All public LivingRuntime Remote artifacts must be buildable from this repository alone.
