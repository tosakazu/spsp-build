// spsp-live: 開催中の大会の速報を 1 分ごとに start.gg から取り、R2 (spsp-data) の live/<地域>/ に書く (中身は live.js 冒頭)。
//
// 配信は data.spsp.games/live/jp/… (R2 の公開ドメイン、Cache-Control は CACHE_CONTROL)。ビルドの R2 同期 (deploy/sync_r2.sh) は
// <地域>/ の下しか触らないので live/ は消されない。HTTP の入口は持たない (wrangler.toml で workers_dev=false・routes なし)。
//
// 鍵: Worker の secret STARTGG_TOKEN (速報専用の start.gg トークン。ランキング用の取得の鍵とは別)。ログに出さない。
// 前の回が終わっていなければ (_lock.json が LOCK_SEC 以内) 何もしない。土曜の昼は 1 回が 1 分を超えるので、その間の回は飛ぶ。
// Workers の有料プランが前提 (無料プランは cron 1 回の CPU 10 ms・問い合わせ 50 回までで足りない)。

import { runLive, makeGql } from "./live.js";

const REGIONS = [{ region: "jp", countryCode: "JP" }];
const LOCK_SEC = 10 * 60;
const CACHE_CONTROL = "public, max-age=30";

export function r2Store(bucket, prefix) {
  return {
    async get(key) {
      const obj = await bucket.get(prefix + key);
      return obj ? obj.json() : null;
    },
    async put(key, value) {
      await bucket.put(prefix + key, JSON.stringify(value), {
        httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: CACHE_CONTROL },
      });
    },
    async del(key) {
      await bucket.delete(prefix + key);
    },
    async list(sub) {
      const keys = [];
      let cursor;
      do {
        const r = await bucket.list({ prefix: prefix + sub, cursor });
        keys.push(...r.objects.map((o) => o.key.slice(prefix.length)));
        cursor = r.truncated ? r.cursor : undefined;
      } while (cursor);
      return keys;
    },
  };
}

async function runRegion(env, { region, countryCode }) {
  const prefix = `live/${region}/`;
  const now = Math.floor(Date.now() / 1000);
  const lock = await env.BUCKET.get(prefix + "_lock.json");
  if (lock) {
    const { since } = await lock.json();
    if (now - since < LOCK_SEC) { console.log(`skip ${region}: previous run since ${since}`); return; }
    console.log(`${region}: stale lock since ${since}, taking over`);
  }
  await env.BUCKET.put(prefix + "_lock.json", JSON.stringify({ since: now }));
  try {
    const gql = makeGql({ token: env.STARTGG_TOKEN, log: (m) => console.log(`${region}: ${m}`) });
    await runLive({ gql, store: r2Store(env.BUCKET, prefix), countryCode, now, log: (m) => console.log(`${region}: ${m}`) });
  } finally {
    await env.BUCKET.delete(prefix + "_lock.json");
  }
}

export default {
  async scheduled(event, env, ctx) {
    if (!env.STARTGG_TOKEN) throw new Error("secret STARTGG_TOKEN is not set");
    for (const r of REGIONS) await runRegion(env, r);
  },
};
