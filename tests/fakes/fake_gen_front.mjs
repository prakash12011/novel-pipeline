// Wraps tests/fake_gen.mjs (env FLOW_GEN): logs FLOW_FRONT per call to <plan dir>/front.log, prints a runner-style
// progress line, and with FAKE_WALL_TEXT replaces fake_gen's text-less "wall" journal line (exit 5) by one that
// carries that text and a timestamp (as the real gen.mjs journals it).
import fs from 'node:fs';
import {loadPlan} from '../../flow/plan.mjs';

const [planFile, id, mode] = process.argv.slice(2);
const dir = planFile.slice(0, Math.max(planFile.lastIndexOf('/'), planFile.lastIndexOf(String.fromCharCode(92))) + 1);
const NL = String.fromCharCode(10);
fs.appendFileSync(dir + 'front.log', `${id}${mode ? ' ' + mode : ''} front=${process.env.FLOW_FRONT || '-'}` + NL);
if (mode !== '--recover') console.log(`12:00:00 ${id} sent (fake)`);
if (process.env.FAKE_WALL_TEXT) {
  process.on('exit', code => {
    if (code !== 5) return;
    const j = loadPlan(planFile).journal;
    const lines = fs.readFileSync(j, 'utf8').split(NL).filter(Boolean);
    lines.pop();   // fake_gen's own wall line
    lines.push(JSON.stringify({id, state: 'wall', error: process.env.FAKE_WALL_TEXT, at: new Date().toISOString()}));
    fs.writeFileSync(j, lines.join(NL) + NL);
  });
}
await import('../fake_gen.mjs');
