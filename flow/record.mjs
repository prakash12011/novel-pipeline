// Record what a person does in Flow, with timings, so a script can copy it exactly.
//   node flow/record.mjs <plan.json> <shotId|grid> <out.json> [maxSeconds]
// Opens that shot's image, then logs clicks, key presses, Flow's notices (upscaling, errors) and every
// download (start, finish or failure, size) until a download has finished plus 5 s, or maxSeconds pass.
// Used when Flow's page changes and the download steps in gen.mjs need relearning.
import fs from 'node:fs';
import {chromium} from 'playwright-core';
import {cdpUrl, loadPlan} from './plan.mjs';

const [planFile, id, outFile, maxArg] = process.argv.slice(2);
const plan = loadPlan(planFile);
const tile = fs.readFileSync(plan.journal, 'utf8').trim().split('\n').map(l => JSON.parse(l))
  .filter(e => e.id === id && e.tile).pop()?.tile;
const GRID = id === 'grid';     // `grid` records on the Images list itself (hover -> menu -> Download), no image opened
if (!tile && !GRID) { console.error(`no tile recorded for ${id}`); process.exit(1); }

const b = await chromium.connectOverCDP(cdpUrl(plan));
const projectId = plan.project.split('/project/')[1].split(/[/?#]/)[0];
const p = b.contexts().flatMap(c => c.pages()).find(x => x.url().includes(projectId));
const t0 = Date.now();
const events = [];
const rec = ev => { ev.t = +((Date.now() - t0) / 1000).toFixed(2); events.push(ev); console.log(JSON.stringify(ev)); };

await p.goto(plan.project); await p.waitForTimeout(4000);
if (GRID) {
  await p.locator('text="Images"').first().click({timeout: 10000}).catch(() => {});
  await p.waitForTimeout(3000);
  rec({type: 'ready', note: 'Images list open - hover an image, click the three dots, Download, then the size'});
} else {
  await p.locator(`img[alt^="Tile displaying"][src^="${tile}"]`).first().click({timeout: 20000});
  await p.waitForTimeout(3000);
  rec({type: 'ready', note: 'image open - download it now the way you normally do'});
}

await p.exposeFunction('__rec', ev => rec(ev)).catch(() => {});
await p.evaluate(() => {
  const describe = el => {
    const t = el.closest('button, [role=button], [role=menuitem], [role=option], a, [role=tab]') || el;
    return {tag: t.tagName.toLowerCase(), role: t.getAttribute('role'), aria: t.getAttribute('aria-label'),
            text: (t.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60)};
  };
  for (const kind of ['click', 'contextmenu']) document.addEventListener(kind, e => window.__rec({type: kind, ...describe(e.target)}), true);
  document.addEventListener('keydown', e => window.__rec({type: 'key', key: e.key}), true);
  const seen = new Set();
  new MutationObserver(muts => {
    for (const m of muts) for (const n of m.addedNodes) {
      const text = (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
      if (text && text.length < 200 && /upscal|download|ready|fail|error|try again|%|2K|1K|4K/i.test(text) && !seen.has(text)) {
        seen.add(text); window.__rec({type: 'notice', text});
      }
    }
  }).observe(document.body, {childList: true, subtree: true});
});

let finished = null;
p.on('download', async d => {
  rec({type: 'download-start', name: d.suggestedFilename(), url: d.url().slice(0, 90)});
  const fail = await d.failure();
  if (fail) { rec({type: 'download-failed', error: fail}); return; }
  const dest = outFile.replace(/\.json$/, '') + '-' + d.suggestedFilename();
  try {
    await d.saveAs(dest);
    rec({type: 'download-done', file: dest, bytes: fs.statSync(dest).size});
    finished ??= Date.now();
  } catch (e) { rec({type: 'download-save-error', error: e.message.split('\n')[0]}); }
});
p.on('request', r => { if (/upscal|download|export/i.test(r.url())) rec({type: 'request', method: r.method(), url: r.url().slice(0, 110)}); });
p.on('response', r => { if (/upscal|download|export/i.test(r.url())) rec({type: 'response', status: r.status(), url: r.url().slice(0, 110)}); });

const max = Number(maxArg || 300) * 1000;
while (Date.now() - t0 < max && !(finished && Date.now() - finished > 5000)) await new Promise(r => setTimeout(r, 500));
fs.writeFileSync(outFile, JSON.stringify(events, null, 1));
console.log(finished ? 'recorded a finished download' : 'stopped without a finished download');
process.exit(0);
