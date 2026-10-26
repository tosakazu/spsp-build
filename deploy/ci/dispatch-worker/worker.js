// spsp-actions-dispatch: 決まった時刻に GitHub Actions を起動する (workflow_dispatch を送るだけ、計算はしない)。
//
// GitHub の schedule が 2026-09 時点でこのアカウントでは起動しないため、起動は外から送る。ConoHa の cron
// (deploy/ci/trigger.sh) から移した。HTTP の入口は持たない (wrangler.toml で workers_dev=false・routes なし)。
//
// 鍵: Worker の secret JP_ACTIONS_DISPATCH_TOKEN (GitHub の fine-grained token。対象は tosakazu/smash_database と
// tosakazu/spsp-build だけ、権限は Actions: Read and write だけ)。ログに出さない。
//
// cron は UTC。日本時間との対応:
//   "3 */3 * * *"          → 日本の大会データ取得 (3 時間ごと、JST も同じ 0,3,6,...時 3 分)
//   "*/15 * * * *"         → 日本の大会データ取得の軽量モード (quick、24 時間以内に終わった未取り込みの大会だけ)。
//                            前の回 (軽量・通常どちらでも) が動いているか待っているときは送らない (skipIfBusy)。
//                            同じ concurrency グループに後から入った回は、待っている回を押し出す (cancelled) ので、
//                            軽量の回が通常の回を消さないようにするため
//   "20 3,9,12,15,21 * * *" → 日本のビルド (JST 0,6,12,18,21 時 20 分。3,9,15 時は飛ばす)
//   "7,22,37,52 * * * *"   → 日本の軽量ビルド (only_if_changed: data-Japan の先頭が前回出したときと同じなら数十秒で終わる)。
//                            軽量の取得 (0,15,30,45 分) の後。定時のビルド (20 分) とは分をずらす。
//                            「build jp」で始まる回 (run-name) が終わっていなければ送らない (北米の長い回は見ない)
// 北米のビルドは定期実行しない (データが揃うまで、2026-09-29 ユーザー判断)。
//
// 起動を送る前に、同じワークフローで STALE_QUEUED_MIN 分以上 queued のまま (= GitHub がランナーを割り当てていない) の
// 回を取り消す。2026-10-05 15:03 の download-japan がランナー未割り当てのまま 2 日以上 queued で残り、同じ
// concurrency グループの後の回が全部 pending のまま次の回に押し出されて (cancelled)、取得が約 2 日止まった。
// concurrency の順番待ち (status pending) は正常な待ちなので触らない。

const JOBS = {
  "3 */3 * * *": [
    { repo: "tosakazu/smash_database", workflow: "download-japan.yml", inputs: { push: "true" } },
  ],
  "*/15 * * * *": [
    { repo: "tosakazu/smash_database", workflow: "download-japan.yml", inputs: { push: "true", quick: "true" }, skipIfBusy: true },
  ],
  "20 3,9,12,15,21 * * *": [
    { repo: "tosakazu/spsp-build", workflow: "build.yml", inputs: { region: "jp", deploy: "r2" } },
  ],
  "7,22,37,52 * * * *": [
    { repo: "tosakazu/spsp-build", workflow: "build.yml", inputs: { region: "jp", deploy: "r2", only_if_changed: "true" },
      skipIfBusy: true, busyTitlePrefix: "build jp" },
  ],
};

const STALE_QUEUED_MIN = 60;

function ghHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "spsp-actions-dispatch",
  };
}

// 失敗しても起動自体は続ける (取り消しは保険)。取り消した回の数を返す
export async function cancelStaleQueued(job, token, now = Date.now()) {
  const base = `https://api.github.com/repos/${job.repo}/actions`;
  let cancelled = 0;
  try {
    const res = await fetch(`${base}/workflows/${job.workflow}/runs?status=queued&per_page=20`, { headers: ghHeaders(token) });
    if (!res.ok) { console.log(`warn: list queued ${job.repo} ${job.workflow}: ${res.status}`); return 0; }
    const runs = (await res.json()).workflow_runs || [];
    for (const run of runs) {
      const ageMin = (now - Date.parse(run.created_at)) / 60000;
      if (run.status !== "queued" || !(ageMin >= STALE_QUEUED_MIN)) continue;
      let r = await fetch(`${base}/runs/${run.id}/cancel`, { method: "POST", headers: ghHeaders(token) });
      if (r.status === 409) r = await fetch(`${base}/runs/${run.id}/force-cancel`, { method: "POST", headers: ghHeaders(token) });
      console.log(`stale queued run ${job.repo} ${run.id} (${Math.round(ageMin)} min): cancel ${r.status}`);
      if (r.status === 202) cancelled++;
    }
  } catch (e) {
    console.log(`warn: stale check ${job.repo} ${job.workflow}: ${e.message}`);
  }
  return cancelled;
}

// 同じワークフローに終わっていない回 (queued / in_progress / waiting / pending …) があるか。busyTitlePrefix があれば
// 表示名 (run-name) がそれで始まる回だけを見る。一覧が取れないときも「ある」とみなす (送らない側に倒す。次の 15 分でまた見る)
export async function isBusy(job, token) {
  try {
    const res = await fetch(`https://api.github.com/repos/${job.repo}/actions/workflows/${job.workflow}/runs?per_page=10`,
                            { headers: ghHeaders(token) });
    if (!res.ok) { console.log(`warn: list runs ${job.repo} ${job.workflow}: ${res.status}`); return true; }
    const busy = ((await res.json()).workflow_runs || []).filter((r) => r.status !== "completed"
      && (!job.busyTitlePrefix || (r.display_title || "").startsWith(job.busyTitlePrefix)));
    if (busy.length) console.log(`skip ${job.repo} ${job.workflow}: ${busy.map((r) => `${r.id} ${r.status}`).join(", ")}`);
    return busy.length > 0;
  } catch (e) {
    console.log(`warn: busy check ${job.repo} ${job.workflow}: ${e.message}`);
    return true;
  }
}

async function dispatch(job, token) {
  const url = `https://api.github.com/repos/${job.repo}/actions/workflows/${job.workflow}/dispatches`;
  let last = "";
  for (let i = 0; i < 3; i++) {
    const res = await fetch(url, {
      method: "POST",
      headers: ghHeaders(token),
      body: JSON.stringify({ ref: "main", inputs: job.inputs }),
    });
    if (res.status === 204 || res.status === 200) {
      console.log(`ok ${job.repo} ${job.workflow}`);
      return;
    }
    last = `${res.status} ${(await res.text()).slice(0, 200)}`;
    await new Promise((r) => setTimeout(r, 20000));
  }
  throw new Error(`dispatch failed ${job.repo} ${job.workflow}: ${last}`);
}

export default {
  async scheduled(event, env, ctx) {
    const jobs = JOBS[event.cron];
    if (!jobs) throw new Error(`unknown cron ${event.cron}`);
    if (!env.JP_ACTIONS_DISPATCH_TOKEN) throw new Error("secret JP_ACTIONS_DISPATCH_TOKEN is not set");
    for (const job of jobs) {
      await cancelStaleQueued(job, env.JP_ACTIONS_DISPATCH_TOKEN);
      if (job.skipIfBusy && await isBusy(job, env.JP_ACTIONS_DISPATCH_TOKEN)) continue;
      await dispatch(job, env.JP_ACTIONS_DISPATCH_TOKEN);
    }
  },
};
