import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";

const gateway = path.resolve("plugin/scripts/pi_tool.mjs");
function invoke(cwd, tool, params, extraEnv={}) {
  const cp = spawnSync(process.execPath, [gateway], {
    cwd, input: JSON.stringify({tool, params}), encoding: "utf8", timeout: 12000,
    env: {...process.env, ...extraEnv},
  });
  assert.ok(cp.stdout, cp.stderr);
  return JSON.parse(cp.stdout);
}
test("real Pi SDK read / edit / write / grep / find", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "lr-pi-sdk-"));
  try {
    assert.equal(invoke(cwd, "write", {path:"sample.txt", content:"alpha\n"}).ok, true);
    const read = invoke(cwd, "read", {path:"sample.txt"});
    assert.equal(read.ok, true, JSON.stringify(read));
    assert.match(read.text, /alpha/);
    const edit = invoke(cwd, "edit", {path:"sample.txt", edits:[{oldText:"alpha", newText:"beta"}]});
    assert.equal(edit.ok, true, JSON.stringify(edit));
    assert.equal(fs.readFileSync(path.join(cwd, "sample.txt"), "utf8"), "beta\n");
    // Regression for reviewer finding: Unicode-space class must not match ASCII digits/uppercase.
    for (const name of ["README.md", "File1.txt", "My Project 2026.txt"]) {
      const payload = "Pi_SDK_FILENAME_OK";
      const written = invoke(cwd, "write", {path:name, content:payload});
      assert.equal(written.ok, true, JSON.stringify(written));
      assert.equal(fs.readFileSync(path.join(cwd, name), "utf8"), payload);
      const looked = invoke(cwd, "read", {path:name});
      assert.equal(looked.ok, true, JSON.stringify(looked));
      assert.match(looked.text, /Pi_SDK_FILENAME_OK/);
    }

    const grep = invoke(cwd, "grep", {pattern:"beta", path:"."});
    assert.equal(grep.ok, true, JSON.stringify(grep));
    const found = invoke(cwd, "find", {pattern:"*.txt", path:"."});
    assert.equal(found.ok, true, JSON.stringify(found));
  } finally {
    fs.rmSync(cwd, {recursive:true, force:true});
  }
});
test("Pi SDK gateway denies workspace escape, including symlinks", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "lr-pi-root-"));
  try {
    assert.equal(invoke(cwd, "read", {path:"../outside.txt"}).ok, false);
    fs.symlinkSync("/etc", path.join(cwd, "outside"));
    assert.equal(invoke(cwd, "read", {path:"outside/passwd"}).ok, false);
    // A broken symlink outside the project must not be treated as an absent file.
    const outsideMissing = path.join(os.tmpdir(), path.basename(cwd) + "-outside-missing.txt");
    assert.equal(fs.existsSync(outsideMissing), false);
    fs.symlinkSync(outsideMissing, path.join(cwd, "dangling"));
    assert.equal(invoke(cwd, "write", {path:"dangling", content:"ESCAPE"}).ok, false);
    assert.equal(fs.existsSync(outsideMissing), false);
  } finally {
    fs.rmSync(cwd, {recursive:true, force:true});
  }
});

test("native Pi path expansion cannot escape through @, ~ or file://", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lr-pi-alias-"));
  const cwd = path.join(root, "workspace");
  fs.mkdirSync(cwd);
  const canary = path.join(root, "canary.txt");
  fs.writeFileSync(canary, "PRIVATE_PI_ALIAS_CANARY");
  try {
    const env = {HOME:root};
    for (const alias of ["@" + canary, "~/canary.txt", pathToFileURL(canary).href]) {
      const read = invoke(cwd, "read", {path:alias}, env);
      assert.ok(!JSON.stringify(read).includes("PRIVATE_PI_ALIAS_CANARY"), alias);
      const write = invoke(cwd, "write", {path:alias, content:"ESCAPED"}, env);
      assert.ok(!JSON.stringify(write).includes("PRIVATE_PI_ALIAS_CANARY"), alias);
      assert.equal(fs.readFileSync(canary, "utf8"), "PRIVATE_PI_ALIAS_CANARY", alias);
    }
  } finally {
    fs.rmSync(root, {recursive:true, force:true});
  }
});
