import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

const gateway = path.resolve("plugin/scripts/pi_tool.mjs");
function invoke(cwd, tool, params) {
  const cp = spawnSync(process.execPath, [gateway], {
    cwd, input: JSON.stringify({tool, params}), encoding: "utf8", timeout: 12000,
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
  } finally {
    fs.rmSync(cwd, {recursive:true, force:true});
  }
});
