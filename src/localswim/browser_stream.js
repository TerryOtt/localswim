/* One combined stream per local board tab. Native EventSource reconnects without polling. */
(() => {
  let source = null, selected = null, mode = 'board', callback = null, active = false;
  let generation = 0;
  function close() {
    generation++;
    if (source) { source.close(); source = null; callback({type:'connection',connected:false}); }
  }
  function connect() {
    close(); if (!active) return;
    const mine = generation;
    const route = mode === 'workspace' ? '/api/v001/workspace/events' : '/api/v001/events';
    source = new EventSource(route + (mode === 'workspace' && selected ? '?board=' + encodeURIComponent(selected) : ''));
    source.onopen = () => { if (mine === generation) callback({type:'connection',connected:true}); };
    source.onerror = () => { if (mine === generation) callback({type:'connection',connected:false}); };
    for (const type of ['status','catalog','board','heartbeat']) source.addEventListener(type, event => {
      if (mine !== generation) return;
      let value;
      try { value = JSON.parse(event.data); } catch (_) { return; }
      callback({type,value});
    });
  }
  window.BoardStream = {
    start(nextMode, listener) { mode = nextMode; callback = listener; active = true; connect(); },
    select(board) { if (selected === board) return; selected = board; if (active) connect(); },
  };
  window.addEventListener('pagehide', () => { active = false; close(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { active = true; connect(); } });
})();
