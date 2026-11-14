// Create Flow Characters from a character bible (v2), in ONE tab of its own (the image run keeps its tab).
//
//   node flow/char.mjs <bible dir> <key|key:variant> [<key|key:variant> ...] [--resend <key|key:variant>] [--plan <plan.json>]
//   node flow/char.mjs <bible dir> --finish <key|key:variant> <character url> [--plan <plan.json>]
//                                   (portrait and name exist: add the body)
//
// <bible dir>/characters.json must be a v2 bible ("version": 2: style + characters, each with look, role,
// variants). "key" means the "base" variant; "key:variant" names another. A variant's own look wins; with
// look null the character's look is used. The Flow Character is named from the variant's flow_name, the
// info field is the character's role. The plan (for the Flow project address, and optional "cdp" and
// "image_model") is --plan, else <bible dir>/../flow/plan.json.
//
// Environment: FLOW_CDP (Chrome address), FLOW_MODEL (expected image model, default plan.image_model, else
// "Nano Banana 2.1"), FLOW_ALLOW_AGENT=1 (Agent mode is otherwise refused).
//
// Per character: portrait from the style + look, name, info, then "Create body", save both images to
// <bible dir>/flow/ (<key>-portrait.jpg / <key>-body.jpg; variants <key>.<variant>-...), click Done, and set
// "flow_made": true on that variant in characters.json (atomic rewrite). One tab is used for the whole run
// and left open; the script never closes a tab. A journal (<bible dir>/flow/journal.jsonl) records "sending"
// before each click, so a character that may already exist in Flow is never created twice: look in Flow,
// then rerun with --resend <key>.
import fs from 'node:fs';
import {chromium} from 'playwright-core';
import {loadPlan, cdpUrl, expectedModel, modelMatches, chipModel} from './plan.mjs';
import {loadBible, resolveTarget, markFlowMade} from './bible.mjs';
import {agentMode, front, raise} from './page.mjs';

const args = process.argv.slice(2);
const ri = args.indexOf('--resend'), resend = ri >= 0 ? args.splice(ri, 2)[1] : null;
const pi = args.indexOf('--plan'), planArg = pi >= 0 ? args.splice(pi, 2)[1] : null;
const fi = args.indexOf('--finish'), finish = fi >= 0 ? args.splice(fi, 3).slice(1) : null;
const [bibleDir, ...keys] = finish ? [args[0], finish[0]] : args;
if (!bibleDir || !keys.length) { console.log('usage: node flow/char.mjs <bible dir> <key|key:variant> ... [--resend <key>] [--plan <plan.json>]'); process.exit(2); }
let bible, targets, plan;
try {
  bible = loadBible(bibleDir);
  targets = keys.map(k => { try { return resolveTarget(bible, k); } catch (e) { console.log(k, '-', e.message); return null; } });
  if (targets.some(t => !t) && !finish) { /* reported above; the valid ones still run */ }
  const planFile = planArg || `${bibleDir}/../flow/plan.json`;
  if (!fs.existsSync(planFile)) throw new Error(`no plan at ${planFile}: give --plan <plan.json> (it holds the Flow project address)`);
  plan = loadPlan(planFile);
} catch (e) { console.log(e.message); process.exit(2); }
const outDir = `${bibleDir}/flow`, journal = `${outDir}/journal.jsonl`;
fs.mkdirSync(outDir, {recursive: true});
const EXPECTED_MODEL = expectedModel(plan);

const note = (id, state, extra = {}) => fs.appendFileSync(journal, JSON.stringify({id, state, ...extra, at: new Date().toISOString()}) + '\n');
const lastState = id => {
  if (!fs.existsSync(journal)) return null;
  const ev = fs.readFileSync(journal, 'utf8').split('\n').filter(Boolean).map(JSON.parse).filter(e => e.id === id);
  return ev.length ? ev[ev.length - 1].state : null;
};
if (resend) note(resend, 'resend-approved');

const b = await chromium.connectOverCDP(cdpUrl(plan));
let p;
const wait = ms => p.waitForTimeout(ms);
const log = (id, ...a) => console.log(new Date().toISOString().slice(11, 19), id, ...a);
const genImgs = () => p.$$eval('img', els => els.filter(e => e.naturalWidth >= 600 && /flow-content|flow\.google\.com\/asb\//.test(e.currentSrc)).map(e => e.currentSrc.split('?')[0]));
const pageText = () => p.evaluate(() => document.body.innerText);
async function saveImg(src, file) {
  const data = await p.evaluate(async s => {
    const img = [...document.images].find(e => e.currentSrc.startsWith(s));
    const a = new Uint8Array(await (await fetch(img.currentSrc)).arrayBuffer());
    let t = ''; for (let i = 0; i < a.length; i += 0x8000) t += String.fromCharCode(...a.subarray(i, i + 0x8000));
    return btoa(t);
  }, src);
  fs.writeFileSync(file, Buffer.from(data, 'base64'));
}
async function waitNew(before, label, id) {
  for (let i = 0; i < 60; i++) {           // 3 minutes
    await wait(3000);
    const n = (await genImgs()).find(s => !before.has(s));
    if (n) return n;
    if (i % 5 === 4 && /violat|policy|can't generate|couldn't generate|unable to generate|try again/i.test(await pageText())) throw new Error(`${label}: Flow refused or failed - see the tab`);
  }
  throw new Error(`${label} did not appear in 3 minutes`);
}
async function prompt(text) {
  if ((await agentMode(p)) && !process.env.FLOW_ALLOW_AGENT) throw new Error('Flow is in Agent mode - switch it off (or set FLOW_ALLOW_AGENT=1) - nothing sent');
  const box = p.locator('div[role=textbox]').first();
  await box.click(); await box.fill(text); await wait(800);
  const chip = await p.locator('button[aria-label="Select model family"]').first().innerText().catch(() => '');
  const found = chipModel(chip);
  if (!modelMatches(EXPECTED_MODEL, found)) throw new Error(`model is "${found}", expected "${EXPECTED_MODEL}" - nothing sent`);
}

// One tab for the whole run, never closed. A tab this script opened earlier is found by the window name it
// set on itself (window.name survives reloads and same-site navigation; it is set again after every goto).
const TAB_MARK = 'novel-flow-char-tab';
async function ensureTab() {
  const pages = b.contexts().flatMap(x => x.pages());
  if (finish) { const f = pages.find(x => x.url() === finish[1]); if (f) return f; }
  for (const pg of pages) { try { if (await pg.evaluate(m => window.name === m, TAB_MARK)) return pg; } catch { /* a page that cannot be read is not ours */ } }
  return await b.contexts()[0].newPage();
}

let failed = 0, fronted = false;
for (const t of targets) {
  if (!t) { failed++; continue; }
  const key = t.id, c = {flow_name: t.flowName, role: t.role};
  if (t.flowMade && !resend && !finish && lastState(key) !== 'resend-approved') { log(key, 'flow_made is true in the bible - skipped (use --resend to make it again)'); continue; }
  const st = lastState(key);
  if (st === 'done') { log(key, 'already created - skipped'); continue; }
  if (!finish && st && st !== 'resend-approved' && st !== 'error-before-send') {
    log(key, `NEEDS CHECK: journal says "${st}". Look in Flow, then rerun with --resend ${key}`); failed++; continue;
  }
  p = await ensureTab();
  if (!fronted) { fronted = true; await front(p); }   // FLOW_FRONT=1 only: once per run
  let stage = 'before-send';
  try {
    await p.goto(finish ? finish[1] : `${plan.project}/character`, {waitUntil: 'domcontentloaded', timeout: 60000}); await wait(5000);
    await p.evaluate(m => { window.name = m; }, TAB_MARK).catch(() => {});
    if (/accounts\.google\.com|signin/i.test(p.url())) throw new Error('Flow is asking you to sign in');
    if (/captcha|unusual traffic|limit/i.test(await pageText())) throw new Error('Flow shows a limit or CAPTCHA - stopping');
    let before;
    const localPortrait = `${outDir}/${t.stem}-portrait.jpg`;
    if (finish) for (let i = 0; i < 10 && !(await genImgs()).length; i++) await wait(2000);   // reloads show it late
    const hasPortrait = finish && (await genImgs()).length > 0;
    if (finish && !hasPortrait && fs.existsSync(localPortrait)) {
      // Flow dropped a portrait it made earlier: upload the copy saved then, no new generation
      before = new Set(await genImgs());
      stage = 'portrait';
      const input = p.locator('input[type=file]');
      if (!(await input.count())) { await p.locator('button:has-text("Upload")').first().click(); await wait(1500); }
      if (!(await input.count())) throw new Error('no file input for the upload - add the portrait by hand');
      await input.first().setInputFiles(localPortrait);
      log(key, 'portrait uploaded from', localPortrait);
      await waitNew(before, 'uploaded portrait', key); await wait(3000);
    } else if (!hasPortrait) {
      if (finish) { await p.locator('button:has-text("Create portrait")').first().click({timeout: 15000}); await wait(1500); }
      await prompt(t.portrait);
      before = new Set(await genImgs());
      note(key, 'sending', finish ? {url: finish[1]} : {}); stage = 'portrait';
      await p.locator('button[aria-label="Start generation"]').first().click({timeout: 15000});
      log(key, 'portrait sent');
      const portrait = await waitNew(before, 'portrait', key);
      await wait(3000);
      note(key, 'portrait', {url: p.url()});
      await saveImg(portrait, localPortrait);
    } else {
      stage = 'portrait'; log(key, 'portrait already on the page');
      if (!fs.existsSync(localPortrait)) await saveImg((await genImgs())[0], localPortrait);
    }
    const named = (await p.evaluate(() => document.body.innerText)).includes(c.flow_name);
    if (!named) {
      await p.getByText(/Untitled character|Character name/).last().click({timeout: 10000}).catch(() => {});
      const title = p.locator('input[aria-label="Editable text"]').first();
      if (await title.count()) { await title.fill(c.flow_name); await p.keyboard.press('Enter'); }
      else throw new Error('name field not found - set the name by hand');
      await wait(800);
      const info = p.locator('textarea').first(); await info.click(); await info.fill(c.role); await wait(800);
      await p.mouse.click(600, 120); await wait(1000);
      log(key, `named "${c.flow_name}"`);
    }

    // The chip sometimes cannot be clicked normally (history panel, animation): fall back to a DOM click.
    // Either way this only opens the Body view; nothing is generated until "Start generation".
    const cb = p.locator('button:has-text("Create body")').first();
    await cb.click({timeout: 8000}).catch(async () => { await cb.evaluate(el => el.click()); });
    await wait(3000);
    before = new Set(await genImgs());   // after the Body view opens: it re-serves old images under new URLs
    await prompt(t.body);
    stage = 'body'; note(key, 'body-sending');
    await p.locator('button[aria-label="Start generation"]').first().click({timeout: 15000});
    log(key, 'body sent');
    const body = await waitNew(before, 'body', key);
    await wait(3000);
    await saveImg(body, `${outDir}/${t.stem}-body.jpg`);
    await p.locator('button:has-text("Done")').first().click({timeout: 15000}); await wait(2500);
    note(key, 'done', {url: p.url()});
    log(key, 'done');
    markFlowMade(bibleDir, t.key, t.variant);
  } catch (e) {
    const msg = e.message.split('\n')[0];
    note(key, stage === 'before-send' ? 'error-before-send' : `error-${stage}`, {error: msg});
    console.error(key, 'ERROR', msg, stage === 'before-send' ? '' : '(tab left open for you to check)');
    if (/limit|CAPTCHA|sign in/i.test(msg)) { await raise(p); process.exit(5); }
    failed++;
    if (stage !== 'before-send' || failed >= 3) break;     // after a send the tab stays as it is, for you to check
  }
  await new Promise(r => setTimeout(r, 4000));
}
process.exit(failed ? 1 : 0);
