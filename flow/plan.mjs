// Shared by every flow script: loading a plan, where Chrome is, which image model is expected.
// No browser code here, so tests can import it.
import fs from 'node:fs';
import path from 'node:path';

export const DEFAULT_CDP = 'http://127.0.0.1:9222';
export const DEFAULT_MODEL = 'Nano Banana 2.1';

// Chrome's DevTools address: env FLOW_CDP, else the plan's "cdp", else the default.
export const cdpUrl = plan => process.env.FLOW_CDP || (plan && plan.cdp) || DEFAULT_CDP;

// A plan path: absolute stays as it is; relative is resolved against the plan's "base" when it has
// one, otherwise against the current directory (old plans; loadPlan fills the base in, see planBase). Always returned with forward slashes
// (gen.mjs splits paths on "/").
export function resolvePlanPath(p, plan = {}) {
  if (p == null || p === '') return p;
  const base = plan.base ? path.resolve(plan.base) : process.cwd();
  return (path.isAbsolute(p) ? path.normalize(p) : path.resolve(base, p)).replace(/\\/g, '/');
}

// Resolves a parsed plan in place and returns it.
export function resolvePlan(plan) {
  for (const k of ['journal', 'stop_file']) if (plan[k]) plan[k] = resolvePlanPath(plan[k], plan);
  for (const s of plan.shots || []) for (const k of ['prompt', 'out', 'meta', 'used']) if (s[k]) s[k] = resolvePlanPath(s[k], plan);
  return plan;
}

// Where relative plan paths start, for a plan read from a file: env FLOW_PLAN_BASE (the tool sets it to
// the episode folder, so a data folder that moved to another PC or path still works), else the plan's own
// "base", else the plan file's grandparent folder (<episode>/flow/plan.json -> <episode>).
export const planBase = (plan, file, env = process.env) =>
  env.FLOW_PLAN_BASE || (plan && plan.base) || path.dirname(path.dirname(path.resolve(file)));

export function loadPlan(file) {
  const plan = JSON.parse(fs.readFileSync(file, 'utf8'));
  plan.base = planBase(plan, file);
  return resolvePlan(plan);
}

// The image model the prompt bar must show: env FLOW_MODEL, else the plan's "image_model", else the old default.
export const expectedModel = plan => process.env.FLOW_MODEL || (plan && plan.image_model) || DEFAULT_MODEL;

// Case-insensitive, ignores surrounding/extra whitespace and a trailing count such as "x1".
const normModel = s => String(s ?? '').replace(/\s+/g, ' ').trim().replace(/\s*\bx\d+$/i, '').trim().toLowerCase();
export const modelMatches = (expected, found) => {
  const e = normModel(expected);
  return e !== '' && e === normModel(found);
};

// Model name out of a settings chip's raw text ("🍌 Nano Banana 2.1 crop_16_9 x1", "Nano Banana Pro arrow_drop_down"):
// drops the aspect icon name, icon-font words and any leading emoji/symbols.
export const chipModel = chip => String(chip ?? '')
  .replace(/\b(crop_\S+|arrow_drop_down|arrow_drop_up|expand_more|expand_less|keyboard_arrow_\w+)\b/g, ' ')
  .replace(/^[^\p{L}\p{N}]+/u, '')
  .replace(/\s+/g, ' ').trim();
