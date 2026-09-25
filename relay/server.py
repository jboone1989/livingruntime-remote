from __future__ import annotations

import asyncio
import html
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.server.apps import APP_MIME_TYPE, Apps, ResourceCsp
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver.resources import TextResource
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route

from auth import verifier_from_env
from embedded_auth import EmbeddedAuthStore, EmbeddedOAuthProvider
from store import RelayStore

NAME = "LivingRuntime Remote"
VERSION = "0.4.27"
PI_JOB_WIDGET_URI = "ui://livingruntime-remote/pi-job-watch-v2.html"
LONG_JOB_WIDGET_URI = "ui://livingruntime-remote/long-job-watch-v2.html"
COGNITION_WIDGET_URI = "ui://livingruntime-remote/agent-cognition-watch-v3.html"
CONTROL_PLANE_WIDGET_URI = "ui://livingruntime-remote/control-plane-v4.html"
CONTROL_PLANE_WIDGET_LEGACY_URI = "ui://livingruntime-remote/control-plane-v3.html"
PI_JOB_WIDGET_DOMAIN = "https://remote.livingruntime.com"
IDENTITY_SCOPES = ["openid", "email"]
SESSION_SCOPES = ["offline_access"]
READ = {"securitySchemes": [{"type": "oauth2", "scopes": ["remote:read", *IDENTITY_SCOPES]}]}
WRITE = {"securitySchemes": [{"type": "oauth2", "scopes": ["remote:read", "remote:write", *IDENTITY_SCOPES]}]}
SUPPORTED_SCOPES = ["remote:read", "remote:write", *IDENTITY_SCOPES, *SESSION_SCOPES]
TOOL_TEXT = {
    "create_pairing_code": ("Create pairing code", "Create a short-lived one-time code used to pair the user's local LivingRuntime Remote Connector."),
    "device_status": ("Check paired device", "Check whether the user's paired LivingRuntime Remote Connector is present and recently online."),
    "disconnect_device": ("Disconnect paired device", "Revoke a paired LivingRuntime Remote Connector and discard its queued or completed relay tasks."),
    "connection_status": ("Check connection status", "Check SSH, gateway, authentication, configured roots, projects, and paired-device reachability before remote work."),
    "capabilities": ("List Remote capabilities", "List the bounded Remote tools currently available and report whether the toolset is healthy and complete."),
    "list_devices": ("List managed devices", "List configured remote hosts, their stable device IDs, reachability, hostnames, projects, and bounded capabilities."),
    "remote_overview": ("Open Remote control plane", "Show one secret-free snapshot of Connector health, managed hosts, durable jobs, pending approvals, credential handles, and recent activity."),
    "list_projects": ("List configured projects", "List the named projects and bounded workspaces configured for this LivingRuntime Remote Connector."),
    "read_file": ("Read remote file", "Read bytes from a file inside an allowed project or configured root. Use this before editing or inspecting source files."),
    "list_dir": ("List remote directory", "List files and directories inside an allowed project or configured root without modifying them."),
    "logs": ("Read service logs", "Read recent journal logs for an allowlisted service, optionally scoped through a configured project."),
    "write_file": ("Write remote file", "Create or replace a file inside an allowed project or configured root, optionally guarded by an expected SHA-256."),
    "git": ("Run Git command", "Run a bounded Git command inside an allowed repository using explicit arguments."),
    "exec": ("Execute bounded command", "Run a built-in development command or an exact command previously approved by the operator. Unapproved commands return a durable approval request."),
    "list_credentials": ("List credential handles", "List local credential handles and scopes without returning secret values."),
    "lease_credential": ("Lease credential handle", "Issue a short-lived opaque credential lease scoped to one capability, project, and device."),
    "list_credential_leases": ("List credential leases", "List opaque credential leases without returning credential values."),
    "revoke_credential_lease": ("Revoke credential lease", "Revoke one opaque credential lease without deleting the underlying credential."),
    "github_identity": ("Verify GitHub credential", "Use a scoped credential lease internally to verify the authenticated GitHub identity without returning the credential value."),
    "create_job": ("Create durable job", "Create a durable LivingRuntime Remote job for a long-running goal, optionally linked to an existing Pi job."),
    "get_job": ("Get durable job", "Read one durable Remote job including progress, backend, next action, and checkpoints."),
    "list_jobs": ("List durable jobs", "List recent durable Remote jobs, optionally filtered by status."),
    "checkpoint_job": ("Checkpoint durable job", "Persist bounded progress, current step, next action, and status for a durable Remote job."),
    "submit_llm_request": ("Submit LLM request", "Persist a bounded agent cognition request for a ChatGPT cognition watcher."),
    "watch_agent_cognition": ("Watch agent cognition", "Attach a no-polling watcher for the next cognition request from a named agent."),
    "wait_llm_request": ("Wait for agent LLM request", "App-only read-only bounded wait for the next pending cognition request."),
    "claim_llm_request": ("Claim LLM request", "Claim one pending cognition request after a watcher wakes ChatGPT, returning the prompt and claim token needed to complete it."),
    "get_llm_request": ("Get LLM request", "Read one durable cognition request including prompt, response contract, and active claim."),
    "get_llm_request_status": ("Get LLM request status", "Read bounded status and provenance for one cognition request without returning its prompt."),
    "complete_llm_request": ("Complete LLM request", "Write a claimed ChatGPT cognition response back to the durable request so the calling agent can continue."),
    "start_long_job": ("Start supervised long job", "Start a command under the durable long-job supervisor and return immediately with a watcher-ready job ID."),
    "watch_long_job": ("Watch supervised long job", "Attach the no-polling watcher to an existing supervised long-running command."),
    "get_long_job": ("Get supervised long job", "Refresh one supervised long-running command from its durable remote receipt."),
    "wait_long_job": ("Wait for supervised long job", "App-only bounded wait used by the long-job watcher."),
    "cancel_long_job": ("Cancel supervised long job", "Request cancellation of a supervised long-running command process group."),
    "list_exec_permissions": ("List command permissions", "List pending dynamic command requests plus active and revoked persisted grants."),
    "approve_exec_permission": ("Approve command permission", "Approve a pending exact command request. Host scope is the default; all-host or diagnostic-class grants are limited to read-only diagnostics."),
    "deny_exec_permission": ("Deny command permission", "Deny a pending dynamic command request without executing it."),
    "revoke_exec_permission": ("Revoke command permission", "Revoke a persisted dynamic command grant so matching commands require approval again."),
    "process": ("Manage remote process", "Perform a permitted process action on the connected host using a PID or bounded process-name match."),
    "systemd": ("Manage allowlisted service", "Inspect or control an explicitly allowlisted systemd unit on the connected host."),
    "apply_patch": ("Apply file patch", "Apply a unified diff to a file inside an allowed workspace, optionally guarded by an expected SHA-256."),
    "diagnostics": ("Run Remote diagnostics", "Collect bounded LivingRuntime Remote diagnostics for connectivity, configuration, and tool-health troubleshooting."),
    "start_pi_agent": ("Start native Pi agent", "Send a goal into Pi exactly like a user prompt. Pi owns its native agent loop and calls ChatGPT through the LivingRuntime cognition provider whenever it needs model inference."),
    "start_pi_step": ("Start detached Pi step", "Run a bounded batch of structured Pi file/search/edit actions in an external-controller session, persist it as a durable job, and attach the Pi watcher. Pi does not call an LLM."),
}

PI_JOB_WIDGET_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body {
    margin: 0;
    padding: 12px;
    font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: var(--color-text-primary, inherit);
    background: transparent;
  }
  .card {
    border: 1px solid var(--color-border-secondary, rgba(127,127,127,.35));
    border-radius: 12px;
    padding: 12px 14px;
  }
  .row { display: flex; gap: 8px; align-items: center; }
  .dot {
    width: 8px; height: 8px; border-radius: 999px;
    background: var(--color-text-info, #4f7cff);
    flex: 0 0 auto;
  }
  #detail { margin-top: 6px; color: var(--color-text-secondary, #777); }
  code { font-family: var(--font-mono, ui-monospace, monospace); font-size: 12px; }
</style>
</head>
<body>
  <div class="card">
    <div class="row"><span class="dot"></span><strong id="status">Preparing Pi job watcher…</strong></div>
    <div id="detail"></div>
  </div>
<script>
(() => {
  const pending = new Map();
  let nextId = 1;
  let connected = false;
  let latestOutput = null;
  let activeJob = null;
  let stopped = false;
  const statusEl = document.getElementById("status");
  const detailEl = document.getElementById("detail");

  function request(method, params) {
    const id = nextId++;
    window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  }

  function notify(method, params = {}) {
    window.parent.postMessage({ jsonrpc: "2.0", method, params }, "*");
  }

  function setStatus(status, detail = "") {
    statusEl.textContent = status;
    detailEl.textContent = detail;
  }

  function toolResultData(result) {
    return result?.structuredContent || result?.structured_content || result || null;
  }

  async function sendFollowUp(jobId, state, runtimeJobId, runtimeGoalStatus) {
    const completionStatus = state?.status || "UNKNOWN";
    const prompt = runtimeJobId
      ? (
          "Pi Remote step " + jobId + " completed with status " + completionStatus +
          ". Durable LivingRuntime goal " + runtimeJobId + " is now " + (runtimeGoalStatus || "UNKNOWN") +
          ". Continue this same goal now: inspect the Pi session evidence and current repository state, " +
          "then either call start_pi_step again with runtime_job_id=" + runtimeJobId +
          " for the next bounded external-controller step, use the normal Remote permission layer for commands/tests, " +
          "or mark the durable goal SUCCEEDED with checkpoint_job only if its acceptance evidence is satisfied. " +
          "Do not ask me to say continue."
        )
      : (
          "Pi Remote job " + jobId + " completed with status " + completionStatus +
          ". Continue this same development task now. Inspect the durable Pi job/session result, " +
          "review the changes and tests, and proceed to the next required step without asking me to say continue."
        );
    try {
      await request("ui/message", {
        role: "user",
        content: [{ type: "text", text: prompt }]
      });
      return;
    } catch (error) {
      const openai = typeof window !== "undefined" ? window.openai : undefined;
      if (openai?.sendFollowUpMessage) {
        await openai.sendFollowUpMessage({ prompt, scrollToBottom: false });
        return;
      }
      throw error;
    }
  }

  async function watch(output) {
    const jobId = output?.jobId;
    const runtimeJobId = output?.runtimeJobId || null;
    const piRemoteDir = output?.piRemoteDir;
    const jobRoot = output?.jobRoot || null;
    if (!connected || !jobId || activeJob === jobId || stopped) return;
    activeJob = jobId;
    const initialStatus = output?.state?.status || "UNKNOWN";
    setStatus(
      output?.terminal ? "Pi already finished: " + initialStatus : "Pi is working…",
      "Job " + jobId + " · state " + initialStatus + " · Remote watcher attached"
    );

    try {
      while (!stopped) {
        const result = await request("tools/call", {
          name: "wait_pi_job_completion",
          arguments: {
            job_id: jobId,
            pi_remote_dir: piRemoteDir,
            job_root: jobRoot,
            timeout_seconds: 30
          }
        });
        const data = toolResultData(result);
        if (data?.terminal) {
          const state = data.state || {};
          const durableId = data.runtimeJobId || runtimeJobId;
          const durableStatus = data.runtimeGoalStatus || output?.runtimeGoalStatus || null;
          setStatus(
            "Pi finished: " + (state.status || "terminal"),
            durableId
              ? "Durable goal " + durableId + " · " + (durableStatus || "awaiting controller")
              : "Sending a follow-up into this ChatGPT conversation…"
          );
          await request("ui/update-model-context", {
            structuredContent: {
              piRemoteCompletion: {
                jobId,
                runtimeJobId: durableId,
                status: state.status || null,
                runtimeGoalStatus: durableStatus,
                finishedAt: state.finishedAt || null
              }
            }
          }).catch(() => {});
          await sendFollowUp(jobId, state, durableId, durableStatus);
          setStatus("Follow-up sent", "ChatGPT can continue from the completed Pi job.");
          stopped = true;
          return;
        }
        if (!data?.timedOut) {
          throw new Error("Pi job wait returned without terminal state or timeout");
        }
        setStatus(
          "Pi is still working…",
          "Job " + jobId + " · Remote connected · watcher heartbeat received"
        );
      }
    } catch (error) {
      const message = String(error?.message || error);
      const disconnected = /offline|device|connect|timeout/i.test(message);
      setStatus(
        disconnected ? "Remote connection lost" : "Watcher stopped",
        message
      );
      const prompt =
        "LivingRuntime Remote watcher for Pi job " + jobId + " stopped: " + message +
        ". Check device_status and the durable Pi job state before assuming Pi is still running.";
      try {
        await request("ui/message", {
          role: "user",
          content: [{ type: "text", text: prompt }]
        });
      } catch (_) {}
    }
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.id !== undefined && pending.has(message.id)) {
      const waiter = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) waiter.reject(message.error);
      else waiter.resolve(message.result);
      return;
    }
    if (message.method === "ui/notifications/tool-result") {
      latestOutput = message.params?.structuredContent || null;
      void watch(latestOutput);
    }
    if (message.method === "ui/notifications/request-teardown") {
      stopped = true;
    }
  }, { passive: true });

  async function connect() {
    try {
      await request("ui/initialize", {
        appInfo: { name: "livingruntime-remote-pi-job-watch", version: "1.0.0" },
        appCapabilities: {},
        protocolVersion: "2026-01-26"
      });
      notify("ui/notifications/initialized");
      connected = true;
      if (!latestOutput && window.openai?.toolOutput) {
        latestOutput = window.openai.toolOutput;
      }
      await watch(latestOutput);
    } catch (error) {
      setStatus("Widget initialization failed", String(error?.message || error));
    }
  }

  void connect();
})();
</script>
</body>
</html>"""

LONG_JOB_WIDGET_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body { margin:0; padding:12px; font:14px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--color-text-primary,inherit); background:transparent; }
  .card { border:1px solid var(--color-border-secondary,rgba(127,127,127,.35)); border-radius:12px; padding:12px 14px; }
  .row { display:flex; gap:8px; align-items:center; }
  .dot { width:8px; height:8px; border-radius:999px; background:var(--color-text-info,#4f7cff); flex:0 0 auto; }
  #detail { margin-top:6px; color:var(--color-text-secondary,#777); white-space:pre-wrap; }
  code { font-family:var(--font-mono,ui-monospace,monospace); font-size:12px; }
</style>
</head>
<body>
<div class="card">
  <div class="row"><span class="dot"></span><strong id="status">Preparing long-job watcher…</strong></div>
  <div id="detail"></div>
</div>
<script>
(() => {
  const pending = new Map();
  let nextId = 1;
  let connected = false;
  let latestOutput = null;
  let activeJob = null;
  let stopped = false;
  const statusEl = document.getElementById("status");
  const detailEl = document.getElementById("detail");

  function request(method, params) {
    const id = nextId++;
    window.parent.postMessage({ jsonrpc:"2.0", id, method, params }, "*");
    return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}));
  }
  function notify(method, params={}) {
    window.parent.postMessage({ jsonrpc:"2.0", method, params }, "*");
  }
  function setStatus(status, detail="") {
    statusEl.textContent = status;
    detailEl.textContent = detail;
  }
  function toolResultData(result) {
    return result?.structuredContent || result?.structured_content || result || null;
  }
  function jobIdFrom(output) {
    return output?.runtimeJobId || output?.job?.job_id || null;
  }
  function describe(data) {
    const job = data?.job || {};
    const state = data?.state || {};
    const heartbeat = state?.heartbeat_age_seconds;
    const progress = state?.progress_age_seconds;
    const bits = [
      "Job " + (job.job_id || "?"),
      "state " + (job.status || state.observed_status || state.status || "UNKNOWN")
    ];
    if (heartbeat !== undefined && heartbeat !== null) bits.push("heartbeat " + heartbeat + "s ago");
    if (progress !== undefined && progress !== null) bits.push("progress " + progress + "s ago");
    return bits.join(" · ");
  }
  async function followUp(jobId, status) {
    const prompt =
      "LivingRuntime Remote long job " + jobId + " is now " + status +
      ". Continue this same task now. Call get_long_job for the durable receipt, inspect the output tail and current repository/process state, " +
      "then either proceed, repair/retry, or cancel as appropriate. Do not ask me to say continue and do not assume the job is still running.";
    try {
      await request("ui/message", { role:"user", content:[{type:"text",text:prompt}] });
      return;
    } catch (error) {
      const openai = typeof window !== "undefined" ? window.openai : undefined;
      if (openai?.sendFollowUpMessage) {
        await openai.sendFollowUpMessage({prompt,scrollToBottom:false});
        return;
      }
      throw error;
    }
  }

  async function watch(output) {
    const jobId = jobIdFrom(output);
    if (!connected || !jobId || activeJob === jobId || stopped) return;
    activeJob = jobId;
    setStatus("Long job watcher attached", "Job " + jobId + " · waiting for heartbeat");
    try {
      while (!stopped) {
        const result = await request("tools/call", {
          name:"wait_long_job",
          arguments:{job_id:jobId,timeout_seconds:30}
        });
        const data = toolResultData(result);
        const job = data?.job || {};
        const state = data?.state || {};
        const status = job.status || state.observed_status || state.status || "UNKNOWN";

        if (data?.terminal || job.terminal) {
          setStatus("Long job finished: " + status, describe(data));
          await request("ui/update-model-context", {
            structuredContent:{longJobCompletion:{jobId,status,terminal:true}}
          }).catch(()=>{});
          await followUp(jobId,status);
          setStatus("Follow-up sent", "ChatGPT can continue from the durable receipt.");
          stopped = true;
          return;
        }
        if (status === "STALLED") {
          setStatus("Long job stalled", describe(data));
          await request("ui/update-model-context", {
            structuredContent:{longJobCompletion:{jobId,status:"STALLED",terminal:false}}
          }).catch(()=>{});
          await followUp(jobId,"STALLED");
          setStatus("Stall reported", "ChatGPT can inspect and recover the job.");
          stopped = true;
          return;
        }
        if (!data?.timedOut) {
          throw new Error("long-job wait returned without terminal/stalled state or timeout");
        }
        setStatus("Long job is working…", describe(data));
        await request("ui/update-model-context", {
          structuredContent:{
            longJobProgress:{
              jobId,
              status,
              heartbeatAgeSeconds:state?.heartbeat_age_seconds ?? null,
              progressAgeSeconds:state?.progress_age_seconds ?? null
            }
          }
        }).catch(()=>{});
      }
    } catch (error) {
      const message = String(error?.message || error);
      setStatus("Long-job watcher stopped", message);
      try {
        await request("ui/message", {
          role:"user",
          content:[{type:"text",text:
            "LivingRuntime Remote long-job watcher for " + jobId + " stopped: " + message +
            ". Check get_long_job and device_status before assuming the command is still running."
          }]
        });
      } catch (_) {}
    }
  }

  window.addEventListener("message",(event)=>{
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.id !== undefined && pending.has(message.id)) {
      const waiter=pending.get(message.id); pending.delete(message.id);
      if (message.error) waiter.reject(message.error); else waiter.resolve(message.result);
      return;
    }
    if (message.method === "ui/notifications/tool-result") {
      latestOutput = message.params?.structuredContent || null;
      void watch(latestOutput);
    }
    if (message.method === "ui/notifications/request-teardown") stopped=true;
  },{passive:true});

  async function connect() {
    try {
      await request("ui/initialize", {
        appInfo:{name:"livingruntime-remote-long-job-watch",version:"1.0.0"},
        appCapabilities:{},
        protocolVersion:"2026-01-26"
      });
      notify("ui/notifications/initialized");
      connected=true;
      if (!latestOutput && window.openai?.toolOutput) latestOutput=window.openai.toolOutput;
      await watch(latestOutput);
    } catch (error) {
      setStatus("Widget initialization failed",String(error?.message || error));
    }
  }
  void connect();
})();
</script>
</body>
</html>"""

COGNITION_WIDGET_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body { margin:0; padding:12px; font:14px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--color-text-primary,inherit); background:transparent; }
  .card { border:1px solid var(--color-border-secondary,rgba(127,127,127,.35)); border-radius:12px; padding:12px 14px; }
  .row { display:flex; gap:8px; align-items:center; }
  .dot { width:8px; height:8px; border-radius:999px; background:var(--color-text-info,#4f7cff); flex:0 0 auto; }
  #detail { margin-top:6px; color:var(--color-text-secondary,#777); white-space:pre-wrap; }
  code { font-family:var(--font-mono,ui-monospace,monospace); font-size:12px; }
</style>
</head>
<body>
<div class="card">
  <div class="row"><span class="dot"></span><strong id="status">Preparing cognition watcher…</strong></div>
  <div id="detail"></div>
</div>
<script>
(() => {
  const pending = new Map();
  let nextId = 1;
  let connected = false;
  let latestOutput = null;
  let activeWatcher = null;
  let stopped = false;
  const statusEl = document.getElementById("status");
  const detailEl = document.getElementById("detail");

  function request(method, params) {
    const id = nextId++;
    window.parent.postMessage({jsonrpc:"2.0",id,method,params},"*");
    return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}));
  }
  function notify(method, params={}) {
    window.parent.postMessage({jsonrpc:"2.0",method,params},"*");
  }
  function setStatus(status, detail="") {
    statusEl.textContent = status;
    detailEl.textContent = detail;
  }
  function data(result) {
    return result?.structuredContent || result?.structured_content || result || null;
  }
  async function followUp(agentId, requestId, purpose, watcherId) {
    const prompt =
      "LivingRuntime agent " + agentId + " submitted cognition request " + requestId +
      " for " + (purpose || "general cognition") + ". Process it now: first call claim_llm_request with request_id=" +
      requestId + " and watcher_id=" + watcherId + ". Reason over the returned messages, tools, and response_format. " +
      "You are acting as Pi's model provider, not as its harness: do not execute a returned Pi tool through Remote. " +
      "If Pi should use a tool, call complete_llm_request with tool_calls=[{id,name,arguments}] and the returned claim.token; " +
      "Pi will execute that tool inside its native agent loop and send the tool result back in the next model request. " +
      "If no tool is needed, complete with response_text. If the request is already claimed or completed, inspect " +
      "get_llm_request_status and do not duplicate work. The watcher remains armed after this request, so do not " +
      "create a second watcher merely to continue the channel. Do not ask the user to type continue.";
    try {
      await request("ui/message", {
        role:"user",
        content:[{type:"text",text:prompt}]
      });
      return;
    } catch (error) {
      const openai = typeof window !== "undefined" ? window.openai : undefined;
      if (openai?.sendFollowUpMessage) {
        await openai.sendFollowUpMessage({prompt,scrollToBottom:false});
        return;
      }
      throw error;
    }
  }
  async function watch(output) {
    const agentId = output?.agentId;
    const watcherId = output?.watcherId;
    if (!connected || !agentId || !watcherId || activeWatcher === watcherId || stopped) return;
    activeWatcher = watcherId;
    let handedOffRequestId = null;
    setStatus("Cognition channel armed", "Agent " + agentId + " · waiting for the next LLM request");
    try {
      while (!stopped) {
        const result = await request("tools/call", {
          name:"wait_llm_request",
          arguments:{
            agent_id:agentId,
            watcher_id:watcherId,
            timeout_seconds:30
          }
        });
        const payload = data(result);
        const llmRequest = payload?.request || null;
        if (llmRequest?.request_id) {
          const requestId = llmRequest.request_id;
          const purpose = llmRequest.purpose || null;
          const reclaimable = Boolean(llmRequest.reclaimable);
          if (requestId === handedOffRequestId && !reclaimable) {
            setStatus(
              "Request handed off",
              "Request " + requestId + " is waiting for ChatGPT to claim it; watcher remains armed."
            );
            await new Promise(resolve=>setTimeout(resolve,1000));
            continue;
          }
          handedOffRequestId = requestId;
          setStatus("Cognition request received", "Request " + requestId + " · handing it to ChatGPT");
          await request("ui/update-model-context", {
            structuredContent:{
              livingRuntimeCognitionRequest:{
                agentId,
                requestId,
                purpose,
                status:llmRequest.status || "DISPATCHED"
              }
            }
          }).catch(()=>{});
          await followUp(agentId, requestId, purpose, watcherId);
          setStatus(
            "Request handed off",
            "ChatGPT can answer " + requestId + "; watcher remains armed for the next queued request."
          );
          await new Promise(resolve=>setTimeout(resolve,500));
          continue;
        }
        if (!payload?.timed_out && !payload?.timedOut) {
          throw new Error("cognition wait returned without a request or timeout");
        }
        setStatus("Cognition channel armed", "Agent " + agentId + " · Remote watcher heartbeat received");
      }
    } catch (error) {
      const message = String(error?.message || error);
      setStatus("Cognition watcher stopped", message);
      try {
        await request("ui/message", {
          role:"user",
          content:[{type:"text",text:
            "LivingRuntime cognition watcher for agent " + agentId + " stopped: " + message +
            ". Check device_status and re-arm watch_agent_cognition before assuming the agent has no pending request."
          }]
        });
      } catch (_) {}
    }
  }
  window.addEventListener("message",(event)=>{
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.id !== undefined && pending.has(message.id)) {
      const waiter=pending.get(message.id); pending.delete(message.id);
      if (message.error) waiter.reject(message.error); else waiter.resolve(message.result);
      return;
    }
    if (message.method === "ui/notifications/tool-result") {
      latestOutput = message.params?.structuredContent || null;
      void watch(latestOutput);
    }
    if (message.method === "ui/notifications/request-teardown") stopped=true;
  },{passive:true});
  async function connect() {
    try {
      await request("ui/initialize", {
        appInfo:{name:"livingruntime-remote-agent-cognition-watch",version:"1.0.0"},
        appCapabilities:{},
        protocolVersion:"2026-01-26"
      });
      notify("ui/notifications/initialized");
      connected=true;
      if (!latestOutput && window.openai?.toolOutput) latestOutput=window.openai.toolOutput;
      await watch(latestOutput);
    } catch (error) {
      setStatus("Widget initialization failed",String(error?.message || error));
    }
  }
  void connect();
})();
</script>
</body>
</html>"""

CONTROL_PLANE_WIDGET_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body { margin:0; padding:12px; font:13px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--color-text-primary,inherit); background:transparent; }
  .top,.section { border:1px solid var(--color-border-secondary,rgba(127,127,127,.35)); border-radius:12px; padding:12px 14px; margin-bottom:10px; }
  .row { display:flex; gap:8px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
  .badge { display:inline-flex; gap:6px; align-items:center; padding:2px 8px; border-radius:999px; background:rgba(127,127,127,.12); }
  .dot { width:8px; height:8px; border-radius:50%; background:#999; }
  .ok .dot { background:#32a852; } .bad .dot { background:#d64545; } .warn .dot { background:#d79a27; }
  h3 { margin:0 0 8px; font-size:13px; }
  .muted { color:var(--color-text-secondary,#777); }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:8px; }
  .item { padding:8px; border-radius:9px; background:rgba(127,127,127,.08); overflow-wrap:anywhere; }
  code { font-family:var(--font-mono,ui-monospace,monospace); font-size:11px; }
  button { border:1px solid var(--color-border-secondary,rgba(127,127,127,.4)); border-radius:8px; padding:6px 10px; background:transparent; color:inherit; }
</style>
</head>
<body>
  <div class="top">
    <div class="row"><strong>LivingRuntime Remote Control Plane</strong><button id="refresh">Refresh</button></div>
    <div id="headline" class="muted">Loading snapshot…</div>
  </div>
  <div class="section"><h3>Execution truth</h3><div id="execution" class="grid"></div></div>
  <div class="section"><h3>Hosts</h3><div id="devices" class="grid"></div></div>
  <div class="section"><h3>Durable jobs</h3><div id="jobs" class="grid"></div></div>
  <div class="section"><h3>Permissions & credentials</h3><div id="security" class="grid"></div></div>
  <div class="section"><h3>Recent activity</h3><div id="activity" class="grid"></div></div>
<script>
(() => {
  const pending = new Map(); let nextId = 1; let connected = false; let latest = null;
  const q = id => document.getElementById(id);
  function request(method, params) {
    const id = nextId++; window.parent.postMessage({jsonrpc:"2.0",id,method,params},"*");
    return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}));
  }
  function notify(method, params={}) { window.parent.postMessage({jsonrpc:"2.0",method,params},"*"); }
  function data(result) { return result?.structuredContent || result?.structured_content || result || null; }
  function esc(v) { return String(v ?? "").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }
  function formatTs(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds)) return String(value ?? "");
    const date = new Date(seconds * 1000);
    if (Number.isNaN(date.getTime())) return String(value ?? "");
    return date.toLocaleString(undefined, {
      year:"numeric", month:"2-digit", day:"2-digit",
      hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false
    });
  }
  function render(snapshot) {
    latest = snapshot || {};
    const connector = latest.connector || {};
    const online = connector.online !== false;
    q("headline").innerHTML = '<span class="badge '+(online?'ok':'bad')+'"><span class="dot"></span>'+(online?'Connector online':'Connector offline')+'</span> · v'+esc(latest.version || latest.overview?.version || "?");
    const overview = latest.overview || latest;
    const execution = overview?.execution || {};
    const state = execution.state || "UNKNOWN";
    const stateClass = state==="RUNNING_EXECUTION" || state==="RECENT_ACTIVITY" ? "ok" : (state==="STALLED" || state==="BLOCKED" ? "bad" : "warn");
    const active = execution.active_jobs || [];
    const lastActivity = execution.last_real_activity_at;
    const warning = execution.ui_warning ? '<div class="item"><strong>UI may be stale</strong><div class="muted">'+esc(execution.ui_warning)+'</div></div>' : '';
    q("execution").innerHTML =
      '<div class="item"><span class="badge '+stateClass+'"><span class="dot"></span>'+esc(state)+'</span><div>'+esc(execution.message||"No execution receipt yet.")+'</div></div>'+
      '<div class="item"><strong>'+esc(execution.active_job_count||0)+'</strong> active durable jobs<div class="muted">'+esc(execution.worker_alive_count||0)+' live workers/children</div></div>'+
      '<div class="item"><strong>Last real activity</strong><div>'+esc(lastActivity?formatTs(lastActivity):"none recorded")+'</div><div class="muted">'+esc(execution.last_tool||"—")+(execution.last_target?' · '+esc(execution.last_target):'')+'</div></div>'+
      warning+
      active.slice(0,4).map(j=>'<div class="item"><strong>'+esc(j.status)+'</strong> <code>'+esc(j.job_id)+'</code><div>'+esc(j.current_step||j.goal||"")+'</div><div class="muted">worker '+esc(j.worker_alive||j.child_alive?"alive":"not alive")+' · heartbeat '+esc(j.heartbeat_age_seconds ?? "?")+'s</div></div>').join("");
    const devices = overview?.devices?.devices || [];
    q("devices").innerHTML = devices.length ? devices.map(d=>{
      const inv=d.inventory||{}; const mem=inv.memory||{};
      const resource=inv.ok ? '<div class="muted">CPU '+esc(inv.cpu_count)+' · RAM '+esc(mem.available_bytes ?? "?")+' free</div>' : '';
      return '<div class="item"><div class="badge '+(d.online?'ok':'bad')+'"><span class="dot"></span>'+esc(d.device_id)+'</div><div>'+esc(d.hostname||d.ssh_host||"")+'</div><div class="muted">'+esc(d.latency_ms)+' ms · '+esc((d.projects||[]).join(", "))+'</div>'+resource+'</div>';
    }).join("") : '<span class="muted">No hosts reported.</span>';
    const jobs = overview?.jobs || [];
    q("jobs").innerHTML = jobs.length ? jobs.slice(0,8).map(j=>'<div class="item"><div><strong>'+esc(j.status)+'</strong> <code>'+esc(j.job_id)+'</code></div><div>'+esc(j.goal)+'</div><div class="muted">'+esc(j.current_step||j.next_action||"No current step")+'</div></div>').join("") : '<span class="muted">No durable jobs.</span>';
    const p=overview?.permissions||{}; const creds=overview?.credentials||[];
    q("security").innerHTML =
      '<div class="item"><strong>'+esc((p.pending||[]).length)+'</strong> pending command approvals<div class="muted">'+esc(p.active_count||0)+' active grants</div></div>'+
      '<div class="item"><strong>'+esc(creds.length)+'</strong> credential handles<div class="muted">'+esc(creds.map(c=>c.handle).slice(0,5).join(", ")||"None")+'</div></div>';
    const activity=overview?.activity||[];
    q("activity").innerHTML = activity.length ? activity.slice().reverse().slice(0,12).map(a=>'<div class="item"><span class="badge '+(a.ok?'ok':'bad')+'"><span class="dot"></span>'+esc(a.tool)+'</span><div class="muted" title="'+esc(a.ts)+'">'+esc(formatTs(a.ts))+'</div></div>').join("") : '<span class="muted">No recent activity.</span>';
  }
  let refreshTimer = null;
  async function refresh() {
    if (!connected) return;
    if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; }
    q("headline").textContent="Refreshing…";
    try {
      const result=await request("tools/call",{name:"remote_overview",arguments:{include_resources:false}});
      render(data(result));
    } catch (e) {
      q("headline").textContent="Refresh failed: "+String(e?.message||e);
    } finally {
      if (connected) refreshTimer = setTimeout(()=>void refresh(), 10000);
    }
  }
  q("refresh").addEventListener("click",()=>void refresh());
  window.addEventListener("message", event=>{
    if(event.source!==window.parent)return; const m=event.data; if(!m||m.jsonrpc!=="2.0")return;
    if(m.id!==undefined&&pending.has(m.id)){const w=pending.get(m.id);pending.delete(m.id);m.error?w.reject(m.error):w.resolve(m.result);return;}
    if(m.method==="ui/notifications/tool-result") render(m.params?.structuredContent||null);
  },{passive:true});
  (async()=>{try{
    await request("ui/initialize",{appInfo:{name:"livingruntime-remote-control-plane",version:"1.0.0"},appCapabilities:{},protocolVersion:"2026-01-26"});
    notify("ui/notifications/initialized"); connected=true;
    render(window.openai?.toolOutput||latest);\n    refreshTimer = setTimeout(()=>void refresh(), 1000);
  }catch(e){q("headline").textContent="Widget initialization failed: "+String(e?.message||e);}})();
})();
</script>
</body>
</html>"""


class PairRateLimiter:
    def __init__(self, limit: int = 10, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.attempts: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        recent = [value for value in self.attempts.get(key, []) if value >= cutoff]
        if len(recent) >= self.limit:
            self.attempts[key] = recent
            return False
        recent.append(now)
        self.attempts[key] = recent
        return True


class Relay:
    def __init__(
        self,
        store: RelayStore,
        timeout: float = 45.0,
        reconnect_grace: float = 8.0,
        device_stale_after: float = 35.0,
    ) -> None:
        self.store = store
        self.timeout = timeout
        self.reconnect_grace = max(0.0, reconnect_grace)
        self.device_stale_after = max(1.0, device_stale_after)
        self._task_events: dict[str, asyncio.Event] = {}
        self._result_events: dict[str, asyncio.Event] = {}

    def _task_event(self, device_id: str) -> asyncio.Event:
        event = self._task_events.get(device_id)
        if event is None:
            event = asyncio.Event()
            self._task_events[device_id] = event
        return event

    def notify_task(self, device_id: str) -> None:
        """Wake a connector poll that is waiting for work on this relay process."""
        self._task_event(device_id).set()

    def arm_task_wait(self, device_id: str) -> None:
        """Clear any stale wakeup before checking the durable queue once."""
        self._task_event(device_id).clear()

    async def wait_for_task(self, device_id: str, timeout: float) -> bool:
        """Sleep without touching SQLite until work arrives or the long poll expires."""
        if timeout <= 0:
            return False
        event = self._task_event(device_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    def _result_event(self, task_id: str) -> asyncio.Event:
        event = self._result_events.get(task_id)
        if event is None:
            event = asyncio.Event()
            self._result_events[task_id] = event
        return event

    def notify_result(self, task_id: str) -> None:
        """Wake an MCP request that is waiting for this task result."""
        event = self._result_events.get(task_id)
        if event is not None:
            event.set()

    def device_status(self, user_sub: str) -> dict[str, Any] | None:
        device = self.store.device_for_user(user_sub)
        if not device:
            return None
        age = max(0.0, time.time() - float(device.get("last_seen") or 0.0))
        return {
            **device,
            "online": age <= self.device_stale_after,
            "stale_for_seconds": round(age, 3),
        }

    async def _wait_for_online_device(self, user_sub: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.reconnect_grace
        while True:
            device = self.device_status(user_sub)
            if device and device["online"]:
                return device
            if time.monotonic() >= deadline:
                if not device:
                    raise RuntimeError(
                        "no paired LivingRuntime Remote device; call create_pairing_code first"
                    )
                raise RuntimeError(
                    "paired LivingRuntime Remote device is offline "
                    f"(last seen {device['stale_for_seconds']}s ago)"
                )
            await asyncio.sleep(0.25)

    async def call(self, user_sub: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        device = await self._wait_for_online_device(user_sub)
        task_id = self.store.enqueue(user_sub, device["device_id"], tool, args)
        result_event = self._result_event(task_id)
        self.notify_task(device["device_id"])
        requested_timeout = 0.0
        try:
            requested_timeout = float(args.get("timeout_seconds") or 0.0)
        except (TypeError, ValueError):
            requested_timeout = 0.0
        wait_timeout = max(self.timeout, min(120.0, max(0.0, requested_timeout)) + 30.0)
        try:
            result = self.store.result(user_sub, task_id, consume=True)
            if result is None:
                try:
                    await asyncio.wait_for(result_event.wait(), timeout=wait_timeout)
                except TimeoutError:
                    pass
                result = self.store.result(user_sub, task_id, consume=True)
            if result is not None:
                if not result.get("ok"):
                    raise RuntimeError(str(result.get("error") or "remote device call failed"))
                value = result.get("result")
                return value if isinstance(value, dict) else {"result": value}
            if self.store.cancel_if_queued(user_sub, task_id):
                raise TimeoutError(
                    "paired device did not claim task before relay timeout; queued task was cancelled"
                )
            raise TimeoutError(
                "paired device claimed task but did not complete before relay timeout"
            )
        finally:
            self._result_events.pop(task_id, None)


def _principal(scope: str) -> str:
    token = get_access_token()
    if token is None or not token.subject:
        raise PermissionError("OAuth authentication required")
    if scope not in token.scopes:
        raise PermissionError(f"OAuth scope required: {scope}")
    return token.subject


def _auth_mode() -> str:
    mode = os.environ.get("LIVINGRUNTIME_AUTH_MODE", "external").strip().lower()
    if mode not in {"external", "embedded"}:
        raise RuntimeError("LIVINGRUNTIME_AUTH_MODE must be external or embedded")
    return mode


def _append_query(url: str, values: dict[str, str | None]) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend((key, value) for key, value in values.items() if value is not None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _login_page(request_id: str, *, error: str | None = None, email: str = "") -> HTMLResponse:
    error_html = (
        f'<p class="error">{html.escape(error)}</p>' if error else ""
    )
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LivingRuntime Remote sign in</title>
<style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#111;color:#eee;margin:0}}
main{{max-width:420px;margin:10vh auto;padding:28px}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:16px;padding:24px}}
h1{{font-size:22px;margin:0 0 8px}}
p{{color:#aaa;line-height:1.5}}
label{{display:block;margin:16px 0 6px;font-size:14px}}
input{{box-sizing:border-box;width:100%;padding:12px;border-radius:10px;border:1px solid #444;background:#0d0d0d;color:#fff}}
button{{width:100%;margin-top:20px;padding:12px;border:0;border-radius:10px;font-weight:700;cursor:pointer}}
.error{{color:#ff9d9d}}
.small{{font-size:12px}}
</style>
</head>
<body><main><div class="card">
<h1>Sign in to LivingRuntime Remote</h1>
<p>Authorize ChatGPT to access your paired LivingRuntime Remote device.</p>
{error_html}
<form method="post" action="/oauth/login">
<input type="hidden" name="request_id" value="{html.escape(request_id, quote=True)}">
<label for="email">Email</label>
<input id="email" name="email" type="email" autocomplete="username" required value="{html.escape(email, quote=True)}">
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Continue</button>
</form>
<p class="small">Credentials are used only by this LivingRuntime Remote authorization server.</p>
</div></main></body></html>"""
    return HTMLResponse(
        body,
        headers={
            "cache-control": "no-store",
            "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
            "x-content-type-options": "nosniff",
        },
    )


def create_mcp(
    relay: Relay,
    embedded_provider: EmbeddedOAuthProvider | None = None,
) -> MCPServer:
    issuer = os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/")
    resource = os.environ["LIVINGRUNTIME_RELAY_RESOURCE_URL"]
    docs_url = os.environ.get("LIVINGRUNTIME_RELAY_DOCS_URL")
    mode = _auth_mode()

    auth_settings = AuthSettings(
        issuer_url=issuer,
        resource_server_url=resource,
        required_scopes=["remote:read"],
        validate_token_resource=True,
        service_documentation_url=docs_url,
        client_registration_options=(
            ClientRegistrationOptions(
                enabled=True,
                valid_scopes=SUPPORTED_SCOPES,
                default_scopes=SUPPORTED_SCOPES,
            )
            if mode == "embedded"
            else None
        ),
        revocation_options=RevocationOptions(enabled=mode == "embedded"),
    )
    kwargs: dict[str, Any]
    if mode == "embedded":
        if embedded_provider is None:
            auth_db = os.environ.get("LIVINGRUNTIME_AUTH_DB", relay.store.path)
            embedded_provider = EmbeddedOAuthProvider(EmbeddedAuthStore(auth_db), issuer)
        kwargs = {"auth_server_provider": embedded_provider}
    else:
        kwargs = {"token_verifier": verifier_from_env()}

    async def pi_job_command(
        user_sub: str,
        command: str,
        job_id: str,
        pi_remote_dir: str,
        job_root: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        if command not in {"job-status", "job-wait"}:
            raise ValueError("unsupported Pi job command")
        job_id = str(job_id).strip()
        pi_remote_dir = str(pi_remote_dir).strip()
        if not job_id or len(job_id) > 128:
            raise ValueError("job_id must be a bounded non-empty string")
        if not pi_remote_dir or len(pi_remote_dir) > 4096:
            raise ValueError("pi_remote_dir must be a bounded non-empty path")
        payload: dict[str, Any] = {"jobId": job_id}
        if job_root:
            if len(job_root) > 4096:
                raise ValueError("job_root path is too long")
            payload["jobRoot"] = job_root
        bounded_timeout = min(90, max(1, int(timeout_seconds)))
        if command == "job-wait":
            payload["timeoutMs"] = bounded_timeout * 1000
        result = await relay.call(
            user_sub,
            "exec",
            {
                "argv": [
                    "node",
                    str(Path(pi_remote_dir) / "cli.js"),
                    command,
                    json.dumps(payload, separators=(",", ":")),
                ],
                "cwd": pi_remote_dir,
                "project": None,
                "timeout_seconds": min(120, bounded_timeout + 10),
            },
        )
        if int(result.get("returncode", 1)) != 0:
            raise RuntimeError(str(result.get("stderr") or "Pi Remote job command failed"))
        stdout = result.get("stdout")
        if not isinstance(stdout, str) or not stdout.strip():
            raise RuntimeError("Pi Remote job command returned no JSON")
        parsed = json.loads(stdout)
        if not isinstance(parsed, dict):
            raise RuntimeError("Pi Remote job command returned non-object JSON")
        return parsed

    async def wait_pi_job_until_terminal(
        user_sub: str,
        binding: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Wait in bounded event-driven chunks without creating model-side polling turns."""
        deadline = time.monotonic() + min(540, max(1, int(timeout_seconds)))
        latest: dict[str, Any] | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest or {"terminal": False, "timedOut": True, "state": None}
            latest = await pi_job_command(
                user_sub,
                "job-wait",
                str(binding["job_id"]),
                str(binding["pi_remote_dir"]),
                binding.get("job_root"),
                timeout_seconds=min(90, max(1, int(remaining))),
            )
            state = latest.get("state") if isinstance(latest.get("state"), dict) else {}
            if latest.get("terminal") or state.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return latest
            if not latest.get("timedOut"):
                return latest

    apps = Apps()

    def add_widget_resource(
        uri: str,
        widget_html: str,
        *,
        name: str,
        title: str,
        description: str,
    ) -> None:
        """Publish both MCP Apps metadata and ChatGPT compatibility aliases."""
        csp = ResourceCsp(connect_domains=[], resource_domains=[])
        apps.add_resource(
            TextResource(
                uri=uri,
                name=name,
                title=title,
                description=description,
                mime_type=APP_MIME_TYPE,
                meta={
                    "ui": {
                        "csp": csp.model_dump(by_alias=True, exclude_none=True),
                        "domain": PI_JOB_WIDGET_DOMAIN,
                        "prefersBorder": True,
                    },
                    "openai/widgetCSP": {
                        "connect_domains": [],
                        "resource_domains": [],
                    },
                    "openai/widgetDomain": PI_JOB_WIDGET_DOMAIN,
                    "openai/widgetPrefersBorder": True,
                },
                text=widget_html,
            )
        )

    add_widget_resource(
        PI_JOB_WIDGET_URI,
        PI_JOB_WIDGET_HTML,
        name="pi-job-watch",
        title="Pi Remote job watcher",
        description="Wait for an existing Pi Remote detached job and continue this conversation when it finishes.",
    )
    add_widget_resource(
        LONG_JOB_WIDGET_URI,
        LONG_JOB_WIDGET_HTML,
        name="long-job-watch",
        title="Long-running job watcher",
        description="Watch a supervised long-running command without model-side polling and report stalled or terminal state back into the same conversation.",
    )
    add_widget_resource(
        COGNITION_WIDGET_URI,
        COGNITION_WIDGET_HTML,
        name="agent-cognition-watch",
        title="Agent cognition watcher",
        description="Wait for a durable agent LLM request and hand it into this ChatGPT conversation without model-side polling.",
    )
    add_widget_resource(
        CONTROL_PLANE_WIDGET_LEGACY_URI,
        CONTROL_PLANE_WIDGET_HTML,
        name="remote-control-plane-v3",
        title="LivingRuntime Remote control plane",
        description="Backward-compatible execution-truth dashboard for existing ChatGPT sessions.",
    )
    add_widget_resource(
        CONTROL_PLANE_WIDGET_URI,
        CONTROL_PLANE_WIDGET_HTML,
        name="remote-control-plane",
        title="LivingRuntime Remote control plane",
        description="Read-only execution-truth snapshot of connector health, real server activity, durable jobs, approvals, credential handles, and recent activity.",
    )

    @apps.tool(
        resource_uri=PI_JOB_WIDGET_URI,
        visibility=["model", "app"],
        name="start_pi_agent",
        title=TOOL_TEXT["start_pi_agent"][0],
        description=TOOL_TEXT["start_pi_agent"][1],
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            openWorldHint=False,
        ),
        meta=WRITE,
    )
    async def start_pi_agent(
        goal: str,
        project: str,
        session_file: str | None = None,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "start_pi_agent",
            {
                "goal": goal,
                "project": project,
                "session_file": session_file,
            },
        )

    @apps.tool(
        resource_uri=PI_JOB_WIDGET_URI,
        visibility=["model", "app"],
        name="start_pi_step",
        title=TOOL_TEXT["start_pi_step"][0],
        description=TOOL_TEXT["start_pi_step"][1],
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            openWorldHint=False,
        ),
        meta=WRITE,
    )
    async def start_pi_step(
        goal: str,
        project: str,
        actions: list[dict[str, Any]],
        session_file: str | None = None,
        runtime_job_id: str | None = None,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "start_pi_step",
            {
                "goal": goal,
                "project": project,
                "actions": actions,
                "session_file": session_file,
                "runtime_job_id": runtime_job_id,
            },
        )

    @apps.tool(
        resource_uri=PI_JOB_WIDGET_URI,
        visibility=["model", "app"],
        name="watch_pi_job",
        title="Watch Pi Remote job",
        description=(
            "Attach a no-polling watcher to an existing Pi Remote detached job. "
            "Use this after starting a Pi Remote job. The widget waits for terminal state "
            "and sends a follow-up message into this same conversation so ChatGPT can continue."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta=READ,
    )
    async def watch_pi_job(
        job_id: str,
        pi_remote_dir: str = "/home/ubuntu/src/pi-remote",
        job_root: str | None = None,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "watch_pi_job",
            {
                "job_id": job_id,
                "pi_remote_dir": pi_remote_dir,
                "job_root": job_root,
            },
        )

    @apps.tool(
        resource_uri=LONG_JOB_WIDGET_URI,
        visibility=["model", "app"],
        name="start_long_job",
        title=TOOL_TEXT["start_long_job"][0],
        description=TOOL_TEXT["start_long_job"][1],
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            openWorldHint=True,
        ),
        meta=WRITE,
    )
    async def start_long_job(
        goal: str,
        argv: list[str],
        cwd: str | None = None,
        project: str | None = None,
        device: str | None = None,
        stall_seconds: int = 300,
        heartbeat_seconds: int = 10,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "start_long_job",
            {
                "goal": goal,
                "argv": argv,
                "cwd": cwd,
                "project": project,
                "device": device,
                "stall_seconds": stall_seconds,
                "heartbeat_seconds": heartbeat_seconds,
            },
        )

    @apps.tool(
        resource_uri=LONG_JOB_WIDGET_URI,
        visibility=["model", "app"],
        name="watch_long_job",
        title=TOOL_TEXT["watch_long_job"][0],
        description=TOOL_TEXT["watch_long_job"][1],
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta=READ,
    )
    async def watch_long_job(job_id: str) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "watch_long_job",
            {"job_id": job_id},
        )

    @apps.tool(
        resource_uri=COGNITION_WIDGET_URI,
        visibility=["model", "app"],
        name="watch_agent_cognition",
        title=TOOL_TEXT["watch_agent_cognition"][0],
        description=TOOL_TEXT["watch_agent_cognition"][1],
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta=READ,
    )
    async def watch_agent_cognition(agent_id: str) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "watch_agent_cognition",
            {"agent_id": agent_id},
        )

    @apps.tool(
        resource_uri=CONTROL_PLANE_WIDGET_URI,
        visibility=["model", "app"],
        name="remote_overview",
        title=TOOL_TEXT["remote_overview"][0],
        description=TOOL_TEXT["remote_overview"][1],
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta=READ,
    )
    async def remote_overview(include_resources: bool = False) -> dict[str, Any]:
        user_sub = _principal("remote:read")
        device = relay.device_status(user_sub)
        connector = None
        if device is not None:
            connector = {
                "name": device.get("name"),
                "online": bool(device.get("online")),
                "last_seen": device.get("last_seen"),
                "stale_for_seconds": device.get("stale_for_seconds"),
            }
        if device is None or not device.get("online"):
            return {
                "version": VERSION,
                "generated_at": time.time(),
                "connector": connector or {"online": False},
                "overview": None,
                "error": "paired connector is offline or unavailable",
            }
        overview = await relay.call(
            user_sub,
            "remote_overview",
            {"include_resources": bool(include_resources)},
        )
        return {
            "version": VERSION,
            "generated_at": time.time(),
            "connector": connector,
            "overview": overview,
        }

    server = MCPServer(
        NAME,
        version=VERSION,
        auth=auth_settings,
        extensions=[apps],
        **kwargs,
    )

    if embedded_provider is not None:
        @server.custom_route("/oauth/login", methods=["GET", "POST"], include_in_schema=False)
        async def oauth_login(request: Request):
            request_id = request.query_params.get("request_id", "")
            if request.method == "POST":
                raw = await request.body()
                if len(raw) > 8192:
                    return PlainTextResponse("request too large", status_code=413)
                form = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
                request_id = (form.get("request_id") or [""])[0]
                email = (form.get("email") or [""])[0].strip()
                password = (form.get("password") or [""])[0]
                pending = embedded_provider.store.load_pending_auth(request_id)
                if not pending:
                    return PlainTextResponse("authorization request expired", status_code=400)
                subject = embedded_provider.store.authenticate_user(email, password)
                if subject is None:
                    response = _login_page(
                        request_id,
                        error="Invalid email or password.",
                        email=email,
                    )
                    response.status_code = 401
                    return response
                code, state = embedded_provider.store.consume_pending_auth(request_id, subject)
                return RedirectResponse(
                    _append_query(
                        str(code.redirect_uri),
                        {"code": code.code, "state": state},
                    ),
                    status_code=303,
                    headers={"cache-control": "no-store"},
                )

            if not request_id or embedded_provider.store.load_pending_auth(request_id) is None:
                return PlainTextResponse("authorization request expired", status_code=400)
            return _login_page(request_id)

    @server.tool(name="create_pairing_code", title=TOOL_TEXT["create_pairing_code"][0],
        description=TOOL_TEXT["create_pairing_code"][1], annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, openWorldHint=False), meta=WRITE)
    def create_pairing_code() -> dict[str, Any]:
        """Create a short-lived one-time code that pairs the user's local connector."""
        return relay.store.create_pairing_code(_principal("remote:write"))

    @server.tool(name="device_status", title=TOOL_TEXT["device_status"][0],
        description=TOOL_TEXT["device_status"][1], annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=False), meta=READ)
    def device_status() -> dict[str, Any]:
        """Report paired-device recency and online state without exposing credentials."""
        user = _principal("remote:read")
        device = relay.device_status(user)
        if not device:
            return {"paired": False, "device": None}
        public_device = {
            "name": device.get("name"),
            "last_seen": device.get("last_seen"),
            "online": device.get("online"),
            "stale_for_seconds": device.get("stale_for_seconds"),
        }
        return {"paired": True, "device": public_device}

    @server.tool(name="disconnect_device", title=TOOL_TEXT["disconnect_device"][0],
        description=TOOL_TEXT["disconnect_device"][1], annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, openWorldHint=False), meta=WRITE)
    def disconnect_device(device_id: str | None = None) -> dict[str, Any]:
        """Revoke a paired connector and discard its queued or completed relay tasks."""
        return {"disconnected": relay.store.revoke_device(_principal("remote:write"), device_id)}

    @server.tool(
        name="wait_long_job",
        title=TOOL_TEXT["wait_long_job"][0],
        description=TOOL_TEXT["wait_long_job"][1],
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta={**READ, "ui": {"visibility": ["app"]}},
    )
    async def wait_long_job(
        job_id: str,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "wait_long_job",
            {"job_id": job_id, "timeout_seconds": timeout_seconds},
        )

    @server.tool(
        name="wait_llm_request",
        title=TOOL_TEXT["wait_llm_request"][0],
        description=TOOL_TEXT["wait_llm_request"][1],
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta={**READ, "ui": {"visibility": ["app"]}},
    )
    async def wait_llm_request(
        agent_id: str,
        watcher_id: str,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "wait_llm_request",
            {
                "agent_id": agent_id,
                "watcher_id": watcher_id,
                "timeout_seconds": timeout_seconds,
            },
        )

    @server.tool(
        name="wait_pi_job_completion",
        title="Wait for Pi Remote job completion",
        description=(
            "App-only long wait for a Pi Remote detached job. This is used by the Pi job watcher "
            "so the model does not poll job status."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta={**READ, "ui": {"visibility": ["app"]}},
    )
    async def wait_pi_job_completion(
        job_id: str,
        pi_remote_dir: str = "/home/ubuntu/src/pi-remote",
        job_root: str | None = None,
        timeout_seconds: int = 90,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "wait_pi_job_completion",
            {
                "job_id": job_id,
                "pi_remote_dir": pi_remote_dir,
                "job_root": job_root,
                "timeout_seconds": timeout_seconds,
            },
        )

    @server.tool(
        name="bind_openai_pi_continuation",
        title="Bind Pi job to OpenAI session",
        description=(
            "Internal OpenAI runtime hook helper. Bind a watched Pi Remote job to the current "
            "Codex or ChatGPT Work session so the Stop hook can continue it after Pi finishes."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta=READ,
    )
    async def bind_openai_pi_continuation(
        session_id: str,
        job_id: str,
        pi_remote_dir: str = "/home/ubuntu/src/pi-remote",
        job_root: str | None = None,
    ) -> dict[str, Any]:
        user_sub = _principal("remote:read")
        session_id = str(session_id).strip()
        job_id = str(job_id).strip()
        pi_remote_dir = str(pi_remote_dir).strip()
        if not session_id or len(session_id) > 256:
            raise ValueError("session_id must be a bounded non-empty string")
        if not job_id or len(job_id) > 128:
            raise ValueError("job_id must be a bounded non-empty string")
        if not pi_remote_dir or len(pi_remote_dir) > 4096:
            raise ValueError("pi_remote_dir must be a bounded non-empty path")
        if job_root is not None and len(job_root) > 4096:
            raise ValueError("job_root path is too long")
        connector = await relay.call(
            user_sub,
            "bind_openai_pi_continuation",
            {
                "session_id": session_id,
                "job_id": job_id,
                "pi_remote_dir": pi_remote_dir,
                "job_root": job_root,
            },
        )
        if connector.get("armed") is False:
            relay.store.clear_continuation(user_sub, session_id)
            return connector
        binding = relay.store.bind_continuation(
            user_sub, session_id, job_id, pi_remote_dir, job_root
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"Pi Remote continuation is armed for job {binding['job_id']} in this OpenAI session."
                ),
            },
            **({"runtimeJobId": connector.get("runtimeJobId")} if connector.get("runtimeJobId") else {}),
        }

    @server.tool(
        name="continue_openai_pi_job",
        title="Continue OpenAI session after Pi job",
        description=(
            "Internal OpenAI Stop-hook helper. Wait for the Pi Remote job bound to this session "
            "and return a Codex continuation decision when the job finishes."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta=READ,
    )
    async def continue_openai_pi_job(
        session_id: str,
        timeout_seconds: int = 540,
        interrupted: bool = False,
        stop_hook_active: bool = False,
    ) -> dict[str, Any]:
        user_sub = _principal("remote:read")
        session_id = str(session_id).strip()
        if not session_id or len(session_id) > 256:
            raise ValueError("session_id must be a bounded non-empty string")
        binding = relay.store.continuation_for_user(user_sub, session_id)
        if binding is None:
            return {"continue": True}

        if interrupted:
            # User interrupt has highest priority. Remove the hosted continuation
            # before any remote cleanup so a slow/offline Connector cannot cause
            # the stopped turn to be resurrected.
            relay.store.clear_continuation(user_sub, session_id)

        result = await relay.call(
            user_sub,
            "continue_openai_pi_job",
            {
                "session_id": session_id,
                "timeout_seconds": min(30, max(1, int(timeout_seconds))),
                "interrupted": bool(interrupted),
                "stop_hook_active": bool(stop_hook_active),
            },
        )
        if interrupted or stop_hook_active or result.get("decision") == "block" or result.get("continue") is False:
            relay.store.clear_continuation(user_sub, session_id)
        return result

    def expose(name: str, read_only: bool, open_world: bool, destructive: bool):
        meta = READ if read_only else WRITE
        annotations = ToolAnnotations(
            readOnlyHint=read_only,
            openWorldHint=open_world,
            destructiveHint=destructive,
            idempotentHint=False if destructive else None,
        )
        title, description = TOOL_TEXT[name]
        return server.tool(
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            meta=meta,
        )

    @expose("connection_status", True, False, False)
    async def connection_status(device: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "connection_status", {"device": device})

    @expose("capabilities", True, False, False)
    async def capabilities() -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "capabilities", {})

    @expose("list_projects", True, False, False)
    async def list_projects() -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "list_projects", {})

    @expose("list_devices", True, False, False)
    async def list_devices(include_resources: bool = False) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "list_devices",
            {"include_resources": bool(include_resources)},
        )

    @expose("read_file", True, False, False)
    async def read_file(path: str, project: str | None = None, device: str | None = None, offset: int = 0,
                        max_bytes: int = 131072) -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "read_file", {"path": path, "project": project, "device": device, "offset": offset, "max_bytes": max_bytes})

    @expose("list_dir", True, False, False)
    async def list_dir(path: str | None = None, project: str | None = None, device: str | None = None,
                       max_entries: int = 200) -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "list_dir", {"path": path, "project": project, "device": device, "max_entries": max_entries})

    @expose("logs", True, False, False)
    async def logs(unit: str | None = None, project: str | None = None, device: str | None = None, lines: int = 200,
                   since_minutes: int = 60) -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "logs", {"unit": unit, "project": project, "device": device, "lines": lines, "since_minutes": since_minutes})

    @expose("write_file", False, False, True)
    async def write_file(path: str, content: str, project: str | None = None, device: str | None = None,
                         mode: str = "replace", expected_sha256: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "write_file", {"path": path, "content": content, "project": project, "device": device, "mode": mode, "expected_sha256": expected_sha256})

    @expose("git", False, True, True)
    async def git(args: list[str], repo_path: str | None = None, project: str | None = None, device: str | None = None,
                  timeout_seconds: int = 30) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "git", {"args": args, "repo_path": repo_path, "project": project, "device": device, "timeout_seconds": timeout_seconds})

    @expose("exec", False, True, True)
    async def exec(argv: list[str], cwd: str | None = None, project: str | None = None, device: str | None = None,
                   timeout_seconds: int = 30, detached: bool = False,
                   heartbeat_seconds: int = 10, stall_seconds: int = 300) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "exec",
            {
                "argv": argv,
                "cwd": cwd,
                "project": project,
                "device": device,
                "timeout_seconds": timeout_seconds,
                "detached": detached,
                "heartbeat_seconds": heartbeat_seconds,
                "stall_seconds": stall_seconds,
            },
        )

    @expose("list_credentials", True, False, False)
    async def list_credentials() -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "list_credentials", {})

    @expose("lease_credential", False, False, False)
    async def lease_credential(
        handle: str,
        capability: str,
        project: str | None = None,
        device: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "lease_credential",
            {
                "handle": handle,
                "capability": capability,
                "project": project,
                "device": device,
                "ttl_seconds": ttl_seconds,
            },
        )

    @expose("list_credential_leases", True, False, False)
    async def list_credential_leases(active_only: bool = True) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "list_credential_leases",
            {"active_only": active_only},
        )

    @expose("revoke_credential_lease", False, False, True)
    async def revoke_credential_lease(lease_id: str) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "revoke_credential_lease",
            {"lease_id": lease_id},
        )

    @expose("github_identity", False, True, False)
    async def github_identity(
        lease_id: str,
        project: str | None = None,
        device: str | None = None,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "github_identity",
            {"lease_id": lease_id, "project": project, "device": device},
        )

    @expose("create_job", False, False, False)
    async def create_job(
        goal: str,
        project: str | None = None,
        device: str | None = None,
        pi_job_id: str | None = None,
        pi_remote_dir: str = "/home/ubuntu/src/pi-remote",
        pi_job_root: str | None = None,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "create_job",
            {
                "goal": goal,
                "project": project,
                "device": device,
                "pi_job_id": pi_job_id,
                "pi_remote_dir": pi_remote_dir,
                "pi_job_root": pi_job_root,
            },
        )

    @expose("get_job", True, False, False)
    async def get_job(job_id: str) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "get_job",
            {"job_id": job_id},
        )

    @expose("list_jobs", True, False, False)
    async def list_jobs(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "list_jobs",
            {"status": status, "limit": limit},
        )

    @expose("checkpoint_job", False, False, False)
    async def checkpoint_job(
        job_id: str,
        summary: str,
        current_step: str | None = None,
        next_action: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "checkpoint_job",
            {
                "job_id": job_id,
                "summary": summary,
                "current_step": current_step,
                "next_action": next_action,
                "status": status,
            },
        )

    @expose("submit_llm_request", False, False, False)
    async def submit_llm_request(
        agent_id: str,
        purpose: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        timeout_seconds: int = 300,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "submit_llm_request",
            {
                "agent_id": agent_id,
                "purpose": purpose,
                "messages": messages,
                "tools": tools,
                "response_format": response_format,
                "options": options,
                "metadata": metadata,
                "timeout_seconds": timeout_seconds,
                "request_id": request_id,
            },
        )

    @expose("claim_llm_request", False, False, False)
    async def claim_llm_request(
        request_id: str,
        watcher_id: str,
        claim_seconds: int = 120,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "claim_llm_request",
            {
                "request_id": request_id,
                "watcher_id": watcher_id,
                "claim_seconds": claim_seconds,
            },
        )

    @expose("get_llm_request", True, False, False)
    async def get_llm_request(request_id: str) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "get_llm_request",
            {"request_id": request_id},
        )

    @expose("get_llm_request_status", True, False, False)
    async def get_llm_request_status(request_id: str) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "get_llm_request_status",
            {"request_id": request_id},
        )

    @expose("complete_llm_request", False, False, False)
    async def complete_llm_request(
        request_id: str,
        claim_token: str,
        response_text: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "complete_llm_request",
            {
                "request_id": request_id,
                "claim_token": claim_token,
                "response_text": response_text,
                "tool_calls": tool_calls,
                "model": model,
                "session_id": session_id,
            },
        )

    @expose("get_long_job", True, False, False)
    async def get_long_job(job_id: str) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:read"),
            "get_long_job",
            {"job_id": job_id},
        )

    @expose("cancel_long_job", False, False, True)
    async def cancel_long_job(job_id: str) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "cancel_long_job",
            {"job_id": job_id},
        )

    @expose("list_exec_permissions", True, False, False)
    async def list_exec_permissions() -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "list_exec_permissions", {})

    @expose("approve_exec_permission", False, False, True)
    async def approve_exec_permission(
        request_id: str,
        scope: str = "host",
        grant_mode: str = "exact",
    ) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "approve_exec_permission",
            {"request_id": request_id, "scope": scope, "grant_mode": grant_mode},
        )

    @expose("deny_exec_permission", False, False, True)
    async def deny_exec_permission(request_id: str) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "deny_exec_permission",
            {"request_id": request_id},
        )

    @expose("revoke_exec_permission", False, False, True)
    async def revoke_exec_permission(permission_id: str) -> dict[str, Any]:
        return await relay.call(
            _principal("remote:write"),
            "revoke_exec_permission",
            {"permission_id": permission_id},
        )

    @expose("process", False, False, True)
    async def process(action: str, pid: int | None = None, contains: str | None = None, device: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "process", {"action": action, "pid": pid, "contains": contains, "device": device})

    @expose("systemd", False, False, True)
    async def systemd(action: str, unit: str | None = None, project: str | None = None, device: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "systemd", {"action": action, "unit": unit, "project": project, "device": device})

    @expose("apply_patch", False, False, True)
    async def apply_patch(path: str, patch: str, expected_sha256: str | None = None,
                          project: str | None = None, device: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "apply_patch", {"path": path, "patch": patch, "expected_sha256": expected_sha256, "project": project, "device": device})

    @expose("diagnostics", True, False, False)
    async def diagnostics() -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "diagnostics", {})

    return server


def _device_token(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer "):
        raise PermissionError("device bearer token required")
    return value[7:]


def create_app(store: RelayStore | None = None) -> Starlette:
    store = store or RelayStore(os.environ.get("LIVINGRUNTIME_RELAY_DB", "state/relay.sqlite3"))
    relay = Relay(
        store,
        float(os.environ.get("LIVINGRUNTIME_RELAY_TIMEOUT", "45")),
        float(os.environ.get("LIVINGRUNTIME_RELAY_RECONNECT_GRACE", "8")),
        float(os.environ.get("LIVINGRUNTIME_RELAY_DEVICE_STALE_AFTER", "35")),
    )
    pair_limiter = PairRateLimiter(
        int(os.environ.get("LIVINGRUNTIME_RELAY_PAIR_LIMIT", "10")),
        float(os.environ.get("LIVINGRUNTIME_RELAY_PAIR_WINDOW", "60")),
    )

    embedded_provider: EmbeddedOAuthProvider | None = None
    if _auth_mode() == "embedded":
        auth_db = os.environ.get("LIVINGRUNTIME_AUTH_DB", store.path)
        auth_store = EmbeddedAuthStore(auth_db)
        bootstrap_email = os.environ.get("LIVINGRUNTIME_AUTH_BOOTSTRAP_EMAIL", "").strip()
        bootstrap_password = os.environ.get("LIVINGRUNTIME_AUTH_BOOTSTRAP_PASSWORD", "")
        if bootstrap_email and bootstrap_password:
            try:
                auth_store.create_user(bootstrap_email, bootstrap_password, email_verified=True)
            except ValueError as exc:
                if str(exc) != "account already exists":
                    raise
        embedded_provider = EmbeddedOAuthProvider(
            auth_store,
            os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/"),
        )

    issuer_parts = urlsplit(os.environ["LIVINGRUNTIME_RELAY_ISSUER"])
    public_host = issuer_parts.hostname or "127.0.0.1"
    public_origin = f"{issuer_parts.scheme}://{issuer_parts.netloc}"
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            public_host,
            f"{public_host}:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            "[::1]",
            "[::1]:*",
        ],
        allowed_origins=[
            public_origin,
            f"{issuer_parts.scheme}://{public_host}:*",
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    )
    mcp_app = create_mcp(relay, embedded_provider).streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=public_host,
        transport_security=transport_security,
    )
    install_script_base = os.environ.get(
        "LIVINGRUNTIME_INSTALL_SCRIPT_BASE",
        "https://raw.githubusercontent.com/jboone1989/livingruntime-remote/main/"
        "plugin/scripts",
    ).rstrip("/")
    release_base = os.environ.get(
        "LIVINGRUNTIME_CONNECTOR_RELEASE_BASE",
        "https://github.com/jboone1989/livingruntime-remote/releases/latest/download",
    ).rstrip("/")

    async def health(_: Request):
        return JSONResponse({
            "ok": True,
            "service": NAME,
            "version": VERSION,
            "auth_mode": _auth_mode(),
        })

    async def challenge(_: Request):
        return PlainTextResponse(os.environ.get("OPENAI_APPS_CHALLENGE", ""))

    def public_page(title: str, body_html: str) -> HTMLResponse:
        body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · LivingRuntime Remote</title>
<style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#111;color:#eee;margin:0}}
main{{max-width:760px;margin:7vh auto;padding:28px}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:16px;padding:24px;margin:18px 0}}
code,pre{{background:#0d0d0d;border-radius:8px;padding:10px;overflow:auto}}
pre{{white-space:pre-wrap}}
p,li{{color:#bbb;line-height:1.55}}
a{{color:#b9ccff}}
nav{{margin-bottom:28px}}
nav a{{margin-right:18px}}
</style>
</head>
<body><main>
<nav><a href="/install">Install</a><a href="/demo">Demo</a><a href="/support">Support</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></nav>
{body_html}
</main></body></html>"""
        return HTMLResponse(
            body,
            headers={
                "cache-control": "public, max-age=300",
                "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
                "x-content-type-options": "nosniff",
            },
        )

    async def home(_: Request):
        return public_page(
            "LivingRuntime Remote",
            """<h1>LivingRuntime Remote</h1>
<p><strong>Let AI agents keep working on machines you control after the conversation stops.</strong></p>
<p>Durable jobs, checkpoints, completion-driven continuation, multi-host routing, and bounded approvals over an OAuth-protected MCP bridge.</p>
<div class="card"><h2>No more status babysitting</h2>
<p>Start long-running work on your own machine, let the runtime persist the job, and let a watcher hand the terminal result back to a continuation-capable controller instead of repeatedly asking a human to check and type &quot;continue&quot;.</p></div>
<div class="card"><p><a href="/install">Install the Connector</a></p>
<p><a href="/demo">See the demo</a></p>
<p><a href="https://github.com/jboone1989/livingruntime-remote">View the MIT-licensed source on GitHub</a></p>
<p><a href="/support">Support and documentation</a></p></div>""",
        )

    async def install_page(_: Request):
        origin = html.escape(public_origin)
        return public_page(
            "Install",
            f"""<h1>LivingRuntime Remote Connector</h1>
<p>Install the small connector on a computer that already has key-based SSH access to your Linux host. Python is not required.</p>
<div class="card">
<h2>Windows</h2>
<pre>irm {origin}/install.ps1 | iex</pre>
</div>
<div class="card">
<h2>Linux / macOS</h2>
<pre>curl -fsSL {origin}/install.sh | sh</pre>
</div>
<p>The installer asks for the pairing code shown in ChatGPT, your SSH host, and the workspace root ChatGPT may access. It verifies SSH before consuming the pairing code.</p>
""",
        )

    async def privacy_page(_: Request):
        return public_page(
            "Privacy",
            """<h1>Privacy Policy</h1>
<p>LivingRuntime Remote connects ChatGPT-compatible clients to machines and repositories that the user is authorized to control.</p>
<div class="card"><h2>Data handled</h2>
<p>The public relay may process OAuth account identity, OAuth client metadata, hashed token values, pairing/device metadata, and the bounded tool inputs and outputs needed to route a request to the paired Connector. The Connector may also maintain non-secret SSH host aliases, allowed roots, project aliases, allowlisted service names, and local audit records.</p></div>
<div class="card"><h2>Credentials</h2>
<p>LivingRuntime Remote does not store SSH passwords or SSH private keys on the public relay. SSH credentials remain in the user's existing SSH configuration on the Connector machine. OAuth passwords are stored only as salted scrypt hashes, and raw OAuth bearer, refresh, and device tokens are not stored in relay databases.</p></div>
<div class="card"><h2>Sharing and retention</h2>
<p>When Remote is used with ChatGPT or another compatible client, tool arguments and results travel through that client's MCP request path and the LivingRuntime Remote relay. Successful relay task results are consumed after delivery; stale task records are eligible for cleanup after 24 hours. OAuth account and paired-device records remain until revoked or removed. Local configuration and audit logs remain on the user's machines until deleted.</p></div>
<p>ChatGPT-side retention follows the user's OpenAI account or workspace settings. Do not place secrets in tool arguments or file contents that you do not want transmitted through the connected client and relay.</p>""",
        )

    async def terms_page(_: Request):
        return public_page(
            "Terms",
            """<h1>Terms of Service</h1>
<p>LivingRuntime Remote is provided for operating machines, repositories, and services that you are authorized to access.</p>
<ol><li>Use Remote only with systems you are authorized to administer.</li>
<li>Remote is a bounded development execution channel, not a hostile-code sandbox.</li>
<li>Do not expose loopback MCP ports directly to the public internet.</li>
<li>ChatGPT plan, developer-mode, and marketplace permissions are controlled by OpenAI.</li></ol>
<p>The software is provided as-is, without warranty, subject to the repository license.</p>""",
        )

    async def support_page(_: Request):
        return public_page(
            "Support",
            """<h1>Support</h1>
<p>For setup problems, start by checking the paired Connector and calling <code>connection_status</code> and <code>capabilities</code> in ChatGPT.</p>
<div class="card"><h2>Expected healthy state</h2>
<p><code>connection_status.ok = true</code> and <code>capabilities.healthy = true</code> with an empty <code>missing</code> list.</p></div>
<div class="card"><h2>Installation</h2><p><a href="/install">Open the Connector installation page</a>.</p></div>
<p>LivingRuntime Remote source and issue tracking are published from the LivingRuntime Remote repository.</p>""",
        )

    async def demo_page(_: Request):
        return public_page(
            "Demo",
            """<h1>LivingRuntime Remote Demo</h1>
<p>The core product loop is durable remote work: start a real task on a machine you control, persist its state, observe completion, and continue from the result without a human status-polling loop.</p>
<div class="card"><h2>Review recording</h2>
<p>The current MP4 demonstrates the production OAuth connection and isolated review workflow for connection status, project listing, bounded file access, Git status, and bounded writes.</p>
<p><a href="/demo.mp4">Open the MP4 recording</a></p></div>
<div class="card"><h2>Durable continuation walkthrough</h2>
<p>See the public reproducible demo guide for the long-running job, watcher, checkpoint, and continuation flow.</p>
<p><a href="https://github.com/jboone1989/livingruntime-remote/blob/main/docs/DEMO.md">Open the durable-work demo guide</a></p></div>""",
        )

    async def demo_recording(_: Request):
        demo_path = Path(os.environ.get(
            "LIVINGRUNTIME_DEMO_RECORDING_PATH",
            "/var/lib/livingruntime-remote-relay/livingruntime-remote-demo.mp4",
        ))
        if not demo_path.exists():
            return PlainTextResponse("demo recording unavailable", status_code=404)
        return FileResponse(
            demo_path,
            media_type="video/mp4",
            headers={"cache-control": "public, max-age=3600"},
        )

    async def install_ps1(_: Request):
        return RedirectResponse(f"{install_script_base}/install-connector.ps1", status_code=302)

    async def install_sh(_: Request):
        return RedirectResponse(f"{install_script_base}/install-connector.sh", status_code=302)

    async def download_connector(request: Request):
        assets = {
            "windows-amd64": "livingruntime-remote-connector-windows-amd64.exe",
            "linux-amd64": "livingruntime-remote-connector-linux-amd64",
            "linux-arm64": "livingruntime-remote-connector-linux-arm64",
            "macos-amd64": "livingruntime-remote-connector-macos-amd64",
            "macos-arm64": "livingruntime-remote-connector-macos-arm64",
        }
        asset = assets.get(request.path_params["target"])
        if asset is None:
            return PlainTextResponse("unsupported connector target", status_code=404)
        return RedirectResponse(f"{release_base}/{asset}", status_code=302)

    async def pair(request: Request):
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        client_key = forwarded or (request.client.host if request.client else "unknown")
        if not pair_limiter.allow(client_key):
            return JSONResponse(
                {"error": "too many pairing attempts"},
                status_code=429,
                headers={"retry-after": str(int(pair_limiter.window_seconds))},
            )
        try:
            body = await request.json()
            result = store.pair_device(str(body.get("code", "")), str(body.get("name", "")))
            return JSONResponse(result)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)

    async def poll(request: Request):
        try:
            device = store.authenticate_device(_device_token(request))
            wait = min(25.0, max(0.0, float(request.query_params.get("wait", "20"))))
            device_id = device["device_id"]

            relay.arm_task_wait(device_id)
            task = store.claim(device_id)
            if task:
                return JSONResponse({"task": task})
            if wait <= 0:
                return JSONResponse({"task": None})

            if not await relay.wait_for_task(device_id, wait):
                return JSONResponse({"task": None})
            task = store.claim(device_id)
            return JSONResponse({"task": task})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)

    async def complete(request: Request):
        try:
            device = store.authenticate_device(_device_token(request))
            body = await request.json()
            task_id = str(body["task_id"])
            store.complete(device["device_id"], task_id, dict(body["result"]))
            relay.notify_result(task_id)
            return JSONResponse({"ok": True})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def embedded_oauth_metadata(_: Request):
        issuer = os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/")
        payload = {
            "issuer": issuer,
            "authorization_endpoint": issuer + "/authorize",
            "token_endpoint": issuer + "/token",
            "registration_endpoint": issuer + "/register",
            "revocation_endpoint": issuer + "/revoke",
            "userinfo_endpoint": issuer + "/userinfo",
            "scopes_supported": SUPPORTED_SCOPES,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "subject_types_supported": ["public"],
        }
        docs = os.environ.get("LIVINGRUNTIME_RELAY_DOCS_URL")
        if docs:
            payload["service_documentation"] = docs
        return JSONResponse(payload, headers={"cache-control": "no-store"})

    async def embedded_resource_metadata(_: Request):
        return JSONResponse({
            "resource": os.environ["LIVINGRUNTIME_RELAY_RESOURCE_URL"],
            "authorization_servers": [os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/")],
            "scopes_supported": SUPPORTED_SCOPES,
            "bearer_methods_supported": ["header"],
            "resource_documentation": os.environ.get(
                "LIVINGRUNTIME_RELAY_DOCS_URL",
                os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/") + "/support",
            ),
            "resource_policy_uri": os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/") + "/privacy",
            "resource_tos_uri": os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/") + "/terms",
        }, headers={"cache-control": "no-store"})

    async def embedded_userinfo(request: Request):
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"www-authenticate": 'Bearer error="invalid_token"'},
            )
        if embedded_provider is None:
            return JSONResponse({"error": "server_error"}, status_code=500)
        token = embedded_provider.store.load_access_token(
            authorization.split(" ", 1)[1].strip()
        )
        if token is None:
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"www-authenticate": 'Bearer error="invalid_token"'},
            )
        if "openid" not in token.scopes:
            return JSONResponse(
                {"error": "insufficient_scope"},
                status_code=403,
                headers={"www-authenticate": 'Bearer error="insufficient_scope", scope="openid email"'},
            )
        info = embedded_provider.store.userinfo(token.subject)
        if info is None:
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        if "email" not in token.scopes:
            info = {"sub": info["sub"]}
        return JSONResponse(info, headers={"cache-control": "no-store"})

    routes = [
        Route("/", home),
        Route("/healthz", health),
        Route("/install", install_page),
        Route("/install.ps1", install_ps1),
        Route("/install.sh", install_sh),
        Route("/download/{target}", download_connector),
        Route("/privacy", privacy_page),
        Route("/terms", terms_page),
        Route("/support", support_page),
        Route("/demo", demo_page),
        Route("/demo.mp4", demo_recording),
        Route("/.well-known/openai-apps-challenge", challenge),
    ]
    if _auth_mode() == "embedded":
        resource_path = urlsplit(os.environ["LIVINGRUNTIME_RELAY_RESOURCE_URL"]).path or ""
        routes.extend([
            Route("/.well-known/oauth-authorization-server", embedded_oauth_metadata),
            Route("/.well-known/openid-configuration", embedded_oauth_metadata),
            Route("/.well-known/oauth-protected-resource" + resource_path, embedded_resource_metadata),
            Route("/userinfo", embedded_userinfo),
        ])
    routes.extend([
        Route("/device/pair", pair, methods=["POST"]),
        Route("/device/poll", poll, methods=["POST"]),
        Route("/device/result", complete, methods=["POST"]),
        Mount("/", app=mcp_app),
    ])
    return Starlette(routes=routes, lifespan=mcp_app.router.lifespan_context)


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("LIVINGRUNTIME_RELAY_BIND", "127.0.0.1"),
                port=int(os.environ.get("LIVINGRUNTIME_RELAY_PORT", "8770")))
