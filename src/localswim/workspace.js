/* Workspace navigation shares the board page's existing controls and writer boundary. */
(() => {
  const categories = [['blocked', 'Blocked'], ['needsHuman', 'Needs Terry'], ['review', 'Ready for Review']];
  let catalog = [], current = null, storageKey = '', state = {records: {}, pins: [], collapsed: false};
  let pending = null, initialized = false, refreshing = false;
  window.workspaceEpoch = 0;
  const link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = '/workspace.css'; document.head.append(link);
  const content = document.createElement('div'); content.id = 'workspace-content';
  const board = document.getElementById('board'); board.before(content);
  const nav = document.createElement('nav'); nav.id = 'workspace-nav'; nav.setAttribute('aria-label', 'Boards');
  nav.innerHTML = '<div class="ws-heading"><strong>Boards</strong><button type="button" id="ws-toggle" aria-label="Collapse boards">☰</button></div>' +
    '<div class="ws-expanded"><input id="ws-find" type="search" placeholder="Find a board" aria-label="Find a board">' +
    '<div class="ws-count-grid ws-labels"><span title="Blocked">Blocked</span><span title="Needs Terry">Terry</span><span title="Ready for Review">Review</span></div>' +
    '<div id="ws-list"></div><p id="ws-status" role="status"></p><a id="ws-window" target="_blank" rel="noopener">Open board in new window ↗</a></div>';
  content.append(nav, board);
  new ResizeObserver(alignCardStarts).observe(board);
  document.getElementById('mk').setAttribute('aria-modal', 'false');
  const rows = new Map();
  function route(key) { return '/board/' + key.split('/').map(encodeURIComponent).join('/'); }
  function urlKey() {
    if (!location.pathname.startsWith('/board/')) return null;
    try { return location.pathname.slice(7).split('/').map(decodeURIComponent).join('/'); }
    catch (_) { return location.pathname.slice(7); }
  }
  function capture() {
    if (!current || pending !== null) return;
    if (openId) drafts[openId] = document.getElementById('say').value;
    const saved = {drafts: {...drafts}, openId, editing, query, unhideOld, relationshipDraft,
      scroll: Object.fromEntries([...board.querySelectorAll('.lane')].map(lane => [lane.dataset.lane, lane.querySelector('.cards').scrollTop]))};
    if (editing) saved.editValue = document.getElementById(editing === 'subject' ? 'p-subject-edit' : 'p-detail-text').value;
    if (mkOpen) saved.make = {state: mkOpen, subject: document.getElementById('mk-subject').value,
      detail: document.getElementById('mk-detail').value, priority: document.getElementById('mk-priority').value,
      owner: document.getElementById('mk-owner').value, links: relationshipSpecs(document.getElementById('mk-relations'))};
    const drawer = document.querySelector('#panel .body');
    saved.drawerScroll = drawer ? drawer.scrollTop : 0;
    state.records[current] = saved; state.lastBoard = current;
  }
  function persist() {
    capture();
    try { if (storageKey) sessionStorage.setItem(storageKey, JSON.stringify(state)); } catch (_) {}
  }
  function scrolls() {
    return Object.fromEntries([...board.querySelectorAll('.lane')].map(lane => [lane.dataset.lane, lane.querySelector('.cards').scrollTop]));
  }
  function restoreScroll(saved) {
    const epoch = window.workspaceEpoch;
    const apply = () => {
      if (epoch !== window.workspaceEpoch) return;
      for (const lane of board.querySelectorAll('.lane')) lane.querySelector('.cards').scrollTop = saved[lane.dataset.lane] || 0;
    };
    apply(); requestAnimationFrame(apply);
  }
  function loaded() {
    if (typeof data.userCSS === 'string') {
      let style = document.getElementById('workspace-user-css');
      if (!style) { style = document.createElement('style'); style.id = 'workspace-user-css'; document.head.append(style); }
      style.textContent = data.userCSS;
    }
    if (!pending) return;
    const saved = pending; pending = null;
    if (saved.openId && itemById(saved.openId)) {
      openCard(saved.openId);
      if (saved.editing === 'subject') startSubjectEdit();
      if (saved.editing === 'detail') startDetailEdit();
      if (saved.editing) document.getElementById(saved.editing === 'subject' ? 'p-subject-edit' : 'p-detail-text').value = saved.editValue || '';
      relationshipDraft = saved.relationshipDraft || null;
      paintRelations(itemById(saved.openId));
    }
    if (saved.make) {
      const lane = data.lanes.find(entry => entry.state === saved.make.state);
      if (lane) {
        openMake(lane);
        for (const field of ['subject', 'detail', 'priority', 'owner']) document.getElementById('mk-' + field).value = saved.make[field] || '';
        const relations = document.getElementById('mk-relations'); relations.replaceChildren();
        for (const spec of saved.make.links || []) addRelationshipRow(relations, spec, null);
      }
    }
    restoreScroll(saved.scroll || {});
    const drawer = document.querySelector('#panel .body'); if (drawer) drawer.scrollTop = saved.drawerScroll || 0;
  }
  function error(message) {
    persist(); closeCard(); closeMake(true); closeEditors();
    window.workspaceReady = false; board.replaceChildren();
    const surface = document.createElement('div'); surface.className = 'ws-error';
    const title = document.createElement('h2'); title.textContent = 'Board unavailable';
    const detail = document.createElement('p'); detail.textContent = message;
    const retry = document.createElement('button'); retry.textContent = 'Retry'; retry.onclick = () => refresh(true);
    surface.append(title, detail, retry); board.append(surface);
    document.getElementById('title').textContent = current || 'Workspace';
    document.title = 'Board unavailable · localswim';
  }
  function select(key, history = 'push') {
    if (!key) { error('No supported boards were found. Add a board to the workspace folder, then retry.'); return; }
    if (key === current && window.workspaceReady) return;
    persist(); window.workspaceEpoch++; window.workspaceReady = false;
    closeCard(); closeMake(true); closeEditors();
    current = key; state.lastBoard = key;
    if (history === 'push') window.history.pushState({}, '', route(key));
    if (history === 'replace') window.history.replaceState({}, '', route(key));
    const entry = catalog.find(entry => entry.key === key);
    pending = state.records[key] || {};
    drafts = {...(pending.drafts || {})}; query = pending.query || ''; unhideOld = !!pending.unhideOld;
    staleReloadState = null; HAY.clear(); seen = null; lastOk = 0; lastCta = null;
    data = {lanes: [], edges: [], counts: {}, actors: [], revision: 0};
    document.getElementById('find').value = query;
    document.getElementById('counts').replaceChildren();
    document.getElementById('banner').classList.remove('show');
    document.getElementById('ws-window').href = route(key);
    renderRows();
    if (!entry || entry.error) { error(entry ? entry.error : 'This bookmark does not identify a board in this workspace. Choose a board from the sidebar.'); return; }
    API_PREFIX = '/api/v001/workspace/boards/' + encodeURIComponent(key);
    document.getElementById('title').textContent = entry.project;
    board.textContent = 'Loading ' + entry.project + '…';
    window.workspaceReady = true;
    tick(); persist();
  }
  function renderRows() {
    const list = document.getElementById('ws-list'), filter = document.getElementById('ws-find').value.toLowerCase();
    const ordered = [...state.pins, ...catalog.map(entry => entry.key).filter(key => !state.pins.includes(key))];
    for (const key of ordered) {
      const entry = catalog.find(entry => entry.key === key); if (!entry) continue;
      let row = rows.get(key);
      if (!row) {
        row = document.createElement('div'); row.className = 'ws-row'; row.dataset.key = key;
        const button = document.createElement('button'); button.className = 'ws-board'; button.type = 'button';
        const name = document.createElement('span'); name.className = 'ws-name';
        const grid = document.createElement('span'); grid.className = 'ws-count-grid';
        for (const [kind] of categories) { const slot = document.createElement('span'); slot.className = 'ws-slot ' + kind; slot.setAttribute('role', 'img'); grid.append(slot); }
        button.append(name, grid); button.onclick = () => select(key);
        const pin = document.createElement('button'); pin.type = 'button'; pin.className = 'ws-pin';
        pin.onclick = () => { state.pins = state.pins.includes(key) ? state.pins.filter(item => item !== key) : [...state.pins, key]; persist(); renderRows(); };
        row.append(button, pin); rows.set(key, row);
      }
      row.hidden = !entry.project.toLowerCase().includes(filter) && !key.toLowerCase().includes(filter);
      row.querySelector('.ws-name').textContent = entry.project;
      row.querySelector('.ws-board').setAttribute('aria-current', key === current ? 'page' : 'false');
      row.querySelector('.ws-board').title = entry.error || entry.project + ' · ' + key;
      categories.forEach(([kind, label], index) => {
        const slot = row.querySelector('.ws-count-grid').children[index];
        const count = entry.counts && entry.counts[kind];
        const text = entry.error || !Number.isInteger(count) ? '?' : count > 0 ? String(count > 99 ? '99+' : count) : '';
        if (slot.textContent !== text) { slot.replaceChildren(); if (text) { const circle = document.createElement('span'); circle.className = 'ws-circle'; circle.textContent = text; slot.append(circle); } }
        slot.setAttribute('aria-label', label + ': ' + (!entry.error && Number.isInteger(count) ? count : 'unavailable'));
        slot.title = slot.getAttribute('aria-label');
      });
      const pin = row.querySelector('.ws-pin'), pinned = state.pins.includes(key);
      pin.textContent = pinned ? '●' : '○'; pin.setAttribute('aria-label', (pinned ? 'Unpin ' : 'Pin ') + entry.project); pin.setAttribute('aria-pressed', String(pinned)); pin.title = pin.getAttribute('aria-label');
      const position = ordered.filter(item => catalog.some(entry => entry.key === item)).indexOf(key);
      if (list.children[position] !== row) list.insertBefore(row, list.children[position] || null);
    }
    for (const [key, row] of rows) if (!catalog.some(entry => entry.key === key)) { row.remove(); rows.delete(key); }
    nav.classList.toggle('collapsed', state.collapsed);
    document.getElementById('ws-toggle').setAttribute('aria-label', state.collapsed ? 'Expand boards' : 'Collapse boards');
    document.getElementById('ws-toggle').setAttribute('aria-expanded', String(!state.collapsed));
  }
  async function refresh(retry = false) {
    if (refreshing) return; refreshing = true;
    try {
      const res = await fetch('/api/v001/workspace', {cache: 'no-store'});
      if (!res.ok) throw new Error('Workspace catalog could not be read.');
      const value = await res.json(); catalog = value.boards;
      document.getElementById('ws-status').textContent = catalog.length ? '' : 'No boards found in this folder.';
      nav.title = value.root;
      if (!initialized) {
        initialized = true; storageKey = 'localswim.workspace.' + value.identity;
        try { const saved = JSON.parse(sessionStorage.getItem(storageKey)); if (saved && saved.records && Array.isArray(saved.pins)) state = saved; } catch (_) {}
        const key = urlKey() || state.lastBoard || (catalog[0] && catalog[0].key);
        if (key) { if (!state.pins.length) state.pins = [key]; select(key, urlKey() ? 'none' : 'replace'); }
        else error('No supported boards were found. Add a board to the workspace folder, then retry.');
      } else if (retry || (!window.workspaceReady && current && catalog.some(entry => entry.key === current && !entry.error))) select(current || (catalog[0] && catalog[0].key), 'none');
      if (current && !catalog.some(entry => entry.key === current && !entry.error) && window.workspaceReady) { persist(); window.workspaceEpoch++; error('This board is missing or cannot be read. Choose another board or retry.'); }
      renderRows();
    } catch (err) { document.getElementById('ws-status').textContent = String(err); if (!initialized) error(String(err)); }
    finally { refreshing = false; }
  }
  function reconcile(prefix, path, body) {
    const key = decodeURIComponent(prefix.split('/').pop());
    const saved = state.records[key]; if (!saved) return;
    const tail = path.slice(prefix.length).split('/').filter(Boolean);
    const id = tail[1] && decodeURIComponent(tail[1]), action = tail[2];
    if (action === 'comment' && saved.drafts && typeof saved.drafts[id] === 'string' && saved.drafts[id].trim() === body.text) {
      delete saved.drafts[id];
      if (current === key && openId === id && document.getElementById('say').value.trim() === body.text) {
        document.getElementById('say').value = ''; delete drafts[id];
      }
    }
    if ((action === 'subject' || action === 'detail') && saved.editing === action && saved.openId === id && saved.editValue === body[action]) {
      saved.editing = null;
      const field = document.getElementById(action === 'subject' ? 'p-subject-edit' : 'p-detail-text');
      if (current === key && openId === id && editing === action && field.value === body[action]) closeEditors();
    }
    if (action === 'link' && saved.relationshipDraft && saved.relationshipDraft.source === id && saved.relationshipDraft.kind === body.kind && saved.relationshipDraft.other === body.other) saved.relationshipDraft = null;
    if (tail.length === 1 && tail[0] === 'cards' && saved.make && saved.make.subject.trim() === body.subject && saved.make.detail === body.detail) {
      delete saved.make;
      if (current === key && mkOpen && document.getElementById('mk-subject').value.trim() === body.subject && document.getElementById('mk-detail').value === body.detail) closeMake(true);
    }
    try { sessionStorage.setItem(storageKey, JSON.stringify(state)); } catch (_) {}
  }
  window.WorkspaceUI = {capture: persist, loaded, scrolls, restoreScroll, error, reconcile};
  document.getElementById('ws-toggle').onclick = () => { const saved = scrolls(); state.collapsed = !state.collapsed; persist(); renderRows(); alignCardStarts(); restoreScroll(saved); };
  document.getElementById('ws-find').oninput = renderRows;
  document.addEventListener('input', persist);
  document.addEventListener('change', persist);
  window.addEventListener('beforeunload', persist);
  window.addEventListener('popstate', () => { const key = urlKey(); if (key) select(key, 'none'); });
  refresh(); setInterval(refresh, 2000);
})();
