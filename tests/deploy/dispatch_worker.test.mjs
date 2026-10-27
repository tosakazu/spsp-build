// deploy/ci/dispatch-worker/worker.js: 通常の取得が軽量の取得より優先されること (GitHub API は fetch の差し替えで見る)
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { cancelPreempted, isJustBeforeFull } from "../../deploy/ci/dispatch-worker/worker.js";

function fakeGitHub(runs) {
  const calls = [];
  globalThis.fetch = async (url, opts = {}) => {
    calls.push(`${opts.method || "GET"} ${url.replace("https://api.github.com/repos/", "")}`);
    if (url.includes("/runs?")) return new Response(JSON.stringify({ workflow_runs: runs }), { status: 200 });
    if (url.endsWith("/cancel")) return new Response("", { status: 202 });
    if (url.endsWith("/dispatches")) return new Response(null, { status: 204 });
    return new Response("", { status: 404 });
  };
  return calls;
}

const QUICK = { id: 1, status: "in_progress", display_title: "download-japan quick", created_at: new Date().toISOString() };
const FULL = { id: 2, status: "in_progress", display_title: "download-japan", created_at: new Date().toISOString() };
const DONE = { id: 3, status: "completed", display_title: "download-japan quick", created_at: new Date().toISOString() };

test("cancelPreempted cancels only unfinished quick runs", async () => {
  const calls = fakeGitHub([QUICK, FULL, DONE]);
  const job = { repo: "o/r", workflow: "download-japan.yml", preemptTitle: "download-japan quick" };
  assert.equal(await cancelPreempted(job, "t"), 1);
  assert.deepEqual(calls.filter((c) => c.startsWith("POST")), ["POST o/r/actions/runs/1/cancel"]);
});

test("full run: cancel the quick run, then dispatch", async () => {
  const calls = fakeGitHub([QUICK]);
  await worker.scheduled({ cron: "3 */3 * * *", scheduledTime: Date.UTC(2026, 9, 27, 3, 3) }, { JP_ACTIONS_DISPATCH_TOKEN: "t" });
  const posts = calls.filter((c) => c.startsWith("POST"));
  assert.deepEqual(posts, ["POST tosakazu/smash_database/actions/runs/1/cancel",
                           "POST tosakazu/smash_database/actions/workflows/download-japan.yml/dispatches"]);
});

test("quick run is not sent just before the full run", async () => {
  assert.equal(isJustBeforeFull(Date.UTC(2026, 9, 27, 3, 0)), true);
  assert.equal(isJustBeforeFull(Date.UTC(2026, 9, 27, 3, 15)), false);
  assert.equal(isJustBeforeFull(Date.UTC(2026, 9, 27, 4, 0)), false);
  const calls = fakeGitHub([]);
  await worker.scheduled({ cron: "*/15 * * * *", scheduledTime: Date.UTC(2026, 9, 27, 3, 0) }, { JP_ACTIONS_DISPATCH_TOKEN: "t" });
  assert.deepEqual(calls, []);
  await worker.scheduled({ cron: "*/15 * * * *", scheduledTime: Date.UTC(2026, 9, 27, 3, 15) }, { JP_ACTIONS_DISPATCH_TOKEN: "t" });
  assert.ok(calls.some((c) => c.endsWith("/dispatches")));
});
