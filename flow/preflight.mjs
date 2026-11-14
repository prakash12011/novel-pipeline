// Check Flow is ready for a plan before any prompt is sent.
//   node flow/preflight.mjs <plan.json>
// Checks: the debugging Chrome is reachable, Flow is signed in and on the plan's project, the prompt
// bar will make ONE 16:9 IMAGE without stopping to ask, the agent instruction is saved, and every
// Flow Character the plan attaches exists in the picker under exactly that name.
import fs from 'node:fs';
import {chromium} from 'playwright-core';
import {agentInstructions, agentMode, agentSettings, allOptionNames, closePanels, front, imageChip, modeChip} from './page.mjs';
import {cdpUrl, chipModel, expectedModel, loadPlan, modelMatches} from './plan.mjs';

const plan = loadPlan(process.argv[2]);
const wantModel = expectedModel(plan);   // env FLOW_MODEL, else the plan's image_model, else "Nano Banana 2.1"
const projectId = (plan.project.split('/project/')[1] || '').split(/[/?#]/)[0];
const need = [...new Set(plan.shots.flatMap(s => s.chars))].sort();
let ok = true;
const say = (good, msg) => { console.log(`${good ? 'ok  ' : 'FAIL'} ${msg}`); ok &&= good; };

let b;
try { b = await chromium.connectOverCDP(cdpUrl(plan)); }
catch { say(false, `no Chrome reachable at ${cdpUrl(plan)} - start the debugging Chrome`); process.exit(1); }
const pages = b.contexts().flatMap(c => c.pages());
const p = pages.filter(x => x.url().includes(projectId) && !/\/character/.test(x.url())).pop() || pages.filter(x => /flow\.google\.com|labs\.google/.test(x.url())).pop();
if (!p) { say(false, 'no Flow tab open'); process.exit(1); }
await front(p);   // FLOW_FRONT=1 only
say(!/accounts\.google\.com|signin/i.test(p.url()), `signed in (${p.url().slice(0, 60)})`);
if (!p.url().includes(projectId)) { await p.goto(plan.project); await p.waitForTimeout(3500); }
say(p.url().includes(projectId), `on project ${projectId}`);

await closePanels(p);
const chip = await modeChip(p);
if (chip) {
  say(imageChip(chip), `prompt bar makes images, not video (it says "${chip}")`);
  const foundModel = chipModel(chip.replace(/\bx\d+\b/g, ''));
  say(modelMatches(wantModel, foundModel), `image model: expected "${wantModel}", found "${foundModel}" (FLOW_MODEL or the plan's image_model sets the expected one)`);
  // Flow shows the aspect as an icon whose name reads "crop_16_9".
  say(/16[:_]9/.test(chip), `aspect 16:9 (from "${chip}"; also checked on every download)`);
  say(!/x[2-9]/.test(chip), `one image per prompt (from "${chip}")`);
} else if (await agentMode(p) && !process.env.FLOW_ALLOW_AGENT) {
  say(false, `Agent mode is ON (white "Agent" pill, "Nano Banana Pro" x2): switch it off so the bar shows "${wantModel} ... x1". Set FLOW_ALLOW_AGENT=1 to run in Agent mode on purpose`);
} else if (await agentMode(p)) {
  const s = await agentSettings(p);
  say(s.never, 'Agent settings: "Confirm before generating" is Never (with Always, every image waits for a click)');
  say(/16[:_]9/.test(s.aspect), `Agent settings: image default 16:9 (it says "${s.aspect}")`);
  // x2 works too: gen.mjs keeps the first new image. Saving both is planned, not built yet.
  say(s.count === 'x1' || s.count === 'x2', `Agent settings: one or two images per prompt (it says "${s.count}")`);
  if (s.count === 'x2') console.log('     note: x2 - only the first image of each pair is saved for now');
  console.log(`     image model: ${s.model || 'unknown'}`);
  const saved = await agentInstructions(p);
  say(/word for word/i.test(saved), 'Agent instruction saved (not just typed in a chat)');
} else {
  say(false, 'could not find the prompt bar settings or the Agent button');
}

try {
  // A tab opened before Characters were added keeps a stale picker list: reload once per run.
  await p.reload({waitUntil: 'domcontentloaded', timeout: 60000}); await p.waitForTimeout(5000);
  await p.locator('button[aria-label="Add ingredients to the prompt box"]').first().click({timeout: 15000});
  await p.waitForTimeout(1500);
  await p.locator('mat-list-item:has-text("Characters"), [role=tab]:has-text("Characters")').first().click({timeout: 15000});
  await p.waitForTimeout(1500);
  const names = await allOptionNames(p);   // scrolls: the list only draws rows on screen
  if (process.env.FLOW_DEBUG) console.log("picker:", p.url(), names.length, JSON.stringify(names));
  for (const n of need) say(names.includes(n), `Flow Character "${n}"`);
  const extra = names.filter(n => !need.includes(n));
  if (extra.length) console.log(`     (also in the picker, unused by this plan: ${extra.join(', ')})`);
  await p.keyboard.press('Escape');
} catch (e) {
  say(false, `could not open the Characters picker: ${e.message.split('\n')[0]}`);
}
console.log(ok ? '\nready' : '\nnot ready');
process.exit(ok ? 0 : 1);
