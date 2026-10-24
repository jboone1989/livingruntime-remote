// Direct Pi SDK tool execution, without an agent/model call.
import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import {
  createReadTool, createEditTool, createWriteTool,
  createGrepTool, createFindTool, createLsTool,
} from "@earendil-works/pi-coding-agent";

const factories = {
  read: createReadTool, edit: createEditTool, write: createWriteTool,
  grep: createGrepTool, find: createFindTool, ls: createLsTool,
};

function checkPath(root, value) {
  const target = path.resolve(root, value);
  const relative = path.relative(root, target);
  if (relative === ".." || relative.startsWith(".." + path.sep) || path.isAbsolute(relative))
    throw new Error("tool path outside project workspace");
  let current = target;
  const suffix = [];
  while (!fs.existsSync(current)) {
    suffix.unshift(path.basename(current));
    const parent = path.dirname(current);
    if (parent === current) throw new Error("tool path has no existing parent");
    current = parent;
  }
  const resolved = path.resolve(fs.realpathSync(current), ...suffix);
  const realRel = path.relative(root, resolved);
  if (realRel === ".." || realRel.startsWith(".." + path.sep) || path.isAbsolute(realRel))
    throw new Error("tool path resolves outside project workspace");
}

try {
  const request = JSON.parse(fs.readFileSync(0, "utf8"));
  const factory = factories[request.tool];
  if (!factory) throw new Error("unsupported Pi SDK tool");
  if (!request.params || typeof request.params !== "object" || Array.isArray(request.params))
    throw new Error("Pi SDK parameters must be an object");
  const cwd = fs.realpathSync(process.cwd());
  for (const field of ["path", "file_path", "cwd"]) {
    if (typeof request.params[field] === "string") checkPath(cwd, request.params[field]);
  }
  const result = await factory(cwd).execute(randomUUID(), request.params, undefined, undefined);
  const text = (result?.content ?? [])
    .filter(v => v?.type === "text").map(v => v.text).join("\n");
  process.stdout.write(JSON.stringify({
    ok: true, tool: request.tool, text: text.slice(0, 131072),
    isError: Boolean(result?.isError),
  }));
} catch (error) {
  process.stdout.write(JSON.stringify({ok: false, error: String(error?.message ?? error).slice(0, 700)}));
  process.exitCode = 1;
}
