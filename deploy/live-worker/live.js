// live.js — 開催中の大会の速報 (プレイヤーページの「参加中の大会」) を start.gg から取って書き出す。副作用は引数の gql / store だけ。
//
// ランキング用の取得 (smash_database の download.py) とは別物。ここで書くものはランキングに入らない。
// 載せるのは start.gg が ACTIVE と言っている部門だけ。ACTIVE でなくなったら最後に 1 回取り直して ENDED_KEEP_SEC だけ残し、
// その後は消す (確定した結果は通常の取得とビルドが拾う)。ランキングの対象外になる大会 (オンライン・身内など) も載せる
// (スマブラ SP の部門、または下位クラスの名前の部門。download.py の _LOWER_CLASS_NAME_PAT と同じ)。
//
// 書き出し (store のキー。R2 では live/<地域>/ の下):
//   index.json             {"v","updated_at","events":[概要…],"players":{"<start.gg user id>":[event_id…]}}
//   events/<event_id>.json 部門: フェーズ・プール (start.gg のブラケット URL)、参加者 (シード・順位)、セット (ラウンド・スコア・キャラ)
//   _state.json            次の回が使うもの (部門ごとの start.gg の updatedAt・取り直した時刻・セットの続き)
//
// 1 回あたりの問い合わせ: 大会一覧 (部門の state / updatedAt 込み) を数ページ。中身を取り直すのは、updatedAt が動いた部門と、
// REFRESH_MAX_AGE_SEC 以上取り直していない部門だけ (updatedAt が試合の報告で動かない場合の保険)。取り直しは
// フェーズ・プール 1 回 + 前回以降に更新されたセット (updatedAfter) + 順位 + 参加者 (初回と人数が変わったときだけ)。
// 1 回に送るのは MAX_REQUESTS_PER_RUN まで (start.gg は 1 分 80 回まで。残りは次の回)。
//
// 順位: entrants[].placement / final は start.gg の standings (final = isFinal)。まだ残っている参加者には、トーナメント
// (ELIMINATION) なら place_min = 残り人数から決まる「最悪でもこの順位」(1,2,3,4,5,7,9,13,17,25,… の区切り) を付ける。
// 例: 残り 12 人なら 9 (= 9 位以上)。
//
// セットの label はビルドの試合と同じ「ブラケットの位置」(Winners TOP 64 / Losers TOP 12 / Grand Final / B-Winners TOP 8 / 総当たり …)。
// 計算は smash_database の redownload_matches_v2.compute_global_top_x と同じ (bracketLabels)。

export const DOC_VERSION = 2;   // 2: セットに label (ブラケットの位置)・bye、フェーズに seeds
export const GAME_ID = 1386;
export const LOWER_CLASS_NAME_PAT = /[BCDEＢＣＤＥ]\s*クラス|(?<![A-Za-z])[BCDE][\s_\-]*class(?![A-Za-z])/i;
export const WINDOW_BEFORE_SEC = 3 * 86400;   // 3 日前までに始まった大会 (複数日の大会)
export const GRACE_AFTER_END_SEC = 12 * 3600;  // … で、endAt (無ければ開始) から 12 時間以内
export const STALE_SEC = 6 * 3600;             // ACTIVE のまま 6 時間動きが無い部門は載せない (主催者が閉じ忘れたもの)
export const REFRESH_MAX_AGE_SEC = 180;
export const ENDED_KEEP_SEC = 3600;            // ACTIVE でなくなってから残す時間 (通常の取得・ビルドに載るまでのつなぎ)
export const SET_CURSOR_MARGIN_SEC = 120;      // セットの続きは少し前から読み直す (時計のずれ)
export const MAX_REQUESTS_PER_RUN = 70;

export class FetchError extends Error {}

const LIST_QUERY = `query LiveTournaments($gameId: ID!, $cc: String!, $before: Timestamp!, $after: Timestamp!, $page: Int!, $perPage: Int!) {
  tournaments(query: {perPage: $perPage, page: $page, sortBy: "startAt desc",
                      filter: {videogameIds: [$gameId], countryCode: $cc, published: true, beforeDate: $before, afterDate: $after}}) {
    pageInfo { totalPages }
    nodes {
      id name slug url startAt endAt images(type: "profile") { url }
      events { id name slug state updatedAt startAt numEntrants isOnline videogame { id } }
    }
  }
}`;

const PHASES_QUERY = `query LivePhases($eid: ID!) {
  event(id: $eid) {
    phases {
      id name phaseOrder bracketType state numSeeds
      phaseGroups(query: {page: 1, perPage: 256}) { nodes { id displayIdentifier state bracketUrl } }
    }
  }
}`;

const ENTRANTS_QUERY = `query LiveEntrants($eid: ID!, $page: Int!, $perPage: Int!) {
  event(id: $eid) {
    entrants(query: {page: $page, perPage: $perPage}) {
      pageInfo { total totalPages }
      nodes { id name initialSeedNum participants { gamerTag user { id } } }
    }
  }
}`;

const SETS_QUERY = `query LiveSets($eid: ID!, $after: Timestamp, $page: Int!, $perPage: Int!) {
  event(id: $eid) {
    sets(page: $page, perPage: $perPage, sortType: RECENT, filters: {updatedAfter: $after, hideEmpty: false}) {
      pageInfo { total totalPages }
      nodes {
        id state round fullRoundText identifier winnerId startedAt completedAt updatedAt totalGames
        phaseGroup { id }
        slots { prereqType entrant { id } standing { stats { score { value } } } }
        games { orderNum winnerId selections { entrant { id } character { id } } }
      }
    }
  }
}`;

const STANDINGS_QUERY = `query LiveStandings($eid: ID!, $page: Int!, $perPage: Int!) {
  event(id: $eid) {
    standings(query: {page: $page, perPage: $perPage}) {
      pageInfo { total totalPages }
      nodes { placement isFinal entrant { id } }
    }
  }
}`;

// トーナメントの順位の区切り (1, 2, 3, 4, 5, 7, 9, 13, 17, 25, …)
const BUCKETS = (() => {
  const out = [1, 2, 3, 4];
  for (let k = 4; k < 1 << 16; k *= 2) out.push(k + 1, k + k / 2 + 1);
  return out;
})();

// 残り alive 人のうちの 1 人が取りうる最悪の順位 (alive が入る区切り)
export function worstPlacement(alive) {
  if (!(alive > 0)) return null;
  let best = 1;
  for (const b of BUCKETS) {
    if (b > alive) break;
    best = b;
  }
  return best;
}

export function isTargetEvent(ev) {
  return String(ev?.videogame?.id) === String(GAME_ID) || LOWER_CLASS_NAME_PAT.test(ev?.name || "");
}

export function inWindow(t, now) {
  const start = t.startAt || 0;
  const end = t.endAt || start;
  return start <= now && now - Math.max(end, start) <= GRACE_AFTER_END_SEC && now - start <= WINDOW_BEFORE_SEC;
}

// start.gg の GraphQL を呼ぶ (間隔をあける・429 は待って再試行・複雑すぎるエラーは呼び出し側で perPage を下げる)
export function makeGql({ token, fetchImpl = fetch, minIntervalMs = 800, maxRequests = MAX_REQUESTS_PER_RUN, sleep = defaultSleep, log = () => {} }) {
  let last = 0;
  const client = async (query, variables) => {
    for (let attempt = 0; ; attempt++) {
      if (client.count >= maxRequests) throw new BudgetExceeded();
      const wait = last + minIntervalMs - Date.now();
      if (wait > 0) await sleep(wait);
      last = Date.now();
      client.count++;
      let res, body;
      try {
        res = await fetchImpl("https://api.start.gg/gql/alpha", {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify({ query, variables }),
        });
        body = res.status === 200 ? await res.json() : null;
      } catch (e) {
        if (attempt >= 2) throw new FetchError(`network: ${e.message}`);
        await sleep(3000);
        continue;
      }
      if (res.status === 429 || res.status >= 500) {
        log(`start.gg ${res.status}, retry`);
        if (attempt >= 2) throw new FetchError(`start.gg ${res.status}`);
        await sleep(res.status === 429 ? 10000 : 3000);
        continue;
      }
      if (!body) throw new FetchError(`start.gg ${res.status}`);
      return body;
    }
  };
  client.count = 0;
  return client;
}

export class BudgetExceeded extends Error {}

function defaultSleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function isComplexityError(body) {
  return (body?.errors || []).some((e) => /complexity/i.test(e.message || ""));
}

// ページを全部たどる (path = data からのキー列)。複雑すぎると言われたら perPage を半分にしてその位置から続ける
export async function fetchAllNodes(gql, query, variables, path, perPage) {
  const out = [];
  let per = perPage;
  for (let page = 1; ; ) {
    const body = await gql(query, { ...variables, page, perPage: per });
    if (isComplexityError(body) && per > 2) {
      const done = (page - 1) * per;
      per = Math.max(2, Math.floor(per / 2));
      page = Math.floor(done / per) + 1;
      out.length = (page - 1) * per;
      continue;
    }
    let conn = body?.data;
    for (const k of path) conn = conn?.[k];
    if (!conn) throw new FetchError(`${path.join(".")}: ${JSON.stringify(body?.errors || body).slice(0, 300)}`);
    const nodes = conn.nodes || [];
    out.push(...nodes);
    const totalPages = conn.pageInfo?.totalPages || 1;
    if (!nodes.length || page >= totalPages) return out;
    page++;
  }
}

export async function listTournaments(gql, countryCode, now) {
  const nodes = await fetchAllNodes(gql, LIST_QUERY,
    { gameId: GAME_ID, cc: countryCode, before: now, after: now - WINDOW_BEFORE_SEC }, ["tournaments"], 40);
  return nodes.filter((t) => inWindow(t, now));
}

async function fetchPhases(gql, eid) {
  const body = await gql(PHASES_QUERY, { eid });
  const ev = body?.data?.event;
  if (!ev) throw new FetchError(`phases of event ${eid}: ${JSON.stringify(body?.errors || body).slice(0, 300)}`);
  return [...(ev.phases || [])]
    .sort((a, b) => (a.phaseOrder || 0) - (b.phaseOrder || 0) || a.id - b.id)
    .map((p) => ({
      id: p.id, name: p.name, order: p.phaseOrder, type: p.bracketType, state: p.state, seeds: p.numSeeds ?? null, cls: LOWER_CLASS_NAME_PAT.test(p.name || ""),
      groups: (p.phaseGroups?.nodes || []).map((g) => ({ id: g.id, ident: g.displayIdentifier, state: g.state, url: g.bracketUrl })),
    }));
}

async function fetchEntrants(gql, eid) {
  const nodes = await fetchAllNodes(gql, ENTRANTS_QUERY, { eid }, ["event", "entrants"], 100);
  const out = {};
  for (const n of nodes) {
    const parts = n.participants || [];
    const solo = parts.length === 1 ? parts[0] : null;
    out[String(n.id)] = { name: n.name, tag: solo?.gamerTag ?? null, uid: solo?.user?.id ?? null, seed: n.initialSeedNum ?? null };
  }
  return out;
}

export function setRecord(s) {
  const slots = s.slots || [];
  const games = [...(s.games || [])].sort((a, b) => (a.orderNum || 0) - (b.orderNum || 0)).map((g) => {
    const c = {};
    for (const x of g.selections || []) c[String(x.entrant?.id)] = x.character?.id ?? null;
    return { w: g.winnerId ?? null, c };
  });
  return {
    id: s.id, pg: s.phaseGroup?.id ?? null, round: s.round ?? null, round_text: s.fullRoundText ?? null,
    ident: s.identifier ?? null, state: s.state ?? null,
    e: slots.map((sl) => sl?.entrant?.id ?? null),
    score: slots.map((sl) => sl?.standing?.stats?.score?.value ?? null),
    winner: s.winnerId ?? null, best_of: s.totalGames ?? null, bye: slots.some((sl) => sl?.prereqType === "bye"),
    started_at: s.startedAt ?? null, completed_at: s.completedAt ?? null, updated_at: s.updatedAt ?? null,
    games: games.length ? games : null,
  };
}

async function fetchStandings(gql, eid) {
  const nodes = await fetchAllNodes(gql, STANDINGS_QUERY, { eid }, ["event", "standings"], 100);
  const out = {};
  for (const n of nodes) if (n.entrant?.id != null) out[String(n.entrant.id)] = [n.placement ?? null, !!n.isFinal];
  return out;
}

// 順位 (start.gg) と、残っている参加者の「最悪でもこの順位」、いま (敗退していれば最後に) いるプール。
// いるプール = その参加者の一番新しいセットのプール (相手待ち・未対戦のセットが一番新しい。次に始まった・終わった時刻の新しいもの)。
// フェーズの phaseOrder は作った順で、進む順とは限らない (TOP8 が TOP96 より前のことがある) ので使わない。
// 同じ部門の中の下位クラス (名前が LOWER_CLASS_NAME_PAT のフェーズ、phases[].cls) にいる参加者は本戦では敗退済み:
// 残り人数に数えない (place_min なし)。
export function applyStandings(doc, standings) {
  const elim = (doc.phases || []).some((p) => !p.cls && /ELIMINATION$/.test(p.type || ""));
  const clsPg = new Set();
  for (const p of doc.phases || []) if (p.cls) for (const g of p.groups || []) clsPg.add(String(g.id));
  const recency = (s) => (s.winner == null && !s.started_at ? Infinity : (s.completed_at || s.started_at || 0));
  const lastPg = {};
  for (const s of doc.sets || []) {
    const r = recency(s);
    for (const id of s.e || []) {
      if (id == null) continue;
      const k = String(id);
      if (!lastPg[k] || r >= lastPg[k][0]) lastPg[k] = [r, s.pg];
    }
  }
  let alive = 0;
  for (const [id, e] of Object.entries(doc.entrants || {})) {
    const [placement, final] = standings[id] || [null, false];
    e.placement = placement;
    e.final = final;
    e.pg = lastPg[id]?.[1] ?? null;
    e.in_class = e.pg != null && clsPg.has(String(e.pg));
    if (!final && !e.in_class) alive++;
  }
  const worst = elim ? worstPlacement(alive) : null;
  for (const e of Object.values(doc.entrants || {})) e.place_min = e.final || e.in_class ? null : worst;
  bracketLabels(doc);   // 敗者側のラベルは負けた人の順位から決まる
}

// ---- ブラケットの位置 (smash_database scripts/common/redownload_matches_v2.py の移植。数え方を変えるときは両方) ----
const CLASS_PHASE_RE = /\b[B-E][- ]?class\b|[B-EＢＣＤＥ][- ]?クラス/i;
const BT_LABEL = { ROUND_ROBIN: "総当たり", SWISS: "スイスドロー", MATCHMAKING: "レート戦" };
const isBracket = (type) => !type || type === "DOUBLE_ELIMINATION" || type === "SINGLE_ELIMINATION";

function classLetter(name) {
  const m = (name || "").match(CLASS_PHASE_RE);
  return m ? m[0][0].replace(/[ＡＢＣＤＥ]/, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0)).toUpperCase() : null;
}
// プレイインを除いた実質の大きさ (2 の累乗ならそのまま、そうでなければ下の 2 の累乗)
function capacityOf(n) {
  if (!(n > 1)) return 1;
  let p = 1;
  while (p < n) p *= 2;
  return p === n ? n : p / 2;
}
// 順位の区切りの上端 1,2,3,4,6,8,12,16,24,…
export function placementBucket(p) {
  if (!(p > 0)) return null;
  if (p <= 4) return p;
  for (let k = 1; k <= 20; k++) {
    if (p <= 3 * 2 ** k) return 3 * 2 ** k;
    if (p <= 2 ** (k + 2)) return 2 ** (k + 2);
  }
  return null;
}
function losersTopX(round) {   // 敗者側ラウンド (start.gg は -1 = 敗者側決勝) で負けたときの区切り
  const w2w = -round + 1;
  const k = Math.floor(w2w / 2);
  return w2w % 2 === 0 ? 3 * 2 ** (k - 1) : 2 ** (k + 1);
}

// 各セットに label を付ける。勝者側は全体のラウンド (フェーズを phaseOrder 順につなぎ、プレイインを除く) と本戦の大きさから、
// 敗者側は負けた人の順位の区切りから (まだ終わっていないセットは同じフェーズ・ラウンドの終わったセット、無ければラウンドから)。
// 下位クラスのフェーズは独立したブラケット (頭に "B-" など、大きさはそのフェーズの numSeeds)
export function bracketLabels(doc) {
  const phaseOf = {};
  for (const p of doc.phases || []) for (const g of p.groups || []) phaseOf[String(g.id)] = p;
  const info = new Map();
  let mainSeeds = 0;
  for (const s of doc.sets || []) {
    const p = phaseOf[String(s.pg)];
    if (!p) continue;
    if (!classLetter(p.name)) mainSeeds = Math.max(mainSeeds, p.seeds || 0);
    if (!isBracket(p.type) || s.bye || !(s.round > 0)) continue;
    let i = info.get(p.id);
    if (!i) info.set(p.id, (i = { order: p.order || 0, counts: {}, se: p.type === "SINGLE_ELIMINATION", letter: classLetter(p.name) }));
    i.counts[s.round] = (i.counts[s.round] || 0) + 1;
  }
  let cumulative = 0;
  for (const [, i] of [...info.entries()].sort((a, b) => a[1].order - b[1].order || a[0] - b[0])) {
    const rounds = Object.keys(i.counts).map(Number).sort((a, b) => a - b);
    i.playIn = new Set();
    for (let k = 0; k < rounds.length - 1; k++) {   // 次のラウンドのちょうど 2 倍でない先頭のラウンドはプレイイン
      if (i.counts[rounds[k]] !== i.counts[rounds[k + 1]] * 2) i.playIn.add(rounds[k]);
      else break;
    }
    i.eff = rounds.filter((r) => !i.playIn.has(r));
    i.offset = i.letter ? 0 : cumulative;
    if (!i.letter) cumulative += i.eff.length;
  }
  const mainCap = capacityOf(mainSeeds);
  const ent = doc.entrants || {};
  const losersDone = {};   // "<フェーズ>:<ラウンド>" → 終わったセットの区切り
  const label = (s, pending) => {
    const p = phaseOf[String(s.pg)];
    if (!p || s.round == null) return null;
    const letter = classLetter(p.name);
    const pre = letter ? letter + "-" : "";
    if (!isBracket(p.type)) return BT_LABEL[p.type] ? pre + BT_LABEL[p.type] : null;
    const i = info.get(p.id);
    const se = !!i?.se;
    const cap = letter ? capacityOf(p.seeds || 0) : mainCap;
    if (s.round > 0) {
      if (!i) return null;
      if (se && !(cap > 1)) return null;
      if (i.playIn.has(s.round)) return `${pre}Winners TOP ${se ? placementBucket(p.seeds || 0) || cap * 2 : cap * 2}`;
      const idx = i.eff.indexOf(s.round);
      if (idx < 0) return null;
      return `${pre}Winners TOP ${Math.max(2, Math.floor(cap / 2 ** (i.offset + idx)))}`;
    }
    if (s.round < 0) {
      if (se) return null;
      if (!letter) {
        const key = p.id + ":" + s.round;
        if (pending) { if (losersDone[key]) return `Losers TOP ${losersDone[key]}`; }
        else {
          const loser = (s.e || []).find((id) => id != null && String(id) !== String(s.winner));
          const e = loser != null ? ent[String(loser)] : null;
          const b = placementBucket(e?.placement);
          if (b) { losersDone[key] = b; return `Losers TOP ${b}`; }
        }
      }
      return `${pre}Losers TOP ${losersTopX(s.round)}`;
    }
    return se ? null : `${pre}Grand Final`;
  };
  for (const s of doc.sets || []) if (s.winner != null) s.label = label(s, false);
  for (const s of doc.sets || []) if (s.winner == null) s.label = label(s, true);
}

// ACTIVE の部門を 1 つ取り直す (doc = 前回の出力か空のもの。その場で書き換える)
export async function updateEvent(gql, doc, ev, tour, now) {
  const first = !doc.entrants || doc.v !== DOC_VERSION;
  const phases = await fetchPhases(gql, ev.id);
  const entrants = first || Object.keys(doc.entrants).length !== (ev.numEntrants || 0) ? await fetchEntrants(gql, ev.id) : doc.entrants;
  const cursor = doc._sets_after;
  const after = first || cursor == null ? null : Math.max(0, cursor - SET_CURSOR_MARGIN_SEC);
  const fresh = (await fetchAllNodes(gql, SETS_QUERY, { eid: ev.id, after }, ["event", "sets"], 40)).map(setRecord);
  const standings = await fetchStandings(gql, ev.id);
  const sets = new Map((first ? [] : doc.sets || []).map((s) => [String(s.id), s]));
  for (const s of fresh) sets.set(String(s.id), s);
  Object.assign(doc, {
    v: DOC_VERSION, event_id: ev.id, event_name: ev.name, tournament_id: tour.id, tournament_name: tour.name,
    url: "https://www.start.gg/" + (ev.slug || ""), tournament_url: "https://www.start.gg" + (tour.url || ""),
    start_at: ev.startAt ?? null, num_entrants: ev.numEntrants ?? null, is_online: ev.isOnline ?? null,
    state: ev.state, phases, entrants,
    sets: [...sets.values()].sort((a, b) => String(a.pg).localeCompare(String(b.pg)) || (a.round || 0) - (b.round || 0) || String(a.id).localeCompare(String(b.id))),
    _sets_after: now, updated_at: now,
  });
  applyStandings(doc, standings);
  return doc;
}

function lastActivity(ev, doc) {
  let t = ev.updatedAt || 0;
  for (const s of doc?.sets || []) t = Math.max(t, s.updated_at || 0);
  return t;
}

function summary(doc, tour) {   // index.json の 1 件
  return {
    event_id: doc.event_id, event_name: doc.event_name, tournament_id: doc.tournament_id, tournament_name: doc.tournament_name,
    url: doc.url, tournament_url: doc.tournament_url, start_at: doc.start_at, num_entrants: doc.num_entrants,
    is_online: doc.is_online, image: tour?.images?.[0]?.url ?? null, ended: !!doc.ended_at, updated_at: doc.updated_at,
  };
}

// 1 回分。store: { get(key) → obj|null, put(key, obj), del(key), list(prefix) → [key] }
export async function runLive({ gql, store, countryCode = "JP", now = Math.floor(Date.now() / 1000), log = () => {} }) {
  const state = (await store.get("_state.json")) || {};
  const prev = state.events || {};
  const tours = await listTournaments(gql, countryCode, now);
  const next = {};
  const work = [];
  let failed = 0;
  let deferred = 0;

  for (const t of tours) {
    for (const ev of t.events || []) {
      if (!isTargetEvent(ev)) continue;
      const key = String(ev.id);
      const p = prev[key];
      if (ev.state === "ACTIVE") {
        if (!p && now - (ev.updatedAt || 0) > STALE_SEC) continue;   // 閉じ忘れ (下で既存のものも見る)
        work.push({ key, ev, t, p, ending: false });
      } else if (p && !p.ended_at) {
        work.push({ key, ev, t, p, ending: true });                  // ACTIVE でなくなった: 最後に 1 回取り直す
      } else if (p && now - p.ended_at < ENDED_KEEP_SEC) {
        next[key] = p;
      }
    }
  }
  // 取り直す順: 初めての部門 → 終わった部門 → 古いものから
  work.sort((a, b) => (a.p ? 1 : 0) - (b.p ? 1 : 0) || (b.ending ? 1 : 0) - (a.ending ? 1 : 0) || (a.p?.refreshed_at || 0) - (b.p?.refreshed_at || 0));
  for (const w of work) {
    const { key, ev, t, p, ending } = w;
    const unchanged = p && p.updated_at === ev.updatedAt && now - (p.refreshed_at || 0) < REFRESH_MAX_AGE_SEC;
    if (unchanged && !ending) { next[key] = p; continue; }
    let doc = (p && (await store.get(`events/${key}.json`))) || {};
    try {
      await updateEvent(gql, doc, ev, t, now);
    } catch (e) {
      if (e instanceof BudgetExceeded) { deferred++; if (p) next[key] = p; continue; }
      failed++;
      log(`${t.name} / ${ev.name}: ${e.message}`);
      if (p) next[key] = p;
      continue;
    }
    if (ending) doc.ended_at = now;
    if (!ending && now - lastActivity(ev, doc) > STALE_SEC) continue;   // 動きが無い ACTIVE (閉じ忘れ)
    await store.put(`events/${key}.json`, doc);
    const uids = [...new Set(Object.values(doc.entrants).map((e) => e.uid).filter((u) => u != null).map(String))];
    next[key] = { tournament_id: t.id, updated_at: ev.updatedAt, refreshed_at: now, ...(ending ? { ended_at: now } : {}),
                  summary: summary(doc, t), uids };
    log(`${t.name} / ${ev.name}: ${doc.sets.length} sets, ${Object.keys(doc.entrants).length} entrants${ending ? " (ended)" : ""}`);
  }
  // 窓から外れた・ENDED_KEEP_SEC を過ぎた部門は消す
  for (const k of await store.list("events/")) {
    const id = k.replace(/^events\//, "").replace(/\.json$/, "");
    if (!next[id]) { await store.del(k); log(`removed ${k}`); }
  }
  // 一覧は _state.json に持っている概要から作る (部門のファイルは読み直さない)
  const events = [];
  const players = {};
  for (const key of Object.keys(next).sort((a, b) => a - b)) {
    const p = next[key];
    events.push(p.summary);
    for (const u of p.uids || []) (players[u] ||= []).push(p.summary.event_id);
  }
  await store.put("index.json", { v: DOC_VERSION, updated_at: now, events, players });
  await store.put("_state.json", { v: DOC_VERSION, updated_at: now, events: next });
  log(`tournaments in window ${tours.length}, events ${events.length}, requests ${gql.count ?? "?"}, failed ${failed}, deferred ${deferred}`);
  return { tournaments: tours.length, events: events.length, failed, deferred };
}
