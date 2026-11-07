"""Execute shipped optimistic/recovery code against deterministic transport schedules."""

import json
import pathlib
import shutil
import subprocess

import pytest

from localswim import api_endpoint

DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const item={id:'one',cardVersion:'a'.repeat(64),subject:'Before',detailRaw:'Before',
  state:'backlog',archived:input.mode==='unarchive',priority:'P3',owner:'bot',ticket:'#0001',comments:[],history:[],links:[]};
const initial={revision:1,lanes:[{state:'backlog',items:[item]},{state:'doing',items:[]}]};
const copy=value=>JSON.parse(JSON.stringify(value));
let server=copy(initial),calls=[],reads=0,release,paints=[],stored=[];
const context=vm.createContext({console,AbortSignal,crypto:require('crypto').webcrypto,
 sessionStorage:{getItem(){return input.mode.startsWith('reload') ? JSON.stringify([{id:'logical',prefix:'/board',path:'/board/cards/one/subject',kind:'subject',field:'subject',cardId:'one',cardVersion:'a'.repeat(64),body:{subject:'After'},phase:'pending'}]):null},setItem(key,value){stored=JSON.parse(value)}},
 window:{workspaceEpoch:1},API_PREFIX:'/board',API_TOKEN:'synthetic',seen:1,
 authoritativeData:copy(initial),data:copy(initial),HAY:{clear(){}},
 paint(){paints.push(copy(context.data))},tick(){},toast(){},
 async apiGet(){reads++;if(input.mode==='staleread')return copy(initial);if(input.mode.startsWith('later'))throw Error('read unavailable');return copy(server)},
 async fetch(path,options){
  const body=JSON.parse(options.body); calls.push({path,body,headers:options.headers});
  const response=(status,out)=>({ok:status===200,status,json:async()=>out});
  const commit=()=>{
   if(path.endsWith('/cards'))server.lanes[0].items.push({...item,...body,cardVersion:'b'.repeat(64)});
   else {server.lanes[0].items[0].subject=body.subject || 'Before';server.lanes[0].items[0].cardVersion='b'.repeat(64)}
   if(path.endsWith('/unarchive'))server.lanes[0].items[0].archived=false;
   server.revision++; return response(200,{revision:server.revision,cardVersion:'b'.repeat(64),result:'accepted'});
  };
  if(input.mode==='slow')await new Promise(resolve=>release=resolve);
  if(input.mode==='refusal')return response(403,{error:'refused'});
  if(input.mode.startsWith('queued') && calls.length===1)await new Promise(resolve=>release=resolve);
  if(input.mode==='knownsame'){server.revision++;server.lanes[0].items[0].subject='After';server.lanes[0].items[0].cardVersion='c'.repeat(64);return response(412,{code:'board_revision_conflict'})}
  if(input.mode==='cardconflict')return response(412,{code:'card_revision_conflict',error:'conflict'});
  if(['boardretry','unarchive'].includes(input.mode) && calls.length===1){server.revision++;return response(412,{code:'board_revision_conflict'})}
  if(input.mode==='changed' && calls.length===1){server.revision++;server.lanes[0].items[0].cardVersion='c'.repeat(64);return response(412,{code:'board_revision_conflict'})}
  if(input.mode.startsWith('later')){commit();throw Error('lost response')}
  if(input.mode==='lostcommit'){commit();throw Error('lost response')}
  if(input.mode==='gatewaylate412scalar'){if(calls.length===1)return response(502,{error:'outcome unknown'});commit();return response(412,{code:'card_revision_conflict'})}
  if(input.mode==='gatewayrefusedscalar'){if(calls.length===1)return response(502,{error:'outcome unknown'});return response(403,{error:'refused'})}
  if(input.mode==='gatewaylate409add'){if(calls.length===1)return response(502,{error:'outcome unknown'});commit();return response(409,{error:'ID already exists'})}
  if(input.mode==='gatewayrefusedadd'){if(calls.length===1)return response(502,{error:'outcome unknown'});return response(409,{error:'refused'})}
  if(input.mode==='gateway502comment')return response(502,{error:'outcome unknown'});
  if(input.mode==='gateway502add'){commit();return response(502,{error:'board command outcome is unknown; inspect board before retrying'})}
  if(input.mode==='commentlost')throw Error('lost response');
  if(input.mode==='addretry' && calls.length===1)throw Error('lost request');
  if(input.mode==='navigation') {await new Promise(resolve=>release=resolve);}
  const result=commit();
  if(input.mode==='queuedforeign'){server.revision++;server.lanes[0].items[0].cardVersion='c'.repeat(64)}
  return result;
 }});
const start=input.page.indexOf('// Pending browser intent'),end=input.page.indexOf('\nfunction acceptRevision(',start);
vm.runInContext(input.page.slice(start,end),context);
(async()=>{
 if(input.mode.startsWith('reload')){
  if(input.mode==='reloadconfirmed'){server.lanes[0].items[0].subject='After';server.lanes[0].items[0].cardVersion='b'.repeat(64);}
  context.latest=server;vm.runInContext('acknowledgeSnapshot(latest,API_PREFIX);repaintActions()',context);
  assert.equal(stored.length,input.mode==='reloadconfirmed'?0:1);
  assert.equal(context.data.lanes[0].items[0].subject,input.mode==='reloadconfirmed'?'After':'Before');
  assert.equal(calls.length,0);process.stdout.write(JSON.stringify({ok:true}));return;
 }
 const add=['addretry','gateway502add','gatewaylate409add','gatewayrefusedadd'].includes(input.mode),comment=['commentlost','gateway502comment'].includes(input.mode);
 const path=add?'/board/cards':comment?'/board/cards/one/comment':input.mode==='unarchive'?'/board/cards/one/unarchive':'/board/cards/one/subject';
 const body=add?{subject:'Added',state:'backlog',priority:'P3',owner:'bot',detail:''}:comment?{text:'Draft'}:{subject:'After'};
 context.path=path;context.body=body;
 const pending=vm.runInContext('apiPost(path,body)',context);
 assert.equal(paints.length,1,'optimistic projection precedes transport');
 if(!comment&&!add&&input.mode!=='unarchive')assert.equal(paints[0].lanes[0].items[0].subject,'After');
 if(input.mode!=='unarchive')assert.equal(paints[0].lanes[0].items[add?1:0].localPending,'Saving…');
 await Promise.resolve();await Promise.resolve();
 if(input.mode==='slow'){assert.equal(calls.length,1);release();}
 if(input.mode==='navigation'){context.window.workspaceEpoch++;context.API_PREFIX='/other';context.data={revision:99,lanes:[]};context.authoritativeData=null;release();}
 let second;
 if(input.mode.startsWith('queued')){second=vm.runInContext("apiPost('/board/cards/one/subject',{subject:'Later'})",context);release();}
 const result=await pending,out=await result.json();
 if(second){const next=await second;assert.equal(next.ok,input.mode==='queuedown');}
 if(input.mode==='staleread'){assert.equal(result.ok,true);assert.equal(stored.length,1);assert.equal(context.data.lanes[0].items[0].subject,'After');}
 else if(input.mode.startsWith('later')){
  assert.equal(result.ok,false);assert.equal(stored.length,1);
  if(input.mode==='laterconflict'){server.lanes[0].items[0].subject='Other';server.lanes[0].items[0].cardVersion='c'.repeat(64);}
  context.latest=copy(server);vm.runInContext('acknowledgeSnapshot(latest,API_PREFIX);repaintActions()',context);
  assert.equal(stored.length,0);
  assert.equal(context.data.lanes[0].items[0].subject,input.mode==='laterconflict'?'Other':'After');
 }else if(['refusal','cardconflict','changed','knownsame'].includes(input.mode))assert.equal(result.ok,false);
 else if(['commentlost','gateway502comment','gatewayrefusedadd','gatewayrefusedscalar'].includes(input.mode)){
  assert.equal(result.ok,false);const sent=input.mode.startsWith('gatewayrefused')?2:1;assert.equal(calls.length,sent);assert.equal(stored[0].phase,'unknown');
  const duplicate=await vm.runInContext('apiPost(path,body)',context);assert.equal(duplicate.ok,false);assert.equal(calls.length,sent);
 }else assert.equal(result.ok,true);
 assert.equal(calls.length,['boardretry','addretry','queuedown','unarchive','gatewaylate409add','gatewayrefusedadd','gatewaylate412scalar','gatewayrefusedscalar'].includes(input.mode)?2:1);
 if(input.mode==='addretry')assert.equal(calls[0].body.id,calls[1].body.id);
 if(input.mode==='boardretry')assert.equal(calls[0].headers['If-Match-Card'],calls[1].headers['If-Match-Card']);
 if(input.mode==='navigation'){assert.equal(out.workspaceBoardChanged,true);assert.equal(context.data.revision,99);}
 process.stdout.write(JSON.stringify({ok:true}));
})().catch(error=>{console.error(error);process.exitCode=1});
"""


@pytest.mark.parametrize(
    "mode",
    [
        "slow",
        "unarchive",
        "refusal",
        "cardconflict",
        "boardretry",
        "changed",
        "lostcommit",
        "addretry",
        "gateway502add",
        "gateway502comment",
        "gatewaylate409add",
        "gatewayrefusedadd",
        "gatewaylate412scalar",
        "gatewayrefusedscalar",
        "commentlost",
        "navigation",
        "knownsame",
        "queuedown",
        "queuedforeign",
        "laterpoll",
        "laterconflict",
        "staleread",
        "reloadconfirmed",
        "reloadunresolved",
    ],
)
def test_optimistic_intents_recover_without_blind_replay(mode: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    result = subprocess.run(
        [node, "-e", DRIVER],
        input=json.dumps({"page": api_endpoint.PAGE, "mode": mode}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


POLL_DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));let calls=0,resolveStatus;
const context=vm.createContext({AbortController,AbortSignal,queueMicrotask,Date,
 window:{workspaceEpoch:1},API_PREFIX:'/one',pollingEpoch:null,pollingController:null,pollingQueued:false,
 lastOk:0,seen:0,fileMs:0,data:{},authoritativeData:null,
 serverBuild:null,codeStale:false,restarting:false,restartProblem:null,pushState:null,
 HAY:{clear(){}},paint(){},restoreStaleReloadState(){},renderLive(){},
 acknowledgeSnapshot(value){context.authoritativeData=value;return true},projectActions(value){return value},
 async apiGet(path,signal){calls++;
  if(input.mode==='overlap')return await new Promise(resolve=>resolveStatus=resolve);
  if(input.mode==='digest')return path.endsWith('/status')?{ok:true,readAt:123,mtime:0,contentVersion:'changed-digest'}:{revision:1,lanes:[]};
  if(path.endsWith('/status'))return {ok:input.mode!=='failedstatus',readAt:123,mtime:1};
  throw Error('render read failed');
 }});
const start=input.page.indexOf('async function tick('),end=input.page.indexOf('\ntick();',start);
vm.runInContext(input.page.slice(start,end),context);
(async()=>{
 const first=vm.runInContext('tick()',context);
 if(input.mode==='overlap'){
  await vm.runInContext('tick()',context);assert.equal(calls,1);
  resolveStatus({ok:true,readAt:123,mtime:0});await first;assert.equal(calls,1);
 }else{await first;assert.equal(context.lastOk,input.mode==='failedstatus'?0:123000);
  if(input.mode==='digest'){assert.equal(calls,2);assert.equal(context.seen,'changed-digest');}}
 process.stdout.write('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""


@pytest.mark.parametrize("mode", ["overlap", "failedstatus", "freshread_failedrender", "digest"])
def test_polling_is_bounded_and_file_freshness_is_independent(mode: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    result = subprocess.run(
        [node, "-e", POLL_DRIVER],
        input=json.dumps({"page": api_endpoint.PAGE, "mode": mode}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


DRAFT_DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const field={value:'preserved draft'};
const context=vm.createContext({current:'alpha',pending:null,openId:'one',drafts:{},
 editing:input.kind,subjectEdit:{cardVersion:'original'},detailEditVersion:'original',
 query:'',unhideOld:false,relationshipDraft:null,mkOpen:null,state:{records:{}},
 board:{querySelectorAll(){return []}},data:{},document:{getElementById(){return field},querySelector(){return null}},
 itemById(){return {id:'one'}},openCard(){},paintRelations(){},restoreScroll(){},
 startSubjectEdit(){context.subjectEdit={cardVersion:'newer'}},
 startDetailEdit(){context.detailEditVersion='newer'}});
function section(start,end){const a=input.source.indexOf(start);return input.source.slice(a,input.source.indexOf(end,a));}
vm.runInContext(section('  function capture()', '  function persist()')+
 section('  function loaded()', '  function error('),context);
vm.runInContext('capture();pending=state.records.alpha;loaded()',context);
assert.equal(context.state.records.alpha.editCardVersion,'original');
assert.equal(input.kind==='subject'?context.subjectEdit.cardVersion:context.detailEditVersion,'original');
assert.equal(field.value,'preserved draft');process.stdout.write('ok');
"""


@pytest.mark.parametrize("kind", ["subject", "detail"])
def test_workspace_restores_original_edit_token_with_draft(kind: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    source = (
        pathlib.Path(api_endpoint.__file__).with_name("workspace.js").read_text(encoding="utf-8")
    )
    result = subprocess.run(
        [node, "-e", DRAFT_DRIVER],
        input=json.dumps({"source": source, "kind": kind}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


PRIORITY_DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8')),handlers={},calls=[];let release;
const field={value:'P2',addEventListener(name,handler){handlers[name]=handler}};
const cards={a:{cardVersion:'a-token'},b:{cardVersion:'b-token'}};
const context=vm.createContext({openId:'a',API_PREFIX:'/board',data:{revision:1},
 document:{getElementById(){return field}},itemById:id=>cards[id],acceptRevision(){},toast(){},
 async apiPost(path,body,version){calls.push(version);
  if(calls.length===1)await new Promise(resolve=>release=resolve);
  return {ok:true,json:async()=>({cardVersion:'committed-a',revision:2})};}});
const start=input.page.indexOf('let priorityEditVersion'),end=input.page.indexOf('// **Enter POSTS here',start);
vm.runInContext(input.page.slice(start,end),context);
(async()=>{
 handlers.focus();const first=handlers.change({target:field});
 context.openId='b';handlers.focus();release();await first;
 await handlers.change({target:field});
 assert.deepEqual(calls,['a-token','b-token']);process.stdout.write('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""


def test_priority_completion_preserves_another_cards_focus_token() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    result = subprocess.run(
        [node, "-e", PRIORITY_DRIVER],
        input=json.dumps({"page": api_endpoint.PAGE}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


MAKE_DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));let resolveResponse,closed=0;
const fields={'mk-subject':{value:'Subject'},'mk-detail':{value:'Detail'},'mk-priority':{value:'P3'},'mk-owner':{value:'bot'},'mk-go':{disabled:false}};
let links=[];
const context=vm.createContext({crypto:require('crypto').webcrypto,mkAttempt:null,mkOpen:'backlog',window:{workspaceEpoch:1},API_PREFIX:'/board',
 document:{getElementById:id=>fields[id]},relationshipSpecs:()=>links,toast(){},acceptRevision(){},
 closeMake(){closed++},apiPost:async()=>await new Promise(resolve=>resolveResponse=resolve)});
const start=input.page.indexOf('function makeDraft()'),end=input.page.indexOf("document.getElementById('mk-go').addEventListener",start);
vm.runInContext(input.page.slice(start,end),context);
(async()=>{
 const pending=vm.runInContext('submitMake()',context);
 if(input.mode==='owner')fields['mk-owner'].value='human';
 if(input.mode==='priority')fields['mk-priority'].value='P2';
 if(input.mode==='links')links=[{kind:'blocks',other:'two'}];
 resolveResponse({ok:true,json:async()=>({revision:2,result:'saved'})});await pending;
 assert.equal(closed,input.mode==='unchanged'?1:0);process.stdout.write('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""


@pytest.mark.parametrize("mode", ["unchanged", "owner", "priority", "links"])
def test_add_completion_preserves_changes_to_all_draft_fields(mode: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    result = subprocess.run(
        [node, "-e", MAKE_DRIVER],
        input=json.dumps({"page": api_endpoint.PAGE, "mode": mode}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


MAKE_RECOVERY_DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));let calls=0,closed=0,posted;
const fields={'mk-subject':{value:'Subject'},'mk-detail':{value:'Detail'},'mk-priority':{value:'P3'},'mk-owner':{value:'bot'},'mk-go':{disabled:false}};
const initial={revision:1,lanes:[{state:'backlog',items:[]}]};
const context=vm.createContext({AbortSignal,crypto:require('crypto').webcrypto,
 mkOpen:'backlog',mkAttempt:null,window:{workspaceEpoch:1},API_PREFIX:'/board',API_TOKEN:'synthetic',
 data:initial,authoritativeData:initial,HAY:{clear(){}},paint(){},tick(){},toast(){},
 sessionStorage:{getItem(){return null},setItem(){}},
 document:{getElementById:id=>fields[id]},relationshipSpecs:()=>[],
 closeMake(){closed++;context.mkOpen=null;context.mkAttempt=null},
 async apiGet(){throw Error('read unavailable')},
 async fetch(path,options){calls++;posted=JSON.parse(options.body);return {ok:false,status:502,
  json:async()=>({error:'board command outcome is unknown; inspect board before retrying'})};}
});
const helpers=input.page.slice(input.page.indexOf('// Pending browser intent'),input.page.indexOf('// **The repaint counter'));
const make=input.page.slice(input.page.indexOf('function restoreMakeAttempt('),input.page.indexOf("document.getElementById('mk-go').addEventListener"));
vm.runInContext(helpers+make,context);
(async()=>{
 await vm.runInContext('submitMake()',context);
 assert.equal(closed,0);assert.equal(calls,1);assert.equal(context.mkOpen,'backlog');
 if(input.mode.includes('changed'))fields['mk-owner'].value='human';
 const saved=JSON.parse(JSON.stringify(context.mkAttempt));
 if(input.mode.startsWith('restored')){context.mkOpen=null;context.mkAttempt=null;}
 context.latest={revision:2,lanes:[{state:'backlog',items:[{...posted,ticket:'#0001',comments:[]}]}]};
 vm.runInContext('acknowledgeSnapshot(latest,API_PREFIX)',context);
 if(input.mode.startsWith('restored')){context.mkOpen='backlog';context.saved=saved;vm.runInContext('restoreMakeAttempt(saved)',context);}
 assert.equal(closed,input.mode.includes('changed')?0:1);
 if(context.mkOpen)await vm.runInContext('submitMake()',context);
 assert.equal(calls,1,'same Add form cannot create another logical card after late confirmation');
 if(input.mode.includes('changed'))assert.equal(fields['mk-owner'].value,'human');
 process.stdout.write('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""


@pytest.mark.parametrize("mode", ["unchanged", "changed", "restored", "restoredchanged"])
def test_late_add_confirmation_resolves_original_form_without_duplicate(mode: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    result = subprocess.run(
        [node, "-e", MAKE_RECOVERY_DRIVER],
        input=json.dumps({"page": api_endpoint.PAGE, "mode": mode}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"
