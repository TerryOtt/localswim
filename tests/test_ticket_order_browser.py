"""Execute the shipped ticket-order dialog against bounded synthetic transports."""

import json
import pathlib
import shutil
import subprocess

import pytest

from localswim import api_endpoint

DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.events={};this.attrs={};this.dataset={};this.textContent='';this.open=false;this.closed=0;this.parts=new Map();this.classList={remove(){}}}
 append(...children){this.children.push(...children)}
 appendChild(child){this.append(child)}
 replaceChildren(...children){this.children=[...children]}
 addEventListener(event,fn){this.events[event]=fn}
 setAttribute(key,value){this.attrs[key]=value}
 querySelector(key){if(!this.parts.has(key))this.parts.set(key,new Element());return this.parts.get(key)}
 showModal(){this.open=true}
 close(){this.open=false;this.closed++}
}
const nodes=new Map(),el=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id)};
const rows=Array.from({length:input.mode==='paging'?205:2},(_,i)=>({
 id:'stable-'+i,oldTicket:input.mode==='paging'?205-i:2-i,newTicket:i+1,
 subject:i===0?'<img src=x onerror=alert(1)>':'Subject '+i}));
const plan={lane:'ready',cards:rows,selectedCount:rows.length,
 changedCount:rows.filter(row=>row.oldTicket!==row.newTicket).length};
let calls=[],toasts=[],ticks=[],accepted=[],release;
const response=(status,body,bad=false)=>({status,ok:status===200,json:async()=>{
 if(bad)throw Error('unreadable');return body;
}});
const context=vm.createContext({AbortSignal,window:{workspaceEpoch:1},API_PREFIX:'/one',
 API_TOKEN:'synthetic',revisionFloors:new Map(),seen:'old',workspaceCanMutate:()=>true,
 document:{getElementById:el,createElement:tag=>new Element(tag)},
 toast(...args){toasts.push(args)},acceptRevision(out){accepted.push(out)},
 async tick(force){ticks.push(force)},
 async fetch(path,options){
  calls.push({path,method:options.method,headers:options.headers,body:JSON.parse(options.body)});
  if(path.endsWith('/preview')){
   if(input.mode==='preview-navigation'||input.mode==='close-preview'
      ||input.mode.startsWith('close-refresh')&&calls.length===2)await new Promise(resolve=>release=resolve);
   const out={revision:10,applied:false,plan:{...plan,lane:JSON.parse(options.body).lane}};
   if(input.mode==='invalid-preview')out.plan={...plan,changedCount:0};
   return response(200,out);
  }
  if(['pending','navigation','replacement','close-apply'].includes(input.mode))await new Promise(resolve=>release=resolve);
  if(input.mode==='lost')throw Error('lost response');
  if(input.mode==='server-error')return response(502,{error:'server failed'});
  if(input.mode==='bad-success')return response(200,null,true);
  if(input.mode==='invalid-success')return response(200,{revision:12,applied:true,plan});
  if(input.mode.startsWith('refusal'))return response(input.mode==='refusal'?412:403,
    {error:'Refresh required'},input.mode==='refusal-unreadable');
  return response(200,{revision:11,applied:true,plan});
 }});
const begin=input.page.indexOf('// ---- dependency ticket ordering');
const end=input.page.indexOf('// ---- end dependency ticket ordering',begin);
assert.ok(begin>0&&end>begin);vm.runInContext(input.page.slice(begin,end),context);
const run=code=>vm.runInContext(code,context),settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 if(input.mode==='header'){
  const start=input.page.indexOf('function laneEl(lane)');
  const stop=input.page.indexOf('  // **Card #0063. Old finished',start);
  assert.ok(start>0&&stop>start);
  vm.runInContext(input.page.slice(start,stop)+'return el;}',context);
  for(const [enabled,count,archived,expected] of [[false,2,0,false],[true,0,0,true],[true,2,2,true],[true,1,0,true],[true,2,1,true],[true,2,0,false]]){
   context.lane={state:'ready',label:'Ready',ownerLabel:'Human',reorderByDependencies:enabled,
    items:Array.from({length:count},(_,i)=>({archived:i<archived}))};
   const header=run('laneEl(lane)').querySelector('h2');
   assert.equal(header.children.length,enabled?1:0);
   if(enabled){const button=header.children[0];assert.equal(button.disabled,expected);
    assert.equal(button.textContent,'');assert.match(button.innerHTML,/<svg/);
    if(count===archived){assert.equal(button.title,'Disabled; no cards in swimlane');
      assert.equal(button.attrs['aria-label'],button.title);}
    else {assert.match(button.title,/dependencies/);assert.match(button.attrs['aria-label'],/Ready/);}
    assert.equal(button.type,'button');}
  }
 }else{
  const opening=run("openTicketOrder('ready')");
  if(input.mode==='close-preview'){
   await settle();run('closeTicketOrder()');release();await opening;
   assert.equal(el('ticket-order-dialog').open,false);assert.equal(run('ticketOrderPreview'),null);
   assert.equal(calls.length,1);
  }else if(input.mode==='preview-navigation'){await settle();context.API_PREFIX='/two';context.window.workspaceEpoch++;
   el('ticket-order-dialog').open=true;release();await opening;
   assert.equal(el('ticket-order-dialog').closed,0);assert.equal(run('ticketOrderPreview'),null);
  }else{
   await opening;assert.equal(calls.length,1);assert.equal(calls[0].method,'POST');
   assert.deepEqual(calls[0].body,{lane:'ready',cards:null});
   assert.equal(calls[0].headers['If-Match'],undefined);assert.equal(accepted.length,0);
   if(input.mode==='invalid-preview'){assert.equal(run('ticketOrderPreview'),null);assert.equal(el('ticket-order-dialog').open,false)}
   else{
    assert.equal(el('ticket-order-dialog').open,true);
    assert.equal(el('ticket-order-rows').children[0].children[2].textContent,rows[0].subject);
    assert.equal(el('ticket-order-rows').children[0].children[2].children.length,0);
    if(input.mode.startsWith('close-refresh')){
     el('ticket-order-refresh').events.click();await settle();assert.equal(calls.length,2);
     let cancelled=false;
     if(input.mode==='close-refresh-escape')el('ticket-order-dialog').events.cancel({preventDefault(){cancelled=true}});
     else el('ticket-order-close').events.click();
     if(input.mode==='close-refresh-escape')assert.equal(cancelled,true);
     assert.equal(el('ticket-order-dialog').open,false);assert.equal(run('ticketOrderPreview'),null);
     release();await settle();assert.equal(el('ticket-order-dialog').open,false);
     assert.equal(run('ticketOrderPreview'),null);assert.equal(calls.length,2);
    }else if(input.mode==='close-apply'){
     const applying=run('applyTicketOrder()');await settle();el('ticket-order-close').events.click();
     assert.equal(el('ticket-order-dialog').open,false);assert.equal(run('ticketOrderPreview'),null);
     await run("openTicketOrder('ready')");await run('applyTicketOrder()');assert.equal(calls.length,2);
     await run("openTicketOrder('other')");assert.equal(el('ticket-order-dialog').open,true);
     release();await applying;assert.equal(el('ticket-order-dialog').closed,1);
     assert.equal(run('ticketOrderPreview.lane'),'other');assert.equal(accepted.length,0);assert.equal(ticks.length,0);
     assert.equal(context.revisionFloors.get('/one'),11);
     await run("openTicketOrder('ready')");assert.equal(calls.length,4,'settled apply releases the lane guard');
    }else if(input.mode.startsWith('workspace-')){
     Object.assign(context,{current:'one',pending:null,state:{records:{}},catalog:[{key:'two',project:'Two'}],
      persist(){},closeCard(){},closeMake(){},closeEditors(){},route:key=>'/'+key,renderRows(){},
      board:new Element(),HAY:{clear(){}},recoverOnCatalog:false});
     context.window.workspaceReady=true;context.window.BoardStream={select(){}};
     const errorStart=input.workspace.indexOf('  function error(message, recover = false)');
     const errorEnd=input.workspace.indexOf('  function resume()',errorStart);
     const selectStart=input.workspace.indexOf('  function select(key, history =');
     const selectEnd=input.workspace.indexOf('  const boardNames',selectStart);
     assert.ok(errorStart>=0&&errorEnd>errorStart&&selectStart>=0&&selectEnd>selectStart);
     vm.runInContext(input.workspace.slice(errorStart,errorEnd)+input.workspace.slice(selectStart,selectEnd),context);
     run(input.mode==='workspace-select'?"select('two','none')":"error('Synthetic unavailable')");
     assert.equal(el('ticket-order-dialog').open,false);assert.equal(el('ticket-order-dialog').closed,1);
     assert.equal(run('ticketOrderPreview'),null);assert.equal(calls.length,1);
    }else if(input.mode==='paging'){
     assert.equal(el('ticket-order-rows').children.length,100);assert.equal(el('ticket-order-prev').disabled,true);
     el('ticket-order-next').events.click();assert.equal(el('ticket-order-page').textContent,'101–200 of 205');
     el('ticket-order-next').events.click();assert.equal(el('ticket-order-rows').children.length,5);
     assert.equal(el('ticket-order-next').disabled,true);assert.equal(el('ticket-order-prev').disabled,false);
     el('ticket-order-prev').events.click();assert.equal(el('ticket-order-page').textContent,'101–200 of 205');
     assert.equal(calls.length,1);
    }else if(input.mode!=='preview'){
     const applying=run('applyTicketOrder()');await settle();
     if(['pending','navigation','replacement'].includes(input.mode)){
      assert.equal(el('ticket-order-apply').disabled,true);assert.equal(el('ticket-order-refresh').disabled,true);
      await run('applyTicketOrder()');await run("openTicketOrder('ready')");assert.equal(calls.length,2);
      if(input.mode==='navigation'){
       context.API_PREFIX='/two';context.window.workspaceEpoch++;await run("openTicketOrder('ready')");
      }
      if(input.mode==='replacement'){
       await run("openTicketOrder('other')");
      }
      release();
     }
     await applying;
     const applyCalls=calls.filter(call=>call.path.endsWith('/apply'));
     assert.equal(applyCalls.length,1);assert.equal(applyCalls[0].method,'POST');
     assert.equal(applyCalls[0].headers['If-Match'],'"revision-10"');
     assert.deepEqual(applyCalls[0].body,{lane:'ready',cards:rows.map(row=>row.id)});
     if(['navigation','replacement'].includes(input.mode)){
      assert.equal(el('ticket-order-dialog').closed,0);assert.equal(ticks.length,0);assert.equal(accepted.length,0);
      assert.equal(context.revisionFloors.get('/one'),11);
      assert.equal(run('ticketOrderPreview.prefix'),input.mode==='navigation'?'/two':'/one');
      assert.equal(run('ticketOrderPreview.lane'),input.mode==='navigation'?'ready':'other');
      assert.equal(el('ticket-order-apply').disabled,false);
     }else if(input.mode.startsWith('refusal')||['lost','server-error','bad-success','invalid-success'].includes(input.mode)){
      assert.equal(el('ticket-order-dialog').open,true);assert.equal(el('ticket-order-apply').disabled,true);
      assert.equal(el('ticket-order-refresh').disabled,false);assert.equal(accepted.length,0);assert.equal(ticks.length,0);
      if(input.mode.startsWith('refusal'))assert.doesNotMatch(el('ticket-order-error').textContent,/unconfirmed/);
      else assert.match(el('ticket-order-error').textContent,/unconfirmed/);
      await run('applyTicketOrder()');assert.equal(calls.length,2,'no automatic or duplicate retry');
      await el('ticket-order-refresh').events.click();await settle();
      assert.equal(calls.length,3);assert.equal(el('ticket-order-apply').disabled,false);
     }else{
      assert.equal(el('ticket-order-dialog').closed,1);assert.equal(run('ticketOrderPreview'),null);
      assert.equal(context.revisionFloors.get('/one'),11);assert.equal(accepted.length,1);
      assert.deepEqual(ticks,[true]);assert.equal(context.seen,null);
     }
    }
   }
  }
 }
 process.stdout.write('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""


@pytest.mark.parametrize(
    "mode",
    [
        "header",
        "preview",
        "invalid-preview",
        "preview-navigation",
        "close-preview",
        "close-refresh",
        "close-refresh-escape",
        "close-apply",
        "workspace-select",
        "workspace-error",
        "success",
        "pending",
        "navigation",
        "replacement",
        "refusal",
        "refusal-unreadable",
        "lost",
        "server-error",
        "bad-success",
        "invalid-success",
        "paging",
    ],
)
def test_ticket_order_preview_apply_and_response_isolation(mode: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    result = subprocess.run(
        [node, "-e", DRIVER],
        input=json.dumps(
            {
                "page": api_endpoint.PAGE,
                "mode": mode,
                "workspace": pathlib.Path(api_endpoint.__file__)
                .with_name("workspace.js")
                .read_text(encoding="utf-8"),
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
