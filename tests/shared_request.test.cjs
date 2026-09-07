/* Shared frontend behavior in a Node VM. No browser or network access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'shared_desk.js'), 'utf8');

function element() {
  const classes = new Set();
  return {
    children: [], attributes: {}, listeners: {}, hidden: false, textContent: '',
    classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x) },
    setAttribute(key, value) { this.attributes[key] = value; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(key, callback) { this.listeners[key] = callback; },
  };
}

function setup(fetcher, search = '', route = '/', nodes = {}, links = []) {
  const timers = new Map();
  const storage = new Map();
  let counter = 0;
  const context = vm.createContext({
    console, AbortController, URL, URLSearchParams, Date, Intl, WeakMap,
    location: { search, pathname: route, href: 'http://preview.invalid' + route + search },
    window: { __API_BASE__: 'https://baked.example.invalid', addEventListener() {} },
    localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
    document: {
      readyState: 'loading', addEventListener() {}, querySelectorAll: () => links, getElementById: id => nodes[id] || null,
      createElement: () => element(), createTextNode: textContent => ({ textContent }),
    },
    setTimeout: (callback, delay) => { const id = ++counter; timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: fetcher,
  });
  vm.runInContext(source, context, { filename: 'shared_desk.js' });
  return { context, timers, storage };
}

function response(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
}

function abortable(_url, options) {
  return new Promise((_resolve, reject) => {
    const fail = () => reject(Object.assign(new Error('Aborted'), { name: 'AbortError' }));
    if (options.signal.aborted) fail();
    else options.signal.addEventListener('abort', fail, { once: true });
  });
}

async function main() {
  for (const [route, active, target] of [['/', 'overview', '#board'], ['/index.html', 'overview', '#board'], ['/ipo-desk', 'ipo', '#main-content'], ['/intraday-desk.html', 'intraday', '#main-content'], ['/quality-desk', 'quality', '#main-content']]) {
    const skip = element();
    const links = ['overview', 'ipo', 'intraday', 'quality'].map(desk => ({ ...element(), dataset: { desk } }));
    setup(async () => response({}), '', route, { 'desk-skip': skip }, links);
    assert.equal(skip.href, target);
    assert.equal(links.find(link => link.dataset.desk === active).attributes['aria-current'], 'page');
    assert.equal(links.filter(link => link.attributes['aria-current'] === 'page').length, 1);
  }
  {
    let request;
    const env = setup(async (url, options) => { request = { url, options }; return response({ ok: true }); }, '?api=https%3A%2F%2Foverride.example.invalid%2F');
    const controller = new AbortController();
    const result = await env.context.deskRequest('/start', { method: 'POST', body: '{"mode":"demo"}', signal: controller.signal });
    assert.equal(result.ok, true);
    assert.equal(request.url, 'https://override.example.invalid/start');
    assert.equal(request.options.method, 'POST');
    assert.equal(request.options.body, '{"mode":"demo"}');
    assert.equal(request.options.cache, 'no-store');
    assert.notEqual(request.options.signal, controller.signal);
    assert.equal(env.storage.get('dalal.api'), 'https://override.example.invalid');
    assert.equal(env.timers.size, 0);
  }
  {
    const env = setup(async () => response({ error: 'Service is restarting' }, 503));
    await assert.rejects(env.context.deskRequest('/quality'), error => error.status === 503 && /restarting/.test(error.message));
    assert.equal(env.timers.size, 0);
  }
  {
    const env = setup(async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('Invalid JSON'); } }));
    await assert.rejects(env.context.deskRequest('/quality'), /unreadable response/i);
    assert.equal(env.timers.size, 0);
  }
  {
    const networkError = new TypeError('Failed to fetch');
    const env = setup(async () => { throw networkError; });
    await assert.rejects(env.context.deskRequest('/quality'), error => error === networkError);
    assert.equal(env.timers.size, 0);
  }
  {
    const env = setup(abortable);
    const pending = env.context.deskRequest('/status');
    assert.equal(env.timers.size, 1);
    const timer = [...env.timers.values()][0];
    assert(timer.delay > 0);
    timer.callback();
    await assert.rejects(pending, /took too long/i);
    assert.equal(env.timers.size, 0);
  }
  {
    const limits = {};
    for (const endpoint of ['/status', '/ipos', '/intraday']) {
      const env = setup(abortable);
      const pending = env.context.deskRequest(endpoint);
      const timer = [...env.timers.values()][0];
      limits[endpoint] = timer.delay;
      timer.callback();
      await assert.rejects(pending, /took too long/i);
      assert.equal(env.timers.size, 0);
    }
    assert(limits['/ipos'] > limits['/status'], 'Offer-document processing needs more time than status polling');
    assert(limits['/intraday'] > limits['/status'], 'A batch session scan needs more time than status polling');
  }
  {
    const env = setup(abortable);
    let listener, removed;
    const signal = { aborted: false, addEventListener: (_type, callback) => { listener = callback; }, removeEventListener: (_type, callback) => { removed = callback; } };
    const pending = env.context.deskRequest('/status', { signal });
    listener();
    await assert.rejects(pending, error => error.name === 'AbortError');
    assert.equal(removed, listener);
    assert.equal(env.timers.size, 0);
  }
  {
    const env = setup(abortable);
    const controller = new AbortController();
    controller.abort();
    await assert.rejects(env.context.deskRequest('/status', { signal: controller.signal }), error => error.name === 'AbortError');
    assert.equal(env.timers.size, 0);
  }
  {
    // Deliberately ignore abort in the fetch mock to reproduce a response that
    // arrives after cancellation; only the latest result may touch the page.
    const requests = [];
    const env = setup((_url, options) => new Promise(resolve => requests.push({ options, resolve })));
    const region = element(), rendered = [];
    const first = env.context.deskLoad('/quality', data => rendered.push(data.id), region);
    const second = env.context.deskLoad('/quality', data => rendered.push(data.id), region);
    assert.equal(requests[0].options.signal.aborted, true);
    requests[1].resolve(response({ id: 'latest' }));
    await second;
    requests[0].resolve(response({ id: 'stale' }));
    await first;
    assert.deepEqual(rendered, ['latest']);
    assert.equal(region.hidden, true);
    assert.equal(region.attributes['aria-busy'], 'false');
    assert.equal(env.timers.size, 0);
  }
  {
    const untrusted = '<img src=x onerror=alert(1)>';
    let attempts = 0;
    const env = setup(async () => { attempts++; return response({ error: untrusted }, 503); });
    const region = element();
    let rendered = 0;
    await env.context.deskLoad('/quality', () => rendered++, region);
    assert.equal(rendered, 0, 'A failed refresh must not render a replacement dataset');
    assert.equal(region.classList.contains('err'), true);
    assert.equal(region.attributes['aria-busy'], 'false');
    assert.equal(region.children.length, 2);
    assert(region.children[0].textContent.includes(untrusted), 'Server errors are assigned as text, not HTML');
    assert.equal(region.children[1].textContent, 'Retry loading');
    assert.equal(typeof region.children[1].listeners.click, 'function');
    region.children[1].listeners.click();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    assert.equal(attempts, 2);
    assert.equal(env.timers.size, 0);
  }
  console.log('Shared request success, HTTP/JSON/network failures, timeout, cancellation, race suppression, text-safe errors, and retry passed.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
