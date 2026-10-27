// deploy/live-worker/live.js の試験 (start.gg は偽物、書き出しはメモリ)。node --test tests/deploy/live_worker.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import {
  worstPlacement, isTargetEvent, inWindow, runLive, applyStandings, bracketLabels, placementBucket, BudgetExceeded,
  ENDED_KEEP_SEC, STALE_SEC, REFRESH_MAX_AGE_SEC,
} from "../../deploy/live-worker/live.js";

const NOW = 1_800_000_000;

function memStore() {
  const m = new Map();
  return {
    m,
    async get(k) { return m.has(k) ? JSON.parse(m.get(k)) : null; },
    async put(k, v) { m.set(k, JSON.stringify(v)); },
    async del(k) { m.delete(k); },
    async list(p) { return [...m.keys()].filter((k) => k.startsWith(p)); },
  };
}

// 偽の start.gg: tours = 一覧に出す大会、ev = 部門の中身 (phases / entrants / sets / standings)
function fakeGql(tours, ev, { budget = Infinity } = {}) {
  const calls = [];
  const gql = async (query, vars) => {
    if (calls.length >= budget) throw new BudgetExceeded();
    const name = query.match(/query (\w+)/)[1];
    calls.push([name, vars]);
    gql.count = calls.length;
    const page = (nodes) => ({ pageInfo: { total: nodes.length, totalPages: 1 }, nodes });
    switch (name) {
      case "LiveTournaments": return { data: { tournaments: page(tours) } };
      case "LivePhases": return { data: { event: { phases: ev.phases } } };
      case "LiveEntrants": return { data: { event: { entrants: page(ev.entrants) } } };
      case "LiveSets": return { data: { event: { sets: page(ev.sets.filter((s) => vars.after == null || s.updatedAt > vars.after)) } } };
      case "LiveStandings": return { data: { event: { standings: page(ev.standings) } } };
      default: throw new Error(name);
    }
  };
  gql.calls = calls;
  gql.count = 0;
  return gql;
}

const entrant = (id, uid, seed, tag) => ({ id, name: tag, initialSeedNum: seed, participants: [{ gamerTag: tag, user: { id: uid } }] });
const set = (id, a, b, winner, updatedAt, pg = 10) => ({
  id, state: winner ? 3 : 2, round: 1, fullRoundText: "Winners Round 1", identifier: "A", winnerId: winner, totalGames: 3,
  startedAt: updatedAt - 60, completedAt: winner ? updatedAt : null, updatedAt, phaseGroup: { id: pg },
  slots: [{ entrant: { id: a }, standing: { stats: { score: { value: winner ? (winner === a ? 2 : 0) : null } } } },
          { entrant: { id: b }, standing: { stats: { score: { value: winner ? (winner === b ? 2 : 0) : null } } } }],
  games: [{ orderNum: 1, winnerId: winner, selections: [{ entrant: { id: a }, character: { id: 1302 } }] }],
});

function fixture({ state = "ACTIVE", updatedAt = NOW - 60 } = {}) {
  const tours = [{
    id: 1, name: "大会", slug: "tournament/x", url: "/tournament/x", startAt: NOW - 3600, endAt: NOW + 3600,
    images: [{ url: "https://images.start.gg/x.png" }],
    events: [
      { id: 100, name: "スマブラSP", slug: "tournament/x/event/sp", state, updatedAt, startAt: NOW - 3600, numEntrants: 4, isOnline: false, videogame: { id: 1386 } },
      { id: 101, name: "スト6", slug: "tournament/x/event/sf", state: "ACTIVE", updatedAt, startAt: NOW - 3600, numEntrants: 4, isOnline: false, videogame: { id: 43868 } },
    ],
  }];
  const ev = {
    phases: [{ id: 5, name: "Main", phaseOrder: 1, bracketType: "DOUBLE_ELIMINATION", state: "ACTIVE",
               phaseGroups: { nodes: [{ id: 10, displayIdentifier: "1", state: 2, bracketUrl: "https://www.start.gg/x/brackets/5/10" }] } }],
    entrants: [entrant(1, 11, 1, "A"), entrant(2, 22, 2, "B"), entrant(3, 33, 3, "C"), entrant(4, 44, 4, "D")],
    sets: [set(1000, 1, 4, 1, NOW - 600), set(1001, 2, 3, null, NOW - 120)],
    standings: [{ placement: 4, isFinal: false, entrant: { id: 4 } }],
  };
  return { tours, ev };
}

test("worstPlacement: 残り人数が入る区切り", () => {
  assert.deepEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 16, 17, 24, 25, 33].map(worstPlacement),
                   [1, 2, 3, 4, 5, 5, 7, 7, 9, 9, 13, 13, 17, 17, 25, 33]);
  assert.equal(worstPlacement(0), null);
});

test("isTargetEvent: スマブラ SP と下位クラスの名前", () => {
  assert.ok(isTargetEvent({ videogame: { id: 1386 }, name: "x" }));
  assert.ok(isTargetEvent({ videogame: { id: 1 }, name: "Bクラス" }));
  assert.ok(isTargetEvent({ videogame: { id: 1 }, name: "C-class" }));
  assert.ok(!isTargetEvent({ videogame: { id: 1 }, name: "Ultimate Singles" }));
});

test("inWindow: 始まっていて、終わりから 12 時間以内", () => {
  assert.ok(inWindow({ startAt: NOW - 10, endAt: NOW + 10 }, NOW));
  assert.ok(!inWindow({ startAt: NOW + 10, endAt: NOW + 100 }, NOW));
  assert.ok(!inWindow({ startAt: NOW - 20 * 3600, endAt: NOW - 13 * 3600 }, NOW));
});

test("applyStandings: 確定は placement、残りは place_min、プールは最後のセットの", () => {
  const doc = {
    phases: [{ order: 1, type: "DOUBLE_ELIMINATION", groups: [{ id: 10 }] }, { order: 2, type: "DOUBLE_ELIMINATION", groups: [{ id: 20 }] }],
    entrants: { 1: {}, 2: {}, 3: {} },
    sets: [{ pg: 10, e: [1, 2] }, { pg: 20, e: [1, 3] }],
  };
  applyStandings(doc, { 2: [3, true] });
  assert.deepEqual([doc.entrants[1].pg, doc.entrants[2].pg, doc.entrants[3].pg], [20, 10, 20]);
  assert.equal(doc.entrants[2].placement, 3);
  assert.equal(doc.entrants[2].place_min, null);
  assert.equal(doc.entrants[1].place_min, 2);   // 残り 2 人
});

test("runLive: ACTIVE のスマブラ部門だけ書き、index に選手の uid を載せる", async () => {
  const { tours, ev } = fixture();
  const store = memStore();
  const gql = fakeGql(tours, ev);
  const r = await runLive({ gql, store, now: NOW });
  assert.equal(r.events, 1);
  const index = await store.get("index.json");
  assert.deepEqual(index.events.map((e) => e.event_id), [100]);
  assert.equal(index.events[0].image, "https://images.start.gg/x.png");
  assert.deepEqual(index.players["11"], [100]);
  const doc = await store.get("events/100.json");
  assert.equal(doc.entrants["1"].seed, 1);
  assert.equal(doc.entrants["4"].final, false);
  assert.equal(doc.entrants["1"].place_min, 4);   // 残り 4 人 (isFinal の人がいない)
  assert.equal(doc.sets.length, 2);
  assert.equal(doc.tournament_url, "https://www.start.gg/tournament/x");
  assert.deepEqual(gql.calls.map((c) => c[0]), ["LiveTournaments", "LivePhases", "LiveEntrants", "LiveSets", "LiveStandings"]);
  const lv = gql.calls[0][1];   // beforeDate は終了予定で効くので、開催中 (終了がまだ先) の大会が落ちないよう先まで取る
  assert.ok(lv.before > NOW && lv.after < NOW, JSON.stringify(lv));
});

test("runLive: updatedAt が同じで取り直して間もない部門は一覧だけ", async () => {
  const { tours, ev } = fixture();
  const store = memStore();
  await runLive({ gql: fakeGql(tours, ev), store, now: NOW });
  const gql = fakeGql(tours, ev);
  await runLive({ gql, store, now: NOW + 30 });
  assert.deepEqual(gql.calls.map((c) => c[0]), ["LiveTournaments"]);
  // REFRESH_MAX_AGE_SEC を過ぎたら取り直す (参加者は人数が同じなので取らない)。セットは続きから
  const gql2 = fakeGql(tours, ev);
  await runLive({ gql: gql2, store, now: NOW + REFRESH_MAX_AGE_SEC + 1 });
  assert.deepEqual(gql2.calls.map((c) => c[0]), ["LiveTournaments", "LivePhases", "LiveSets", "LiveStandings"]);
  assert.ok(gql2.calls[2][1].after > 0);
  assert.equal((await store.get("events/100.json")).sets.length, 2);   // 前回のセットは残る
});

test("runLive: ACTIVE でなくなったら最後に取り直して ended、ENDED_KEEP_SEC 後に消す", async () => {
  const { tours, ev } = fixture();
  const store = memStore();
  await runLive({ gql: fakeGql(tours, ev), store, now: NOW });
  const done = fixture({ state: "COMPLETED", updatedAt: NOW + 100 });
  done.ev.standings = [1, 2, 3, 4].map((id) => ({ placement: id, isFinal: true, entrant: { id } }));
  await runLive({ gql: fakeGql(done.tours, done.ev), store, now: NOW + 120 });
  let index = await store.get("index.json");
  assert.equal(index.events[0].ended, true);
  assert.equal((await store.get("events/100.json")).entrants["2"].placement, 2);
  const gql = fakeGql(done.tours, done.ev);
  await runLive({ gql, store, now: NOW + 600 });
  assert.deepEqual(gql.calls.map((c) => c[0]), ["LiveTournaments"]);   // 終わった後は取り直さない
  await runLive({ gql: fakeGql(done.tours, done.ev), store, now: NOW + 120 + ENDED_KEEP_SEC });
  index = await store.get("index.json");
  assert.equal(index.events.length, 0);
  assert.equal(await store.get("events/100.json"), null);
});

test("runLive: 動きの無い ACTIVE (閉じ忘れ) は載せない", async () => {
  const { tours, ev } = fixture({ updatedAt: NOW - STALE_SEC - 10 });
  const gql = fakeGql(tours, ev);
  const r = await runLive({ gql, store: memStore(), now: NOW });
  assert.equal(r.events, 0);
  assert.equal(gql.calls.length, 1);
});

test("runLive: 問い合わせの上限に当たったら残りは次の回 (前回の分は残す)", async () => {
  const { tours, ev } = fixture();
  const store = memStore();
  const r = await runLive({ gql: fakeGql(tours, ev, { budget: 3 }), store, now: NOW });
  assert.equal(r.deferred, 1);
  assert.equal(r.events, 0);
  const r2 = await runLive({ gql: fakeGql(tours, ev), store, now: NOW + 60 });
  assert.equal(r2.events, 1);
});

test("applyStandings: 下位クラスのフェーズにいる人は残り人数に数えない・プールは時刻で決める", () => {
  const doc = {
    phases: [{ order: 1, type: "DOUBLE_ELIMINATION", groups: [{ id: 10 }] },
             { order: 2, type: "SINGLE_ELIMINATION", cls: true, groups: [{ id: 30 }] },
             { order: 0, type: "DOUBLE_ELIMINATION", groups: [{ id: 20 }] }],   // 後のフェーズが order 0 (作った順)
    entrants: { 1: {}, 2: {}, 3: {}, 4: {} },
    sets: [{ pg: 10, e: [1, 2], winner: 1, completed_at: 100 }, { pg: 10, e: [3, 4], winner: 3, completed_at: 110 },
           { pg: 20, e: [1, 3], winner: null, started_at: 200 }, { pg: 30, e: [2, 4], winner: 2, completed_at: 210 },
           { pg: 30, e: [2, null], winner: null }],
  };
  applyStandings(doc, {});
  assert.deepEqual([1, 2, 3, 4].map((i) => doc.entrants[i].pg), [20, 30, 20, 30]);
  assert.deepEqual([1, 2, 3, 4].map((i) => doc.entrants[i].in_class), [false, true, false, true]);
  assert.equal(doc.entrants[1].place_min, 2);
  assert.equal(doc.entrants[2].place_min, null);
});

test("bracketLabels: ビルドと同じブラケットの位置 (フェーズをつなぐ・敗者側は順位・下位クラスは独立)", () => {
  const set = (id, pg, round, e, winner) => ({ id, pg, round, e, winner, bye: false });
  const doc = {
    phases: [
      { id: 1, name: "Pool", order: 1, type: "DOUBLE_ELIMINATION", seeds: 16, groups: [{ id: 11 }, { id: 12 }] },
      { id: 2, name: "TOP8", order: 2, type: "DOUBLE_ELIMINATION", seeds: 8, groups: [{ id: 21 }] },
      { id: 3, name: "Bクラス", order: 3, type: "SINGLE_ELIMINATION", seeds: 8, groups: [{ id: 31 }] },
      { id: 4, name: "予選", order: 0, type: "ROUND_ROBIN", seeds: 16, groups: [{ id: 41 }] },
    ],
    entrants: { 1: { placement: 7 }, 2: {}, 3: {}, 4: {} },
    sets: [
      ...[11, 11, 11, 11, 12, 12, 12, 12].map((pg, i) => set(100 + i, pg, 1, [3, 4], 3)),
      ...[11, 11, 12, 12].map((pg, i) => set(200 + i, pg, 2, [3, 4], null)),
      { ...set(299, 12, 1, [3, null], null), bye: true },   // bye は数えない
      ...[0, 1, 2, 3].map((i) => set(300 + i, 21, 1, [3, 4], null)),
      set(400, 21, -1, [2, 1], 2),        // 負けた人は 7 位 → Losers TOP 8
      set(401, 21, -1, [3, 4], null),     // 同じラウンドの終わったセットに合わせる
      set(402, 21, -2, [3, 4], null),     // 無ければラウンドから (start.gg は敗者側決勝ほど |round| が大きい。ここでは -2 = 敗者側決勝 = 3)
      ...[0, 1, 2, 3].map((i) => set(500 + i, 31, 1, [3, 4], null)),
      ...[0, 1].map((i) => set(510 + i, 31, 2, [3, 4], null)),
      set(600, 41, 1, [3, 4], 3),
    ],
  };
  bracketLabels(doc);
  const at = (id) => doc.sets.find((s) => s.id === id).label;
  assert.equal(at(100), "Winners TOP 16");
  assert.equal(at(200), "Winners TOP 8");
  assert.equal(at(300), "Winners TOP 4");
  assert.equal(at(400), "Losers TOP 8");
  assert.equal(at(401), "Losers TOP 8");
  assert.equal(at(402), "Losers TOP 3");
  assert.equal(at(500), "B-Winners TOP 8");
  assert.equal(at(510), "B-Winners TOP 4");
  assert.equal(at(600), "総当たり");
  assert.deepEqual([1, 2, 3, 4, 5, 7, 9, 13, 25, 49, 289].map(placementBucket), [1, 2, 3, 4, 6, 8, 12, 16, 32, 64, 384]);
});

test("bracketLabels: 本戦のフェーズは人数の多い順につなぐ (phaseOrder が TOP8 → TOP96 でも)", () => {
  const sets = (pg, counts) => Object.entries(counts).flatMap(([r, n]) =>
    Array.from({ length: n }, (_, i) => ({ id: `${pg}-${r}-${i}`, pg, round: Number(r), e: [1, 2], winner: null, bye: false })));
  const doc = {
    phases: [
      { id: 1, name: "Bracket", order: 1, type: "DOUBLE_ELIMINATION", seeds: 256, groups: [{ id: 11 }] },
      { id: 2, name: "TOP8", order: 2, type: "DOUBLE_ELIMINATION", seeds: 8, groups: [{ id: 21 }] },
      { id: 3, name: "TOP96", order: 3, type: "DOUBLE_ELIMINATION", seeds: 96, groups: [{ id: 31 }] },
    ],
    entrants: {},
    sets: [...sets(11, { 1: 128, 2: 64, 3: 32 }), ...sets(21, { 1: 2, 2: 1, 3: 1 }), ...sets(31, { 1: 16, 2: 8, 3: 4 })],
  };
  bracketLabels(doc);
  const at = (id) => doc.sets.find((s) => s.id === id).label;
  assert.equal(at("31-1-0"), "Winners TOP 32");
  assert.equal(at("21-1-0"), "Winners TOP 4");
});

test("bracketLabels: 敗者側のまだ終わっていないラウンドは敗者側決勝から数える (19 人: -4 = 敗者側 1 回戦 … -10 = 敗者側決勝)", () => {
  const doc = {
    phases: [{ id: 1, name: "Singles", order: 1, type: "DOUBLE_ELIMINATION", seeds: 19, groups: [{ id: 11 }] }],
    entrants: {},
    sets: [-4, -5, -6, -7, -8, -9, -10].map((r) => ({ id: r, pg: 11, round: r, e: [1, 2], winner: null, bye: false })),
  };
  bracketLabels(doc);
  assert.deepEqual(doc.sets.map((s) => s.label), [24, 16, 12, 8, 6, 4, 3].map((n) => `Losers TOP ${n}`));
});

test("bracketLabels: プールの敗者側決勝は次のフェーズに進む人数の次の順位", () => {
  const doc = {
    phases: [
      { id: 1, name: "Pools", order: 1, type: "DOUBLE_ELIMINATION", seeds: 64, groups: [{ id: 11 }] },
      { id: 2, name: "TOP8", order: 2, type: "DOUBLE_ELIMINATION", seeds: 8, groups: [{ id: 21 }] },
    ],
    entrants: {},
    sets: [
      ...[1, 2, 3].map((r) => ({ id: `w${r}`, pg: 11, round: r, e: [1, 2], winner: null, bye: false })),
      { id: "w21", pg: 21, round: 1, e: [1, 2], winner: null, bye: false },
      ...[-1, -2, -3].map((r) => ({ id: r, pg: 11, round: r, e: [1, 2], winner: null, bye: false })),
    ],
  };
  bracketLabels(doc);
  const at = (id) => doc.sets.find((s) => s.id === id).label;
  assert.equal(at(-3), "Losers TOP 12");   // 8 人が進む → 敗者側決勝で負けた人は 9〜12 位
  assert.equal(at(-2), "Losers TOP 16");
});
