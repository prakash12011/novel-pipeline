// Generate ONE shot in Google Flow from an episode plan, attaching that shot's Flow Characters.
// Based on D:/Automations/stickman-projects/hungry-tree/tools/gen2.mjs, generalised to a plan file.
//
//   node flow/gen.mjs <plan.json> <shotId>             send the prompt, wait, download 2K
//   node flow/gen.mjs <plan.json> <shotId> --recover   only download the tile already journaled for it
//
// Exit codes (flow/run.mjs acts on these):
//   0 saved
//   2 failed BEFORE sending - safe to retry
//   3 failed AFTER sending  - never resend automatically; a tile may already exist
//   4 Flow refused the prompt
//   5 stop everything: limit, CAPTCHA, sign-in wall, or the wrong aspect ratio set in Flow
//
// Every step is journaled to plan.journal BEFORE it happens where it matters: "sending" is written
// before the click, so a crash in between is still known to have possibly sent.
import fs from 'node:fs';
import {chromium} from 'playwright-core';
import {agentMode, closePanels, findOption, front, imageChip, modeChip, raise} from './page.mjs';
import {cdpUrl, loadPlan} from './plan.mjs';

let [planFile, id, mode] = process.argv.slice(2);   // --batch: id is a comma list, and id/shot follow each shot in turn
const plan = loadPlan(planFile);
let shot = plan.shots.find(s => s.id === id);
if (!shot && mode !== '--batch') { console.error(`unknown shot ${id}`); process.exit(2); }
const projectId = (plan.project.split('/project/')[1] || '').split(/[/?#]/)[0];
const journal = (state, extra = {}) =>
  fs.appendFileSync(plan.journal, JSON.stringify({id, state, at: new Date().toISOString(), ...extra}) + '\n');
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), id, ...a);

// Text Flow shows when it will not, or cannot, go on. Only lines that APPEAR after sending are
// tested, so wording already on the page (a footer mentioning policies) can't trigger them.
const WALL = /captcha|unusual (traffic|activity)|not a robot|sign in to continue|out of credits|not enough credits|daily limit|reached (your|the) limit|limit reached|quota|too many requests|try again later/i;
const REFUSED = /couldn.?t generate|can.?t generate|unable to generate|violat|against our (policies|guidelines)|might be (harmful|inappropriate|unsafe)|try a different prompt/i;
class Stop extends Error {}
class Refused extends Error {}

let b, p, sent = false;
const wait = ms => p.waitForTimeout(ms);
const norm = s => (s || '').split('?')[0].replace(/=s\d+[^/]*$/, '');
const tiles = () => p.$$eval('img[alt^="Tile displaying"]', els => els.map(e => e.currentSrc.split('?')[0]));

const lines = async () => new Set((await p.evaluate(() => document.body.innerText)).split('\n').map(s => s.trim()).filter(Boolean));
const chips = () => p.$$eval('img', els => els.filter(e => /ingredient image/i.test(e.alt) && e.getBoundingClientRect().top > window.innerHeight * 0.5).map(e => (e.currentSrc || e.src).split('?')[0].replace(/=s\d+[^/]*$/, '')));
const attached = [];

async function click(loc, label, tries = 3) {
  for (let i = 1; i <= tries; i++) {
    try { await loc.first().click({timeout: 15000}); return; }
    catch (e) { log(`click retry ${i} ${label}`); await p.keyboard.press('Escape').catch(() => {}); await wait(2000); if (i === tries) throw e; }
  }
}

function jpegSize(file) {
  const x = fs.readFileSync(file);
  if (x.length < 50000 || x[0] !== 0xFF || x[1] !== 0xD8 || x[x.length - 2] !== 0xFF || x[x.length - 1] !== 0xD9) return null;
  for (let i = 2; i < x.length - 9;) {
    if (x[i] !== 0xFF) { i++; continue; }
    const m = x[i + 1], len = x.readUInt16BE(i + 2);
    if ([0xC0, 0xC1, 0xC2].includes(m)) return {h: x.readUInt16BE(i + 5), w: x.readUInt16BE(i + 7)};
    i += 2 + len;
  }
  return null;
}

// The person's own download, recorded with flow/record.mjs on 2026-11-10:
//   Download media -> menu "1K Original size / 2K Upscaled / 4K Upscaled" -> 2K Upscaled
//   -> notice "Upscaling your image, download will start automatically"
//   -> the download starts by itself (1.8 s for an image already upscaled; longer for a fresh one)
//   -> notice "Upscaling complete, your image has been downloaded!"
// The first version took the first download event as final; on a fresh image that one had no file
// yet (saveAs: ENOENT). Now every download event is collected, failed ones are skipped, and the wait
// goes on until a good file arrives, Flow reports an error, or UPSCALE_WAIT passes.
const UPSCALE_WAIT = 240000;

// Once 2K fails in a chapter, the rest of that chapter goes straight to 1K (trying 2K first on every
// image costs ~10 s each); the next chapter tries 2K again. The marker file holds that chapter's id.
const UPGRADE = mode === '--upgrade';   // replace a 1K image with its 2K version; never sends a prompt
const QUALITY_MARK =plan.journal.replace(/journal\.jsonl$/, 'quality-1k.txt');
const chapterId = id.split('-')[0];
const markedChapter = () => { try { return fs.readFileSync(QUALITY_MARK, 'utf8').trim(); } catch { return ''; } };
const ALWAYS_1K = plan.journal.replace(/journal\.jsonl$/, 'always-1k.txt');   // create this file: every download is 1K, 2K is never tried
const force1k = () => process.env.FLOW_QUALITY === '1K' || fs.existsSync(ALWAYS_1K) || markedChapter() === chapterId;   // read each time: a batch learns mid-way

// Download from the Images list itself, as recorded 2026-11-11: hover the tile -> its "More options"
// (three dots) -> Download -> the size submenu. No image page is opened, so there is no page load, no
// "Done" click and no way back: about 8-10 s saved per image. Every tile carries a stable data-media-id.
async function gridTile(tile, media) {
  const loc = p.locator(media ? `img[data-media-id="${media}"]` : `img[alt^="Tile displaying"][src^="${tile}"]`).first();
  for (let i = 0; i < 80; i++) {          // the list only draws the rows on screen: scroll until it appears
    if (await loc.count()) { await loc.scrollIntoViewIfNeeded({timeout: 10000}).catch(() => {}); return loc; }
    const moved = await p.evaluate(() => {
      let el = document.querySelector('img[alt^="Tile displaying"]');
      while (el && !(el.scrollHeight > el.clientHeight + 5 && /(auto|scroll)/.test(getComputedStyle(el).overflowY))) el = el.parentElement;
      if (!el) return false;
      const before = el.scrollTop; el.scrollTop += 500; return el.scrollTop !== before;
    });
    if (!moved) break;
    await wait(400);
  }
  throw new Error('tile not found in the Images list');
}

async function openGridMenu(tile, media) {
  const img = await gridTile(tile, media);
  await img.hover(); await wait(600);
  await click(img.locator('xpath=ancestor::flow-image-tile[1]').locator('button[aria-label="More options"]').first(), 'more options');
  await wait(700);
  await p.locator('[role=menuitem]').filter({hasText: /Download/}).first().hover(); await wait(900);
}

async function finishFromGrid(tile, media) {
  const size = await download2k(tile, media);
  journal('saved', {tile, media});
  fs.writeFileSync(shot.used, '');       // the image page is never opened, so Flow's "used prompt" is not read
  fs.writeFileSync(shot.meta, JSON.stringify({
    id, tile, media, chars: shot.chars, prompt_file: shot.prompt, size, attached, via: 'grid',
    prompt_used_share: null, prompt_shortened: false,
  }, null, 2));
}

async function download2k(tile = null, media = null) {
  let want = '2K';
  for (let i = 1; i <= 3; i++) {
    const arrived = [];
    const onDownload = d => arrived.push(d);
    p.on('download', onDownload);
    try {
      if (media) await openGridMenu(tile, media);
      else { await click(p.locator('button[aria-label="Download media"]'), 'download'); await wait(1500); }
      const shown = await lines();     // only messages that appear AFTER choosing 2K count
      // Flow's 2K upscaler sometimes fails for a whole stretch ("Upscaling failed!"). One try at 2K,
      // then "1K Original size" (1376x768, no upscale step). FLOW_QUALITY=1K starts there.
      want = UPGRADE ? '2K' : (force1k() || i >= 2) ? '1K' : '2K';
      await click(p.locator(`button[role=menuitem]:has-text("${want}")`), want === '1K' ? '1K Original size' : '2K Upscaled');
      const start = Date.now();
      let saved = null;
      while (!saved && Date.now() - start < UPSCALE_WAIT) {
        for (const d of arrived.splice(0)) {
          const failure = await d.failure();
          if (failure) { log(`a download attempt ended (${failure}); waiting for the next`); continue; }
          try { await d.saveAs(shot.out); saved = d; break; }
          catch (e) { log(`a download had no file (${e.message.split('\n')[0]}); waiting for the next`); }
        }
        if (saved) break;
        // Matching the whole page stopped scene 2's first attempt on text that was already there.
        const fresh = [...await lines()].filter(l => !shown.has(l));
        const bad = fresh.find(l => /upscal\w*[^.\n]{0,30}(fail|error)|couldn.?t (upscale|download)|something went wrong/i.test(l));
        if (bad) throw new Error(`Flow says: "${bad.slice(0, 120)}"`);
        await wait(1000);
      }
      if (!saved) throw new Error(`no download within ${UPSCALE_WAIT / 1000} s of choosing 2K`);
      log(`download arrived ${((Date.now() - start) / 1000).toFixed(1)} s after choosing ${want}`);
      const s = jpegSize(shot.out);
      if (s) {
        const ratio = s.w / s.h;
        // The aspect is set by hand in Flow. A wrong one would ruin every image after it, so it stops the run.
        if (Math.abs(ratio - 16 / 9) > 0.04) throw new Stop(`image is ${s.w}x${s.h}, not 16:9 - set Flow's aspect ratio to 16:9`);
        if (s.w >= (UPGRADE ? 2000 : 1300)) {
          if (want === '1K') fs.writeFileSync(QUALITY_MARK, chapterId);   // rest of this chapter: 1K without trying 2K
          log('saved', shot.out, `${s.w}x${s.h}`, want === '1K' ? '(original size, not upscaled)' : '');
          return {...s, quality: want};
        }
      }
      log(`download ${i} invalid; trying again`);
    } catch (e) {
      if (e instanceof Stop) { fs.rmSync(shot.out, {force: true}); throw e; }
      log(`download ${i} error ${e.message.split('\n')[0]}; trying again`);
      journal('download-error', {attempt: i, error: e.message.split('\n')[0].slice(0, 200)});
    } finally {
      p.off('download', onDownload);
    }
    fs.rmSync(shot.out, {force: true}); await p.keyboard.press('Escape').catch(() => {}); await wait(5000);
    if (UPGRADE && i >= 2) break;      // upgrading: two tries at 2K, then leave the 1K image as it is
    // Retrying in place failed on the first real image: the first 2K request starts Flow's upscale,
    // its "Upscaling..." notice then covers the download button, and the download event finds no
    // file. Reopening the image fresh, as --recover does, saved it in 8 seconds.
    if (tile && !media) await reopen(tile);     // from the list there is no page to reopen: the tile is still there
  }
  throw new Error(UPGRADE ? 'no 2K download: the upscaler is still failing' : 'no valid download (2K once, then original size twice)');
}

// The left sidebar's Images view: generated images only. All media also holds Character portraits,
// so a Character made while a batch runs showed up there as a "new" tile beside the scene image.
async function showImages() {
  await p.locator('text="Images"').first().click({timeout: 10000}).catch(() => {});
  await wait(2500);
}

// Back to the image's own page. A fresh tile's address (flow-content.google/image/...) is not the one
// it has after a reload (flow.google.com/asb/...), so searching the grid for it failed; the image's
// edit-page address, journaled when it was first opened, does not change. The project page is usable
// at domcontentloaded (0.4 s); waiting for "load" timed out at 30 s on a grid full of images.
async function reopen(tile) {
  const entries = fs.readFileSync(plan.journal, 'utf8').trim().split('\n').map(l => JSON.parse(l));
  const edit = entries.filter(e => e.id === id && e.edit).pop()?.edit;
  if (edit) { await p.goto(edit, {waitUntil: 'domcontentloaded', timeout: 60000}); await wait(4000); return; }
  await p.goto(plan.project, {waitUntil: 'domcontentloaded', timeout: 60000}); await wait(4000);
  await showImages();
  await click(p.locator(`img[alt^="Tile displaying"][src^="${tile}"]`), 'reopen tile'); await wait(3500);
}

async function attach(name) {
  await click(p.locator('button[aria-label="Add ingredients to the prompt box"]'), 'picker'); await wait(1500);
  await click(p.locator('mat-list-item:has-text("Characters"), [role=tab]:has-text("Characters")'), 'Characters tab'); await wait(1200);
  // Exact name: has-text("Leah") alone could also match a longer name containing it.
  const opt = await findOption(p, name);   // scrolls: the list only draws rows on screen
  if (!opt) throw new Error(`no Flow Character named exactly "${name}" in the picker`);
  const optSrc = norm(await opt.locator('img').first().getAttribute('src').catch(() => ''));
  const before = await chips();
  await click(opt, name); await wait(1000);
  const add = p.locator('button:has-text("Add to prompt")');
  if (await add.count() && await add.first().isVisible()) { await add.first().click(); await wait(1500); }
  const added = (await chips()).filter(s => !before.includes(s));
  attached.push({name, option_thumb: optSrc.slice(-40), chip: (added[0] || '').slice(-40)});
  if (added.length !== 1) throw new Error(`could not verify attachment of ${name}: ${added.length} new chips`);
  log(`attached ✓ ${name}`);
}

async function openProject() {
  b = await chromium.connectOverCDP(cdpUrl(plan));
  const pages = b.contexts().flatMap(c => c.pages());
  p = pages.filter(x => x.url().includes(projectId) && !/\/character/.test(x.url())).pop() || pages.filter(x => /flow\.google\.com|labs\.google/.test(x.url())).pop();
  if (!p) throw new Error('no Flow tab open in the debugging Chrome - run flow/start-chrome.ps1 and open Flow');
  if (/accounts\.google\.com|signin/i.test(p.url())) throw new Stop('Flow is asking you to sign in');
  if (!p.url().includes(projectId) || /\/(edit|character|scene)\//.test(p.url())) { await p.goto(plan.project, {waitUntil: 'domcontentloaded', timeout: 60000}); await wait(3500); }
  if (!p.url().includes(projectId)) throw new Stop(`the tab did not open project ${projectId}`);
  await front(p);       // FLOW_FRONT=1 only: raised once per run, never closed or navigated for this
}

async function usedPrompt() {
  const mark = plan.header_mark;
  return await p.evaluate(m => [...document.querySelectorAll('body *')]
    .filter(e => (e.innerText || '').includes(m) && ![...e.children].some(c => (c.innerText || '').includes(m)))
    .map(e => e.innerText)[0] || '', mark);
}

async function finishTile(tile) {
  if (/\/edit\//.test(p.url())) {
    // already on the image's page (a reopen)
  } else {
    await click(p.locator(`img[alt^="Tile displaying"][src^="${tile}"]`), 'new tile'); await wait(3500);
  }
  if (/\/edit\//.test(p.url())) journal('opened', {edit: p.url()});
  const used = await usedPrompt();
  fs.writeFileSync(shot.used, used);
  const sentText = fs.readFileSync(shot.prompt, 'utf8');
  const usedShare = used ? used.trim().length / sentText.trim().length : 0;
  const reported = (await p.$$eval('img', els => els.filter(e => /ingredient image/i.test(e.alt))
    .map(e => (e.currentSrc || e.src).split('?')[0].replace(/=s\d+[^/]*$/, ''))))
    .map(src => attached.find(a => a.chip && src.endsWith(a.chip))?.name || 'UNKNOWN ' + src.slice(-30));
  const size = await download2k(tile);
  journal('saved', {tile});
  fs.writeFileSync(shot.meta, JSON.stringify({
    id, tile, chars: shot.chars, prompt_file: shot.prompt, size, attached,
    flow_reported_ingredients: [...new Set(reported)],
    prompt_used_share: Number(usedShare.toFixed(3)), prompt_shortened: usedShare < 0.95,
  }, null, 2));
  if (usedShare < 0.95) log(`WARNING: Flow used ${(usedShare * 100).toFixed(0)}% of the prompt - paste flow/agent-instruction.txt into the project`);
  await click(p.getByRole('button', {name: 'Done'}), 'Done').catch(() => {});
  await wait(2000);
}

// --batch: send several prompts back to back (Flow generates them side by side), wait until all the new
// tiles have loaded, then download each from the Images list. A new tile only exists once its picture is
// ready, so the tiles are matched to the shots by order: the list shows the newest first, so the newest
// new tile belongs to the last prompt sent. The count must match exactly or nothing is guessed: the
// shots stay journaled as "sent" and show up as NEEDS CHECK (flow/adopt.mjs can match them by prompt).
const GAP = Number(process.env.FLOW_BATCH_GAP || 8000);      // between sends inside a batch
async function batch(ids) {
  await closePanels(p);
  await showImages();
  const chip = await modeChip(p);
  if (chip ? !imageChip(chip) : !(await agentMode(p)))
    throw new Stop(`Flow's prompt bar is set to "${chip || 'unknown'}" - switch it to Image, 16:9, x1, or to Agent mode`);
  const mediaOnPage = () => p.evaluate(() => [...document.querySelectorAll('img[alt^="Tile displaying"]')]
    .map(i => i.getAttribute('data-media-id')).filter(Boolean));
  const known = await mediaOnPage();
  const seen = await lines();
  const sentShots = [];
  let failure = null;
  try {
    for (const sid of ids) {
      id = sid; shot = plan.shots.find(s => s.id === sid); attached.length = 0; sent = false;
      if (!shot) throw new Error(`no shot ${sid} in the plan`);
      if (sentShots.length && (await chips()).length) throw new Error('character images are still in the prompt bar after the last send');
      for (const c of shot.chars) await attach(c);
      if (attached.length !== shot.chars.length) throw new Error(`expected ${shot.chars.length} characters, verified ${attached.length}`);
      const box = p.locator('div[role=textbox]').first(); await box.click();
      await box.fill(fs.readFileSync(shot.prompt, 'utf8')); await wait(800);
      journal('sending'); sent = true;
      await click(p.locator('button[aria-label="Start generation"]'), 'send');
      journal('sent', {chars: shot.chars});
      log('sent with', shot.chars.join(', ') || 'no characters');
      sentShots.push({id: sid, shot, attached: [...attached]});
      const newLines = [...await lines()].filter(l => !seen.has(l)).join('\n');
      if (WALL.test(newLines)) throw new Stop(`Flow says: ${newLines.match(WALL)[0]}`);
      await wait(GAP);
    }
  } catch (e) { failure = e; }          // what was already sent is still collected below
  if (!sentShots.length) throw failure;

  let fresh = [];
  for (let t = 0; t < 240; t++) {        // 6 minutes
    await wait(1500);
    fresh = await p.evaluate(known => [...document.querySelectorAll('img[alt^="Tile displaying"]')]
      .filter(i => i.getAttribute('data-media-id') && !known.includes(i.getAttribute('data-media-id')))
      .map(i => ({tile: i.currentSrc.split('?')[0], media: i.getAttribute('data-media-id'), ok: i.complete && i.naturalWidth > 0})), known);
    if (fresh.length >= sentShots.length && fresh.every(f => f.ok)) break;
    if (t % 4 === 3) {
      const newLines = [...await lines()].filter(l => !seen.has(l)).join('\n');
      if (WALL.test(newLines)) throw new Stop(`Flow says: ${newLines.match(WALL)[0]}`);
    }
  }
  if (fresh.length !== sentShots.length) {
    id = sentShots[sentShots.length - 1].id;
    throw new Error(`${fresh.length} new images for ${sentShots.length} prompts: not matched, left for a check`);
  }
  let firstError = null;
  for (let i = 0; i < sentShots.length; i++) {
    const s = sentShots[sentShots.length - 1 - i], f = fresh[i];     // newest tile <-> last prompt sent
    id = s.id; shot = s.shot; attached.length = 0; attached.push(...s.attached);
    journal('tile', {tile: f.tile, media: f.media, batch: sentShots.length});
    try { await finishFromGrid(f.tile, f.media); }
    catch (e) { journal('error-after-send', {error: (e.message || String(e)).split('\n')[0]}); console.error(id, 'ERROR', e.message.split('\n')[0]); firstError ??= e; }
  }
  if (failure) throw failure;
  if (firstError) throw firstError;
}

try {
  await openProject();
  if (mode === '--batch') {
    await batch(id.split(','));
  } else if (mode === '--recover') {
    const entries = fs.existsSync(plan.journal) ? fs.readFileSync(plan.journal, 'utf8').trim().split('\n').map(l => JSON.parse(l)) : [];
    const tile = entries.filter(e => e.id === id && e.tile).pop()?.tile;
    if (!tile) throw new Error('no tile journaled for this shot - nothing to recover');
    sent = true;    // whatever happens here, the prompt was sent earlier
    log('recovering', tile.slice(-40));
    const media = entries.filter(e => e.id === id && e.media).pop()?.media;
    const mine = entries.map((e, i) => ({e, i})).filter(x => x.e.id === id);
    const lastOf = k => Math.max(-1, ...mine.filter(x => x.e[k]).map(x => x.i));
    const hasEdit = lastOf('edit') > lastOf('media');      // a media id newer than any page address wins
    if (media && !hasEdit) {     // sent in grid mode, or adopted with its media id: download from the list
      await showImages();
      await finishFromGrid(tile, media);
    } else {
      await reopen(tile);        // its edit page if journaled, else find the tile in Images
      await finishTile(tile);
    }
  } else if (mode === '--upgrade') {
    const entries = fs.existsSync(plan.journal) ? fs.readFileSync(plan.journal, 'utf8').trim().split('\n').map(l => JSON.parse(l)) : [];
    const tile = entries.filter(e => e.id === id && e.tile).pop()?.tile;
    const edit = entries.filter(e => e.id === id && e.edit).pop()?.edit;
    const cur = fs.existsSync(shot.out) ? jpegSize(shot.out) : null;
    if (!cur) throw new Error('no valid image on disk to upgrade');
    const media = entries.filter(e => e.id === id && e.media).pop()?.media;
    if (!tile && !edit && !media) throw new Error('no tile, media id or edit page journaled for this shot');
    if (cur.w >= 2000) { log('already full size', `${cur.w}x${cur.h}`); }
    else {
      const finalOut = shot.out;
      shot.out = finalOut.replace(/\.jpg$/, '.upgrade.jpg');      // the original is untouched until 2K arrives
      log('upgrading', `${cur.w}x${cur.h} -> 2K`);
      const viaGrid = media && !edit;        // saved in grid mode: no page address, download from the list
      if (viaGrid) await showImages(); else await reopen(tile);
      const text = await p.evaluate(() => document.body.innerText);
      if (WALL.test(text)) throw new Stop(`Flow says: ${text.match(WALL)[0]}`);
      const size = await download2k(tile, viaGrid ? media : null);
      const dir = finalOut.replace(/\/[^/]*$/, '');
      fs.mkdirSync(`${dir}/1k`, {recursive: true});
      fs.copyFileSync(finalOut, `${dir}/1k/${finalOut.split('/').pop().replace('-2k', '-1k')}`);
      fs.renameSync(shot.out, finalOut);
      shot.out = finalOut;
      try {
        const meta = JSON.parse(fs.readFileSync(shot.meta, 'utf8'));
        fs.writeFileSync(shot.meta, JSON.stringify({...meta, size, upgraded_from: cur}, null, 2));
      } catch { /* the image is what matters */ }
      journal('upgraded', {size});
    }
  } else {
    await closePanels(p);
    // The "new image" is whatever tile appears after sending, so the grid must already be on screen
    // before it: from the empty Scenes view every existing tile looked new (23 on the first run).
    await showImages();
    // A prompt sent in Video mode spends credits on two videos. The plain bar shows its mode in a
    // chip; Agent mode has none and uses Agent settings, which preflight verifies before each run.
    const chip = await modeChip(p);
    if (chip ? !imageChip(chip) : !(await agentMode(p)))
      throw new Stop(`Flow's prompt bar is set to "${chip || 'unknown'}" - switch it to Image, 16:9, x1, or to Agent mode`);
    for (const c of shot.chars) await attach(c);
    if (attached.length !== shot.chars.length) throw new Error(`expected ${shot.chars.length} characters, verified ${attached.length}`);
    const text = fs.readFileSync(shot.prompt, 'utf8');
    const box = p.locator('div[role=textbox]').first(); await box.click(); await box.fill(text); await wait(800);
    const before = new Set(await tiles());
    const seen = await lines();
    journal('sending');
    sent = true;
    await click(p.locator('button[aria-label="Start generation"]'), 'send');
    journal('sent', {chars: shot.chars});
    log('sent with', shot.chars.join(', ') || 'no characters');
    // Nano Banana 2.1 finishes in 10-15 s, but this loop took 45-50 s to notice: it slept 5 s per
    // round, scanned every element on a page full of images for a "NN%" (3-15 s per scan), and
    // wanted two quiet rounds. Now: a cheap tile check every 1.5 s, done as soon as the new tile's
    // picture has loaded; the whole-page text (for refusals and limits) is read every 4th round.
    let fresh = [];
    for (let t = 0; t < 200; t++) {
      await wait(1500);
      fresh = (await tiles()).filter(s => !before.has(s));
      if (fresh.length && await p.evaluate(src => [...document.querySelectorAll('img[alt^="Tile displaying"]')]
        .some(i => i.currentSrc.split('?')[0] === src && i.complete && i.naturalWidth > 0), fresh[0])) break;
      if (t % 4 === 3) {
        const newLines = [...await lines()].filter(l => !seen.has(l)).join('\n');
        if (WALL.test(newLines)) throw new Stop(`Flow says: ${newLines.match(WALL)[0]}`);
        if (!fresh.length && REFUSED.test(newLines)) throw new Refused(newLines.split('\n').find(l => REFUSED.test(l)));
      }
    }
    if (!fresh.length) throw new Error('no new image appeared within 5 minutes');
    const media = await p.locator(`img[alt^="Tile displaying"][src^="${fresh[0]}"]`).first().getAttribute('data-media-id').catch(() => null);
    journal('tile', {tile: fresh[0], media, extra_new_tiles: fresh.length - 1});
    if (media) await finishFromGrid(fresh[0], media);
    else await finishTile(fresh[0]);        // no media id on the tile: the old route, through the image page
  }
} catch (e) {
  const msg = (e.message || String(e)).split('\n')[0];
  if (UPGRADE && !(e instanceof Stop)) {      // an upgrade never touches the shot's sent/saved state
    journal('upgrade-error', {error: msg}); console.error(id, 'UPGRADE FAILED', msg); process.exit(2);
  }
  if (e instanceof Stop) { await raise(p); journal('wall', {error: msg}); console.error(id, 'STOP', msg); process.exitCode = 5; }
  else if (e instanceof Refused) { journal('refused', {error: msg}); console.error(id, 'REFUSED', msg); process.exitCode = 4; }
  else { journal(sent ? 'error-after-send' : 'error-before-send', {error: msg}); console.error(id, 'ERROR', msg); process.exitCode = sent ? 3 : 2; }
}
process.exit(process.exitCode || 0);
