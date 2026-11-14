// Stand-in for flow/gen.mjs in tests: plays a scripted outcome per call and journals like the real one.
// FAKE_SCRIPT is JSON {shotId: ["ok" | "before" | "after" | "after-tile" | "refused" | "wall", ...]};
// each call to a shot consumes the next outcome. FAKE_STOP_AFTER=<shotId> creates the STOP file after that shot saves. --recover succeeds only if a tile was journaled.
import fs from 'node:fs';
import {loadPlan} from '../flow/plan.mjs';

const [planFile, id, mode] = process.argv.slice(2);
const plan = loadPlan(planFile);
const shot = plan.shots.find(s => s.id === id);
const dir = planFile.replace(/[^/\\]+$/, '');
const counts = fs.existsSync(dir + 'counts.json') ? JSON.parse(fs.readFileSync(dir + 'counts.json', 'utf8')) : {};
const j = (state, extra = {}) => fs.appendFileSync(plan.journal, JSON.stringify({id, state, ...extra}) + '\n');
fs.appendFileSync(dir + 'calls.log', `${id}${mode ? ' ' + mode : ''}\n`);
const save = () => {
  fs.writeFileSync(shot.out, Buffer.concat([Buffer.from([0xFF, 0xD8]), Buffer.alloc(60000), Buffer.from([0xFF, 0xD9])]));
  fs.writeFileSync(shot.meta, JSON.stringify({prompt_shortened: false}));
  j('saved');
};

if (mode === '--recover') {
  const tile = fs.readFileSync(plan.journal, 'utf8').trim().split('\n').map(l => JSON.parse(l)).filter(e => e.id === id && e.tile).pop();
  if (tile) { save(); process.exit(0); }
  process.exit(3);
}
const script = JSON.parse(process.env.FAKE_SCRIPT)[id] || ['ok'];
const n = counts[id] || 0;
counts[id] = n + 1;
fs.writeFileSync(dir + 'counts.json', JSON.stringify(counts));
const outcome = script[Math.min(n, script.length - 1)];
if (outcome === 'ok') {
  j('sending'); j('sent'); j('tile', {tile: 'T-' + id}); save();
  if (process.env.FAKE_STOP_AFTER === id) fs.writeFileSync(dir + 'STOP', '');   // the person asks to stop while this shot runs
  process.exit(0);
}
if (outcome === 'before') { j('error-before-send'); process.exit(2); }
if (outcome === 'after') { j('sending'); j('sent'); j('error-after-send'); process.exit(3); }
if (outcome === 'after-tile') { j('sending'); j('sent'); j('tile', {tile: 'T-' + id}); j('error-after-send'); process.exit(3); }
if (outcome === 'refused') { j('sending'); j('sent'); j('refused'); process.exit(4); }
if (outcome === 'wall') { j('sending'); j('sent'); j('wall'); process.exit(5); }
