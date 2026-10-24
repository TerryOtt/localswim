/* Execute shipped PAGE in VM/DOM without a browser package. Default cases are offline;
 * the optional restore fixture forwards bounded requests to real loopback HTTP services. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const page = fs.readFileSync(process.argv[2], 'utf8');
const source = page.includes('<script>') ? page.split('<script>')[1].split('</script>')[0] : page;
const start = source.indexOf('// BEGIN PRIVATE BROWSER INTENT');
const end = source.indexOf('// END PRIVATE BROWSER INTENT');
assert(start >= 0 && end > start, 'Embedded intent boundary is required');
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context);
const factory = vm.runInContext('createBrowserIntent', context);
const g1 = '11111111-1111-4111-8111-111111111111';
const g2 = '22222222-2222-4222-8222-222222222222';
const snapshot = (generation, revision) => ({writeProtocol: 2, generation, revision,
  writeToken: '"generation-' + generation + '-revision-' + revision + '"'});
function windowState(prefix = '/direct', epoch = 0) {
  let current = {prefix, epoch};
  let invalidations = 0;
  const state = factory(() => current, () => { invalidations++; });
  state.observe(snapshot(g1, 1));
  return {state, navigate: (prefix, epoch) => {current = {prefix, epoch};},
    invalidations: () => invalidations};
}
let scenarioCount = 0;

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {};
    this.dataset = {}; this.style = {setProperty() {}}; this.value = ''; this.disabled = false;
    this.className = ''; this.hidden = false; this.scrollTop = 0; this.textContent = '';
    const classes = new Set();
    this.classList = {add: (...names) => names.forEach(n => classes.add(n)),
      remove: (...names) => names.forEach(n => classes.delete(n)), contains: n => classes.has(n),
      toggle(n, enabled) {if (enabled) classes.add(n); else classes.delete(n);}};
  }
  addEventListener(type, fn) {(this.listeners[type] ||= []).push(fn);}
  async fire(type, extra = {}) {
    const event = {target: this, preventDefault() {}, stopPropagation() {}, ...extra};
    for (const fn of this.listeners[type] || []) await fn(event);
  }
  append(...nodes) {this.children.push(...nodes);}
  get options() {return this.children;}
  appendChild(node) {this.append(node); return node;}
  replaceChildren(...nodes) {this.children = [...nodes];}
  querySelectorAll() {return [];}
  querySelector(selector) {
    if (selector === 'button:disabled') return null;
    return this.children.find(n => n.className === selector.slice(1)) || new Element();
  }
  setAttribute(name, value) {this[name] = value;}
  getAttribute(name) {return this[name];}
  focus() {} select() {} blur() {} contains() {return false;} closest() {return null;}
  click() {}
  remove() {} getBoundingClientRect() {return {left: 0, top: 0, height: 0};}
  showModal() {this.open = true;} close() {this.open = false;}
}

function browser(mode = '/direct', suppliedBoard = null) {
  const elements = new Map();
  const document = {title: 'Synthetic board', activeElement: null, body: new Element(),
    documentElement: new Element(), head: new Element(),
    getElementById(id) {
      if (!elements.has(id)) {
        const tag = page.match(new RegExp('<([a-z]+)[^>]*\\bid="' + id + '"'));
        elements.set(id, new Element(tag ? tag[1] : 'div'));
      }
      return elements.get(id);
    },
    createElement: tag => new Element(tag), createTextNode: text => ({textContent: text}),
    querySelectorAll(selector) {
      return selector.includes('#panel button') ? [...elements.values()].filter(n => n.tagName === 'BUTTON') : [];
    },
    querySelector() {return null;}, addEventListener() {},
    fonts: {ready: {then() {}}, addEventListener() {}},
  };
  let requests = [], responder = null;
  const window = {workspaceEpoch: 0, localswimWorkspace: {}, workspaceReady: false,
    addEventListener() {}, location: {href: 'http://localhost/board/one'}, confirm: () => true,
    history: {replaceState() {}}};
  const ctx = vm.createContext({window, document, console, URL, Map, Set, Date, JSON, crypto,
    HTMLSelectElement: Element, HTMLInputElement: Element, HTMLTextAreaElement: Element,
    CSS: {escape: value => value}, AbortSignal: {timeout() {}},
    sessionStorage: {getItem() {return null;}, removeItem() {}, setItem() {}},
    localStorage: {getItem() {return null;}, setItem() {}},
    setInterval() {}, setTimeout() {}, clearTimeout() {}, requestAnimationFrame() {},
    fetch: async (path, options) => {
      requests.push({path, options});
      if (responder) return responder(path, options);
      return {ok: true, status: 200, json: async () => ({...snapshot(g1, 2), result: 'saved'})};
    },
  });
  vm.runInContext(source, ctx);
  const run = code => vm.runInContext(code, ctx);
  const board = suppliedBoard || {...snapshot(g1, 1), browserUser: 'terry', attachmentMaxBytes: 100,
    actors: [{id: 'terry', class: 'human'}, {id: 'bot', class: 'bot'}], priorities: [{id: 'p3'}],
    lanes: [{state: 'ready_for_review', label: 'Review', creatable: true, items: [
      {id: 'a', ticket: '#0001', subject: 'Original', state: 'ready_for_review', priority: 'p3',
        owner: 'terry', comments: [], history: [], links: [], attachments: [], detailRaw: 'Original detail'},
      {id: 'b', ticket: '#0002', subject: 'Other', state: 'ready_for_review', priority: 'p3',
        owner: 'bot', comments: [], history: [], links: [], attachments: []}]},
      {state: 'completed', label: 'Completed', items: []}],
    edges: [['ready_for_review', 'completed']], counts: {}};
  ctx.initial = board;
  run(`API_PREFIX = ${JSON.stringify(mode)}; data = initial; browserIntent.observe(data); openCard('a');`);
  const poll = run('tick');
  run('tick = async () => {};'); // Drop cleanup is tested without starting a polling loop.
  return {ctx, run, document, window, board, element: id => document.getElementById(id),
    poll, requests: () => requests, reset: () => {requests = [];},
    respond: fn => {responder = fn;},
    restore(generation = g2) {ctx.next = {...board, ...snapshot(generation, 0)};
      run('browserIntent.observe(next); data = next;');},
  };
}

const controls = {
  subject: b => {b.run('startSubjectEdit()'); b.element('p-subject-edit').value = 'Draft subject'; return b.run('saveSubject()');},
  detail: b => {b.run('startDetailEdit()'); b.element('p-detail-text').value = 'Draft detail'; return b.run('saveDetail()');},
  comment: b => {b.element('say').value = 'Draft comment'; return b.run('postComment()');},
  create: b => {b.run('openMake(data.lanes[0])'); b.element('mk-subject').value = 'Draft creation'; return b.run('submitMake()');},
  assignment: b => b.element('p-owner').fire('click'),
  priority: b => {b.element('p-pri').value = 'p3'; return b.element('p-pri').fire('change');},
  approval: b => b.run('approveReview()'),
  relationship: b => b.run("editableRelationshipRow(itemById('a'), {kind: 'blocks', id: 'b', ticket: '#0002'})").children[2].fire('click'),
  cover: b => attachmentButton(b, 0).fire('click'),
  attachment: b => attachmentButton(b, 1).fire('click'),
  upload: async b => {
    b.run("fileBase64 = async () => 'YWJj';");
    await b.element('p-upload').fire('click');
    await b.element('p-upload-file').fire('change', {target: {files: [{name: 'draft.txt', size: 3}], value: 'draft.txt'}});
  },
  drag: async b => {
    b.run('dragNode = card(data.lanes[0].items[0]); dropNode = laneEl(data.lanes[1]);');
    const transfer = {value: '', setData(_, value) {this.value = value;}, getData() {return this.value;}};
    await b.run('dragNode').fire('dragstart', {dataTransfer: transfer});
    await b.run('dropNode').fire('drop', {dataTransfer: transfer});
  },
};

function attachmentButton(b, index) {
  b.run("itemById('a').attachments = [{id: 'image', name: 'example.png', mediaType: 'image/png', size: 3, url: '/image'}]; paintAttachments(itemById('a'));");
  const row = b.element('p-attachments').children[0];
  return row.children.find(node => node.className === 'attachment-actions').children[index];
}

// These envelopes are recorded by a real offline restore and restarted HTTP services.
// Scheduling old responses remains deterministic VM/DOM evidence, not a native browser.
async function restoredClients(workspace) {
  if (!process.argv[4]) return null;
  const fixture = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
  const before = fixture.before.board, after = fixture.after.board;
  assert.equal(fixture.evidence.restoreOutcome, 'committed');
  assert.equal(before.revision, after.revision, 'Actual restore must reuse the numeric revision');
  assert.notEqual(before.generation, after.generation);
  assert.notEqual(before.writeToken, after.writeToken);
  const refresh = workspace.slice(workspace.indexOf('  async function refresh('),
    workspace.indexOf('  function reconcile('));
  const cases = [];
  for (const mode of ['/direct', '/api/v001/workspace/boards/one']) {
    const base = mode === '/direct' ? fixture.directBase : fixture.gatewayBase;
    const prefix = mode === '/direct' ? fixture.directPrefix : fixture.gatewayPrefix;
    const session = mode === '/direct' ? fixture.directSession : fixture.gatewaySession;
    const boardSession = mode === '/direct' ? session : fixture.gatewayBoardSession;
    const initialSession = mode === '/direct' ? fixture.oldDirectSession : fixture.oldGatewaySession;
    assert.notEqual(initialSession.token, session.token, 'Restart rotates actual credentials');
    const fresh = () => {
      const b = browser(mode, structuredClone(before));
      b.ctx.restored = structuredClone(after);
      b.ctx.actualSession = session;
      b.run('API_TOKEN = actualSession.token;');
      b.window.workspaceReady = true;
      return b;
    };
    const serveRestored = b => b.respond(async path => ({ok: true, status: 200,
      json: async () => path.endsWith('/status') ? fixture.after.status
        : path.endsWith('/browser-session') ? boardSession : structuredClone(after)}));
    const pollRestored = async b => {serveRestored(b); await b.poll();};
    const realResponses = [];
    const serveLive = b => b.respond(async (path, options) => {
      const response = await fetch(base + prefix + path.slice(mode.length), options);
      if (options && options.method === 'POST')
        realResponses.push({status: response.status, body: await response.clone().json()});
      return response;
    });
    const review = async (b, live = false) => {
      if (live) serveLive(b); else serveRestored(b);
      await b.element('intent-review-go').fire('click');
      assert.equal(b.run('browserIntent.blocked()'), false);
      b.run("openCard('a');");
    };
    const serverStale = fresh();
    serverStale.element('say').value = 'Actual stale comment must remain local';
    serveLive(serverStale);
    await serverStale.run('postComment()');
    assert.equal(serverStale.requests().length, 1);
    assert.equal(serverStale.requests()[0].options.headers['If-Match'], before.writeToken);
    assert.equal(serverStale.requests()[0].options.headers.Authorization, 'Bearer ' + session.token);
    assert.equal(realResponses[0].status, 412);
    assert.equal(realResponses[0].body.error, 'state_changed');
    assert.equal(serverStale.run('browserIntent.blocked()'), true);
    assert.equal(serverStale.element('say').value, 'Actual stale comment must remain local');
    assert(serverStale.run('preservedIntentDrafts.some(d => d.comment === "Actual stale comment must remain local")'));
    realResponses.length = 0;
    cases.push({mode, claim: 'actual stale POST with fresh restart credential refuses once with 412 and preserves draft'});
    scenarioCount++;
    const b = fresh();
    b.run('startDetailEdit(); origin = drawerIntent; originEpoch = window.workspaceEpoch;');
    b.element('say').value = 'Restore acceptance comment draft';
    b.element('p-detail-text').value = 'Restore acceptance detail draft';
    b.run('dragNode = card(itemById("a")); dropNode = laneEl(data.lanes.find(l => l.state === "completed"));');
    const transfer = {value: '', setData(_, value) {this.value = value;}, getData() {return this.value;}};
    await b.run('dragNode').fire('dragstart', {dataTransfer: transfer});
    if (mode !== '/direct') {
      b.ctx.actualCatalog = fixture.gatewayCatalog;
      b.ctx.actualBoardKey = decodeURIComponent(fixture.gatewayPrefix.split('/').pop());
      b.run(`let retryRequested = false, refreshing = false, initialized = true,
        storageKey = 'localswim.workspace.' + actualCatalog.identity, catalog = [],
        gatewayStatus = null, gatewayPush = null, current = actualBoardKey, recoverOnCatalog = false;
        const nav = document.createElement('nav');
        function normalizePageStatus(value) {return value;}
        function acceptPreferences() {} function renderRows() {} function showWorkspaceStatus() {}
        function flushPinCommands() {} function persist() {} function error() {}
      `);
      b.run(refresh);
      b.respond(async path => ({ok: true, status: 200, json: async () =>
        path.endsWith('/browser-session') ? session : fixture.gatewayCatalog}));
      await b.run('refresh()');
      assert.equal(b.run('API_TOKEN'), session.token);
    }
    assert.equal(b.run('origin.generation'), before.generation);
    assert.equal(b.run('browserIntent.token(origin)'), before.writeToken);
    assert.equal(b.run('window.workspaceEpoch'), b.run('originEpoch'));
    await pollRestored(b);
    assert.equal(b.run('data.generation'), after.generation);
    assert.equal(b.run('data.revision'), after.revision);
    assert.equal(b.run('browserIntent.valid(origin)'), false);
    assert.equal(b.run('origin.generation'), before.generation);
    assert(b.run('preservedIntentDrafts.some(d => d.comment === "Restore acceptance comment draft" && d.detail === "Restore acceptance detail draft")'));
    assert.equal(b.element('post').disabled, true);
    assert.equal(b.element('p-detail-save').disabled, true);
    b.reset();
    await b.run('postComment()'); await b.run('saveDetail()');
    await b.run('dropNode').fire('drop', {dataTransfer: transfer});
    assert.equal(b.requests().length, 0, 'Stale comment/detail/drop never dispatch');
    await review(b, true);
    assert.equal(b.run('browserIntent.valid(origin)'), false, 'Review cannot repair old capture');
    await b.element('intent-reattach').fire('click');
    assert.equal(b.element('say').value, 'Restore acceptance comment draft');
    assert.equal(b.element('p-detail-text').value, 'Restore acceptance detail draft');
    assert.equal(b.element('post').disabled, false);
    assert.equal(b.element('p-detail-save').disabled, false);
    const reviewedToken = b.run('data.writeToken'), reviewedRevision = b.run('data.revision');
    b.reset();
    serveLive(b);
    await b.run('postComment()'); await b.run('saveDetail()');
    assert.equal(b.requests().length, 2, 'Explicit reviewed edits each dispatch once');
    assert.equal(b.requests()[0].options.headers['If-Match'], reviewedToken);
    assert.equal(b.requests()[0].options.headers.Authorization, 'Bearer ' + session.token);
    assert.equal(b.requests()[1].options.headers['If-Match'],
      snapshot(after.generation, reviewedRevision + 1).writeToken);
    assert.deepEqual(realResponses.map(r => r.status), [200, 200]);
    assert(realResponses.every(r => r.body.generation === after.generation));
    const persisted = await (await fetch(base + prefix + '/board')).json();
    const savedCard = persisted.lanes.flatMap(lane => lane.items).find(card => card.id === 'a');
    assert.equal(savedCard.detailRaw, 'Restore acceptance detail draft');
    assert(savedCard.comments.some(comment => comment.text === 'Restore acceptance comment draft'));
    assert(!savedCard.comments.some(comment => comment.text === 'Actual stale comment must remain local'));
    cases.push({mode, claim: 'restart credential rotation, poll, preserved comment/detail, disabled stale submit, queued drag, explicit review'});
    scenarioCount++;

    for (const navigation of [false, true]) {
      const late = fresh();
      let finish;
      late.respond(async () => ({ok: true, status: 200,
        json: () => new Promise(resolve => {finish = resolve;})}));
      late.element('say').value = 'Old in-flight draft';
      const pending = late.run('postComment()');
      for (let i = 0; i < 20 && !finish; i++) await Promise.resolve();
      assert(finish);
      await pollRestored(late);
      if (navigation) {
        late.run("API_PREFIX = '/workspace/two'; window.workspaceEpoch++;");
        await pollRestored(late);
      }
      await review(late);
      late.run("closeCard(); openCard('b'); startDetailEdit();");
      late.element('say').value = 'Current comment';
      late.element('p-detail-text').value = 'Current detail';
      finish({...before, result: 'previous success'});
      await pending;
      assert.equal(late.run('openId'), 'b');
      assert.equal(late.element('say').value, 'Current comment');
      assert.equal(late.element('p-detail-text').value, 'Current detail');
      assert.equal(late.run('data.generation'), after.generation);
      assert.equal(late.run('data.revision'), after.revision);
      assert.equal(late.requests().filter(r => r.options && r.options.method === 'POST').length, 1);
      cases.push({mode, claim: navigation ? 'stale navigation success cannot publish/clear/close' : 'in-flight old success cannot publish/clear/close'});
      scenarioCount++;
    }
    for (const failure of ['state_changed', 'credential', 'uncertain']) {
      const refused = fresh();
      await pollRestored(refused); await review(refused);
      refused.element('say').value = 'Keep refused draft';
      refused.reset();
      refused.respond(async () => {
        if (failure === 'uncertain') throw new Error('Synthetic uncertain completion');
        return {ok: false, status: failure === 'credential' ? 401 : 412,
          json: async () => ({error: failure === 'credential' ? 'Unauthorized' : 'state_changed'})};
      });
      if (failure === 'uncertain')
        await assert.rejects(refused.run('postComment()'), /Synthetic uncertain completion/);
      else await refused.run('postComment()');
      assert.equal(refused.requests().length, 1, 'Refusal/uncertainty must not replay POST');
      assert.equal(refused.element('say').value, 'Keep refused draft');
      cases.push({mode, claim: 'no POST replay after ' + failure});
      scenarioCount++;
    }
  }
  return {provenance: fixture.evidence, cases, nativeBrowser: false,
    responseScheduling: 'deterministic VM/DOM', freshMutationResponses: 'actual restarted HTTP service/gateway'};
}

(async () => {
  for (const mode of ['/direct', '/workspace/one']) {
    const w = windowState(mode);
    for (const control of ['subject', 'detail', 'comment', 'create', 'upload', 'drag',
      'relationship', 'assignment', 'priority', 'approval', 'attachment', 'cover']) {
      const intent = w.state.capture(control);
      assert(w.state.valid(intent));
      w.state.observe(snapshot(g1, 2));
      assert.equal(w.state.token(intent), snapshot(g1, 2).writeToken);
      scenarioCount++;
    }
    const old = w.state.capture('drawer');
    w.state.observe(snapshot(g2, 0));
    assert(!w.state.valid(old));
    assert(w.state.blocked());
    assert.equal(w.invalidations(), 1);
    assert.equal(w.state.capture('create'), null);
    w.state.review();
    assert(w.state.valid(w.state.capture('new drawer')));
    assert(!w.state.valid(old), 'Explicit review never repairs an old intent');
  }
  const w = windowState('/workspace/one');
  const intent = w.state.capture('upload');
  const other = windowState('/workspace/one');
  assert(!other.state.valid(intent), 'Windows cannot exchange intent');
  const saved = w.state.save();
  w.navigate('/workspace/two', 1);
  w.state.observe(snapshot(g1, 1));
  assert(!w.state.valid(intent));
  w.state.restore(saved);
  assert(w.state.blocked(), 'Restored forms require explicit review after navigation');
  w.navigate('/workspace/one', 2);
  w.state.observe(snapshot(g2, 3));
  assert(!w.state.valid(intent));
  // Suspended response captures immutable origin before its first await.
  let release;
  const wait = new Promise(resolve => {release = resolve;});
  w.state.review();
  const request = w.state.capture('comment');
  const pending = (async () => {await wait; return w.state.valid(request);})();
  w.state.observe(snapshot(g1, 0));
  release();
  assert.equal(await pending, false);
  assert.throws(() => w.state.observe({...snapshot(g1, 0), writeToken: '"revision-0"'}));
  scenarioCount += 8;
  for (const mode of ['/direct', '/api/v001/workspace/boards/one']) {
    for (const [name, invoke] of Object.entries(controls)) {
      const b = browser(mode);
      await invoke(b);
      assert.equal(b.requests().length, 1, name + ' fresh dispatch');
      assert.equal(b.requests()[0].options.headers['If-Match'], snapshot(g1, 1).writeToken, name);
      scenarioCount++;
      // Every actual caller must refuse stale drawer/form/drag state.
      const stale = browser(mode);
      stale.run('openMake(data.lanes[0])');
      stale.element('say').value = 'Keep comment';
      stale.element('mk-subject').value = 'Keep creation';
      stale.restore();
      await invoke(stale);
      assert.equal(stale.requests().length, 0, name + ' stale dispatch');
      assert(stale.run('preservedIntentDrafts.some(d => d.comment === "Keep comment" && d.makeSubject === "Keep creation")'));
      scenarioCount++;
    }
    for (const name of Object.keys(controls)) {
      for (const outcome of ['success', 'failure']) {
      const b = browser(mode);
      let release, reject;
      b.respond(async () => ({ok: true, status: 200,
        json: () => new Promise((resolve, fail) => {release = resolve; reject = fail;})}));
      const pending = controls[name](b);
      for (let i = 0; i < 20 && !release; i++) await Promise.resolve();
      assert(release, name + ' suspended response');
      b.restore(); b.run("closeCard(); closeMake(true); browserIntent.review(); openCard('b'); openMake(data.lanes[0]);");
      b.element('say').value = 'New drawer draft';
      b.element('mk-subject').value = 'New creation draft';
      if (outcome === 'success') release({...snapshot(g1, 9), result: 'old result'});
      else reject(new Error('old response failed'));
      await pending;
      assert.equal(b.run('openId'), 'b');
      assert.equal(b.element('say').value, 'New drawer draft');
      assert.equal(b.element('mk-subject').value, 'New creation draft');
      assert.equal(b.run('mkOpen'), 'ready_for_review');
      assert.equal(b.run('data.generation'), g2);
      assert.equal(b.run('data.revision'), 0);
      scenarioCount++;
      }
    }
    const b = browser(mode);
    b.element('say').value = 'Saved comment';
    b.restore();
    b.respond(async path => ({ok: true, status: 200, json: async () => path.endsWith('/browser-session')
      ? {writeProtocol: 2, generation: g2, token: 'fresh credential'}
      : {...b.board, ...snapshot(g2, 0)}}));
    await b.element('intent-review-go').fire('click');
    assert.equal(b.run('openId'), null);
    b.run("openCard('b')");
    await b.element('intent-reattach').fire('click');
    assert.equal(b.element('say').value, '');
    b.run("openCard('a')");
    await b.element('intent-reattach').fire('click');
    assert.equal(b.element('say').value, 'Saved comment');
    b.run("closeCard(); itemById('a').ticket = '#9999'; openCard('a');");
    assert.equal(b.element('say').value, '');
    await b.element('intent-reattach').fire('click');
    assert.equal(b.element('say').value, '');
    b.run("data.lanes[0].items = data.lanes[0].items.filter(it => it.id !== 'a'); closeCard(); openCard('a');");
    await b.element('intent-reattach').fire('click');
    assert.equal(b.run('openId'), null);
    scenarioCount += 4;
    // The file-picker and file-read origins both precede asynchronous work.
    for (const phase of ['picker', 'read']) {
      const u = browser(mode);
      let release;
      u.ctx.fileRead = new Promise(resolve => {release = resolve;});
      u.run('fileBase64 = () => fileRead;');
      await u.element('p-upload').fire('click');
      if (phase === 'picker') u.restore();
      const pending = u.element('p-upload-file').fire('change',
        {target: {files: [{name: 'keep.txt', size: 3}], value: 'keep.txt'}});
      if (phase === 'read') u.restore();
      u.run("closeCard(); browserIntent.review(); openCard('b');");
      release('YWJj'); await pending;
      assert.equal(u.requests().length, 0);
      assert(u.run("preservedIntentDrafts.some(d => d.upload && d.upload.name === 'keep.txt')"));
      scenarioCount++;
    }
    const d = browser(mode);
    d.run('dragNode = card(itemById("a")); dropNode = laneEl(data.lanes[1]);');
    const transfer = {value: '', setData(_, value) {this.value = value;}, getData() {return this.value;}};
    await d.run('dragNode').fire('dragstart', {dataTransfer: transfer});
    d.restore();
    await d.run('dropNode').fire('drop', {dataTransfer: transfer});
    assert.equal(d.requests().length, 0);
    scenarioCount++;
    // A new same-card drag cannot lend its authority to an old queued drop.
    d.run('browserIntent.review(); dragNode = card(itemById("a"));');
    const newerTransfer = {value: '', setData(_, value) {this.value = value;}, getData() {return this.value;}};
    await d.run('dragNode').fire('dragstart', {dataTransfer: newerTransfer});
    await d.run('dropNode').fire('drop', {dataTransfer: transfer});
    assert.equal(d.requests().length, 0);
    const otherWindow = browser(mode);
    otherWindow.run('dragNode = card(itemById("a")); dropNode = laneEl(data.lanes[1]);');
    await otherWindow.run('dragNode').fire('dragstart', {dataTransfer: newerTransfer});
    await otherWindow.run('dropNode').fire('drop', {dataTransfer: transfer});
    assert.equal(otherWindow.requests().length, 0);
    scenarioCount += 2;
    // Credential refresh is authentication only; the original drawer stays stale.
    const auth = browser(mode);
    auth.restore(); auth.run("API_TOKEN = 'new credential';");
    await controls.comment(auth);
    assert.equal(auth.requests().length, 0);
    scenarioCount++;
    // A late network rejection from another navigation cannot touch the new drawer.
    const failed = browser(mode);
    let reject;
    failed.respond(() => new Promise((_, fail) => {reject = fail;}));
    const old = controls.comment(failed);
    failed.run("API_PREFIX = '/workspace/two'; window.workspaceEpoch++; closeCard();");
    failed.restore(); failed.run("browserIntent.review(); openCard('b');");
    failed.element('say').value = 'Current board';
    reject(new Error('suspended network error')); await old;
    assert.equal(failed.element('say').value, 'Current board');
    assert.equal(failed.run('openId'), 'b');
    scenarioCount++;
    // The review fetch itself has the same asynchronous origin boundary as writes.
    for (const change of ['navigation', 'drawer', 'newer review']) {
      const review = browser(mode);
      review.restore();
      const rejects = [];
      review.respond(() => new Promise((_, fail) => {rejects.push(fail);}));
      review.run('let toastRecords = []; toast = (...args) => toastRecords.push(args);');
      const pending = review.element('intent-review-go').fire('click');
      if (change === 'navigation') review.run("API_PREFIX = '/workspace/two'; window.workspaceEpoch++;");
      let newer;
      if (change !== 'newer review') review.run("closeCard(); openCard('b');");
      else newer = review.element('intent-review-go').fire('click');
      review.element('say').value = 'Current draft';
      rejects[0](new Error('Previous review failed'));
      await pending;
      assert.equal(review.run('toastRecords.length'), 0);
      assert.equal(review.element('say').value, 'Current draft');
      if (newer) {
        review.window.workspaceEpoch++;
        rejects[1](new Error('Superseded newer review failed'));
        await newer;
        assert.equal(review.run('toastRecords.length'), 0);
      }
      scenarioCount++;
    }
    const review = browser(mode);
    review.restore();
    let release;
    review.respond(async path => ({ok: true, status: 200, json: () => path.endsWith('/browser-session')
      ? new Promise(resolve => {release = resolve;}) : {...review.board, ...snapshot(g2, 0)}}));
    const pendingReview = review.element('intent-review-go').fire('click');
    for (let i = 0; i < 20 && !release; i++) await Promise.resolve();
    assert(release);
    review.window.workspaceReady = true;
    review.respond(async path => ({ok: true, status: 200, json: async () => path.endsWith('/status')
      ? {ok: true, mtime: 100, generation: g2} : {...review.board, ...snapshot(g2, 5)}}));
    await review.poll();
    release({writeProtocol: 2, generation: g2, token: 'review credential'});
    await pendingReview;
    assert.equal(review.run('data.revision'), 5);
    assert.equal(review.run('API_TOKEN'), '%TOKEN%');
    assert(review.run('browserIntent.blocked()'));
    assert.equal(review.run('openId'), 'a');
    review.run('browserIntent.review();');
    assert.equal(review.run('browserIntent.token(browserIntent.capture("probe"))'), snapshot(g2, 5).writeToken);
    scenarioCount++;
    const supersededReview = browser(mode);
    supersededReview.restore();
    let finishSession;
    supersededReview.respond(async path => ({ok: true, status: 200,
      json: () => path.endsWith('/browser-session')
        ? new Promise(resolve => {finishSession = resolve;})
        : {...supersededReview.board, ...snapshot(g2, 0)}}));
    const superseded = supersededReview.element('intent-review-go').fire('click');
    for (let i = 0; i < 20 && !finishSession; i++) await Promise.resolve();
    assert(finishSession);
    supersededReview.run("closeCard(); openCard('b'); openMake(data.lanes[0]);");
    supersededReview.element('mk-subject').value = 'New creation while reviewing';
    supersededReview.element('say').value = 'New drawer while reviewing';
    finishSession({writeProtocol: 2, generation: g2, token: 'superseded credential'});
    await superseded;
    assert.equal(supersededReview.run('openId'), 'b');
    assert.equal(supersededReview.run('mkOpen'), 'ready_for_review');
    assert.equal(supersededReview.element('mk-subject').value, 'New creation while reviewing');
    assert.equal(supersededReview.element('say').value, 'New drawer while reviewing');
    assert.equal(supersededReview.run('API_TOKEN'), '%TOKEN%');
    scenarioCount++;
    // A snapshot poll is bound before status/board awaits, including failure paths.
    for (const outcome of ['success', 'failure']) {
      const polling = browser(mode);
      let release, reject;
      polling.window.workspaceReady = true;
      polling.respond(async path => ({ok: true, status: 200,
        json: () => path.endsWith('/status') ? {ok: true, mtime: 10, generation: g1}
          : new Promise((resolve, fail) => {release = resolve; reject = fail;})}));
      const pending = polling.poll();
      for (let i = 0; i < 12 && !release; i++) await Promise.resolve();
      assert(release, 'Board snapshot was suspended');
      polling.restore();
      if (outcome === 'success') release({...polling.board, ...snapshot(g1, 5)});
      else reject(new Error('old snapshot failed'));
      await pending;
      assert.equal(polling.run('data.generation'), g2);
      assert.equal(polling.run('data.revision'), 0);
      scenarioCount++;
    }
    const poll = browser(mode);
    poll.window.workspaceReady = true;
    poll.respond(async path => ({ok: true, status: 200, json: async () => path.endsWith('/status')
      ? {ok: true, mtime: 11, generation: g1} : {...poll.board, ...snapshot(g1, 7)}}));
    poll.element('say').value = 'Draft across poll';
    await poll.poll();
    assert.equal(poll.run('data.revision'), 7);
    assert.equal(poll.element('say').value, 'Draft across poll');
    poll.reset(); poll.respond(null); poll.run("API_TOKEN = 'rotated credential';");
    await controls.comment(poll);
    assert.equal(poll.requests()[0].options.headers['If-Match'], snapshot(g1, 7).writeToken);
    assert.equal(poll.requests()[0].options.headers.Authorization, 'Bearer rotated credential');
    scenarioCount++;
  }
  // Execute the changed workspace capture/loaded functions from the shipped source.
  assert(process.argv[3], 'workspace.js is required for executable session scenarios');
  const workspace = fs.readFileSync(process.argv[3], 'utf8');
  const capture = workspace.slice(workspace.indexOf('  function capture()'), workspace.indexOf('  function persist()'));
  const loaded = workspace.slice(workspace.indexOf('  function loaded()'), workspace.indexOf('  function error('));
  const ws = browser('/workspace/one');
  ws.run("let current = 'one', pending = null, state = {records: {}}; const board = document.getElementById('board'); function restoreScroll() {}");
  ws.run(capture + loaded);
  ws.element('say').value = 'Board one draft';
  ws.run('capture();');
  assert.equal(ws.run('state.records.one.browserIntent.generation'), g1);
  ws.run("closeCard(); current = 'two'; API_PREFIX = '/workspace/two'; window.workspaceEpoch++; browserIntent.observe(initial); browserIntent.review(); drafts = {}; openCard('b');");
  ws.element('say').value = 'Board two draft';
  ws.run('capture();');
  assert.equal(ws.run('state.records.two.drafts.b'), 'Board two draft');
  ws.run("closeCard(); current = 'one'; API_PREFIX = '/workspace/one'; window.workspaceEpoch++; browserIntent.observe(initial); browserIntent.review(); pending = state.records.one; drafts = {...pending.drafts}; loaded();");
  assert(ws.run('browserIntent.blocked()'));
  assert.equal(ws.element('say').value, 'Board one draft');
  await controls.comment(ws);
  assert.equal(ws.requests().length, 0);
  assert(ws.run('preservedIntentDrafts.some(d => d.comment === "Board one draft")'));
  scenarioCount += 4;
  const independent = browser('/workspace/one');
  assert(!independent.run('browserIntent.blocked()'));
  await controls.comment(independent);
  assert.equal(independent.requests().length, 1);
  assert(ws.run('browserIntent.blocked()'));
  scenarioCount++;
  const reconcile = workspace.slice(workspace.indexOf('  function reconcile('),
    workspace.indexOf('  window.WorkspaceUI ='));
  ws.run(reconcile);
  for (const savedGeneration of [g1, g2, null]) {
    ws.run(`current='two'; state.records.one={drafts:{a:'confirmed text'},
      browserIntent:${savedGeneration ? `{generation:'${savedGeneration}'}` : 'null'}};
      reconcile('/workspace/one','/workspace/one/cards/a/comment',
        {text:'confirmed text'},'${g1}');`);
    assert.equal(ws.run("state.records.one.drafts.a"),
      savedGeneration === g1 ? undefined : 'confirmed text');
    scenarioCount++;
  }
  const restore = await restoredClients(workspace);
  console.log(JSON.stringify({passed: true, scenarioCount, engine: 'Node VM deterministic DOM', restore,
    modes: ['direct', 'workspace'], mutationFamilies: Object.keys(controls)}));
})().catch(error => {console.error(error); process.exitCode = 1;});
