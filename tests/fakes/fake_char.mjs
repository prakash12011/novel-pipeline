// Stand-in for flow/char.mjs in tests (env FLOW_CHAR). Same argument shape:
//   node fake_char.mjs <bible dir> <key|key:variant> ... [--resend <id>] [--plan <plan.json>]
// FAKE_CHAR_SCRIPT is JSON {id: ["ok" | "fail" | "wall", ...]}: each call for an id consumes the next outcome
// (the last repeats). ok = journals like char.mjs, writes portrait/body files, sets flow_made in the bible.
// wall = journals an error-before-send with a CAPTCHA text and exits 5. Calls are logged to <bible>/flow/char-calls.log
// as "<ids> front=<FLOW_FRONT> resend=<id|->".
import fs from 'node:fs';
import {loadBible, resolveTarget, markFlowMade} from '../../flow/bible.mjs';

const args = process.argv.slice(2);
const ri = args.indexOf('--resend'), resend = ri >= 0 ? args.splice(ri, 2)[1] : null;
const pi = args.indexOf('--plan'); if (pi >= 0) args.splice(pi, 2);
const [bibleDir, ...ids] = args;
const outDir = `${bibleDir}/flow`; fs.mkdirSync(outDir, {recursive: true});
const journal = `${outDir}/journal.jsonl`;
const note = (id, state, extra = {}) => fs.appendFileSync(journal, JSON.stringify({id, state, ...extra, at: new Date().toISOString()}) + '\n');
const last = id => {
  if (!fs.existsSync(journal)) return null;
  const ev = fs.readFileSync(journal, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l)).filter(e => e.id === id);
  return ev.length ? ev[ev.length - 1].state : null;
};
fs.appendFileSync(`${outDir}/char-calls.log`, `${ids.join(',')} front=${process.env.FLOW_FRONT || '-'} resend=${resend || '-'}\n`);
const script = JSON.parse(process.env.FAKE_CHAR_SCRIPT || '{}');
const countsFile = `${outDir}/char-counts.json`;
const counts = fs.existsSync(countsFile) ? JSON.parse(fs.readFileSync(countsFile, 'utf8')) : {};
if (resend) note(resend, 'resend-approved');
let failed = 0;
for (const id of ids) {
  const bible = loadBible(bibleDir);
  const t = resolveTarget(bible, id);
  const st = last(id);
  if (t.flowMade && !resend) { console.log(id, 'flow_made is true - skipped'); continue; }
  if (st === 'done') { console.log(id, 'already created - skipped'); continue; }
  const list = script[id] || ['ok'];
  const n = counts[id] || 0; counts[id] = n + 1;
  fs.writeFileSync(countsFile, JSON.stringify(counts));
  const outcome = list[Math.min(n, list.length - 1)];
  if (outcome === 'wall') {
    note(id, 'error-before-send', {error: 'Flow shows a limit or CAPTCHA - stopping'});
    console.error(id, 'ERROR Flow shows a limit or CAPTCHA - stopping');
    process.exit(5);
  }
  if (outcome === 'fail') { note(id, 'error-before-send', {error: 'portrait did not appear'}); console.error(id, 'ERROR portrait did not appear'); failed++; continue; }
  note(id, 'sending'); note(id, 'portrait');
  fs.writeFileSync(`${outDir}/${t.stem}-portrait.jpg`, 'fake portrait');
  note(id, 'body-sending');
  fs.writeFileSync(`${outDir}/${t.stem}-body.jpg`, 'fake body');
  note(id, 'done');
  markFlowMade(bibleDir, t.key, t.variant);
  console.log(id, `done as "${t.flowName}"`);
}
process.exit(failed ? 1 : 0);
