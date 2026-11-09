"""Execute the shipped workspace cache and navigation code without a browser."""

import json
import pathlib
import shutil
import subprocess

import pytest

from localswim import api_endpoint

DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const {workspace,page,mode}=JSON.parse(fs.readFileSync(0,'utf8'));
function section(start,end){const at=workspace.indexOf(start);assert.ok(at>=0);
 const until=workspace.indexOf(end,at);assert.ok(until>at);return workspace.slice(at,until)}
const nodes=new Map();const el=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',
 classList:{remove(){}},replaceChildren(){},append(){},querySelectorAll(){return []}});return nodes.get(id)};
let now=0,ticks=[],streams=[],painted=[],captured=[],loads=0;
const context=vm.createContext({Date:{now:()=>now},TextEncoder,AbortController,AbortSignal,setTimeout(){return 1},clearTimeout(){},queueMicrotask(){},console,window:{workspaceEpoch:0,workspaceReady:false,
 history:{pushState(){},replaceState(){}},BoardStream:{select:key=>streams.push(key)}},
 document:{getElementById:el,createElement:()=>el(Math.random()),querySelector:()=>null,title:''},
 sessionStorage:{setItem(){captured.push(context.current)}},storageKey:'localswim.test',
 openId:null,editing:null,relationshipDraft:null,mkOpen:null,
 API_PREFIX:'',pollingEpoch:null,revisionFloors:new Map(),streamBoardError:null,
 current:null,catalog:[{key:'one',project:'One'},{key:'two',project:'Two'}],
 state:{records:{one:{drafts:{c:'unposted'},query:'remember me',unhideOld:true}}},pending:null,
 board:el('board'),HAY:new Map(),
 closeCard(){},closeMake(){},closeEditors(){},route:key=>'/board/'+key,renderRows(){},
 renderLive(){},renderBoardBanner(){},loaded(){loads++;context.pending=null},
 projectActions(raw){const out=JSON.parse(JSON.stringify(raw));out.optimistic=true;return out},
 paint(){painted.push(JSON.parse(JSON.stringify(context.data)))},tick:force=>ticks.push(force),
 recoverOnCatalog:false,renderSaveRecovery(){},lastOk:0,
 scrolls(){},restoreScroll(){},reconcile(){},gatewayStatus:{},refresh(){},flushPinCommands(){}});
const run=code=>vm.runInContext(code,context);
run(page.slice(page.indexOf('function applyBoardStatus('),page.indexOf('async function tick(')));
run(section('  function createRecentBoardCache(', '  const categories'));
if(mode==='bounds'){
 run('var small=createRecentBoardCache({budget:180})');
 const put=(key,revision,extra={})=>{context.raw={revision,items:[{subject:'raw'}],...extra};
  return run(`small.remember('${key}',raw)`)};
 const get=(key,floor=0)=>run(`small.get('${key}',${floor})`);
 run("small.select('one')");assert.equal(put('one',2),true);
 assert.equal(put('one',1),false);assert.equal(get('one').revision,2);
 assert.equal(put('one',3,{error:'unavailable'}),false);
 assert.equal(get('one',3),null);
 run("small.select('two')");put('two',1);
 run("small.select('three')");put('three',1);
 // Reads and background replacement never renew selection recency.
 get('one');put('one',3);run("small.configure(80)");
 assert.equal(get('one'),null);assert.equal(get('two'),null);assert.ok(get('three'));
 // Watch resource bounds rotate every inactive board while always retaining active.
 context.watchKeys=['active','a','b','c','d'];
 const batches=[];let offset=0;
 for(let n=0;n<4;n++){
  context.batchOffset=offset;
  const batch=run("watchBatch(watchKeys,'active',{maxKeys:2,maxBytes:60,offset:batchOffset})");
  assert.equal(batch.watched[0],'active');assert.equal(batch.watched.length,2);
  assert.equal(batch.overflow,true);batches.push(batch.watched[1]);offset=batch.next;
 }
 assert.deepEqual(batches,['a','b','c','d']);
 assert.equal(run("watchBatch(watchKeys,'active',{maxKeys:10,maxBytes:1000}).overflow"),false);
 // No count limit or age-based expiry.
 run('var many=createRecentBoardCache({budget:10000})');
 for(let n=0;n<12;n++)run(`many.select('${n}');many.remember('${n}',{revision:1})`);
 now=10**12;assert.equal(run('many.stats().count'),12);
 // Growing old background candidate self-evicts, preserving newer boards.
 run('var grow=createRecentBoardCache({budget:140})');
 run("grow.select('old');grow.remember('old',{revision:1});grow.select('new');grow.remember('new',{revision:1})");
 assert.equal(run("grow.remember('old',{revision:2,blob:'x'.repeat(110)})"),false);
 assert.equal(run("grow.get('old')"),null);assert.ok(run("grow.get('new')"));
 // An impossible inactive candidate does not evict useful entries.
 assert.equal(run("grow.remember('old',{revision:3,blob:'x'.repeat(300)})"),false);
 assert.ok(run("grow.get('new')"));
 // Active oversized model survives and leaves a valid active-only state.
 run("grow.select('huge');grow.remember('huge',{revision:1,blob:'x'.repeat(300)})");
 assert.equal(run('grow.stats().count'),1);assert.ok(run('grow.stats().bytes>grow.stats().budget'));
 // Shrinking replacement is re-accounted exactly.
 run("grow.remember('huge',{revision:2})");assert.equal(run('grow.stats().bytes'),14);
 context.raw={revision:4};context.raw.circular=context.raw;
 assert.equal(run("small.remember('bad',raw)"),false);
 }else if(mode==='scheduler'){
 const prefix='/api/v001/workspace/boards/one';context.API_PREFIX='/api/v001/workspace/boards/two';
 context.window.workspaceReady=true;
 const pending=[];context.apiGet=(path,signal)=>new Promise(resolve=>pending.push({path,signal,resolve}));
 run("recentBoards.select('/api/v001/workspace/boards/one');recentBoards.remember('/api/v001/workspace/boards/one',{revision:1,sync:{boardIdentity:'id',contentVersion:'v1'}});recentBoards.select(API_PREFIX)");
 run("metadata({type:'connection',connected:true,generation:1});metadata({type:'watch',generation:2,value:{after:10}})");
 run("metadata({type:'board',generation:2,value:{board:'one',status:{ok:true,revision:2,boardIdentity:'id',contentVersion:'v2',sampledAt:11}}})");
 run("cachedView={prefix:API_PREFIX,phase:'failed'}");
 const first=run('refreshBackground()');run('refreshBackground()');assert.equal(pending.length,1);
 assert.equal(pending[0].path,prefix+'/board');
 // An evicted request cannot resurrect its snapshot, even if abort is ignored.
 run("recentBoards.remove('/api/v001/workspace/boards/one')");
 pending[0].resolve({revision:2,sync:{boardIdentity:'id',contentVersion:'v2'}});
 first.then(()=>assert.equal(run("recentBoards.get('/api/v001/workspace/boards/one')"),null));
}else if(mode==='freshness'){
 const prefix='/api/v001/workspace/boards/one';context.API_PREFIX=prefix;
 context.window.workspaceReady=true;
 const raw={revision:5,sync:{boardIdentity:'identity',contentVersion:'v1'}};
 context.raw=raw;run("recentBoards.select(API_PREFIX);recentBoards.remember(API_PREFIX,raw)");
 const event=status=>({type:'board',generation:2,value:{board:'one',status}});
 const meta={ok:true,revision:5,boardIdentity:'identity',contentVersion:'v1',sampledAt:11};
 run("metadata({type:'connection',connected:true,generation:1});metadata({type:'watch',generation:2,value:{after:10}})");
 context.event=event({...meta,sampledAt:9});run('metadata(event)');assert.equal(run('currentSnapshot(API_PREFIX)'),false);
 context.event=event(meta);run('metadata(event)');assert.equal(run('currentSnapshot(API_PREFIX)'),true);
 assert.deepEqual(ticks,[]);
 run("metadata({type:'connection',connected:false,generation:2});metadata({type:'connection',connected:true,generation:2});metadata({type:'watch',generation:3,value:{after:20}})");
 assert.equal(run('currentSnapshot(API_PREFIX)'),false);
 context.event={...event(meta),generation:3};run('metadata(event)');assert.equal(run('currentSnapshot(API_PREFIX)'),false);
 context.event={...event({...meta,sampledAt:21}),generation:3};run('metadata(event)');assert.equal(run('currentSnapshot(API_PREFIX)'),true);
 // Same-revision external replacement is not falsely considered current.
 context.event={...event({...meta,sampledAt:22,contentVersion:'v2'}),generation:3};run('metadata(event)');
 assert.equal(run('currentSnapshot(API_PREFIX)'),false);assert.equal(ticks.length,1);
 context.event={...event({...meta,sampledAt:23,boardIdentity:'other'}),generation:3};run('metadata(event)');
 assert.equal(run('currentSnapshot(API_PREFIX)'),false);assert.equal(ticks.length,2);
 run("cachedView={prefix:API_PREFIX,phase:'refreshing'};var token=settling(API_PREFIX,window.workspaceEpoch);refreshed(API_PREFIX,window.workspaceEpoch,token)");
 assert.equal(run('cachedView.phase'),'refreshing');
}else{
 // Exercise the actual persistence boundary used by initial load and sidebar navigation.
 run(section('  function capture()', '  function scrolls()'));
 run(section('  function error(message, recover = false)', '  const boardNames'));
 run(section('  window.WorkspaceUI =', "  document.getElementById('ws-toggle').onclick"));
 const ui=context.window.WorkspaceUI,prefix='/api/v001/workspace/boards/one';
 const raw={revision:5,lanes:[],project:'One',items:[{subject:'confirmed'}]};context.rawPrefix=prefix;
 context.raw=raw;assert.equal(run('recentBoards.remember(rawPrefix,raw)'),true);
 run("select('one','none')");
 assert.equal(painted[0].items[0].subject,'confirmed');assert.equal(painted[0].optimistic,true);
 assert.equal(context.authoritativeData.optimistic,undefined);
 assert.equal(context.query,'remember me');assert.equal(context.drafts.c,'unposted');
 assert.equal(ui.cachePhase(),'refreshing');assert.equal(ui.canMutate(),false);
 assert.deepEqual(ticks,[]);assert.deepEqual(streams,['one']);assert.equal(loads,1);
 const oldEpoch=context.window.workspaceEpoch;
 assert.equal(ui.settling(prefix,oldEpoch-1),null);
 const oldToken=ui.settling(prefix,oldEpoch);assert.equal(typeof oldToken,'number');
 assert.equal(ui.cachePhase(),'settling');
 ui.refreshed(prefix,oldEpoch-1,oldToken);assert.equal(ui.canMutate(),false);
 run("error('fresh read failed')");assert.equal(ui.cachePhase(),'failed');
 assert.equal(context.window.workspaceReady,true);assert.equal(context.drafts.c,'unposted');
 assert.equal(context.streamBoardError,'fresh read failed');assert.equal(ui.canMutate(),false);
 ui.refreshed(prefix,oldEpoch,oldToken);assert.equal(ui.cachePhase(),'failed');
 assert.equal(ui.resume(),true);assert.deepEqual(ticks,[true]);
 ui.refreshed(prefix,oldEpoch,oldToken);assert.equal(ui.cachePhase(),'refreshing');
 const newToken=ui.settling(prefix,oldEpoch);assert.notEqual(newToken,oldToken);
 ui.refreshed(prefix,oldEpoch,oldToken);assert.equal(ui.cachePhase(),'settling');
 ui.refreshed(prefix,oldEpoch,newToken);assert.equal(ui.canMutate(),true);assert.equal(context.streamBoardError,null);
 run("select('two','none')");assert.equal(ui.cachePhase(),null);
 assert.match(context.board.textContent,/Loading Two/);assert.equal(painted.length,1);
 ui.refreshed(prefix,oldEpoch,newToken);assert.equal(ui.selected(),'two');
 context.revisionFloors.set(prefix,6);run("select('one','none')");
 assert.equal(ui.cachePhase(),null);assert.equal(painted.length,1);
 assert.match(context.board.textContent,/Loading One/);
 assert.ok(captured.includes('one'));assert.deepEqual(streams,['one','two','one']);
 // A current retained return restores that board's status immediately after selection reset.
 context.revisionFloors.delete(prefix);
 run("recentBoards.remember(API_PREFIX,{revision:6,sync:{boardIdentity:'one-id',contentVersion:'one-v'}})");
 run("select('two','none')");
 run("metadata({type:'connection',connected:true,generation:1});metadata({type:'watch',generation:2,value:{after:10}})");
 run("metadata({type:'board',generation:2,value:{board:'one',status:{ok:true,revision:6,boardIdentity:'one-id',contentVersion:'one-v',sampledAt:11,mtime:123,build:'one-build',codeStale:true,restarting:true,restartProblem:'one-status',push:{state:'failed'}}}})");
 const beforeReturn=ticks.length;
 run("select('one','none')");
 assert.equal(ticks.length,beforeReturn);assert.equal(context.fileMs,123000);
 assert.equal(context.serverBuild,'one-build');assert.equal(context.codeStale,true);
 assert.equal(context.restarting,true);assert.equal(context.restartProblem,'one-status');
 assert.equal(context.pushState.state,'failed');

}
process.stdout.write('ok');
"""


@pytest.mark.parametrize("mode", ["bounds", "navigation", "freshness", "scheduler"])
def test_recent_board_cache(mode: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    workspace = (
        pathlib.Path(api_endpoint.__file__).with_name("workspace.js").read_text(encoding="utf-8")
    )
    result = subprocess.run(
        [node, "-e", DRIVER],
        input=json.dumps({"workspace": workspace, "page": api_endpoint.PAGE, "mode": mode}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"
