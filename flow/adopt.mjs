// Link images that already exist in Flow to shots whose download was lost, so run.mjs can download them.
//
//   node flow/adopt.mjs <plan.json> <shotId> [<shotId> ...] [--search]
//
// For a shot journaled as "sent" with no tile (Flow showed nothing in time, then the image appeared,
// or the person regenerated it by hand), this opens the newest tiles in the Images view, reads each
// tile's prompt, and matches it to the shot by its SCENE sentence. A match is journaled as a tile plus
// its edit-page address; the next `node flow/run.mjs` downloads it in recovery mode. Nothing is sent to
// Flow: the tool only opens pages and reads text.
import fs from 'node:fs';
import {chromium} from 'playwright-core';
import {cdpUrl, loadPlan} from './plan.mjs';

const argv = process.argv.slice(2);
const search = argv.includes('--search');   // find old images with Flow's search box instead of the newest tiles
const [planFile, ...ids] = argv.filter(a => a !== '--search');
if (!planFile || !ids.length) { console.log('usage: node flow/adopt.mjs <plan.json> <shotId> ...'); process.exit(2); }
const plan = loadPlan(planFile);
const projectId = (plan.project.split('/project/')[1] || '').split(/[/?#]/)[0];
const norm = s => (s || '').replace(/\s+/g, ' ').trim();
const MAX_TILES = 24;

const want = ids.map(id => {
  const shot = plan.shots.find(s => s.id === id);
  if (!shot) throw new Error(`no shot ${id} in the plan`);
  const scene = norm(fs.readFileSync(shot.prompt, 'utf8').split('SCENE:')[1]?.split('\n')[0]);
  if (scene.length < 30) throw new Error(`${id}: no SCENE sentence in its prompt`);
  return {id, shot, key: scene.slice(0, 70), found: null};
});
const journal = (id, state, extra = {}) =>
  fs.appendFileSync(plan.journal, JSON.stringify({id, state, at: new Date().toISOString(), ...extra}) + '\n');

const b = await chromium.connectOverCDP(cdpUrl(plan));
const pages = b.contexts().flatMap(c => c.pages());
const p = pages.filter(x => x.url().includes(projectId) && !/\/character/.test(x.url())).pop();
if (!p) throw new Error('no Flow project tab open in the debugging Chrome');
const wait = ms => p.waitForTimeout(ms);

async function grid() {
  await p.goto(plan.project, {waitUntil: 'domcontentloaded', timeout: 60000}); await wait(3500);
  await p.locator('text="Images"').first().click({timeout: 10000}).catch(() => {});
  await wait(2500);
  for (let i = 0; i < 8 && !(await p.locator('img[alt^="Tile displaying"]').count()); i++) await wait(2000);   // tiles load late
}
async function usedPrompt() {
  return await p.evaluate(m => [...document.querySelectorAll('body *')]
    .filter(e => (e.innerText || '').includes(m) && ![...e.children].some(c => (c.innerText || '').includes(m)))
    .map(e => e.innerText)[0] || '', plan.header_mark);
}

// SEARCH="map" searches that word (Flow titles images itself, so a word from the scene works best)
const term = w => process.env.SEARCH || w.key.slice(0, 45);
async function findBySearch(w) {
  for (let i = 0; i < 12 && !w.found; i++) {
    await grid();
    const box = p.locator('input[placeholder*="earch"], input[aria-label*="earch"]').first();
    await box.click({timeout: 10000}); await box.fill(term(w)); await p.keyboard.press('Enter'); await wait(3500);
    const n = await p.locator('img[alt^="Tile displaying"]').count();
    if (i === 0) console.log(`${w.id}: search for "${w.key.slice(0, 45)}" shows ${n} tiles`.replace(w.key.slice(0, 45), term(w)));
    if (i >= n) break;
    const tile = p.locator('img[alt^="Tile displaying"]').nth(i);
    const src = ((await tile.getAttribute('src').catch(() => '')) || '').split('?')[0];
    const media = await tile.getAttribute('data-media-id').catch(() => null);
    await tile.click({timeout: 10000}).catch(() => {}); await wait(3500);
    if (!/\/edit\//.test(p.url())) continue;
    const used = norm(await usedPrompt());
    console.log(`  result ${i}: ${used.includes(w.key) ? 'MATCH' : 'no match'}`);
    if (used.includes(w.key)) w.found = {tile: src, media, edit: p.url()};
  }
}

if (search) {
  for (const w of want) await findBySearch(w);
} else {
await grid();
const total = await p.locator('img[alt^="Tile displaying"]').count();
console.log(`${total} tiles in the Images view; checking the newest ${Math.min(total, MAX_TILES)}`);
for (let i = 0; i < Math.min(total, MAX_TILES) && want.some(w => !w.found); i++) {
  await grid();
  const tile = p.locator('img[alt^="Tile displaying"]').nth(i);
  const src = ((await tile.getAttribute('src').catch(() => '')) || '').split('?')[0];
  const media = await tile.getAttribute('data-media-id').catch(() => null);
  await tile.click({timeout: 10000}).catch(() => {}); await wait(3500);
  if (!/\/edit\//.test(p.url())) { console.log(`tile ${i}: did not open an image page`); continue; }
  const used = norm(await usedPrompt());
  const hit = want.find(w => !w.found && used.includes(w.key));
  console.log(`tile ${i}: ${hit ? 'MATCH ' + hit.id : 'no match'} (${used.slice(0, 0)}${used.length} chars of prompt read)`);
  if (hit) hit.found = {tile: src, media, edit: p.url()};
}
}
for (const w of want) {
  if (w.found) {
    // With the grid's media id the download comes from the Images list (hover -> More options -> Download),
    // not from the image's own page; the page address is kept only when the id could not be read.
    if (w.found.media) journal(w.id, 'tile', {tile: w.found.tile, media: w.found.media, adopted: true});
    else { journal(w.id, 'tile', {tile: w.found.tile, adopted: true}); journal(w.id, 'opened', {edit: w.found.edit}); }
    console.log(`${w.id}: adopted ${w.found.media ? 'media ' + w.found.media.slice(0, 8) + ' (downloads from the grid)' : w.found.edit.split('/').pop() + ' (no media id: downloads from its page)'}`);
  } else console.log(`${w.id}: no matching image in the newest ${MAX_TILES} tiles - left as NEEDS CHECK`);
}
await p.goto(plan.project, {waitUntil: 'domcontentloaded', timeout: 60000}).catch(() => {});
process.exit(want.every(w => w.found) ? 0 : 1);
