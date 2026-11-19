/* Load the rebuilt dashboard in real headless Chrome, in DEMO mode.
   Reports console errors, verifies the boot sequence completes and the
   analyse view renders, and writes screenshots. */
const { chromium } = require('/opt/npm-tools/node_modules/playwright');
const fs = require('fs');
const path = require('path');

const FILE = process.argv[2] || '/home/claude/build/dashboard_v5.html';
const OUT = '/home/claude/shots';
fs.mkdirSync(OUT, { recursive: true });

/* Minimal Chart.js stand-in: the sandbox has no network, so the CDN script
   cannot load. The real app gets the real library; this only has to keep the
   render path from throwing so the layout can be inspected. */
const CHART_STUB = `
window.Chart = function(ctx, cfg){
  this.config = cfg; this.data = cfg && cfg.data;
  this.destroy = function(){}; this.update = function(){};
  this.resize = function(){};
  window.__charts = (window.__charts||0) + 1;
  window.__chartTypes = (window.__chartTypes||[]); window.__chartTypes.push(cfg&&cfg.type);
};
window.Chart.defaults = { font:{}, plugins:{} };
window.Chart.register = function(){};
`;

(async () => {
  const browser = await chromium.launch({
    executablePath: '/opt/pw-browsers/chromium-1243/chrome-linux64/chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const errors = [], warnings = [], pageErrors = [];
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 },
                                         deviceScaleFactor: 2 });
  const page = await ctx.newPage();

  // stub the CDN chart library; block font requests quietly
  await page.route('**/chart.umd.min.js', r =>
    r.fulfill({ status: 200, contentType: 'application/javascript', body: CHART_STUB }));
  await page.route('**/fonts.googleapis.com/**', r => r.abort());
  await page.route('**/fonts.gstatic.com/**', r => r.abort());

  page.on('console', m => {
    if (m.type() === 'error') errors.push(m.text());
    else if (m.type() === 'warning') warnings.push(m.text());
  });
  page.on('pageerror', e => pageErrors.push(e.message));

  await page.goto('file://' + FILE, { waitUntil: 'load' });

  const step = async (label, fn) => {
    try { const r = await fn(); console.log(`  [ OK ] ${label}` + (r ? ` — ${r}` : '')); return r; }
    catch (e) { console.log(`  [FAIL] ${label} — ${e.message}`); errors.push(label + ': ' + e.message); }
  };

  console.log('\n1. Boot sequence');
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(OUT, '01-boot.png') });
  await step('boot overlay visible', async () => {
    const vis = await page.isVisible('#boot'); if (!vis) throw new Error('not visible');
    const pct = await page.textContent('#boot-pct');
    const bars = await page.$$eval('#boot-chart .bcd', els => els.length);
    return `at ${pct}%, ${bars} candles drawn`;
  });
  await step('boot clears within 20s', async () => {
    await page.waitForSelector('#boot.gone', { timeout: 20000 });
    return 'app revealed';
  });

  console.log('\n2. Home view');
  await page.waitForTimeout(500);
  await step('mode badge reads DEMO', async () => (await page.textContent('#mode-badge')).trim());
  await step('hero headline rendered', async () => (await page.textContent('.hero-title')).replace(/\s+/g, ' ').trim());
  await step('ticker tape populated', async () =>
    (await page.$$eval('#tape-in span', e => e.length)) + ' cells');
  await step('budget slider ready', async () => {
    const ready = await page.$eval('#rp-slider', el => el.classList.contains('ready'));
    const sub = (await page.textContent('#rp-sub')).trim();
    return (ready ? 'ready · ' : 'NOT ready · ') + sub.slice(0, 60);
  });
  await page.screenshot({ path: path.join(OUT, '02-home.png'), fullPage: true });

  console.log('\n3. Generate the ranked lists');
  await step('click Generate lists', async () => {
    await page.click('#rp-generate');
    await page.waitForSelector('#reco-grid', { state: 'visible', timeout: 10000 });
    await page.waitForTimeout(900);
    const n = await page.$$eval('.reco-card', e => e.length);
    return n + ' ranked cards across the three lists';
  });
  await step('expand the first card', async () => {
    const h = await page.$('.reco-card .reco-head');
    if (!h) throw new Error('no card');
    await h.click(); await page.waitForTimeout(500);
    const open = await page.$eval('.reco-card', el => el.classList.contains('open'));
    return open ? 'expands' : 'did NOT expand';
  });
  await page.screenshot({ path: path.join(OUT, '03-lists.png'), fullPage: true });

  console.log('\n4. Command palette');
  await step('Ctrl+K opens, typing filters', async () => {
    await page.keyboard.press('Control+k');
    await page.waitForSelector('#v5-cmdk.show', { timeout: 4000 });
    await page.keyboard.type('luck');
    await page.waitForTimeout(350);
    const n = await page.$$eval('#v5-cmd-list .v5-cmd-item', e => e.length);
    const first = await page.textContent('#v5-cmd-list .v5-cmd-item .sym');
    await page.screenshot({ path: path.join(OUT, '04-cmdk.png') });
    await page.keyboard.press('Escape');
    return `${n} matches, top = ${first.trim()}`;
  });

  console.log('\n5. Fundamental analysis');
  await step('run analysis on OGDC', async () => {
    await page.click('#depth-cta');
    await page.waitForTimeout(500);
    await page.fill('#stock-input', 'OGDC');
    await page.click('#analyze-btn');
    await page.waitForSelector('#stage.show', { timeout: 20000 });
    await page.waitForTimeout(2200);
    return 'stage shown';
  });
  await step('score + verdict rendered', async () => {
    const s = (await page.textContent('#gauge-score')).trim();
    const v = (await page.textContent('#verdict-label')).trim();
    return `score ${s} · verdict "${v}"`;
  });
  await step('all 11 metric rows present', async () =>
    (await page.$$eval('#metrics .metric', e => e.length)) + ' rows');
  await step('metric bars filled', async () => {
    const w = await page.$$eval('#metrics .m-fill', els =>
      els.filter(e => parseFloat(e.style.width) > 0).length);
    return w + ' bars animated';
  });
  await step('NEW · score composition built', async () => {
    const rows = await page.$$eval('#v5-contrib .v5-crow', e => e.length);
    const seg = await page.$$eval('#v5-stack i', e => e.length);
    return `${rows} contribution rows, ${seg} stack segments`;
  });
  await step('NEW · radar chart instantiated', async () => {
    const types = await page.evaluate(() => window.__chartTypes || []);
    if (!types.includes('radar')) throw new Error('radar never created; types=' + types);
    return 'radar + ' + types.filter(t => t !== 'radar').join(', ');
  });
  await step('NEW · small multiples built', async () =>
    (await page.$$eval('#v5-multiples .v5-mult', e => e.length)) + ' mini charts');
  await step('NEW · provenance table built', async () =>
    (await page.$$eval('#v5-sources .v5-srow', e => e.length)) + ' source rows');
  await step('workbench identity synced', async () =>
    (await page.textContent('#co-symbol')).trim() + ' / ' +
    (await page.textContent('#co-name')).trim());
  await step('Fundamental tab marked active', async () => {
    const on = await page.$eval('#analyze-btn', el => el.classList.contains('on'));
    return on ? 'yes' : 'NO';
  });
  await page.screenshot({ path: path.join(OUT, '05-fundamental.png'), fullPage: true });

  console.log('\n6. Metric explainer modal');
  await step('open a metric explainer', async () => {
    await page.click('#metrics .m-info');
    await page.waitForSelector('#kpi-modal.show', { timeout: 4000 });
    const t = (await page.textContent('#km-title')).trim();
    await page.screenshot({ path: path.join(OUT, '06-metric-modal.png') });
    await page.click('#km-close');
    return t;
  });

  console.log('\n7. Technical analysis');
  await step('run technical', async () => {
    await page.click('#predict-btn');
    await page.waitForSelector('#pstage.show', { timeout: 20000 });
    await page.waitForTimeout(2200);
    return 'stage shown';
  });
  await step('verdict + scores', async () => {
    const l = (await page.textContent('#pv-label')).trim();
    const t = (await page.textContent('#pv-tech')).trim();
    const f = (await page.textContent('#pv-fund')).trim();
    const c = (await page.textContent('#pv-comb')).trim();
    return `"${l}" · chart ${t} / business ${f} / overall ${c}`;
  });
  await step('NEW · verdict basis stated', async () =>
    '"' + (await page.textContent('#pv-basis')).trim().slice(0, 70) + '"');
  await step('story cards rendered', async () =>
    (await page.$$eval('#eli5-grid .eli5', e => e.length)) + ' cards');
  await step('candle chart drawn', async () => {
    const d = await page.$eval('#pcandle-canvas', c => c.width + 'x' + c.height);
    return 'canvas ' + d;
  });
  await step('wyckoff season strip', async () =>
    (await page.$$eval('#wy-cycle .wy-seg', e => e.length)) + ' seasons, ' +
    (await page.$$eval('#wy-cycle .wy-seg.on', e => e.length)) + ' marked NOW');
  await step('trade plan ladder', async () =>
    (await page.$$eval('#plan-cards .plan-card', e => e.length)) + ' plan rows');
  await step('Technical tab marked active', async () => {
    const on = await page.$eval('#predict-btn', el => el.classList.contains('on'));
    return on ? 'yes' : 'NO';
  });
  await page.screenshot({ path: path.join(OUT, '07-technical.png'), fullPage: true });

  console.log('\n8. Light theme');
  await step('toggle to light and back', async () => {
    await page.click('#theme-toggle');
    await page.waitForTimeout(700);
    const th = await page.getAttribute('html', 'data-theme');
    await page.screenshot({ path: path.join(OUT, '08-light.png'), fullPage: true });
    return 'theme = ' + th;
  });

  console.log('\n9. Mobile layout');
  await step('render at 390x844', async () => {
    const m = await ctx.newPage();
    await m.route('**/chart.umd.min.js', r =>
      r.fulfill({ status: 200, contentType: 'application/javascript', body: CHART_STUB }));
    await m.route('**/fonts.g*/**', r => r.abort());
    await m.setViewportSize({ width: 390, height: 844 });
    await m.goto('file://' + FILE, { waitUntil: 'load' });
    await m.waitForSelector('#boot.gone', { timeout: 20000 });
    await m.waitForTimeout(700);
    const w = await m.evaluate(() => document.documentElement.scrollWidth);
    await m.screenshot({ path: path.join(OUT, '09-mobile.png'), fullPage: true });
    await m.close();
    if (w > 400) throw new Error('horizontal overflow: scrollWidth=' + w);
    return 'no horizontal overflow (scrollWidth ' + w + ')';
  });

  console.log('\n10. Console health');
  const realErrors = errors.filter(e =>
    !/favicon|ERR_FAILED|net::|Failed to load resource/i.test(e));
  if (pageErrors.length) {
    console.log('  [FAIL] uncaught page errors:');
    pageErrors.slice(0, 8).forEach(e => console.log('         ' + e));
  } else console.log('  [ OK ] no uncaught page errors');
  if (realErrors.length) {
    console.log('  [WARN] console errors:');
    [...new Set(realErrors)].slice(0, 8).forEach(e => console.log('         ' + e.slice(0, 150)));
  } else console.log('  [ OK ] no console errors');
  const v5warn = warnings.filter(w => /\[v5\]/.test(w));
  if (v5warn.length) {
    console.log('  [WARN] v5 module warnings:');
    [...new Set(v5warn)].slice(0, 6).forEach(w => console.log('         ' + w.slice(0, 150)));
  } else console.log('  [ OK ] no v5 render warnings');

  await browser.close();
  console.log('\nscreenshots → ' + OUT);
  process.exit(pageErrors.length ? 1 : 0);
})();
