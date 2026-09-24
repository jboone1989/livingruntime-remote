# Launch kit

This file is a lightweight distribution kit for the first public validation of LivingRuntime Remote.

The goal is not broad marketing. The goal is to make the product understandable enough that developers who already have the problem can discover and try it.

## One-line positioning

**LivingRuntime Remote gives AI agents durable, bounded execution on machines you control, so long-running work can continue after the initiating chat turn ends.**

## Short positioning

Most AI coding assistants are good at issuing a command but still depend on a live conversation to babysit long-running work. LivingRuntime Remote separates the task lifetime from the chat lifetime: jobs persist, checkpoints survive, watchers observe completion, and continuation-capable controllers can resume from the result.

It runs against your own machines through a paired Connector and bounded SSH-backed tools rather than requiring the whole repository to move into a hosted IDE VM.

## The story to lead with

Do not lead with "MCP over SSH."

Lead with:

> **I stopped having to type "continue."**

Then show the real sequence:

```text
ask for a bug fix
-> remote test runs for a long time
-> chat turn is no longer waiting
-> job finishes
-> watcher observes the result
-> controller continues
-> acceptance test passes
```

The underlying MCP/OAuth/SSH architecture matters after the reader understands why the product exists.

## Demo shot list

A 30-60 second clip is enough.

1. Show the initial request in ChatGPT.
2. Show a durable job ID/receipt.
3. Cut to the remote task running on the user's machine.
4. Show that no manual "continue" message is sent.
5. Show the watcher/completion event returning to the same workflow.
6. Show the next action starting automatically.
7. End on passing tests or another concrete acceptance signal.

Avoid long terminal recordings, architecture slides, or feature tours in the first demo.

## X launch copy

### Version A — product story

I built LivingRuntime Remote because I was tired of babysitting AI coding tasks and typing "continue."

Now a long-running task can keep running on my own server after the chat turn ends, persist its state, and notify a continuation-capable controller when it finishes.

The useful loop is:

execute -> wait -> observe completion -> continue

It is open source (MIT), uses bounded MCP/SSH-backed tools, supports multiple hosts and approval-gated commands, and keeps SSH credentials on the user's Connector machine.

Repo: https://github.com/jboone1989/livingruntime-remote

### Version B — concise

I wanted ChatGPT to keep working on my own machines without me coming back every 20 minutes to type "continue."

So I built LivingRuntime Remote: durable remote jobs + checkpoints + watchers + bounded approvals over MCP.

Open source (MIT):
https://github.com/jboone1989/livingruntime-remote

## Show HN

### Title

```text
Show HN: LivingRuntime Remote – durable AI-agent execution on machines you control
```

### Body

```text
I built LivingRuntime Remote after repeatedly running into the same problem:
an AI agent could start real work on my server, but long-running tasks still
needed me to come back, check status, and type "continue."

LivingRuntime Remote separates the execution lifetime from the chat lifetime.

A controller can start a durable job on a machine I control, persist checkpoints,
observe terminal state through a watcher instead of polling, and continue from the
result. Remote actions are bounded: configured workspace roots, multi-host routing,
dynamic command approvals, audit records, and credential handles instead of raw
secrets in model context.

It is not a hosted cloud IDE. The Connector uses the SSH setup already present on
the user's machine.

The repo is MIT licensed:
https://github.com/jboone1989/livingruntime-remote

I am especially interested in feedback from people running coding agents against
VPSes, homelabs, NAS boxes, or multi-host development environments.
```

## Reddit / self-hosted angle

Lead with the ownership boundary:

```text
I wanted an AI agent to work on my own VPS/homelab without uploading the whole
repo into a vendor-controlled VM, and without giving it an unrestricted shell.

LivingRuntime Remote pairs a local Connector with bounded SSH-backed MCP tools.
It now also has durable jobs/watchers so long-running work can finish after the
initiating conversation turn and be resumed from a checkpoint.

MIT licensed:
https://github.com/jboone1989/livingruntime-remote
```

## GitHub metadata

Recommended repository metadata:

**Description**

```text
Durable remote execution for AI agents on machines you control — MCP, approvals, multi-host, long-running jobs and continuation.
```

**Homepage**

```text
https://remote.livingruntime.com/
```

**Topics**

```text
mcp
chatgpt
codex
ai-agents
agent-runtime
remote-development
self-hosted
ssh
long-running-agents
developer-tools
```

## What to measure

The first validation does not need revenue metrics.

Track the funnel in this order:

1. repository visits;
2. stars / watches / forks;
3. install-page visits;
4. Connector installation attempts;
5. successful pairing;
6. first successful remote task;
7. first durable long-running task;
8. second-session/return usage;
9. issues or feature requests from people you do not already know.

The strongest early signal is not a large impression count. It is a stranger successfully installing the project and returning to use it again.

## Launch rule

Do not add a large feature because a launch post performed poorly.

First check whether visitors understood the product, reached the install path, and could complete pairing. Fix the earliest broken conversion step before expanding scope.
