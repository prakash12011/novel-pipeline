// Character bible v2 helpers for flow/char.mjs. Pure functions plus one file write; no browser code.
//
// <bibleDir>/characters.json:
//   {"version": 2, "style": "...", "characters": {"<key>": {display, role, look, props, variants: {
//       "base": {flow_name, chapters, look: null | "...", flow_made}, "<variant>": {...}}}}}
import fs from 'node:fs';
import path from 'node:path';

export const portraitPrompt = (style, look) =>
  `${style}. Character reference: ${look}. Head and shoulders, facing the viewer, neutral expression, hands not visible, plain warm-gray background, even soft light. No props, no text, no logo.`;

export const bodyPrompt = style =>
  `${style}. Full body, standing straight, facing the viewer, arms relaxed at the sides, hands open and empty, holding nothing, no bag, no weapon, same face, hair and outfit. Plain warm-gray background, even soft light. No text, no logo.`;

// "tarot_lady" -> {key, variant: "base"}; "tarot_lady:brown_dress" -> {key, variant: "brown_dress"}
export function parseTarget(arg) {
  const i = String(arg).indexOf(':');
  const key = i < 0 ? String(arg) : arg.slice(0, i);
  const variant = i < 0 ? 'base' : arg.slice(i + 1);
  if (!key || !variant) throw new Error(`bad character argument "${arg}": use key or key:variant`);
  return {key, variant};
}

export function loadBible(bibleDir) {
  const file = path.join(bibleDir, 'characters.json');
  if (!fs.existsSync(file)) throw new Error(`no ${file}`);
  const bible = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (bible.version !== 2) throw new Error(`${file} is not a character bible v2 (no "version": 2); the v1 format is no longer supported by flow/char.mjs`);
  return bible;
}

// Everything char.mjs needs for one target. Throws a clear error for an unknown character or variant.
export function resolveTarget(bible, arg) {
  const {key, variant} = parseTarget(arg);
  const c = bible.characters && bible.characters[key];
  if (!c) throw new Error(`"${key}" is not in the bible (characters: ${Object.keys(bible.characters || {}).join(', ') || 'none'})`);
  const v = c.variants && c.variants[variant];
  if (!v) throw new Error(`character "${key}" has no variant "${variant}" (variants: ${Object.keys(c.variants || {}).join(', ') || 'none'})`);
  const look = v.look || c.look;
  if (!look) throw new Error(`"${arg}" has no look text (neither the variant nor the character has one)`);
  if (!v.flow_name) throw new Error(`"${arg}" has no flow_name`);
  const stem = variant === 'base' ? key : `${key}.${variant}`;
  return {
    key, variant, id: variant === 'base' ? key : `${key}:${variant}`, stem,
    flowName: v.flow_name, role: c.role || '', look, flowMade: v.flow_made === true,
    portrait: portraitPrompt(bible.style, look), body: bodyPrompt(bible.style),
  };
}

// Sets flow_made on one variant and rewrites the file atomically (temp file in the same folder, then rename).
// Everything else is kept, including key order; the file's indent and final newline are kept too.
export function markFlowMade(bibleDir, key, variant, value = true) {
  const file = path.join(bibleDir, 'characters.json');
  const raw = fs.readFileSync(file, 'utf8');
  const bible = JSON.parse(raw);
  const v = bible.characters?.[key]?.variants?.[variant];
  if (!v) throw new Error(`cannot mark ${key}:${variant}: not in ${file}`);
  v.flow_made = value;
  const indent = (raw.match(/\n([ \t]+)"/) || [, '  '])[1];
  const eol = raw.includes('\r\n') ? '\r\n' : '\n';
  let out = JSON.stringify(bible, null, indent);
  if (eol === '\r\n') out = out.replace(/\n/g, '\r\n');
  if (/\n$/.test(raw)) out += eol;
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, out);
  fs.renameSync(tmp, file);
}
