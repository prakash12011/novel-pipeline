// Unit tests for the pure parts of flow/: model comparison, plan paths, bible v2. No browser, no network.
//   node --test flow/flow.test.mjs      (tests/test_flow_units.py runs it from pytest)
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {modelMatches, chipModel, expectedModel, cdpUrl, resolvePlanPath, planBase, loadPlan, DEFAULT_CDP} from './plan.mjs';
import {portraitPrompt, bodyPrompt, parseTarget, resolveTarget, markFlowMade} from './bible.mjs';

test('model comparison', () => {
  assert.ok(modelMatches('Nano Banana Pro', 'Nano Banana Pro x1'));
  assert.ok(modelMatches('Nano Banana Pro', 'nano banana pro'));
  assert.ok(modelMatches('Nano Banana Pro', '  Nano   Banana Pro  x2 '));
  assert.ok(modelMatches(' nano banana 2.1 ', 'Nano Banana 2.1'));
  assert.ok(!modelMatches('Nano Banana Pro', 'Nano Banana 2.1 x1'));
  assert.ok(!modelMatches('Nano Banana Pro', 'Nano Banana'));
  assert.ok(!modelMatches('', ''));
});

test('chip text to model name', () => {
  assert.equal(chipModel('\u{1F34C} Nano Banana 2.1 crop_16_9'), 'Nano Banana 2.1');
  assert.equal(chipModel('Nano Banana Pro arrow_drop_down'), 'Nano Banana Pro');
});

test('settings: env, plan, default', () => {
  const keep = {m: process.env.FLOW_MODEL, c: process.env.FLOW_CDP};
  delete process.env.FLOW_MODEL; delete process.env.FLOW_CDP;
  assert.equal(expectedModel({}), 'Nano Banana 2.1');
  assert.equal(expectedModel({image_model: 'Nano Banana Pro'}), 'Nano Banana Pro');
  process.env.FLOW_MODEL = 'X';
  assert.equal(expectedModel({image_model: 'Nano Banana Pro'}), 'X');
  assert.equal(cdpUrl({}), DEFAULT_CDP);
  assert.equal(cdpUrl({cdp: 'http://127.0.0.1:9333'}), 'http://127.0.0.1:9333');
  process.env.FLOW_CDP = 'http://127.0.0.1:1';
  assert.equal(cdpUrl({cdp: 'http://127.0.0.1:9333'}), 'http://127.0.0.1:1');
  for (const [k, v] of [['FLOW_MODEL', keep.m], ['FLOW_CDP', keep.c]]) v === undefined ? delete process.env[k] : (process.env[k] = v);
});

test('plan paths', () => {
  const fwd = s => s.split(path.sep).join('/');
  const abs = fwd(path.resolve(os.tmpdir(), 'a', 'b.jpg'));
  assert.equal(resolvePlanPath(abs, {base: '/elsewhere'}), abs);
  assert.equal(resolvePlanPath('x/y.jpg', {base: os.tmpdir()}), fwd(path.resolve(os.tmpdir(), 'x/y.jpg')));
  assert.equal(resolvePlanPath('x/y.jpg', {}), fwd(path.resolve('x/y.jpg')));
});

test('plan base: env, plan, grandparent of the plan file; relative plans move with the folder', () => {
  const fwd = s => s.split(path.sep).join('/');
  const keep = process.env.FLOW_PLAN_BASE;
  delete process.env.FLOW_PLAN_BASE;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'planbase-'));
  const file = path.join(root, 'E001', 'flow', 'plan.json');
  fs.mkdirSync(path.dirname(file), {recursive: true});
  fs.writeFileSync(file, JSON.stringify({journal: 'flow/journal.jsonl', shots: [{id: 'c0001-s01', out: 'images/c0001/s01-2k.jpg'}]}));
  try {
    assert.equal(planBase({}, file), path.join(root, 'E001'));
    assert.equal(planBase({base: '/x'}, file), '/x');
    assert.equal(planBase({base: '/x'}, file, {FLOW_PLAN_BASE: '/y'}), '/y');
    // no env, no base: the plan file's grandparent (the episode folder)
    assert.equal(loadPlan(file).shots[0].out, fwd(path.join(root, 'E001', 'images/c0001/s01-2k.jpg')));
    assert.equal(loadPlan(file).journal, fwd(path.join(root, 'E001', 'flow/journal.jsonl')));
    // the env var wins over the plan's own base
    process.env.FLOW_PLAN_BASE = path.join(root, 'elsewhere');
    assert.equal(loadPlan(file).shots[0].out, fwd(path.join(root, 'elsewhere', 'images/c0001/s01-2k.jpg')));
    // absolute paths in an old plan stay as they are
    const abs = fwd(path.join(root, 'old', 'x.jpg'));
    fs.writeFileSync(file, JSON.stringify({shots: [{id: 'a', out: abs}]}));
    assert.equal(loadPlan(file).shots[0].out, abs);
  } finally {
    keep === undefined ? delete process.env.FLOW_PLAN_BASE : (process.env.FLOW_PLAN_BASE = keep);
    fs.rmSync(root, {recursive: true, force: true});
  }
});

const STYLE = 'Flat ink style';
const bible = {version: 2, style: STYLE, characters: {tarot_lady: {
  display: 'Tarot Lady', role: 'A fortune teller.', look: 'woman in her thirties, long black hair, green eyes, grey coat',
  props: ['tarot cards'], variants: {
    base: {flow_name: 'Tarot Lady v2', chapters: '4-', look: null, flow_made: false},
    brown_dress: {flow_name: 'Tarot Lady - brown dress', chapters: '25-26', look: 'woman in her thirties, long black hair, green eyes, brown dress', flow_made: false}}}}};

test('bible prompts: golden strings', () => {
  assert.equal(portraitPrompt(STYLE, 'woman in her thirties'),
    'Flat ink style. Character reference: woman in her thirties. Head and shoulders, facing the viewer, neutral expression, hands not visible, plain warm-gray background, even soft light. No props, no text, no logo.');
  assert.equal(bodyPrompt(STYLE),
    'Flat ink style. Full body, standing straight, facing the viewer, arms relaxed at the sides, hands open and empty, holding nothing, no bag, no weapon, same face, hair and outfit. Plain warm-gray background, even soft light. No text, no logo.');
  const base = resolveTarget(bible, 'tarot_lady');
  assert.equal(base.portrait, `${STYLE}. Character reference: woman in her thirties, long black hair, green eyes, grey coat. Head and shoulders, facing the viewer, neutral expression, hands not visible, plain warm-gray background, even soft light. No props, no text, no logo.`);
  assert.equal(base.flowName, 'Tarot Lady v2');
  assert.equal(base.role, 'A fortune teller.');
  assert.equal(base.stem, 'tarot_lady');
  const v = resolveTarget(bible, 'tarot_lady:brown_dress');
  assert.equal(v.portrait, `${STYLE}. Character reference: woman in her thirties, long black hair, green eyes, brown dress. Head and shoulders, facing the viewer, neutral expression, hands not visible, plain warm-gray background, even soft light. No props, no text, no logo.`);
  assert.equal(v.flowName, 'Tarot Lady - brown dress');
  assert.equal(v.stem, 'tarot_lady.brown_dress');
  assert.equal(v.id, 'tarot_lady:brown_dress');
});

test('key:variant parsing and errors', () => {
  assert.deepEqual(parseTarget('tarot_lady'), {key: 'tarot_lady', variant: 'base'});
  assert.deepEqual(parseTarget('tarot_lady:brown_dress'), {key: 'tarot_lady', variant: 'brown_dress'});
  assert.throws(() => resolveTarget(bible, 'tarot_lady:nope'), /no variant "nope".*base, brown_dress/);
  assert.throws(() => resolveTarget(bible, 'ghost'), /"ghost" is not in the bible/);
});

test('flow_made write-back keeps everything else', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bible-'));
  const file = path.join(dir, 'characters.json');
  const original = JSON.stringify({...bible, extra: {z: 1, a: [1, 2, {k: null}]}}, null, 2) + '\n';
  fs.writeFileSync(file, original);
  markFlowMade(dir, 'tarot_lady', 'brown_dress');
  const after = JSON.parse(fs.readFileSync(file, 'utf8'));
  assert.equal(after.characters.tarot_lady.variants.brown_dress.flow_made, true);
  const expected = JSON.parse(original);
  expected.characters.tarot_lady.variants.brown_dress.flow_made = true;
  assert.deepEqual(after, expected);
  assert.equal(fs.readFileSync(file, 'utf8'), JSON.stringify(expected, null, 2) + '\n');   // same order, indent, final newline
  assert.deepEqual(Object.keys(after), Object.keys(expected));
  assert.deepEqual(fs.readdirSync(dir), ['characters.json']);                              // no temp file left
});

// FLOW_FRONT: bringToFront only when asked; a stop always raises the tab; neither closes or navigates anything.
import {front, raise} from './page.mjs';
test('front() raises the tab only with FLOW_FRONT=1; raise() always', async () => {
  const calls = [];
  const page = {bringToFront: async () => { calls.push('front'); }, close: async () => { calls.push('CLOSE'); }, goto: async () => { calls.push('GOTO'); }};
  const old = process.env.FLOW_FRONT;
  try {
    delete process.env.FLOW_FRONT; await front(page); assert.deepEqual(calls, []);
    process.env.FLOW_FRONT = '1'; await front(page); assert.deepEqual(calls, ['front']);
    delete process.env.FLOW_FRONT; await raise(page); assert.deepEqual(calls, ['front', 'front']);
    await raise(undefined);
  } finally { if (old === undefined) delete process.env.FLOW_FRONT; else process.env.FLOW_FRONT = old; }
});
