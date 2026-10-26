// 手元で 1 回分を動かす (Worker と同じ live.js、書き出し先は手元のディレクトリ)。
//   STARTGG_TOKEN_FILE=… node deploy/live-worker/run_local.mjs <出力ディレクトリ> [国コード (既定 JP)]
import fs from "node:fs";
import path from "node:path";
import { runLive, makeGql } from "./live.js";

const [dir, cc = "JP"] = process.argv.slice(2);
if (!dir) { console.error("usage: run_local.mjs <dir> [country]"); process.exit(2); }
const token = fs.readFileSync(process.env.STARTGG_TOKEN_FILE, "utf8").trim();
const store = {
  async get(k) { try { return JSON.parse(fs.readFileSync(path.join(dir, k), "utf8")); } catch { return null; } },
  async put(k, v) { const f = path.join(dir, k); fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f, JSON.stringify(v)); },
  async del(k) { fs.rmSync(path.join(dir, k), { force: true }); },
  async list(sub) { try { return fs.readdirSync(path.join(dir, sub)).map((f) => sub + f); } catch { return []; } },
};
const t0 = Date.now();
const r = await runLive({ gql: makeGql({ token, log: console.log }), store, countryCode: cc, log: console.log });
console.log(JSON.stringify(r), `${((Date.now() - t0) / 1000).toFixed(1)}s`);
