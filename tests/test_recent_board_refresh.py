"""Execute the shipped cache/write and refresh boundaries under controlled races."""

import json
import pathlib
import shutil
import subprocess

import pytest

from localswim import api_endpoint

DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const copy=x=>JSON.parse(JSON.stringify(x));
const snapshot={revision:12,lanes:[{state:'backlog',items:[{id:'one',subject:'Saved',
 cardVersion:'a'.repeat(64),state:'backlog',comments:[],history:[],links:[]}]}]};
const calls=[],stored=[],paints=[],events=[];
let phase='refreshing',release,finish,readResult=copy(snapshot);
const elements=new Map();
const el=id=>{if(!elements.has(id))elements.set(id,{textContent:'',dataset:{},classList:{values:new Set(),
 toggle(name,on){if(on)this.values.add(name);else this.values.delete(name)},contains(name){return this.values.has(name)}},addEventListener(){}});return elements.get(id)};
const context=vm.createContext({console,AbortController,AbortSignal,queueMicrotask,Date,
 crypto:require('crypto').webcrypto,setTimeout,clearTimeout,
 window:{workspaceEpoch:3,workspaceReady:true,localswimWorkspace:true,WorkspaceUI:{
  canMutate:()=>phase===null,cachePhase:()=>phase,cachePending:()=>phase!==null,
  pageStatus:()=>({}),rememberSnapshot(...args){events.push(['remember',...args])},
  loaded(){events.push(['loaded'])},settling(){phase='settling';return 7},
  refreshed(prefix,epoch,token){events.push(['refreshed',prefix,epoch,token]);phase=null},
  error(message){events.push(['error',message]);phase='failed'},reconcile(){}}},
 API_PREFIX:'/one',API_TOKEN:'synthetic',seen:'old',authoritativeData:copy(snapshot),data:copy(snapshot),
 HAY:{clear(){}},paint(...args){paints.push(args)},restoreStaleReloadState(){},
 renderBoardBanner(){},captureCacheCards:()=>new Map([['one',{}]]),
 settleCacheCards:()=>new Promise(resolve=>finish=resolve),
 sessionStorage:{getItem:()=>null,setItem(key,value){stored.push(JSON.parse(value))}},
 document:{getElementById:el},toast(){},tick(){},
 apiGet:async()=>readResult,
 fetch:async(path,options)=>{calls.push({path,options});if(calls.length===1)await new Promise(resolve=>release=resolve);
  return {ok:true,status:200,json:async()=>({revision:13,cardVersion:'b'.repeat(64)})}},
 lastOk:0,fileMs:0,streamConnected:true,streamClosedAt:null,streamBoardError:null,pageOpenedAt:0,
 serverBuild:null,codeStale:false,restarting:false,restartProblem:null,pushState:null,
 PAGE_BUILD:'test',STREAM_GRACE_MS:10000,clockOf:()=>['12',':34'],agoOf:()=>[1,'second'],
 reloadForServerBuild:()=>false,clearStaleReloadMarker(){}});
const run=code=>vm.runInContext(code,context);
const section=(start,end,source=input.page)=>{const a=source.indexOf(start),b=source.indexOf(end,a);
 assert.ok(a>=0&&b>a,`Missing section ${start}`);run(source.slice(a,b));};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 if(input.mode.startsWith('write')){
  section('// Pending browser intent','\nfunction acceptRevision(');
  if(input.mode==='write-cached'){
   const body={subject:'Unsaved draft',state:'backlog'};context.body=body;
   const result=await run("apiPost('/one/cards',body)");
   assert.equal(result.status,409);assert.match((await result.json()).error,/draft is preserved/);
   assert.deepEqual(body,{subject:'Unsaved draft',state:'backlog'});
   assert.equal(stored.length,0);assert.equal(paints.length,0);assert.equal(calls.length,0);
   assert.equal(run('localActions.length'),0);assert.equal(context.data.lanes[0].items[0].subject,'Saved');
  }else{
   phase=null;
   const first=run("apiPost('/one/cards/one/subject',{subject:'First'})");await flush();
   const queued=run("apiPost('/one/cards/one/subject',{subject:'Second'})");
   if(!['write-sameepoch','write-conflict'].includes(input.mode))context.window.workspaceEpoch++;
   context.API_PREFIX=input.mode==='write-other'?'/other':'/one';phase=input.mode==='write-conflict'?null:'refreshing';
   readResult={...copy(snapshot),revision:13};
   readResult.lanes[0].items[0].cardVersion=(input.mode==='write-conflict'?'c':'b').repeat(64);
   readResult.lanes[0].items[0].subject='First';release();await first;
   const result=await queued;
   const refused=['write-sameepoch','write-conflict'].includes(input.mode);
   assert.equal(calls.length,refused?1:2);assert.equal(result.ok,!refused);
   if(refused)assert.equal(run('localActions.length'),0);
   if(!refused)assert.equal(calls[1].options.headers['If-Match-Card'],'"'+'b'.repeat(64)+'"');
   assert.ok(calls.every(call=>call.path.startsWith('/one/')));
  }
 }else if(input.mode==='direct-writes'){
  section('function cachedWriteRefusal()', 'async function apiPost(');
  section('// ---- lane review approval','// ---- end lane review approval');
  section('async function applyTicketOrder()',"document.getElementById('ticket-order-apply')");
  context.lane={state:'ready_for_review',items:[{id:'one',cardVersion:'a'.repeat(64)}]};context.button={disabled:false};
  await run("approveLane(lane,'/one',3,12,1,button)");await run('applyTicketOrder()');
  assert.equal(calls.length,0);assert.equal(context.button.disabled,false);
  assert.equal(run('pendingLaneApprovals.size'),0);
 }else if(input.mode==='startup-status'){
  let now=100000;context.Date={now:()=>now};
  const initialStart=input.page.indexOf('const pageOpenedAt =');assert.ok(initialStart>=0);
  run(input.page.slice(initialStart,input.page.indexOf('\n',input.page.indexOf('let streamConnected =',initialStart))));
  section('function renderLive()', '// Push-driven snapshots');
  run('renderLive()');assert.equal(el('dot').classList.contains('alive'),true);
  phase=null;run('renderLive()');assert.equal(el('badge').textContent,'RECONNECTING');
  now+=9999;run('renderLive()');assert.equal(el('dot').classList.contains('alive'),true);
  now++;run('renderLive()');assert.equal(el('dot').classList.contains('stale'),true);
  run('streamConnected=true;streamClosedAt=null');now+=60000;run('renderLive()');
  assert.equal(el('dot').classList.contains('alive'),true);
 }else if(input.mode==='status'){
  section('function renderLive()', '// Push-driven snapshots');
  for(const [state,label] of [['refreshing','REFRESHING'],['settling','REFRESHING'],['failed','REFRESH FAILED'],[null,'CONNECTED']]){
   phase=state;run('renderLive()');assert.equal(el('badge').textContent,label);
   assert.equal(el('badge').className,state==='failed'?'warn':state?'refreshing':'');
   assert.equal(el('dot').classList.contains('alive'),true,'board loading must not signal a lost stream');
   assert.equal(el('dot').classList.contains('stale'),false);
  }
  phase='refreshing';context.streamConnected=false;context.streamClosedAt=Date.now()-5000;
  run('renderLive()');assert.equal(el('dot').classList.contains('alive'),true,'intentional reconnect keeps existing grace');
  context.streamClosedAt=Date.now()-15000;run('renderLive()');
  assert.equal(el('dot').classList.contains('stale'),true,'a real stream outage remains visible');
  assert.equal(el('badge').textContent,'REFRESHING');
  context.streamConnected=true;context.streamClosedAt=null;run('renderLive()');
  assert.equal(el('dot').classList.contains('alive'),true);
 }else if(input.mode.startsWith('tick')){
  section('// Pending browser intent','\nfunction acceptRevision(');
  section('function renderLive()', '// Push-driven snapshots');
  section('let pollingEpoch =', '\nfunction streamMessage(');
  if(input.mode==='tick-error')readResult={error:'Read failed'};
  if(input.mode==='tick-obsolete')readResult={...copy(snapshot),revision:11};
  if(input.mode==='tick-navigation')context.apiGet=()=>new Promise(resolve=>release=resolve);
  const pending=run('tick(true,{contentVersion:"new",mtime:42})');await flush();
  if(input.mode==='tick-navigation'){
   context.window.workspaceEpoch++;context.API_PREFIX='/two';release(readResult);await pending;
   assert.equal(events.length,0);assert.equal(paints.length,0);assert.equal(context.lastOk,0);
  }else if(input.mode==='tick-fresh'){
   assert.equal(phase,'settling');assert.equal(el('badge').textContent,'REFRESHING');
   assert.ok(context.lastOk>0);assert.equal(context.seen,'new');
   assert.equal(events.filter(e=>e[0]==='refreshed').length,0);
   assert.deepEqual(paints,[[false]]);finish();await pending;
   assert.equal(phase,null);assert.equal(el('badge').textContent,'CONNECTED');
   assert.equal(events.filter(e=>e[0]==='refreshed').length,1);
  }else{
   await pending;assert.equal(context.lastOk,0);assert.equal(context.seen,'old');
   assert.equal(paints.length,0);assert.equal(events.filter(e=>e[0]==='remember').length,0);
   assert.equal(events.filter(e=>e[0]==='refreshed').length,0);
   assert.equal(phase,'failed');
  }
 }else if(input.mode==='settlement-race'){
  section('  let cachedView =','  const categories =',input.workspace);
  run("cachedView={prefix:'/one',phase:'refreshing'};globalThis.old=settling('/one',3)");
  context.window.workspaceEpoch=4;
  run("cachedView={prefix:'/one',phase:'refreshing'};globalThis.fresh=settling('/one',4);refreshed('/one',3,old)");
  assert.equal(run('cachedView.phase'),'settling');
  run("refreshed('/one',4,old)");assert.equal(run('cachedView.phase'),'settling');
  run("refreshed('/other',4,fresh)");assert.equal(run('cachedView.phase'),'settling');
  run("refreshed('/one',4,fresh)");assert.equal(run('cachedView'),null);
 }else if(input.mode.startsWith('animation')){
  const animations=[],overlays=[];let timer;
  context.setTimeout=fn=>{timer=fn;return 1};context.clearTimeout=()=>{};
  const rect=(left,top,width=100,height=80)=>({left,top,right:left+width,bottom:top+height,width,height});
  const sourceLane={getBoundingClientRect:()=>rect(20,80,180,420)};
  const targetLane={getBoundingClientRect:()=>rect(300,80,300,420)};
  const card=(id,left,top,lane=sourceLane)=>({dataset:{id},style:{visibility:''},attrs:{},
   getBoundingClientRect:()=>rect(left,top),closest:()=>lane,
   cloneNode(){return card(id,left,top,lane)},removeAttribute(name){delete this.attrs[name]},
   setAttribute(name,value){this.attrs[name]=value},remove(){this.removed=true},
   animate(frames,options){let resolve;const a={target:this,frames,options,cancelled:false,finished:new Promise(r=>resolve=r),
    resolve:()=>resolve(),cancel(){this.cancelled=true;resolve()}};animations.push(a);return a}});
  const old=card('gone',10,60),source=card('moved',30,100);
  const moved=card('moved',330,120,targetLane),added=card('added',450,160,targetLane);
  const board=el('board');board.getBoundingClientRect=()=>rect(20,30,680,520);
  context.window.innerWidth=800;context.window.innerHeight=600;
  let cards=[source,old];
  context.document.querySelectorAll=selector=>selector==='#board .cards'?[sourceLane,targetLane]:cards;
  context.document.body={append:overlay=>overlays.push(overlay)};
  context.document.createElement=()=>({style:{},attrs:{},children:[],
   setAttribute(name,value){this.attrs[name]=value},append(child){this.children.push(child)},remove(){this.removed=true}});
  context.window.matchMedia=()=>({matches:input.mode==='animation-reduced'});
  section('let activeCacheTransition =','// **A card can ask');
  run('globalThis.before=captureCacheCards()');cards=[moved,added];
  const pending=run('settleCacheCards(before)');await flush();
  if(input.mode==='animation-reduced'){assert.equal(animations.length,0);assert.equal(overlays.length,0)}
  else{
   assert.equal(animations.length,3);assert.equal(overlays.length,1);
   const overlay=overlays[0];assert.equal(overlay.inert,true);assert.equal(overlay.attrs['aria-hidden'],'true');
   assert.equal(overlay.children.length,2);assert.equal(overlay.style.clipPath,'inset(80px 100px 50px 20px)');
   assert.equal(overlay.removed,undefined);assert.equal(run('activeCacheTransition===null'),false);
   assert.equal(animations[0].target,overlay.children[0]);assert.notEqual(animations[0].target,moved);
   assert.equal(animations[0].frames[0].transform,'translate(0,0)');
   assert.equal(animations[0].frames[1].transform,'translate(300px,20px)');
   assert.equal(overlay.children[1].style.clipPath,'inset(20px 0px 0px 10px)');
   assert.equal(moved.style.visibility,'hidden');
   if(input.mode==='animation-timeout')timer();
   else if(input.mode==='animation-cancel')run('cancelCacheTransition()');
   else animations.forEach(a=>a.resolve());
  }
  await pending;assert.equal(run('activeCacheTransition'),null);
  assert.ok(animations.every(a=>a.cancelled));if(overlays.length)assert.equal(overlays[0].removed,true);
  assert.equal(moved.style.visibility,'');
 }
 process.stdout.write('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""


@pytest.mark.parametrize(
    "mode",
    [
        "write-cached",
        "write-return",
        "write-sameepoch",
        "write-conflict",
        "write-other",
        "direct-writes",
        "status",
        "startup-status",
        "tick-fresh",
        "tick-error",
        "tick-obsolete",
        "tick-navigation",
        "settlement-race",
        "animation-finished",
        "animation-timeout",
        "animation-cancel",
        "animation-reduced",
    ],
)
def test_cached_board_refresh_boundary(mode: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    result = subprocess.run(
        [node, "-e", DRIVER],
        input=json.dumps(
            {
                "page": api_endpoint.PAGE,
                "workspace": pathlib.Path(api_endpoint.__file__)
                .with_name("workspace.js")
                .read_text(encoding="utf-8"),
                "mode": mode,
            }
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"
