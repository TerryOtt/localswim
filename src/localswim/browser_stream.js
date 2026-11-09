/* One combined stream per local board tab. Native EventSource reconnects without polling. */
(() => {
  let source = null, selected = null, mode = 'board', callback = null, active = false;
  let generation = 0, session = 0, watched = [];
  function close() {
    generation++;
    if (source) { source.close(); source = null; callback({type:'connection',connected:false,generation:session}); }
  }
  function connect() {
    close(); if (!active) return;
    const mine = generation; session++;
    const route = mode === 'workspace' ? '/api/v001/workspace/events' : '/api/v001/events';
    const query = mode === 'workspace' ? watched.map(key => 'board=' + encodeURIComponent(key)).join('&') : '';
    source = new EventSource(route + (query ? '?' + query : ''));
    source.onopen = () => { if (mine === generation) callback({type:'connection',connected:true,generation:session}); };
    source.onerror = () => { if (mine === generation) callback({type:'connection',connected:false,generation:session}); };
    for (const type of ['watch','status','catalog','board','heartbeat']) source.addEventListener(type, event => {
      if (mine !== generation) return;
      let value;
      try { value = JSON.parse(event.data); } catch (_) { return; }
      if (type === 'watch') session++;
      callback({type,value,generation:session});
    });
  }
  window.BoardStream = {
    start(nextMode, listener) { mode = nextMode; callback = listener; active = true; connect(); },
    select(board) { selected = board; },
    watch(boards) {
      const next = [...new Set(boards)].sort();
      if (next.join('\n') === watched.join('\n')) return;
      watched = next; if (active) connect();
    },
  };
  window.addEventListener('pagehide', () => { active = false; close(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { active = true; connect(); } });
})();
