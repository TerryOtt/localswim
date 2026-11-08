"""Exercise the shipped one-click lane approval UI with bounded transports."""

import json
import shutil
import subprocess

import pytest

from localswim import api_endpoint

DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(){this.events={};this.attrs={};this.disabled=false;}
 addEventListener(key,fn){this.events[key]=fn;}
 setAttribute(key,value){this.attrs[key]=value;}
}
let calls=[],toasts=[],ticks=[],accepts=[],release;
const version='a'.repeat(64),lane={state:'ready_for_review',label:'Ready for Review',approveAll:true,
 items:[{id:'first',cardVersion:version},{id:'second',cardVersion:version},{id:'archived',archived:true,cardVersion:version}]};
const context=vm.createContext({AbortSignal,window:{workspaceEpoch:1},API_PREFIX:'/one',API_TOKEN:'synthetic',
 data:{revision:10,actors:[{id:'human',class:'human'}],browserUser:'human',lanes:[lane,{state:'completed'}]},
 localActions:[],revisionFloors:new Map(),seen:'old',lane,document:{createElement(){return new Element()}},
 allowed(){return true},workspaceCanMutate(){return true},toast(...args){toasts.push(args)},acceptRevision(out){accepts.push(out)},
 async tick(force){ticks.push(force)},
 async fetch(path,options){
  calls.push({path,method:options.method,headers:options.headers,body:JSON.parse(options.body)});
  if(['pending','navigation'].includes(input.mode))await new Promise(resolve=>release=resolve);
  if(input.mode==='lost')throw new Error('lost');
  const status=input.mode.startsWith('refusal')?412:input.mode==='server-error'?503:200;
  return {status,ok:status===200,async json(){
   if(['bad-success','refusal-unreadable'].includes(input.mode))throw new Error('unreadable');
   if(input.mode==='invalid-success')return {revision:11,approvedCount:99,result:'Wrong'};
   return status===200?{revision:11,approvedCount:2,result:'Approved 2 cards'}:{error:'Changed'};
  }};
 }});
const start=input.page.indexOf('// ---- lane review approval');
const end=input.page.indexOf('// ---- end lane review approval',start);
assert.ok(start>0&&end>start);vm.runInContext(input.page.slice(start,end),context);
const run=code=>vm.runInContext(code,context),settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 if(input.mode==='header'){
  let button=run('laneApprovalButton(lane)');assert.match(button.innerHTML,/<svg\b/);
  assert.match(button.innerHTML,/aria-hidden="true"/);
  assert.equal(button.type,'button');assert.equal(button.disabled,false);
  assert.match(button.title,/all 2 cards/);assert.equal(button.title,button.attrs['aria-label']);
  lane.approveAll=false;assert.equal(run('laneApprovalButton(lane)'),null);lane.approveAll=true;
  lane.items.forEach(item=>item.archived=true);assert.equal(run('laneApprovalButton(lane)').disabled,true);
  lane.items[0].archived=false;context.localActions.push({prefix:'/one',phase:'unknown'});
  assert.equal(run('laneApprovalButton(lane)').disabled,true);
  context.localActions=[];context.data.browserUser='bot';assert.equal(run('laneApprovalButton(lane)'),null);
 }else if(input.mode==='local-pending'){
  const button=run('laneApprovalButton(lane)');context.button=button;
  context.localActions.push({prefix:'/one',phase:'pending'});
  await button.events.click();assert.equal(calls.length,0);
 }else{
  const button=run('laneApprovalButton(lane)');context.button=button;
  const work=button.events.click();await settle();assert.equal(button.disabled,true);
  if(['pending','navigation'].includes(input.mode)){
   await button.events.click();assert.equal(calls.length,1);
   if(input.mode==='navigation'){context.API_PREFIX='/two';context.window.workspaceEpoch++;}
   release();
  }
  await work;assert.equal(calls.length,1);
  assert.equal(calls[0].path,'/one/board/lane-approval/apply');assert.equal(calls[0].method,'POST');
  assert.equal(calls[0].headers['If-Match'],'"revision-10"');
  assert.deepEqual(calls[0].body,{lane:'ready_for_review',cards:{first:version,second:version}});
  if(input.mode==='navigation'){
   assert.equal(ticks.length,0);assert.equal(toasts.length,0);assert.equal(accepts.length,0);
   assert.equal(context.revisionFloors.get('/one'),11);
  }else if(['lost','server-error','bad-success','invalid-success'].includes(input.mode)){
   assert.equal(accepts.length,0);assert.match(toasts[0][0],/unconfirmed/);
   assert.equal(run('laneApprovalButton(lane)').disabled,true);
   await button.events.click();assert.equal(calls.length,1,'no blind retry while readback unavailable');
   context.data={...context.data};assert.equal(run('laneApprovalButton(lane)').disabled,false);
  }else if(input.mode.startsWith('refusal')){
   assert.equal(accepts.length,0);assert.doesNotMatch(toasts[0][0],/unconfirmed/);
   assert.equal(run('unconfirmedLaneApprovals.size'),0);
  }else{
   assert.equal(accepts.length,1);assert.equal(context.revisionFloors.get('/one'),11);
  }
  if(input.mode!=='navigation')assert.deepEqual(ticks,[true]);
 }
 process.stdout.write('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""


@pytest.mark.parametrize(
    "mode",
    [
        "header",
        "local-pending",
        "success",
        "pending",
        "navigation",
        "refusal",
        "refusal-unreadable",
        "lost",
        "server-error",
        "bad-success",
        "invalid-success",
    ],
)
def test_one_click_approval_and_transport_isolation(mode: str) -> None:
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
    assert result.stdout == "ok"
