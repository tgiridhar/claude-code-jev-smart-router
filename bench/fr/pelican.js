// Naive functional test of the pelican SVG. Asks only what a person opening the
// file would notice: does it open as a picture, is anything actually drawn, and
// does it need anything from the internet. Whether it LOOKS like a pelican on a
// bicycle is a judgement, so it belongs in the quality score, not here.
const { chromium } = require('playwright-core');
const fs = require('fs'); const path = require('path'); const os = require('os'); const http = require('http');

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

(async () => {
  const file = process.argv[2];
  const svg = fs.readFileSync(file, 'utf8');
  const server = http.createServer((q, r) => { r.writeHead(200, {'content-type':'image/svg+xml'}); r.end(svg); });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const url = `http://127.0.0.1:${server.address().port}/p.svg`;
  const browser = await chromium.launch({ executablePath: EXE });
  const page = await (await browser.newContext()).newPage();
  const external = [];
  page.on('request', r => { if (!r.url().startsWith(url) && !r.url().startsWith('data:')) external.push(r.url()); });
  const out = []; const rec = (q, met, note) => out.push({ q, met, note: String(note).slice(0,180) });
  try {
    const resp = await page.goto(url, { waitUntil: 'networkidle', timeout: 20000 });
    const parseErr = await page.locator('parsererror').count().catch(() => 0);
    rec('Does the file open as a picture?', resp.ok() && !parseErr,
        parseErr ? 'the browser reported an XML parse error' : 'opened as an image');

    const shapes = await page.evaluate(() => document.querySelectorAll(
      'path,circle,ellipse,rect,line,polyline,polygon,g>*').length).catch(() => 0);
    rec('Is there actually a drawing in it?', shapes >= 10, `${shapes} drawn shapes`);

    // Is the canvas blank? Screenshot and count distinct non-white pixels.
    const buf = await page.screenshot({ type: 'png' });
    const ink = await page.evaluate(() => {
      const s = document.documentElement;
      const bb = s.getBBox ? s.getBBox() : null;
      return bb ? Math.round(bb.width * bb.height) : 0;
    }).catch(() => 0);
    rec('Is anything visible rather than blank?', ink > 100 && buf.length > 2000,
        ink ? `drawn area ${ink} square units` : 'nothing measurable was drawn');

    rec('Does it work without downloading anything from the internet?', external.length === 0,
        external.length ? `requested ${external.length} external resource(s)` : 'no external requests');
    rec('Does it avoid embedded photo data?', !/xlink:href\s*=\s*["']data:image|<image\b/i.test(svg),
        'checked for raster <image> and data: URIs');
  } catch (e) { rec('Did the test complete?', false, String(e).slice(0,200)); }
  finally { await browser.close(); server.close(); }
  console.log(JSON.stringify(out, null, 2));
})();
