// What Flow's project page is set to, read from the page itself. Shared by preflight.mjs and gen.mjs.
//
// Flow has two prompt bars. The plain one shows a settings chip ("Video · 720p · 8s crop_16_9 x2").
// In Agent mode there is no chip: the agent uses the defaults in its Settings panel, and a saved
// Agent instruction keeps it from rewriting prompts. Both are read here rather than trusted.

const text = e => (e.innerText || '').replace(/\s+/g, ' ').trim();

// FLOW_FRONT=1: bring the Flow tab to the front, once (the Python side sets it for the first invocation of a
// run and for the re-check after a stop). Never closes or navigates a tab. A stop always raises the tab (raise()).
export async function front(p) {
  if (process.env.FLOW_FRONT === '1') await p.bringToFront().catch(() => {});
}
export async function raise(p) {
  if (p) await p.bringToFront().catch(() => {});
}

export async function closePanels(p) {
  for (let i = 0; i < 3; i++) {
    const open = await p.getByText('Agent settings', {exact: true}).count() || await p.getByText('Agent instructions', {exact: true}).count();
    if (!open) return;
    await p.locator('button[aria-label="Back"]').first().click({timeout: 5000}).catch(() => {});
    await p.waitForTimeout(1200);
  }
}

// The plain bar's settings chip, Flow's "Settings trigger" button: "🍌 Nano Banana 2.1 crop_16_9 x1",
// or "Video · 720p · 8s crop_16_9 x2" in video mode. '' while Agent mode hides it.
export async function modeChip(p) {
  if (await agentMode(p)) return '';
  return await p.evaluate(() => [...document.querySelectorAll('button[aria-label="Settings trigger"]')]
    .filter(b => b.getBoundingClientRect().height > 0)
    .map(b => b.innerText.replace(/\s+/g, ' ').trim())[0] || '');
}

// The Agent button stays on screen when Agent mode is off; aria-pressed says which.
export async function agentMode(p) {
  return await p.evaluate(() => [...document.querySelectorAll('button')]
    .some(b => (b.innerText || '').trim() === 'Agent' && b.getAttribute('aria-pressed') === 'true'));
}

// A plain-mode chip that will make an image, not a video.
export const imageChip = chip => !!chip && !/video/i.test(chip) && /banana|imagen|image/i.test(chip);

// Agent settings: confirmation policy and the IMAGE defaults (the first aspect/count group and model).
export async function agentSettings(p) {
  await closePanels(p);
  await p.locator('button[aria-label="Settings"]:has-text("tune")').first().click({timeout: 15000});
  await p.waitForTimeout(1500);
  const s = await p.evaluate(() => {
    const t = e => (e.innerText || '').replace(/\s+/g, ' ').trim();
    const radio = [...document.querySelectorAll('input[type=radio]')]
      .find(r => /^Never/.test(t(r.closest('label, mat-radio-button') || r.parentElement)));
    const checked = [...document.querySelectorAll('button[aria-checked="true"]')].map(t);
    const models = [...document.querySelectorAll('button')].filter(b => /arrow_drop_down/.test(t(b)))
      .map(b => t(b).replace('arrow_drop_down', '').trim());
    return {never: !!(radio && radio.checked), aspect: checked[0] || '', count: checked[1] || '', model: models[0] || ''};
  });
  await closePanels(p);
  return s;
}

// The saved Agent instructions, as text ('' if none are saved).
export async function agentInstructions(p) {
  await closePanels(p);
  await p.locator('button[aria-label="Agent instructions"]').first().click({timeout: 15000});
  await p.waitForTimeout(1500);
  // Each saved instruction is an editable box: its words are in .value, not in the panel's text.
  const saved = await p.evaluate(() => {
    const add = [...document.querySelectorAll('button')].find(b => /Add instruction/.test(b.innerText || ''));
    let panel = add;
    for (let i = 0; i < 6 && panel; i++) panel = panel.parentElement;
    return panel ? [...panel.querySelectorAll('textarea, input')].map(e => e.value).join('\n') : '';
  });
  await closePanels(p);
  return saved;
}

// The picker's Characters list only draws the rows on screen: with ~24 Characters, a name below
// the fold isn't in the DOM until the list is scrolled. Walk the list top to bottom.
const optionName = async o => ((await o.innerText().catch(() => '')) || '').trim().split('\n')[0];
async function scrollList(p, to) {
  return p.evaluate(to => {
    let el = [...document.querySelectorAll('cdk-virtual-scroll-viewport')].find(v => v.offsetParent && v.querySelector('button[role=option]'));
    if (!el) { el = document.querySelector('button[role=option]'); while (el && el.scrollHeight <= el.clientHeight + 2) el = el.parentElement; }
    if (!el) return false;
    const s = el.scrollTop; el.scrollTop = to === 'top' ? 0 : s + 250; return el.scrollTop !== s;
  }, to);
}
export async function findOption(p, name) {
  await scrollList(p, 'top'); await p.waitForTimeout(300);
  for (let i = 0; i < 60; i++) {
    for (const o of await p.locator('button[role=option]').all()) {
      if (await optionName(o) === name) { await o.scrollIntoViewIfNeeded().catch(() => {}); return o; }
    }
    if (!(await scrollList(p, 'down'))) return null;
    await p.waitForTimeout(350);
  }
  return null;
}
export async function allOptionNames(p) {
  const seen = new Set();
  await scrollList(p, 'top'); await p.waitForTimeout(300);
  for (let i = 0; i < 60; i++) {
    for (const o of await p.locator('button[role=option]').all()) seen.add(await optionName(o));
    if (!(await scrollList(p, 'down'))) break;
    await p.waitForTimeout(350);
  }
  return [...seen].filter(Boolean);
}
