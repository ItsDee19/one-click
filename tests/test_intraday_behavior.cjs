// Offline contract tests: no Flask import, requests, broker or scheduler.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '..', 'intraday_page.html'), 'utf8');
const code = html.split('<!-- shared:js -->')[1].match(/<script>([\s\S]*?)<\/script>/)[1];
const START = Date.parse('2026-09-08T05:00:00Z');
const iso = offset => new Date(START + offset).toISOString();
function pick(symbol, direction = 'long', qualified = true) {
  return {symbol, direction, strategy: 'Fixture rule', state: 'entry_ready', entry: 100, last: 100.1,
    stop: direction === 'short' ? 101 : 99, target: direction === 'short' ? 98 : 102,
    current_reward_risk: 1.7, rvol: 2.1, signal_at: iso(-300000), confirmed_at: iso(0),
    as_of: iso(0), expires_at: iso(600000), why: 'Observed pattern',
    validation: {qualified, status: qualified ? 'qualified' : 'legacy_unverified', reasons: qualified ? [] : ['No clean holdout evidence']},
    record: {enough: true, trusted: true, trades: 1000, expectancy_r: .3}};
}
function book() {
  return {status: 'done', tradeable: true, generated: iso(0), phase: 'open',
    picks: [pick('LONG'), pick('SHORT', 'short')], candidates: [pick('RESEARCH', 'long', false)], history: [],
    coverage: {listed: 2288, eligible: 164, usable_sessions: 150, missing_sessions: 10, stale_sessions: 4},
    strategies: {Legacy: {trades: 1000, enough: true, trusted: true, expectancy_r: .3, win_rate_pct: 60},
      Net: {trades: 140, is_net: true, validation_status: 'research_only', expectancy_r: -.05, win_rate_pct: 42},
      Empty: {trades: 0}}, record_sessions: 9676, record_window: 'Fixture sample'};
}
class Element {
  constructor() { this.textContent = ''; this.listeners = {}; this.attributes = {}; this.hidden = false; this.disabled = false; this.children = []; this.classList = {add(){}, remove(){}}; }
  set innerHTML(value) { this._html = value; this.children = [...value.matchAll(/<article\b/g)].map(() => new Element()); }
  get innerHTML() { return this._html || ''; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  async fire(name) { for (const callback of this.listeners[name] || []) { await callback(); } }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelectorAll() { return this.children; }
  focus() { this.focused = true; }
}
const flush = () => new Promise(resolve => setImmediate(resolve));
async function harness(initial = book(), failInitially = false) {
  const nodes = new Map([...html.matchAll(/id="([^"]+)"/g)].map(m => [m[1], new Element()]));
  const el = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const document = new Element(), window = new Element(); document.hidden = false;
  let current = initial, error = failInitially ? Error('Failed to fetch') : null, deferred = null, hold = false, now = START, serial = 0;
  let timers = []; const calls = [];
  class Clock extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } }
  const context = {document, window, Date: Clock, Intl, isFinite, console, el,
    setTimeout(fn, delay) { const id = ++serial; timers.push({id, fn, delay}); return id; },
    clearTimeout(id) { timers = timers.filter(t => t.id !== id); },
    text: (node, value) => { node.textContent = String(value); },
    esc: v => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
    deskMoney: v => typeof v === 'number' && Number.isFinite(v) ? '₹' + v.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '—',
    async deskRequest(route) { calls.push(route); if (route === '/config') return {brand: 'Fixture'};
      if (hold) return new Promise((resolve, reject) => { deferred = {resolve, reject}; });
      if (error) throw error; return structuredClone(current); }
  };
  vm.createContext(context); vm.runInContext(code, context); await flush();
  return {el, document, window, calls, setBook(v) {current = v;}, setError(v) {error = v;}, setHold(v) {hold = v;},
    async resolve(v) { deferred.resolve(v); hold = false; await flush(); }, timers: () => timers,
    async tick(delay) { const timer = timers.find(t => t.delay === delay); assert(timer, 'Timer with delay ' + delay); timers = timers.filter(t => t.id !== timer.id); now += delay; await timer.fn(); await flush(); },
    async click(id) { await el(id).fire('click'); await flush(); }};
}
(async () => {
  const h = await harness();
  assert.match(h.el('picks').innerHTML, />BUY</); assert.match(h.el('picks').innerHTML, />SELL</);
  for (const text of ['Signal entry', 'Observed price', 'Current reward:risk', 'Signal:', 'Confirmed:', 'Price as of:', 'Entry expires:', 'IST']) assert(h.el('picks').innerHTML.includes(text));
  assert(!h.el('picks').innerHTML.includes('RESEARCH')); assert.match(h.el('candidates').innerHTML, /Research only/);
  assert.match(h.el('scoreboard').innerHTML, /Legacy gross · unverified/); assert.match(h.el('scoreboard').innerHTML, /After modeled costs/);
  assert(!h.el('scoreboard').innerHTML.includes('—%')); assert(!h.el('scoreboard').innerHTML.includes('+—R'));
  assert.match(h.el('coverage').innerHTML, /2,288/); assert.match(h.el('coverage').innerHTML, /Stale sessions/);
  const many = book(); many.candidates = Array.from({length: 25}, (_, i) => pick('RESEARCH' + i, 'long', false)); h.setBook(many);
  await h.click('refresh'); assert.equal(h.calls.at(-1), '/intraday?refresh=1'); assert.equal(h.el('candidates').children.length, 12);
  await h.click('candidate-more'); assert.equal(h.el('candidates').children.length, 24); assert(h.el('candidates').children[12].focused);
  await h.click('candidate-more'); assert.equal(h.el('candidates').children.length, 25); assert(h.el('candidate-more').hidden);
  h.setError(Error('Failed to fetch')); await h.click('refresh');
  assert(!h.el('load-error').hidden); assert.match(h.el('error-message').textContent, /Last received research remains visible/);
  assert(!h.el('picks').innerHTML.includes('Entry ready')); assert.match(h.el('candidates').innerHTML, /Previous snapshot/);
  h.setError(null); await h.click('retry'); assert.equal(h.calls.at(-1), '/intraday?refresh=1'); assert(h.el('load-error').hidden); assert.match(h.el('picks').innerHTML, /Entry ready/);
  h.setBook({status: 'error', error: 'Provider failed', picks: [], candidates: [], history: []}); await h.click('refresh');
  assert.match(h.el('candidates').innerHTML, /RESEARCH0/); assert(!h.el('picks').innerHTML.includes('Entry ready'));
  h.setBook(book()); await h.click('retry');
  const escaped = book(); escaped.picks[0].symbol = '<img src=x onerror=alert(1)>'; escaped.picks[0].why = '<script>unsafe</script>'; h.setBook(escaped); await h.click('refresh');
  assert.match(h.el('picks').innerHTML, /&lt;img/); assert(!h.el('picks').innerHTML.includes('<script>unsafe'));

  const run = book(); run.status = 'running'; run.progress = {completed: 44, total: 164};
  const p = await harness(run); assert(p.el('refresh').disabled); assert(p.timers().some(t => t.delay === 3000)); assert.match(p.el('scan-status').textContent, /44 of 164/);
  const callsBefore = p.calls.length; await p.click('refresh'); assert.equal(p.calls.length, callsBefore, 'No duplicate scan while running');
  p.setHold(true); await p.tick(3000); const beforeVisible = p.calls.length;
  p.document.hidden = true; await p.document.fire('visibilitychange'); assert.equal(p.timers().length, 0);
  p.document.hidden = false; await p.document.fire('visibilitychange'); assert.equal(p.calls.length, beforeVisible, 'No overlapping poll');
  p.document.hidden = true; await p.resolve(run); assert.equal(p.timers().length, 0, 'Hidden completion must not schedule');
  p.setBook(book()); p.document.hidden = false; await p.document.fire('visibilitychange'); await flush(); assert.match(p.el('picks').innerHTML, /Entry ready/);

  const exp = book(); exp.picks[0].expires_at = iso(1000); exp.picks[1].expires_at = null;
  const e = await harness(exp); assert.match(e.el('picks').innerHTML, /LONG/); assert(!e.el('picks').innerHTML.includes('SHORT'));
  await e.tick(1020); assert(!e.el('picks').innerHTML.includes('Entry ready')); assert.match(e.el('history').innerHTML, /LONG/); assert.match(e.el('history').innerHTML, /expired/);
  const closed = book(); closed.tradeable = false; closed.phase = 'closed'; const c = await harness(closed);
  assert.match(c.el('picks').innerHTML, /Waiting for a live session/); assert(!c.el('picks').innerHTML.includes('Entry ready'));
  const f = await harness(book(), true); assert.match(f.el('picks').innerHTML, /Session data unavailable/); assert(f.el('scan-spinner').hidden); assert(!f.el('retry').disabled);
  console.log('PASS: explicit BUY/SELL, evidence qualification, net/gross labels, timestamp and missing-value formatting, pagination and focus, escaping, refresh/HTTP-error retention and retry, server-error retention, no-overlap 3s polling, hidden-tab pause/resume, expiry demotion and missing-expiry rejection, closed/initial-failure states. All transport used offline fixtures.');
})().catch(error => {console.error(error); process.exitCode = 1;});
