#!/usr/bin/env node
// Stress tests for Workshop Live. Node 18 or newer, nothing to install.
//
//   node tools/stress.mjs viewers  [--users 50] [--every 5] [--for 60] [--models]
//       Many students with the class screen open: each virtual viewer polls /api/state like a
//       browser tab does, and with --models first downloads every published model once.
//       Costs no Blob operations (reads go through Vercel's cache).
//
//   node tools/stress.mjs publish  [--groups 8] [--rounds 3] [--yes]
//       Many groups pressing Publish at the same instant, as throwaway test groups T1, T2, ...
//       (never a real group). After each round it watches /api/state until every test group shows
//       its newest version, and reports any that the list lost. Removes the test groups at the
//       end (--keep to leave them). Costs about 3 Blob operations per publish and per removal;
//       the estimate is shown first and nothing is sent without --yes.
//
//   node tools/stress.mjs cleanup  [--groups 8] [--yes]
//       Remove test groups left behind (T1 .. Tn).
//
// Common options: --url https://your-project.vercel.app (default below), --key PASSCODE (or the
// WORKSHOP_KEY environment variable; publish and cleanup only), --prefix T (test group names).
//
// Test groups show on the live class screen while they exist: run this outside class hours, or
// against a deployment with its own Blob store.
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { performance } from 'node:perf_hooks';

const DEFAULT_URL = 'https://tainanrocks.vercel.app';
const OPS_PER_PUBLISH = 3;   // put the model, list, put the list (lib/manifest.js)
const OPS_PER_REMOVE = 3;    // list, delete, put the list
const MAX_USERS = 500;

// ---------------------------------------------------------------- options
const [mode, ...rest] = process.argv.slice(2);
const opts = {};
for (let i = 0; i < rest.length; i++) {
  const a = rest[i];
  if (!a.startsWith('--')) fail(`Unexpected argument: ${a}`);
  const name = a.slice(2);
  if (['yes', 'keep', 'models'].includes(name)) opts[name] = true;
  else opts[name] = rest[++i];
}
const base = String(opts.url || DEFAULT_URL).replace(/\/+$/, '');
const num = (name, dflt, min, max) => {
  const v = opts[name] === undefined ? dflt : Number(opts[name]);
  if (!Number.isFinite(v) || v < min || v > max) fail(`--${name} must be a number from ${min} to ${max}.`);
  return v;
};

function fail(msg) { console.error(`stress: ${msg}`); process.exit(1); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fmtMs = (ms) => `${Math.round(ms)} ms`;

// ---------------------------------------------------------------- stats
class Stats {
  constructor() { this.times = []; this.status = {}; this.cache = {}; this.errors = {}; this.bytes = 0; }
  ok(ms, status, cache, bytes = 0) {
    this.times.push(ms);
    this.status[status] = (this.status[status] || 0) + 1;
    if (cache) this.cache[cache] = (this.cache[cache] || 0) + 1;
    this.bytes += bytes;
  }
  error(err) {
    const key = String(err && err.name === 'TimeoutError' ? 'timeout' : (err && err.message) || err).slice(0, 80);
    this.errors[key] = (this.errors[key] || 0) + 1;
  }
  pct(p) {
    if (!this.times.length) return NaN;
    const s = [...this.times].sort((a, b) => a - b);
    return s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))];
  }
  report(label) {
    const n = this.times.length, errs = Object.values(this.errors).reduce((a, b) => a + b, 0);
    console.log(`\n${label}: ${n} answered, ${errs} failed`);
    if (n) {
      console.log(`  time    p50 ${fmtMs(this.pct(50))} · p95 ${fmtMs(this.pct(95))} · p99 ${fmtMs(this.pct(99))} · max ${fmtMs(Math.max(...this.times))}`);
      console.log(`  status  ${Object.entries(this.status).map(([k, v]) => `${k}: ${v}`).join(' · ')}`);
    }
    if (Object.keys(this.cache).length) console.log(`  cache   ${Object.entries(this.cache).map(([k, v]) => `${k}: ${v}`).join(' · ')}`);
    if (this.bytes > 102400) console.log(`  data    ${(this.bytes / 1048576).toFixed(1)} MB`);
    for (const [k, v] of Object.entries(this.errors)) console.log(`  error   ${v}× ${k}`);
  }
}

async function timed(stats, url, init = {}) {
  const t0 = performance.now();
  try {
    const res = await fetch(url, { ...init, signal: AbortSignal.timeout(30000) });
    const body = await res.arrayBuffer();
    stats.ok(performance.now() - t0, res.status, res.headers.get('x-vercel-cache'), body.byteLength);
    return { res, body };
  } catch (err) {
    stats.error(err);
    return null;
  }
}

async function getState(fresh = false) {
  const res = await fetch(`${base}/api/state${fresh ? '?fresh=1' : ''}`, { signal: AbortSignal.timeout(30000) });
  if (!res.ok) throw new Error(`/api/state answered ${res.status}`);
  return res.json();
}

// ---------------------------------------------------------------- viewers
async function viewers() {
  const users = num('users', 50, 1, MAX_USERS), every = num('every', 5, 1, 600), seconds = num('for', 60, 5, 3600);
  console.log(`${users} viewers polling ${base}/api/state every ${every} s for ${seconds} s ` +
    `(about ${(users / every).toFixed(1)} requests a second${opts.models ? ', plus every model once per viewer' : ''}).`);
  const state = new Stats(), models = new Stats();
  let urls = [];
  if (opts.models) {
    const s = await getState();
    urls = Object.values(s.blocks || {}).map((b) => b && b.url).filter(Boolean);
    console.log(`${urls.length} published models to download per viewer.`);
  }
  const end = performance.now() + seconds * 1000;
  const ticker = setInterval(() => process.stdout.write(`\r  ${state.times.length} polls answered…`), 1000);
  await Promise.all(Array.from({ length: users }, async () => {
    await sleep(Math.random() * every * 1000);   // tabs don't all open in the same instant
    for (const u of urls) await timed(models, u);
    while (performance.now() < end) {
      const t0 = performance.now();
      await timed(state, `${base}/api/state`);
      await sleep(Math.max(0, every * 1000 - (performance.now() - t0)));
    }
  }));
  clearInterval(ticker);
  state.report('/api/state polls');
  if (opts.models) models.report('model downloads');
  console.log('\nWhat to look for: no failures, p95 well under a second, and mostly HIT or STALE in the cache line.');
}

// ---------------------------------------------------------------- publish
function testNames() {
  const groups = num('groups', 8, 1, 50), prefix = String(opts.prefix || 'T').toUpperCase();
  if (!/^[A-Z][A-Z0-9_-]{0,15}$/.test(prefix)) fail('--prefix must start with a letter (letters, digits, - and _).');
  const names = Array.from({ length: groups }, (_, i) => `${prefix}${i + 1}`);
  // never a name the class screen reads as a real group (1, G1, GROUP-1, ...)
  const real = names.filter((n) => /^(?:GROUP|GRP|G)?[-_]?\d{1,3}$/.test(n));
  if (real.length) fail(`--prefix ${prefix} would publish as real groups (${real.join(', ')}); pick another.`);
  return names;
}
function needKey() {
  const key = opts.key || process.env.WORKSHOP_KEY;
  if (!key) fail('Give the workshop passcode with --key or the WORKSHOP_KEY environment variable.');
  return key;
}
function confirm(ops) {
  console.log(`This uses about ${ops} of the 2,000 "advanced" Blob operations in the monthly allowance.`);
  if (!opts.yes) { console.log('Nothing sent. Run again with --yes to go ahead.'); process.exit(0); }
}

async function removeGroups(names, key) {
  const st = new Stats();
  for (const n of names) {   // one at a time: removals rewrite the list too, and would race
    const r = await timed(st, `${base}/api/publish?block=${encodeURIComponent(n)}`, { method: 'DELETE', headers: { 'x-workshop-key': key } });
    if (r && !r.res.ok) console.log(`  ${n}: ${r.res.status} ${new TextDecoder().decode(r.body).slice(0, 120)}`);
  }
  st.report('removals');
}

async function publish() {
  const names = testNames(), rounds = num('rounds', 3, 1, 20), key = needKey();
  const model = new Uint8Array(await readFile(opts.model || fileURLToPath(new URL('./stress-cube.3dm', import.meta.url))));
  console.log(`${rounds} round${rounds === 1 ? '' : 's'} of ${names.length} publishes at the same instant to ${base}, ` +
    `as test groups ${names[0]}..${names[names.length - 1]} (${(model.length / 1024).toFixed(0)} KB each).`);
  confirm(names.length * rounds * OPS_PER_PUBLISH + (opts.keep ? 0 : names.length * OPS_PER_REMOVE));

  const st = new Stats();
  let lost = 0;
  try {
    for (let round = 1; round <= rounds; round++) {
      console.log(`\nRound ${round}: publishing ${names.length} at once…`);
      const latest = {};
      await Promise.all(names.map(async (n) => {
        const r = await timed(st, `${base}/api/publish?block=${encodeURIComponent(n)}`, {
          method: 'POST', body: model, headers: { 'x-workshop-key': key, 'content-type': 'application/octet-stream' } });
        if (!r) return;
        let j = {};
        try { j = JSON.parse(new TextDecoder().decode(r.body)); } catch { /* not JSON */ }
        if (r.res.ok && j.ok) latest[n] = j.url;
        else console.log(`  ${n}: ${r.res.status} ${j.error || ''}`);
      }));
      console.log(`  ${Object.keys(latest).length}/${names.length} accepted. Watching /api/state (its cache can be up to a minute behind)…`);
      // Every accepted publish should show as its group's newest version once the cache catches up.
      const t0 = performance.now();
      let missing = Object.keys(latest);
      while (missing.length && performance.now() - t0 < 120000) {
        await sleep(10000);
        try {
          const blocks = (await getState()).blocks || {};
          missing = missing.filter((n) => !blocks[n] || blocks[n].url !== latest[n]);
        } catch (err) { console.log(`  /api/state: ${err.message}`); }
      }
      if (missing.length) {
        lost += missing.length;
        console.log(`  After 2 minutes the list still misses the newest version of ${missing.join(', ')}: ` +
          'a lost update (it heals on the next publish, or when someone presses Update).');
      } else console.log(`  All showing after ${Math.round((performance.now() - t0) / 1000)} s.`);
    }
  } finally {
    st.report('publishes');
    console.log(lost ? `\nLost updates: ${lost} (see above).` : '\nNo lost updates.');
    if (opts.keep) console.log(`Test groups kept; remove them with: node tools/stress.mjs cleanup --groups ${names.length} --yes`);
    else { console.log('\nRemoving the test groups…'); await removeGroups(names, key); }
  }
}

async function cleanup() {
  const names = testNames(), key = needKey();
  console.log(`Removing test groups ${names[0]}..${names[names.length - 1]} from ${base}, one at a time.`);
  confirm(names.length * OPS_PER_REMOVE);
  await removeGroups(names, key);
}

const modes = { viewers, publish, cleanup };
if (!modes[mode]) {
  console.log('Usage: node tools/stress.mjs viewers|publish|cleanup [options]   (see the top of this file)');
  process.exit(mode ? 1 : 0);
}
await modes[mode]();
