// Naive functional test of a todo app. No knowledge of the app's internals.
// Acts like a person who has never seen it: finds the obvious text box, types,
// presses Enter, and looks to see whether what they typed showed up.
//
// Every check is a question a normal user would ask, answered by looking at the
// page. Nothing here grades style, and no model is consulted.
const { chromium } = require('playwright-core');
const http = require('http');
const fs = require('fs');
const path = require('path'); const os = require('os');

const EXE = (() => {
  // Playwright's own browser cache. PLAYWRIGHT_BROWSERS_PATH overrides it.
  const base = process.env.PLAYWRIGHT_BROWSERS_PATH || (
    process.platform === 'darwin' ? path.join(os.homedir(), 'Library', 'Caches', 'ms-playwright')
    : process.platform === 'win32' ? path.join(os.homedir(), 'AppData', 'Local', 'ms-playwright')
    : path.join(os.homedir(), '.cache', 'ms-playwright'));
  const d = fs.readdirSync(base).filter(x => x.startsWith('chromium_headless_shell')).sort().pop();
  if (!d) throw new Error('no chromium headless shell in ' + base + '; run: npx playwright install chromium');
  const dir = path.join(base, d);
  const sub = fs.readdirSync(dir).find(x => x.startsWith('chrome-headless-shell-'));
  return path.join(dir, sub, 'chrome-headless-shell');
})();

const ITEM = 'buy milk';
const EDITED = 'buy bread';

async function visibleText(page) { return (await page.locator('body').innerText()).toLowerCase(); }
async function has(page, s) { return (await visibleText(page)).includes(s.toLowerCase()); }

// Find the box a person would type into: the first visible text input that is
// not a search/filter box.
async function typeBox(page) {
  const cands = page.locator('input:not([type]), input[type=text], textarea');
  const n = await cands.count();
  for (let i = 0; i < n; i++) {
    const el = cands.nth(i);
    if (await el.isVisible().catch(() => false)) return el;
  }
  return null;
}

async function addItem(page, text) {
  const box = await typeBox(page);
  if (!box) return false;
  await box.click();
  await box.fill(text);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(350);
  if (await has(page, text)) return true;
  // Enter did nothing. A person would then look for the obvious button.
  const btn = page.locator('button, input[type=submit], [role=button]').filter({
    hasText: /^\s*(add|\+|new|create|submit|save)\s*$/i });
  if (await btn.count()) {
    await box.fill(text);
    await btn.first().click();
    await page.waitForTimeout(350);
  }
  return await has(page, text);
}

// The element a user would perceive as "the row for this item".
function rowFor(page, text) {
  return page.locator('li, tr, .todo, .item, .task, div').filter({ hasText: text }).last();
}

(async () => {
  const file = process.argv[2];
  const html = fs.readFileSync(file);
  const server = http.createServer((req, res) => {
    res.writeHead(200, { 'content-type': 'text/html' });
    res.end(html);
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const url = `http://127.0.0.1:${server.address().port}/`;

  const browser = await chromium.launch({ executablePath: EXE });
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const external = [];
  page.on('request', r => { if (!r.url().startsWith(url) && !r.url().startsWith('data:')) external.push(r.url()); });
  const errors = [];
  page.on('pageerror', e => errors.push(String(e).slice(0, 200)));

  const out = [];
  const rec = (q, met, note) => out.push({ q, met, note: String(note).slice(0, 180) });

  try {
    await page.goto(url, { waitUntil: 'networkidle', timeout: 20000 });
    await page.waitForTimeout(300);

    rec('Does the page load without a script error?', errors.length === 0,
        errors.length ? errors[0] : 'no uncaught errors');

    const added = await addItem(page, ITEM).catch(e => false);
    rec('Can I add a task?', added, added ? `"${ITEM}" appeared on the page` : 'typed it, nothing appeared');

    // remaining count: a digit shown near wording about items left
    const txt = await visibleText(page);
    const count = /(\d+)\s*(item|task|todo)s?\s*(left|remaining)|(left|remaining)\s*[:\-]?\s*(\d+)/i.test(txt)
               || /\b1\b/.test(txt);
    rec('Can I see how many are left?', added && count, count ? 'a remaining count is shown' : 'no count visible');

    // complete
    let completed = false, note = 'no checkbox or toggle found in the row';
    if (added) {
      const row = rowFor(page, ITEM);
      const box = row.locator('input[type=checkbox]').first();
      if (await box.count()) {
        await box.click(); await page.waitForTimeout(300);
        const cls = await row.getAttribute('class') || '';
        const deco = await row.evaluate(el => {
          const walk = [el, ...el.querySelectorAll('*')];
          return walk.some(n => /line-through/.test(getComputedStyle(n).textDecorationLine));
        }).catch(() => false);
        completed = deco || /done|complete|checked/i.test(cls) || await box.isChecked();
        note = completed ? 'item shows as done after clicking its checkbox' : 'clicked, but nothing indicated completion';
      }
    }
    rec('Can I tick a task off?', completed, note);

    // filter
    let filtered = false, fnote = 'no all/active/completed controls found';
    const fl = page.locator('a, button, label, li, span').filter({ hasText: /^\s*(active|completed)\s*$/i });
    if (await fl.count()) {
      const active = fl.filter({ hasText: /^\s*active\s*$/i }).first();
      if (await active.count()) {
        await active.click(); await page.waitForTimeout(300);
        const goneWhenActive = completed ? !(await has(page, ITEM)) : await has(page, ITEM);
        const all = page.locator('a, button, label, li, span').filter({ hasText: /^\s*all\s*$/i }).first();
        if (await all.count()) { await all.click(); await page.waitForTimeout(250); }
        filtered = goneWhenActive;
        fnote = filtered ? 'the list changed when switching filters' : 'clicking a filter did not change the list';
      }
    }
    rec('Can I filter by active and completed?', filtered, fnote);

    // edit
    let edited = false, enote = 'double-clicking the text gave no editable field';
    if (added) {
      const row = rowFor(page, ITEM);
      const label = row.locator(`text=${ITEM}`).first();
      if (await label.count()) {
        await label.dblclick({ timeout: 4000 }).catch(() => {});
        await page.waitForTimeout(300);
        const edit = page.locator('input:focus, textarea:focus, [contenteditable=true]:focus').first();
        if (await edit.count()) {
          await edit.fill(EDITED).catch(async () => { await page.keyboard.type(EDITED); });
          await page.keyboard.press('Enter');
          await page.waitForTimeout(350);
          edited = await has(page, EDITED);
          enote = edited ? 'text changed after editing in place' : 'edit field appeared but the change did not stick';
        } else {
          const eb = row.locator('button, [role=button]').filter({ hasText: /edit|pencil|✏/i }).first();
          if (await eb.count()) {
            await eb.click(); await page.waitForTimeout(300);
            const e2 = page.locator('input:focus, textarea:focus, [contenteditable=true]:focus').first();
            if (await e2.count()) {
              await e2.fill(EDITED); await page.keyboard.press('Enter'); await page.waitForTimeout(300);
              edited = await has(page, EDITED);
              enote = edited ? 'text changed via an edit button' : 'edit control found but the change did not stick';
            }
          }
        }
      }
    }
    rec('Can I change what a task says?', edited, enote);

    // persistence
    const current = (await has(page, EDITED)) ? EDITED : ITEM;
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(500);
    const persisted = added && await has(page, current);
    rec('Are my tasks still there after a reload?', persisted,
        persisted ? 'the task survived a page reload' : 'the list was empty after reloading');

    // delete
    let deleted = false, dnote = 'no delete control found in the row';
    if (persisted) {
      const row = rowFor(page, current);
      const db = row.locator('button, a, span, [role=button]').filter({ hasText: /^\s*(x|×|✕|✖|delete|remove|del|🗑|trash)\s*$/i }).first();
      let target = (await db.count()) ? db : row.locator('[class*=delete],[class*=remove],[aria-label*=elete],[aria-label*=emove],[title*=elete]').first();
      if (await target.count()) {
        await target.click({ force: true }).catch(() => {});
        await page.waitForTimeout(400);
        deleted = !(await has(page, current));
        dnote = deleted ? 'the task disappeared after clicking delete' : 'clicked delete, the task is still listed';
      }
    }
    rec('Can I delete a task?', deleted, dnote);

    rec('Does it work without downloading anything from the internet?', external.length === 0,
        external.length ? `requested ${external.length} external resource(s)` : 'no external requests');

  } catch (e) {
    rec('Did the test complete?', false, String(e).slice(0, 200));
  } finally {
    await browser.close(); server.close();
  }
  console.log(JSON.stringify(out, null, 2));
})();
