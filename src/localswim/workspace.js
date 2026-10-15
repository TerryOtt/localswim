/* Workspace navigation shares the board page's existing controls and writer boundary. */
(() => {
  const categories = [['blocked', 'Blocked'], ['needsHuman', 'Needs Terry'], ['review', 'Ready for Review']];
  let catalog = [], current = null, storageKey = '', state = {records: {}, collapsed: false, pins: []};
  let pending = null, initialized = false, refreshing = false, recoverOnCatalog = false, retryRequested = false;
  const preferencesRoute = '/api/v001/workspace/preferences';
  let preferences = null, pinCommands = [], savingPins = false, pinFailure = '', pinRetryAt = 0;
  let pinFailurePermanent = false;
  let gatewayPush = null;
  window.workspaceEpoch = 0;
  const link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = '/workspace.css'; document.head.append(link);
  const content = document.createElement('div'); content.id = 'workspace-content';
  const board = document.getElementById('board'); board.before(content);
  const nav = document.createElement('nav'); nav.id = 'workspace-nav'; nav.setAttribute('aria-label', 'Boards');
  nav.innerHTML = '<div class="ws-heading"><strong>Boards</strong><button type="button" id="ws-toggle" aria-label="Collapse boards">☰</button></div>' +
    '<div class="ws-expanded"><input id="ws-find" type="search" placeholder="Find a board" aria-label="Find a board">' +
    '<div class="ws-count-grid ws-labels"><span title="Blocked">Blocked</span><span title="Needs Terry">Terry</span><span title="Ready for Review">Review</span></div>' +
    '<div id="ws-list"></div><p id="ws-announcement" class="ws-sr-only" aria-live="polite" aria-atomic="true"></p><p id="ws-status" role="status"></p><a id="ws-window" target="_blank" rel="noopener">Open board in new window ↗</a></div>';
  content.append(nav, board);
  new ResizeObserver(alignCardStarts).observe(board);
  document.getElementById('mk').setAttribute('aria-modal', 'false');
  const rows = new Map();
  const sidebarMinimum = 312, boardMinimum = 320;
  const resizer = document.createElement('div');
  resizer.id = 'ws-resize'; resizer.tabIndex = 0;
  resizer.setAttribute('role', 'separator');
  resizer.setAttribute('aria-label', 'Resize boards sidebar');
  resizer.setAttribute('aria-orientation', 'vertical');
  resizer.setAttribute('aria-controls', 'workspace-nav');
  resizer.title = 'Drag to resize · Left/Right arrows, Home/End · Double-click to reset';
  nav.append(resizer);
  let sidebarDrag = null;
  function sidebarMaximum() { return Math.max(sidebarMinimum, content.clientWidth - boardMinimum); }
  function requestedSidebarWidth() {
    return Number.isFinite(state.sidebarWidth) && state.sidebarWidth >= sidebarMinimum
      ? state.sidebarWidth : sidebarMinimum;
  }
  function applySidebarWidth() {
    const width = Math.round(Math.min(requestedSidebarWidth(), sidebarMaximum()));
    nav.style.setProperty('--ws-sidebar-width', width + 'px');
    if (state.collapsed && document.activeElement === resizer)
      document.getElementById('ws-toggle').focus({preventScroll: true});
    resizer.hidden = !!state.collapsed;
    resizer.setAttribute('aria-valuemin', String(sidebarMinimum));
    resizer.setAttribute('aria-valuemax', String(sidebarMaximum()));
    resizer.setAttribute('aria-valuenow', String(width));
    resizer.setAttribute('aria-valuetext', width + ' pixels');
  }
  function resizeSidebar(width) {
    state.sidebarWidth = Math.round(Math.max(sidebarMinimum, Math.min(sidebarMaximum(), width)));
    applySidebarWidth();
  }
  function finishSidebarResize(cancel = false) {
    if (!sidebarDrag) return;
    const saved = sidebarDrag; sidebarDrag = null;
    if (resizer.hasPointerCapture(saved.id)) resizer.releasePointerCapture(saved.id);
    document.body.classList.remove('ws-resizing');
    if (cancel) state.sidebarWidth = saved.requestedWidth;
    applySidebarWidth(); alignCardStarts(); restoreScroll(saved.scroll); persist();
  }
  resizer.addEventListener('pointerdown', event => {
    if (event.button !== 0 || !event.isPrimary || state.collapsed || sidebarDrag) return;
    event.preventDefault();
    sidebarDrag = {id: event.pointerId, x: event.clientX,
      width: Number(resizer.getAttribute('aria-valuenow')), requestedWidth: state.sidebarWidth,
      scroll: scrolls()};
    resizer.focus({preventScroll: true}); resizer.setPointerCapture(event.pointerId);
    document.body.classList.add('ws-resizing');
  });
  resizer.addEventListener('pointermove', event => {
    if (!sidebarDrag || event.pointerId !== sidebarDrag.id) return;
    resizeSidebar(sidebarDrag.width + event.clientX - sidebarDrag.x);
    restoreScroll(sidebarDrag.scroll);
  });
  resizer.addEventListener('pointerup', event => {
    if (sidebarDrag && event.pointerId === sidebarDrag.id) finishSidebarResize();
  });
  resizer.addEventListener('pointercancel', () => finishSidebarResize(true));
  resizer.addEventListener('lostpointercapture', () => finishSidebarResize(true));
  resizer.addEventListener('dblclick', () => { resizeSidebar(sidebarMinimum); persist(); });
  resizer.addEventListener('keydown', event => {
    if (event.key === 'Escape' && sidebarDrag) {
      event.preventDefault(); event.stopPropagation(); finishSidebarResize(true); return;
    }
    const width = Number(resizer.getAttribute('aria-valuenow')), step = event.shiftKey ? 64 : 16;
    const next = {ArrowLeft: width - step, ArrowRight: width + step,
      Home: sidebarMinimum, End: sidebarMaximum()}[event.key];
    if (next === undefined || sidebarDrag) return;
    event.preventDefault();
    event.stopPropagation();
    const saved = scrolls(); resizeSidebar(next); alignCardStarts(); restoreScroll(saved); persist();
  });
  new ResizeObserver(applySidebarWidth).observe(content);
  applySidebarWidth();
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
    const saved = pending;
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
    pending = null;
  }
  function error(message, recover = false) {
    recoverOnCatalog = recover;
    persist();
    if (pending === null) pending = state.records[current] || {};
    window.workspaceEpoch++; lastOk = 0;
    closeCard(); closeMake(true); closeEditors();
    window.workspaceReady = false; board.replaceChildren();
    const surface = document.createElement('div'); surface.className = 'ws-error';
    const title = document.createElement('h2'); title.textContent = 'Board unavailable';
    const detail = document.createElement('p'); detail.textContent = message;
    const retry = document.createElement('button'); retry.textContent = 'Retry'; retry.onclick = () => { retry.disabled = true; retry.textContent = 'Retrying…'; refresh(true); };
    surface.append(title, detail, retry); board.append(surface);
    const entry = catalog.find(item => item.key === current);
    document.getElementById('title').textContent = entry ? entry.project : current || 'Workspace';
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
    if (!entry || entry.error) { error(entry ? entry.error : 'This bookmark does not identify a board in this workspace. Choose a board from the sidebar.', true); return; }
    API_PREFIX = '/api/v001/workspace/boards/' + encodeURIComponent(key);
    document.getElementById('title').textContent = entry.project;
    boardTitle = entry.project; document.title = entry.project + ' · Loading';
    recoverOnCatalog = false;
    board.textContent = 'Loading ' + entry.project + '…';
    window.workspaceReady = true;
    tick(); persist();
  }
  const boardNames = new Intl.Collator('en', {sensitivity: 'accent'});
  function minutesSinceEdit(entry, now) {
    return Number.isFinite(entry.lastCardEditMs)
      ? Math.floor(Math.max(0, now - entry.lastCardEditMs) / 60000) : Infinity;
  }
  function editTimestamp(entry) {
    return Number.isFinite(entry.lastCardEditMs) ? entry.lastCardEditMs : -Infinity;
  }
  function attentionTotal(entry) {
    if (entry.error) return 0;
    return categories.reduce((total, [kind]) => {
      const count = entry.counts && entry.counts[kind];
      return total + (Number.isInteger(count) && count > 0 ? count : 0);
    }, 0);
  }
  function compareBoards(a, b) {
    const stampA = editTimestamp(a), stampB = editTimestamp(b);
    if (stampA !== stampB) return stampA > stampB ? -1 : 1;
    return attentionTotal(b) - attentionTotal(a)
      || boardNames.compare(a.project, b.project)
      || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
  }
  const pinnedHeading = document.createElement('h2');
  pinnedHeading.id = 'ws-pinned-heading'; pinnedHeading.className = 'ws-group-heading';
  pinnedHeading.textContent = 'Pinned';
  const otherHeading = document.createElement('h2');
  otherHeading.id = 'ws-other-heading'; otherHeading.className = 'ws-group-heading';
  otherHeading.textContent = 'Other boards';
  let dragKey = null;
  function normalizePins() {
    const known = new Set(catalog.map(entry => entry.key));
    state.pins = [...new Set((Array.isArray(state.pins) ? state.pins : [])
      .filter(key => typeof key === 'string' && known.has(key)))];
  }
  function announce(message) { document.getElementById('ws-announcement').textContent = message; }
  function showWorkspaceStatus() {
    let message = !catalog.length ? 'No boards found in this folder.' : '';
    if (preferences && preferences.autopush && gatewayPush
        && ['failed', 'off'].includes(gatewayPush.state))
      message = 'Pinned boards are saved locally, but gateway backup is unavailable: ' + gatewayPush.detail;
    if (pinCommands.length) message = 'Saving pinned boards…';
    if (pinFailure) message = 'Pinned change was not saved: ' + pinFailure
      + (pinFailurePermanent ? '' : ' Retrying when available.');
    document.getElementById('ws-status').textContent = message;
  }
  function projectPins() {
    if (!preferences) return;
    let pins = [...preferences.pinnedBoards], initializedPins = preferences.pinsInitialized;
    for (const command of pinCommands) {
      if (command.action === 'initialize') {
        if (!initializedPins) pins = [...command.pinnedBoards];
      } else if (command.action === 'pin') {
        if (!pins.includes(command.key)) pins.push(command.key);
      } else if (command.action === 'unpin') {
        pins = pins.filter(key => key !== command.key);
      } else if (command.action === 'move' && pins.includes(command.key) && pins.includes(command.target)) {
        pins = pins.filter(key => key !== command.key);
        pins.splice(pins.indexOf(command.target) + (command.after ? 1 : 0), 0, command.key);
      }
      initializedPins = true;
    }
    state.pins = pins;
    normalizePins();
  }
  function acceptPreferences(value) {
    if (!value || !Number.isInteger(value.revision) || !Array.isArray(value.pinnedBoards)
        || typeof value.pinsInitialized !== 'boolean') throw new Error('Gateway preferences could not be read.');
    if (!preferences || value.revision >= preferences.revision) preferences = value;
    if (!dragKey) projectPins();
  }
  async function savePinCommand(command) {
    for (let attempt = 0; attempt < 4; attempt++) {
      if (command.action === 'initialize' && preferences.pinsInitialized) return;
      const response = await fetch(preferencesRoute, {
        method: 'POST', keepalive: true, signal: AbortSignal.timeout(20000),
        headers: {'Authorization': 'Bearer ' + API_TOKEN, 'Content-Type': 'application/json',
          'If-Match': '"revision-' + preferences.revision + '"'},
        body: JSON.stringify(command),
      });
      const out = await response.json();
      if (response.status === 412) {
        acceptPreferences(await apiGet(preferencesRoute));
        continue;
      }
      if (response.status === 401) {
        const session = await apiGet('/api/v001/browser-session');
        if (session.workspaceIdentity !== storageKey.slice('localswim.workspace.'.length))
          throw new Error('The workspace changed. Reload this page.');
        API_TOKEN = session.token;
        continue;
      }
      if (!response.ok) {
        const failure = new Error(out.error || 'The gateway could not save this change.');
        failure.permanent = response.status >= 400 && response.status < 500;
        throw failure;
      }
      acceptPreferences(out);
      return;
    }
    throw new Error('Another window is updating these preferences.');
  }
  async function flushPinCommands() {
    if (savingPins || !preferences || !pinCommands.length || Date.now() < pinRetryAt) return;
    savingPins = true; showWorkspaceStatus();
    try {
      while (pinCommands.length) {
        const command = pinCommands[0];
        await savePinCommand(command);
        pinCommands.shift(); pinFailure = ''; pinFailurePermanent = false; pinRetryAt = 0;
        projectPins(); persist(); renderRows();
        announce('Pinned boards saved.');
      }
    } catch (err) {
      pinFailurePermanent = !!err.permanent;
      if (pinFailurePermanent) pinCommands.shift();
      pinFailure = err instanceof TypeError ? 'The gateway could not be reached.' : err.message;
      pinRetryAt = Date.now() + 5000;
      projectPins(); persist(); renderRows();
    } finally { savingPins = false; showWorkspaceStatus(); }
  }
  function queuePinCommand(command) {
    if (!preferences) { toast('Gateway preferences are not available. Retry the workspace.', true); return; }
    pinCommands.push(command); pinFailure = ''; pinFailurePermanent = false; pinRetryAt = 0;
    projectPins(); persist(); renderRows(); showWorkspaceStatus();
    flushPinCommands();
  }
  function clearDropFeedback() {
    for (const row of rows.values()) row.classList.remove('ws-drop-before', 'ws-drop-after');
  }
  function reorderPin(key, target, after = false) {
    normalizePins();
    if (key === target || !state.pins.includes(key) || !state.pins.includes(target)) return;
    const order = state.pins.filter(item => item !== key);
    const position = order.indexOf(target) + (after ? 1 : 0);
    order.splice(position, 0, key);
    queuePinCommand({action: 'move', key, target, after});
    const entry = catalog.find(entry => entry.key === key);
    announce(entry.project + ' moved to pinned position ' + (position + 1) + ' of ' + order.length + '.');
  }
  function togglePin(key) {
    normalizePins();
    const entry = catalog.find(entry => entry.key === key); if (!entry) return;
    const pinned = state.pins.includes(key);
    queuePinCommand({action: pinned ? 'unpin' : 'pin', key});
    announce(entry.project + (pinned ? ' unpinned. It now follows the automatic board order.' : ' pinned at position ' + state.pins.length + '.'));
  }
  function makePinControls(row, key) {
    const grip = document.createElement('button'); grip.type = 'button'; grip.className = 'ws-grip';
    grip.draggable = true;
    grip.innerHTML = '<svg viewBox="0 0 16 20" aria-hidden="true" focusable="false"><g fill="currentColor"><circle cx="5" cy="4" r="1.5"/><circle cx="11" cy="4" r="1.5"/><circle cx="5" cy="10" r="1.5"/><circle cx="11" cy="10" r="1.5"/><circle cx="5" cy="16" r="1.5"/><circle cx="11" cy="16" r="1.5"/></g></svg>';
    grip.onclick = event => event.stopPropagation();
    grip.onkeydown = event => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      const index = state.pins.indexOf(key);
      if (index < 0 || !['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? state.pins.length - 1
        : Math.max(0, Math.min(state.pins.length - 1, index + (event.key === 'ArrowUp' ? -1 : 1)));
      if (next !== index) reorderPin(key, state.pins[next], next > index);
      else announce('Already at pinned position ' + (index + 1) + '.');
    };
    grip.ondragstart = event => {
      if (!state.pins.includes(key) || !event.dataTransfer) { event.preventDefault(); return; }
      dragKey = key; event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('application/x-localswim-pinned-board', key);
      event.dataTransfer.setDragImage(row, Math.min(30, row.clientWidth), 15);
      row.classList.add('ws-dragging');
    };
    grip.ondragend = () => { dragKey = null; row.classList.remove('ws-dragging'); clearDropFeedback(); };
    row.ondragover = event => {
      if (!dragKey || dragKey === key || !state.pins.includes(dragKey) || !state.pins.includes(key)) return;
      event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
      const after = event.clientY >= row.getBoundingClientRect().top + row.clientHeight / 2;
      clearDropFeedback(); row.classList.add(after ? 'ws-drop-after' : 'ws-drop-before');
    };
    row.ondragleave = event => { if (!row.contains(event.relatedTarget)) row.classList.remove('ws-drop-before', 'ws-drop-after'); };
    row.ondrop = event => {
      if (!dragKey || !state.pins.includes(dragKey) || !state.pins.includes(key)) return;
      event.preventDefault();
      const after = event.clientY >= row.getBoundingClientRect().top + row.clientHeight / 2;
      const source = dragKey; dragKey = null; clearDropFeedback();
      rows.get(source).classList.remove('ws-dragging');
      reorderPin(source, key, after);
    };
    const pin = document.createElement('button'); pin.type = 'button'; pin.className = 'ws-pin';
    pin.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M7 2h6l-1 5 3 3v2h-4v5l-1 1-1-1v-5H5v-2l3-3z"/></svg>';
    pin.onclick = () => togglePin(key);
    row.append(grip, pin);
  }
  function renderRows() {
    normalizePins();
    const list = document.getElementById('ws-list'), filter = document.getElementById('ws-find').value.toLowerCase();
    const now = Date.now(), pinned = new Set(state.pins);
    const others = catalog.filter(entry => !pinned.has(entry.key)).sort(compareBoards);
    const ordered = [...state.pins, ...others.map(entry => entry.key)];
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    const scrollTop = list.scrollTop;
    let visiblePinned = 0, visibleOthers = 0;
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
        row.append(button); makePinControls(row, key); rows.set(key, row);
      }
      const isPinned = pinned.has(key);
      row.classList.toggle('ws-pinned', isPinned);
      row.hidden = !entry.project.toLowerCase().includes(filter) && !key.toLowerCase().includes(filter);
      if (!row.hidden) { if (isPinned) visiblePinned++; else visibleOthers++; }
      const grip = row.querySelector('.ws-grip'); grip.hidden = !isPinned; grip.draggable = isPinned;
      grip.setAttribute('aria-label', 'Reorder ' + entry.project + '. Use Up and Down arrows, Home or End.');
      grip.title = 'Drag to reorder · Up/Down, Home/End';
      const pin = row.querySelector('.ws-pin'); pin.setAttribute('aria-pressed', String(isPinned));
      pin.setAttribute('aria-label', (isPinned ? 'Unpin ' : 'Pin ') + entry.project);
      pin.title = (isPinned ? 'Unpin ' : 'Pin ') + entry.project;
      row.querySelector('.ws-name').textContent = entry.project;
      row.querySelector('.ws-board').setAttribute('aria-current', key === current ? 'page' : 'false');
      const age = minutesSinceEdit(entry, now);
      row.querySelector('.ws-board').title = entry.error || entry.project + ' · ' + key
        + ' · Last card edit: ' + (Number.isFinite(age) ? age + ' minutes ago' : 'unknown');
      categories.forEach(([kind, label], index) => {
        const slot = row.querySelector('.ws-count-grid').children[index];
        const count = entry.counts && entry.counts[kind];
        const text = entry.error || !Number.isInteger(count) ? '?' : count > 0 ? String(count > 99 ? '99+' : count) : '';
        if (slot.textContent !== text) { slot.replaceChildren(); if (text) { const circle = document.createElement('span'); circle.className = 'ws-circle'; circle.textContent = text; slot.append(circle); } }
        slot.setAttribute('aria-label', label + ': ' + (!entry.error && Number.isInteger(count) ? count : 'unavailable'));
        slot.title = slot.getAttribute('aria-label');
      });
    }
    for (const [key, row] of rows) if (!catalog.some(entry => entry.key === key)) { row.remove(); rows.delete(key); }
    pinnedHeading.hidden = visiblePinned === 0;
    otherHeading.hidden = state.pins.length === 0 || visibleOthers === 0;
    otherHeading.classList.toggle('ws-divider', visiblePinned > 0 && visibleOthers > 0);
    const desired = [pinnedHeading, ...state.pins.map(key => rows.get(key)), otherHeading, ...others.map(entry => rows.get(entry.key))];
    desired.forEach((element, index) => { if (list.children[index] !== element) list.insertBefore(element, list.children[index] || null); });
    if (focused && focused.isConnected && !focused.closest('.ws-row').hidden) {
      const target = focused.hidden ? focused.closest('.ws-row').querySelector('.ws-pin') : focused;
      if (document.activeElement !== target) target.focus({preventScroll: true});
    }
    list.scrollTop = scrollTop;
    nav.classList.toggle('collapsed', state.collapsed);
    applySidebarWidth();
    document.getElementById('ws-toggle').setAttribute('aria-label', state.collapsed ? 'Expand boards' : 'Collapse boards');
    document.getElementById('ws-toggle').setAttribute('aria-expanded', String(!state.collapsed));
  }
  async function refresh(retry = false) {
    retryRequested = retryRequested || retry;
    if (refreshing) return; refreshing = true;
    const retryNow = retryRequested; retryRequested = false;
    try {
      const value = await apiGet('/api/v001/workspace');
      const session = await apiGet('/api/v001/browser-session');
      if (session.workspaceIdentity !== value.identity || typeof session.token !== 'string' || !session.token)
        throw new Error('The workspace session could not be recovered.');
      if (initialized && storageKey !== 'localswim.workspace.' + value.identity)
        throw new Error('The workspace folder changed. Reload this page.');
      // A restarted gateway has a new browser credential even when its build is unchanged.
      API_TOKEN = session.token;
      catalog = value.boards;
      gatewayPush = value.gatewayPush || null;
      nav.title = value.root;
      if (!initialized) {
        storageKey = 'localswim.workspace.' + value.identity;
        try { const saved = JSON.parse(sessionStorage.getItem(storageKey)); if (saved && saved.records) { state = saved; } } catch (_) {}
        normalizePins();
        const legacyPins = [...state.pins];
        acceptPreferences(value.preferences); initialized = true;
        if (!preferences.pinsInitialized && legacyPins.length)
          queuePinCommand({action: 'initialize', pinnedBoards: legacyPins});
        const key = urlKey() || state.lastBoard || (catalog[0] && catalog[0].key);
        if (key) select(key, urlKey() ? 'none' : 'replace');
        else error('No supported boards were found. Add a board to the workspace folder, then retry.');
      } else {
        acceptPreferences(value.preferences);
        if (retryNow || (recoverOnCatalog && !window.workspaceReady && current && catalog.some(entry => entry.key === current && !entry.error))) select(current || (catalog[0] && catalog[0].key), 'none');
      }
      if (current && !catalog.some(entry => entry.key === current && !entry.error) && window.workspaceReady) { persist(); window.workspaceEpoch++; error('This board is missing or cannot be read. Choose another board or retry.', true); }
      renderRows(); showWorkspaceStatus(); flushPinCommands();
    } catch (err) { document.getElementById('ws-status').textContent = String(err); if (!initialized || retryNow) error('Could not reach the workspace. Restart localswim, then Retry.'); }
    finally { refreshing = false; if (retryRequested) refresh(); }
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
  document.getElementById('ws-toggle').onclick = () => { finishSidebarResize(); const saved = scrolls(); state.collapsed = !state.collapsed; persist(); renderRows(); alignCardStarts(); restoreScroll(saved); };
  document.getElementById('ws-find').oninput = renderRows;
  document.addEventListener('input', persist);
  document.addEventListener('change', persist);
  window.addEventListener('beforeunload', persist);
  window.addEventListener('popstate', () => { const key = urlKey(); if (key) select(key, 'none'); });
  refresh(); setInterval(refresh, 2000);
})();
