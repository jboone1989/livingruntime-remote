import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const workflow = readFileSync(new URL('../workflows/eventgraph-glm-review.yml', import.meta.url), 'utf8');
const script = workflow.slice(workflow.lastIndexOf('            const {owner, repo} = context.repo;')).replace(/^            /gm, '');
const execute = new (Object.getPrototypeOf(async function(){}).constructor)('github','core','context','process','require',script);
const head = 'a'.repeat(40), base = 'b'.repeat(40);
async function publish({ moved = false, stale = false, outcome = 'success' } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'glm-provenance-'));
  const statuses = [];
  writeFileSync(join(dir, 'pr-review.diff'), 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1 @@\n+new\n');
  try {
    await execute({rest: {
      pulls: {get: async () => ({data:{state:'open',head:{sha:stale?'c'.repeat(40):head},base:{ref:'develop'}}})},
      repos: {getBranch: async () => ({data:{commit:{sha:moved?'c'.repeat(40):base}}}), createCommitStatus: async s => statuses.push(s)},
      issues: {createComment: async () => {}}
    }}, {info(){},error(){},setFailed(){}}, {repo:{owner:'owner',repo:'repo'}}, {env: {
      CHECK_SHA:head,PR_NUMBER:'42',BASE_REF:'develop',BASE_SHA:base,
      REVIEW_OUTCOME:outcome,CANDIDATE_OUTCOME:'success',NEEDS_VERIFICATION:'false',
      REVIEW_JSON:'{"findings":[]}',GITHUB_WORKSPACE:dir
    }}, createRequire(import.meta.url));
    return statuses;
  } finally {rmSync(dir,{recursive:true,force:true});}
}
test('successful review identifies the exact target base for safe reuse', async () => {
  const [s] = await publish();
  assert.equal(s.state,'success');
  assert.ok(s.description.startsWith(`base=develop@${base}; `));
  assert.ok(s.description.length <= 140);
});
test('base movement cannot publish reusable success', async () => {
  const [s] = await publish({moved:true});
  assert.equal(s.state,'error');
});
test('model failure cannot publish reusable success', async () => {
  const [s] = await publish({outcome:'failure'});
  assert.equal(s.state,'error');
});
test('superseded PR head receives no review status', async () => {
  assert.deepEqual(await publish({stale:true}),[]);
});
