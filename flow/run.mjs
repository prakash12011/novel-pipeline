// Run an episode plan's shots in order, one gen.mjs process per shot, safe to stop and rerun.
//
//   node flow/run.mjs <plan.json> [--only c0001] [--max 22] [--pause 8] [--chapter-pause 300] [--resend <shotId>]
//                     [--batch 5 --batch-pause 45]   send 5 prompts side by side, then download all 5 from the list
//
// --chapter-pause: seconds to rest after each chapter's images before starting the next (default 300),
// so Flow sees human-sized bursts rather than hundreds of images back to back.
//
// It never sends a shot twice on its own. Replaces batch-v2.sh, which could: its retry hid a second
// "failed after sending", and with no jpg on disk the next run sent that prompt again.
//   - a valid jpg on disk                      -> skipped
//   - journaled as sending/sent with no image  -> download-only recovery if a tile was found,
//                                                 otherwise NEEDS CHECK (look in Flow, then --resend)
//   - exit 2 (before sending)                  -> retried once
//   - exit 4 (refused)                         -> recorded; the scene gets rewritten, not resent
//   - exit 5, three failures in a row, or Flow -> stop
//     shortening a prompt
//
// Stop file: create <plan.stop_file> (default: a file named STOP next to the journal) and the run ends
// cleanly BEFORE the next shot or batch is sent (never while gen.mjs is running). It writes status.json
// with stopped: "stop file", deletes the stop file, and exits with code 6. Shots not yet sent stay
// eligible for the next run (nothing is journaled for them).
//
// Exit codes: 0 finished, 1 stopped by a problem (preflight failed, wall, three failures ...),
//             6 stopped on request through the stop file (orderly, nothing is wrong).
//
// Paths: plan paths (journal, stop_file, each shot's prompt/out/meta/used) may be absolute, or relative
// to the plan's optional "base" (else to the current directory). gen.mjs and preflight.mjs are found next
// to this file, so the current directory does not matter.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {loadPlan} from './plan.mjs';

const args = process.argv.slice(2);
const planFile = args[0];
const opt = (name, dflt) => { const i = args.indexOf(name); return i > 0 ? args[i + 1] : dflt; };
const only = opt('--only', ''), max = Number(opt('--max', 1e9)), pause = Number(opt('--pause', 3)) * 1000;
const resend = opt('--resend', null);
const chapterPause = Number(opt('--chapter-pause', 300)) * 1000;
const batchSize = Math.max(1, Number(opt('--batch', 1)));            // prompts sent side by side (Flow generates them in parallel)
const batchPause = Number(opt('--batch-pause', 45)) * 1000;          // rest between batches
const restEvery = Number(opt('--rest-every', 50));                       // after this many images ...
const restFor = Number(opt('--rest', 120)) * 1000;                    // ... rest this long, so Flow sees human-sized bursts (0 = never)
const plan = loadPlan(planFile);
const here = p => fileURLToPath(new URL(p, import.meta.url));
const STOP_FILE = plan.stop_file || path.join(path.dirname(plan.journal), 'STOP');
const STOP_REASON = 'stop file', STOP_EXIT = 6;
const stopRequested = () => fs.existsSync(STOP_FILE);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const journalAppend = (id, state) => fs.appendFileSync(plan.journal, JSON.stringify({id, state, at: new Date().toISOString()}) + '\n');

function history() {
  const h = {};
  if (fs.existsSync(plan.journal)) {
    for (const line of fs.readFileSync(plan.journal, 'utf8').split('\n')) {
      if (!line.trim()) continue;
      const e = JSON.parse(line); (h[e.id] ||= []).push(e);
    }
  }
  return h;
}

function jpegOk(file) {
  if (!fs.existsSync(file)) return false;
  const x = fs.readFileSync(file);
  return x.length > 50000 && x[0] === 0xFF && x[1] === 0xD8 && x[x.length - 2] === 0xFF && x[x.length - 1] === 0xD9;
}

// Was this shot possibly sent without its image arriving? Only the events after the last
// clean end (saved, or failed before sending, or an approved resend) count.
function maybeSent(events) {
  let state = null, tile = null;
  for (const e of events || []) {
    if (['saved', 'error-before-send', 'resend-approved'].includes(e.state)) { state = null; tile = null; }
    else if (['sending', 'sent', 'tile', 'error-after-send', 'refused', 'wall'].includes(e.state)) state = e.state;
    if (e.tile && e.state !== 'saved') tile = e.tile;
  }
  return {state, tile};
}

// FLOW_FRONT=1 raises the Flow tab ONCE per run: only the first child process (preflight, else the first gen) sees it.
let frontPending = process.env.FLOW_FRONT === '1';
const childEnv = () => { const e = {...process.env}; if (frontPending) e.FLOW_FRONT = '1'; else delete e.FLOW_FRONT; frontPending = false; return e; };
const GEN = process.env.FLOW_GEN || here('./gen.mjs');   // tests swap in a fake Flow
const gen = (id, recover = false) =>
  spawnSync('node', [GEN, planFile, id, ...(recover ? ['--recover'] : [])], {stdio: 'inherit', env: childEnv()}).status;

// Settings are checked once per run, not per image: opening Flow's panels 22 times would be slow,
// and nothing changes them mid-run except a person. Skipped when tests swap in a fake Flow.
if (!process.env.FLOW_GEN && !args.includes('--upgrade') && spawnSync('node', [here('./preflight.mjs'), planFile], {stdio: 'inherit', env: childEnv()}).status !== 0) {
  console.log('\npreflight failed - nothing sent');
  process.exit(1);
}
if (resend) journalAppend(resend, 'resend-approved');
const prefixes = only ? only.split(',').map(p => p.trim()) : [''];
const shots = plan.shots.filter(s => prefixes.some(p => s.id.startsWith(p)));

// --upgrade: replace every saved 1K image (1376 px wide) with its 2K version. No prompt is sent: each
// image's own page is reopened and its 2K download requested. The 1K file is kept in <chapter>/1k/ and
// replaced only when a full-size file arrives. Three failures in a row mean the upscaler is still down.
if (args.includes('--upgrade')) {
  const widthOf = file => {
    const x = fs.readFileSync(file);
    for (let i = 2; i < x.length - 9;) {
      if (x[i] !== 0xFF) { i++; continue; }
      const m = x[i + 1];
      if ([0xC0, 0xC1, 0xC2].includes(m)) return x.readUInt16BE(i + 7);
      i += 2 + x.readUInt16BE(i + 2);
    }
    return 0;
  };
  const todo = shots.filter(s => jpegOk(s.out) && widthOf(s.out) < 2000);
  console.log(`${todo.length} images at 1K to upgrade`);
  let upgraded = 0, fails = 0;
  for (const shot of todo) {
    if (upgraded >= max) break;
    if (stopRequested()) { fs.rmSync(STOP_FILE, {force: true}); console.log('STOPPED: stop file found - upgrade ended before the next image'); process.exit(STOP_EXIT); }
    const code = spawnSync('node', [GEN, planFile, shot.id, '--upgrade'], {stdio: 'inherit', env: childEnv()}).status;
    if (code === 0) { upgraded++; fails = 0; }
    else if (code === 5) { console.log('STOPPED: Flow put up a limit or check - see above'); process.exitCode = 1; break; }
    else if (++fails >= 3) { console.log('STOPPED: three upgrades failed in a row - the 2K upscaler is probably still down; try later'); process.exitCode = 1; break; }
    await sleep(pause);
  }
  console.log(`\nupgraded ${upgraded}, left ${todo.length - upgraded}`);
  process.exit(process.exitCode || 0);
}
const report = {saved: [], skipped: 0, needs_check: [], refused: [], stopped: null};
let failures = 0, generated = 0, lastChapter = null;
let sinceRest = 0;                // images sent since the last long rest
async function restIfDue() {
  if (restEvery > 0 && restFor > 0 && sinceRest >= restEvery) {
    console.log(`${sinceRest} images sent - resting ${restFor / 60000} min`);
    await sleep(restFor); sinceRest = 0;
  }
}
const consumed = new Set();       // shots already handled as part of a batch
const deferred = [];              // batch members that were never sent (a click failed first): tried again at the end of the chapter
const tries = new Map();
async function retryDeferred() {
  const again = shots.filter(s => deferred.includes(s.id));
  deferred.length = 0;
  for (const s of again) consumed.delete(s.id);
  console.log(`retrying ${again.length} shot(s) that were never sent: ${again.map(s => s.id).join(' ')}`);
  await sweep(again);
}

async function sweep(list) {
for (const shot of list) {
  if (consumed.has(shot.id)) continue;
  if (jpegOk(shot.out)) { report.skipped++; continue; }
  if (generated >= max) break;
  if (stopRequested()) { report.stopped = STOP_REASON; break; }
  const {state, tile} = maybeSent(history()[shot.id]);
  if (state === 'refused') { report.refused.push(shot.id); continue; }
  if (state && state !== 'wall') {
    if (tile && gen(shot.id, true) === 0) { report.saved.push(shot.id); continue; }
    console.log(`${shot.id}: NEEDS CHECK - it may have been sent already. Look in Flow, then rerun with --resend ${shot.id}`);
    report.needs_check.push(shot.id);
    continue;
  }

  const chapter = shot.id.split('-')[0];
  if (lastChapter && chapter !== lastChapter && deferred.length) await retryDeferred();
  if (report.stopped) break;
  if (lastChapter && chapter !== lastChapter && chapterPause > 0) {
    console.log(`${lastChapter} done - resting ${chapterPause / 60000} min before ${chapter}`);
    await sleep(chapterPause); sinceRest = 0;
    if (stopRequested()) { report.stopped = STOP_REASON; break; }    // asked to stop during the rest: nothing sent yet
  }
  lastChapter = chapter;
  if (batchSize > 1) {
    // This shot plus the next clean ones of the same chapter, up to the batch size.
    const group = [shot];
    for (const nx of shots.slice(shots.indexOf(shot) + 1)) {
      if (group.length >= batchSize || generated + group.length >= max) break;
      if (nx.id.split('-')[0] !== chapter) break;
      if (jpegOk(nx.out) || maybeSent(history()[nx.id]).state) continue;
      group.push(nx);
    }
    group.forEach(g => consumed.add(g.id));
    generated += group.length; sinceRest += group.length;
    const bcode = spawnSync('node', [GEN, planFile, group.map(g => g.id).join(','), '--batch'], {stdio: 'inherit', env: childEnv()}).status;
    let okCount = 0;
    for (const g of group) {              // the files and the journal say what happened, not the exit code
      if (jpegOk(g.out)) { report.saved.push(g.id); okCount++; continue; }
      const st = maybeSent(history()[g.id]).state;
      if (st === 'refused') report.refused.push(g.id);
      else if (st) { report.needs_check.push(g.id); console.log(`${g.id}: NEEDS CHECK - sent, but its image was not downloaded`); }
      else { tries.set(g.id, (tries.get(g.id) || 0) + 1); if (tries.get(g.id) < 3) deferred.push(g.id); }
    }
    if (bcode === 5) { report.stopped = `${group[0].id}...: Flow put up a limit, CAPTCHA, sign-in or aspect problem - see above`; break; }
    if (okCount === 0) { if (++failures >= 3) { report.stopped = 'three batches in a row saved nothing'; break; } } else failures = 0;
    await sleep(batchPause);
    await restIfDue();
    continue;
  }
  generated++; sinceRest++;
  let code = gen(shot.id);
  if (code === 2) { console.log(`${shot.id}: failed before sending, retrying once`); await sleep(pause); code = gen(shot.id); }
  if (code === 3) {
    const t = maybeSent(history()[shot.id]).tile;
    code = t && gen(shot.id, true) === 0 ? 0 : 3;
  }
  if (code === 0) {
    report.saved.push(shot.id); failures = 0;
    const meta = JSON.parse(fs.readFileSync(shot.meta, 'utf8'));
    if (meta.prompt_shortened) { report.stopped = `Flow shortened the prompt of ${shot.id}: paste flow/agent-instruction.txt into the project's agent instructions`; break; }
  } else if (code === 4) {
    report.refused.push(shot.id); failures = 0;
  } else if (code === 5) {
    report.stopped = `${shot.id}: Flow put up a limit, CAPTCHA, sign-in or aspect problem - see above`; break;
  } else {
    if (code === 3) report.needs_check.push(shot.id);
    if (++failures >= 3) { report.stopped = 'three failures in a row'; break; }
  }
  await sleep(pause);
  await restIfDue();
}
}
await sweep(shots);
for (let r = 0; r < 2 && deferred.length && !report.stopped; r++) await retryDeferred();

const left = shots.filter(s => !jpegOk(s.out)).length;
if (!report.stopped && stopRequested()) fs.rmSync(STOP_FILE, {force: true});   // asked during the last shot: nothing more was due anyway
console.log(`\nsaved ${report.saved.length}, already done ${report.skipped}, left ${left}`);
if (report.needs_check.length) console.log('NEEDS CHECK (never resent automatically):', report.needs_check.join(' '));
if (report.refused.length) console.log('REFUSED (scene needs rewriting):', report.refused.join(' '));
if (report.stopped === STOP_REASON) {
  fs.rmSync(STOP_FILE, {force: true});
  console.log(`STOPPED: stop file found - ended cleanly before the next shot (${left} left; rerun to continue)`);
  process.exitCode = STOP_EXIT;
} else if (report.stopped) { console.log('STOPPED:', report.stopped); process.exitCode = 1; }
fs.writeFileSync(planFile.replace(/plan\.json$/, 'status.json'), JSON.stringify({...report, left, at: new Date().toISOString()}, null, 1));
