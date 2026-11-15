// Stand-in for flow/gen.mjs in tests: plays a scripted outcome per call and journals like the real one.
// FAKE_SCRIPT is JSON {shotId: ["ok" | "before" | "after" | "after-tile" | "refused" | "wall", ...]};
// each call to a shot consumes the next outcome. FAKE_STOP_AFTER=<shotId> creates the STOP file after that shot saves. --recover succeeds only if a tile was journaled.
// --batch <id1,id2,...>: like flow/gen.mjs --batch, each shot in turn gets its outcome; the exit code is the worst one.
import fs from 'node:fs';
import {loadPlan} from '../flow/plan.mjs';

const [planFile, idArg, mode] = process.argv.slice(2);
const plan = loadPlan(planFile);
const dir = planFile.replace(/[^/\\]+$/, '');
const counts = fs.existsSync(dir + 'counts.json') ? JSON.parse(fs.readFileSync(dir + 'counts.json', 'utf8')) : {};
const j = (id, state, extra = {}) => fs.appendFileSync(plan.journal, JSON.stringify({id, state, ...extra}) + '\n');
const save = (shot, id) => {
  fs.writeFileSync(shot.out, Buffer.concat([Buffer.from([0xFF, 0xD8]), Buffer.alloc(60000), Buffer.from([0xFF, 0xD9])]));
  fs.writeFileSync(shot.meta, JSON.stringify({prompt_shortened: false}));
  j(id, 'saved');
};

if (mode === '--recover') {
  fs.appendFileSync(dir + 'calls.log', `${idArg} --recover\n`);
  const tile = fs.readFileSync(plan.journal, 'utf8').trim().split('\n').map(l => JSON.parse(l)).filter(e => e.id === idArg && e.tile).pop();
  if (tile) { save(plan.shots.find(s => s.id === idArg), idArg); process.exit(0); }
  process.exit(3);
}

function one(id) {
  const shot = plan.shots.find(s => s.id === id);
  fs.appendFileSync(dir + 'calls.log', `${id}${mode ? ' ' + mode : ''}\n`);
  const script = JSON.parse(process.env.FAKE_SCRIPT)[id] || ['ok'];
  const n = counts[id] || 0;
  counts[id] = n + 1;
  fs.writeFileSync(dir + 'counts.json', JSON.stringify(counts));
  const outcome = script[Math.min(n, script.length - 1)];
  if (outcome === 'ok') {
    j(id, 'sending'); j(id, 'sent'); j(id, 'tile', {tile: 'T-' + id}); save(shot, id);
    if (process.env.FAKE_STOP_AFTER === id) fs.writeFileSync(dir + 'STOP', '');   // the person asks to stop while this shot runs
    return 0;
  }
  if (outcome === 'before') { j(id, 'error-before-send'); return 2; }
  if (outcome === 'after') { j(id, 'sending'); j(id, 'sent'); j(id, 'error-after-send'); return 3; }
  if (outcome === 'after-tile') { j(id, 'sending'); j(id, 'sent'); j(id, 'tile', {tile: 'T-' + id}); j(id, 'error-after-send'); return 3; }
  if (outcome === 'refused') { j(id, 'sending'); j(id, 'sent'); j(id, 'refused'); return 4; }
  if (outcome === 'wall') { j(id, 'sending'); j(id, 'sent'); j(id, 'wall'); return 5; }
  return 1;
}

const ids = mode === '--batch' ? idArg.split(',') : [idArg];
let code = 0;
for (const id of ids) code = Math.max(code, one(id));
process.exit(code);
