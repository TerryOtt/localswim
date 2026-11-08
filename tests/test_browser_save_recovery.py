"""Exercise shipped save-recovery code with persisted intent and bounded transport."""

import json
import shutil
import subprocess

import pytest

from localswim import api_endpoint

DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const copy=value=>JSON.parse(JSON.stringify(value));
const card={id:'one',ticket:'#0001',subject:'Before',detailRaw:'Before',state:'completed',
 cardVersion:'a'.repeat(64),comments:[],history:[],links:[],archived:false};
const board={revision:4,lanes:[{state:'completed',items:[card]}]};
const saved=(kind,prefix='/board')=>({id:'intent-'+prefix,prefix,path:prefix+'/cards/one/'+kind,
 kind,cardId:'one',cardVersion:card.cardVersion,body:kind==='comment'?{text:'Submitted comment'}:{},phase:'pending'});
let persisted=[],calls=[],reads=0,release;
function runtime(initial) {
 const nodes=new Map(),notices=[],opened=[];
 function element(tag) {return {tag,children:[],textContent:'',value:'',hidden:false,disabled:false,
  classList:{remove(){}},append(...items){for(const item of items){item.parent=this;this.children.push(item)}},
  replaceChildren(){this.children=[]},setAttribute(){},addEventListener(name,fn){this[name]=fn},
  remove(){this.parent.children=this.parent.children.filter(item=>item!==this)},
  showModal(){this.open=true},close(){this.open=false}};}
 const el=id=>{if(!nodes.has(id))nodes.set(id,element(id));return nodes.get(id)};
 const context=vm.createContext({console,AbortSignal,crypto:require('crypto').webcrypto,
  sessionStorage:{getItem(){return JSON.stringify(initial)},setItem(key,value){persisted=JSON.parse(value)}},
  document:{getElementById:el,createElement:element,createTextNode(text){return {textContent:text}}},window:{workspaceEpoch:1},
  API_PREFIX:'/board',API_TOKEN:'synthetic',seen:4,authoritativeData:copy(board),data:copy(board),
  drafts:{one:'Newer draft'},openId:'one',relationshipDraft:null,HAY:{clear(){}},
  closeEditors(){},restoreCardFocus(){},paint(){},tick(){},
  toast(message,error){notices.push({message,error})},openCard(id){opened.push(id)},
  async apiGet(){reads++;if(input.mode.includes('read-failure'))throw Error('read unavailable');return copy(board)},
  async fetch(path,options){
   calls.push({path,body:JSON.parse(options.body),headers:options.headers,signal:options.signal});
   if(input.mode==='close-pending')await new Promise(resolve=>release=resolve);
   if(input.mode==='known-read-failure')return {ok:false,status:412,json:async()=>({code:'board_revision_conflict'})};
   throw Error('response lost');
  }});
 const start=input.page.indexOf('// Pending browser intent'),end=input.page.indexOf('\nfunction acceptRevision(',start);
 assert.ok(start>=0&&end>start);vm.runInContext(input.page.slice(start,end),context);
 const run=code=>vm.runInContext(code,context);
 return {context,el,run,notices,opened};
}
function acknowledgeRow(row) {
 const clear=row.children.at(-1),checkbox=row.children.find(child=>child.tag==='label').children[0];
 assert.equal(clear.disabled,true);checkbox.checked=true;checkbox.change();assert.equal(clear.disabled,false);
}
(async()=>{
 let r=runtime(input.mode==='approval-reload'?[saved('approve')]:input.mode==='comment-dismiss'?
  [saved('comment'),saved('approve','/other')]:[]);
 if(input.mode==='approval-reload'){
  await r.run('reviewUnconfirmedSaves()');
  assert.equal(reads,1);assert.equal(calls.length,0);
  assert.equal(persisted[0].phase,'unknown');
  const row=r.el('save-recovery-items').children[0];
  assert.match(row.children[1].textContent,/Outcome unknown.*completed/);
  assert.equal(r.notices.length,0,'a Completed lane cannot prove this approval committed');
  const blocked=await r.run("apiPost('/board/cards/one/approve',{})");
  assert.equal(blocked.ok,false);assert.equal((await blocked.json()).intentPending,true);assert.equal(calls.length,0);
  row.children.at(-1).click();assert.equal(persisted.length,1,'unchecked acknowledgement cannot clear a warning');
  acknowledgeRow(row);row.children.at(-1).click();
  assert.equal(persisted.length,0);assert.equal(calls.length,0);assert.equal(r.notices.length,0);
  r.run('renderSaveRecovery()');assert.equal(r.el('save-recovery').hidden,true);
  const next=await r.run("apiPost('/board/cards/one/approve',{})");
  assert.equal(next.ok,false);assert.equal(calls.length,1,'only a new explicit submission sends again');
 } else if(input.mode==='comment-dismiss'){
  r.el('say').value='Newer unsent draft';
  await r.run('reviewUnconfirmedSaves()');
  const row=r.el('save-recovery-items').children[0];
  assert.equal(row.children.find(child=>child.tag==='textarea').value,'Submitted comment');
  row.children.at(-1).click();assert.equal(persisted[0].phase,'unknown');
  acknowledgeRow(row);row.children.at(-1).click();
  assert.equal(calls.length,0);assert.equal(r.notices.length,0);
  assert.equal(persisted[0].phase,'dismissed');assert.equal(persisted[0].body.text,'Submitted comment');
  assert.equal(persisted[1].phase,'unknown','another board intent is untouched');
  assert.equal(r.el('say').value,'Newer unsent draft');assert.equal(r.context.drafts.one,'Newer draft');
  r=runtime(copy(persisted));
  r.run('acknowledgeSnapshot(authoritativeData,API_PREFIX);repaintActions();renderSaveRecovery()');
  assert.equal(r.context.data.lanes[0].items[0].localPending,undefined);
  assert.equal(r.context.data.lanes[0].items[0].comments.length,0);
  assert.match(r.el('save-recovery').textContent,/Saved request drafts/);
  await r.run('reviewUnconfirmedSaves()');
  const restored=r.el('save-recovery-items').children[0];
  assert.equal(restored.children.find(child=>child.tag==='textarea').value,'Submitted comment');
  assert.match(restored.children[1].textContent,/Warning cleared/);
  restored.children.at(-1).click();assert.equal(persisted[0].phase,'dismissed','unchecked acknowledgement cannot discard saved text');
  await r.run("apiPost('/board/cards/one/comment',{text:'Submitted comment'})");
  assert.equal(calls.length,1,'dismissed text does not block a deliberate submission');
  assert.equal(persisted.find(action=>action.prefix==='/other').phase,'unknown');
  acknowledgeRow(restored);restored.children.at(-1).click();
  assert.equal(persisted.filter(action=>action.phase==='dismissed').length,0);
  assert.equal(calls.length,1,'discarding saved text never submits a request');
 } else if(input.mode.startsWith('conflict-')){
  const kind=input.mode.slice('conflict-'.length),action=saved(kind);
  action.field=kind;action.body={[kind]:'Submitted '+kind};action.cardVersion='b'.repeat(64);
  r=runtime([action]);r.el('say').value='Newer unsent comment';
  r.context.drafts.one='Newer editor draft';
  await r.run('reviewUnconfirmedSaves()');
  assert.equal(calls.length,0);assert.equal(persisted.length,1);
  assert.equal(persisted[0].phase,'dismissed');
  assert.equal(persisted[0].body[kind],'Submitted '+kind);
  assert.equal(r.context.data.lanes[0].items[0][kind==='detail'?'detailRaw':kind],'Before');
  assert.equal(r.context.data.lanes[0].items[0].localPending,undefined);
  assert.equal(r.context.drafts.one,'Newer editor draft');assert.equal(r.el('say').value,'Newer unsent comment');
  assert.equal(r.el('save-recovery-items').children[0].children.find(child=>child.tag==='textarea').value,'Submitted '+kind);
  r=runtime(copy(persisted));
  await r.run('reviewUnconfirmedSaves()');
  assert.equal(persisted[0].phase,'dismissed');assert.equal(calls.length,0);
  assert.equal(r.context.data.lanes[0].items[0][kind==='detail'?'detailRaw':kind],'Before');
  assert.equal(r.context.data.lanes[0].items[0].localPending,undefined);
  assert.equal(r.el('save-recovery-items').children[0].children.find(child=>child.tag==='textarea').value,'Submitted '+kind);
 } else if(input.mode==='navigation'){
  r=runtime([saved('comment')]);await r.run('reviewUnconfirmedSaves()');
  const row=r.el('save-recovery-items').children[0];
  const open=row.children.find(child=>child.textContent==='Open card');
  open.click();assert.equal(r.opened.length,1);assert.equal(r.opened[0],'one');
  r.el('save-recovery-dialog').showModal();
  r.context.API_PREFIX='/other';r.context.window.workspaceEpoch++;
  open.click();assert.equal(r.opened.length,1,'an old recovery row cannot open a card on the new board');
  assert.equal(r.el('save-recovery-dialog').open,true,'stale open cannot close the current dialog');
  row.children.at(-1).click();
  assert.equal(r.run('localActions[0].phase'),'unknown');assert.equal(calls.length,0);
 } else {
  const path=input.mode==='close-pending'?'/board/cards/one/comment':'/board/cards/one/approve';
  r.context.path=path;
  const pending=r.run("apiPost(path,path.endsWith('/comment')?{text:'Submitted comment'}:{})");
  await new Promise(resolve=>setImmediate(resolve));
  if(input.mode==='close-pending'){
   const start=input.page.indexOf('function closeCard()'),end=input.page.indexOf("\ndocument.getElementById('close')",start);
   vm.runInContext(input.page.slice(start,end),r.context);
   r.el('say').value='Unsent drawer text';r.run('closeCard()');
   assert.equal(r.context.openId,null);assert.equal(r.context.drafts.one,'Unsent drawer text');
   assert.equal(calls[0].signal.aborted,false);release();
  }
  const result=await pending,out=await result.json();
  assert.equal(result.ok,false);assert.equal(calls.length,1);assert.equal(reads,1);
  assert.equal(calls[0].headers.Authorization,'Bearer synthetic');
  assert.equal(calls[0].headers['If-Match-Card'],'"'+card.cardVersion+'"');
  if(input.mode==='known-read-failure'){
   assert.equal(result.status,412);assert.equal(persisted.length,0);
   assert.doesNotMatch(out.error,/unknown|unconfirmed/i);
  } else {
   assert.equal(result.status,0);assert.match(out.error,/unconfirmed/i);assert.equal(persisted[0].phase,'unknown');
  }
 }
 process.stdout.write(JSON.stringify({ok:true}));
})().catch(error=>{console.error(error);process.exitCode=1});
"""


@pytest.mark.parametrize(
    "mode",
    [
        "approval-reload",
        "comment-dismiss",
        "conflict-subject",
        "conflict-detail",
        "navigation",
        "known-read-failure",
        "unknown-read-failure",
        "close-pending",
    ],
)
def test_saved_intent_recovery_requires_explicit_local_dismissal(mode: str) -> None:
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
