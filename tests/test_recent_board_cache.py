"""Execute the shipped workspace cache and navigation code without a browser."""

import json
import pathlib
import shutil
import subprocess

import pytest

from localswim import api_endpoint

DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const {workspace,mode}=JSON.parse(fs.readFileSync(0,'utf8'));
function section(start,end){const at=workspace.indexOf(start);assert.ok(at>=0);
 const until=workspace.indexOf(end,at);assert.ok(until>at);return workspace.slice(at,until)}
const nodes=new Map();const el=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',
 classList:{remove(){}},replaceChildren(){},append(){}});return nodes.get(id)};
let now=0,ticks=[],streams=[],painted=[],captured=[],loads=0;
const context=vm.createContext({Date:{now:()=>now},console,window:{workspaceEpoch:0,workspaceReady:false,
 history:{pushState(){},replaceState(){}},BoardStream:{select:key=>streams.push(key)}},
 document:{getElementById:el,createElement:()=>el(Math.random()),title:''},
 API_PREFIX:'',revisionFloors:new Map(),streamBoardError:null,
 current:null,catalog:[{key:'one',project:'One'},{key:'two',project:'Two'}],
 state:{records:{one:{drafts:{c:'unposted'},query:'remember me',unhideOld:true}}},pending:null,
 board:el('board'),HAY:new Map(),persist(){captured.push(context.current)},
 closeCard(){},closeMake(){},closeEditors(){},route:key=>'/board/'+key,renderRows(){},
 renderLive(){},renderBoardBanner(){},loaded(){loads++;context.pending=null},
 projectActions(raw){const out=JSON.parse(JSON.stringify(raw));out.optimistic=true;return out},
 paint(){painted.push(JSON.parse(JSON.stringify(context.data)))},tick:force=>ticks.push(force),
 recoverOnCatalog:false,renderSaveRecovery(){},lastOk:0,
 scrolls(){},restoreScroll(){},reconcile(){},gatewayStatus:{},refresh(){},flushPinCommands(){}});
const run=code=>vm.runInContext(code,context);
run(section('  function createRecentBoardCache(', '  const categories'));
if(mode==='bounds'){
 run('var small=createRecentBoardCache({ttl:100,limit:2,budget:180,now:()=>Date.now()})');
 const put=(key,revision,extra={})=>{context.raw={revision,items:[{subject:'raw'}],...extra};
  return run(`small.remember('${key}',raw)`)};
 const get=(key,floor=0)=>run(`small.get('${key}',${floor})`);
 assert.equal(put('one',2),true);context.raw.items[0].subject='mutated';
 assert.equal(get('one').items[0].subject,'raw');
 get('one').items[0].subject='projected';assert.equal(get('one').items[0].subject,'raw');
 assert.equal(put('one',1),false);assert.equal(get('one').revision,2);
 assert.equal(put('one',3,{error:'unavailable'}),false);
 assert.equal(put('one',3,{blob:'x'.repeat(200)}),false);
 assert.equal(get('one',3),null);
 now=50;put('two',1);get('one');put('three',1);
 assert.equal(get('two'),null);assert.equal(get('one').revision,2);
 now=140;run("small.touch('one')");now=200;
 assert.equal(get('three'),null);assert.equal(get('one').revision,2);
 now=300;assert.equal(get('one'),null);
 run('var tiny=createRecentBoardCache({budget:110})');
 run("tiny.remember('one',{revision:1,blob:'x'.repeat(35)})");
 run("tiny.remember('two',{revision:1,blob:'x'.repeat(35)})");
 assert.equal(run("tiny.get('one')"),null);assert.equal(run("tiny.get('two').revision"),1);
 context.raw={revision:4};context.raw.circular=context.raw;
 assert.equal(run("small.remember('bad',raw)"),false);
}else{
 run(section('  function error(message, recover = false)', '  const boardNames'));
 run(section('  window.WorkspaceUI =', "  document.getElementById('ws-toggle').onclick"));
 const ui=context.window.WorkspaceUI,prefix='/api/v001/workspace/boards/one';
 const raw={revision:5,lanes:[],project:'One',items:[{subject:'confirmed'}]};
 assert.equal(ui.rememberSnapshot(prefix,raw),true);raw.items[0].subject='later mutation';
 run("select('one','none')");
 assert.equal(painted[0].items[0].subject,'confirmed');assert.equal(painted[0].optimistic,true);
 assert.equal(context.authoritativeData.optimistic,undefined);
 assert.equal(context.query,'remember me');assert.equal(context.drafts.c,'unposted');
 assert.equal(ui.cachePhase(),'refreshing');assert.equal(ui.canMutate(),false);
 assert.deepEqual(ticks,[true]);assert.deepEqual(streams,['one']);assert.equal(loads,1);
 const oldEpoch=context.window.workspaceEpoch;
 assert.equal(ui.settling(prefix,oldEpoch-1),null);
 const oldToken=ui.settling(prefix,oldEpoch);assert.equal(typeof oldToken,'number');
 assert.equal(ui.cachePhase(),'settling');
 ui.refreshed(prefix,oldEpoch-1,oldToken);assert.equal(ui.canMutate(),false);
 run("error('fresh read failed')");assert.equal(ui.cachePhase(),'failed');
 assert.equal(context.window.workspaceReady,true);assert.equal(context.drafts.c,'unposted');
 assert.equal(context.streamBoardError,'fresh read failed');assert.equal(ui.canMutate(),false);
 ui.refreshed(prefix,oldEpoch,oldToken);assert.equal(ui.cachePhase(),'failed');
 assert.equal(ui.resume(),true);assert.deepEqual(ticks,[true,true]);
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
}
process.stdout.write('ok');
"""


@pytest.mark.parametrize("mode", ["bounds", "navigation"])
def test_recent_board_cache(mode: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    workspace = (
        pathlib.Path(api_endpoint.__file__).with_name("workspace.js").read_text(encoding="utf-8")
    )
    result = subprocess.run(
        [node, "-e", DRIVER],
        input=json.dumps({"workspace": workspace, "mode": mode}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"
