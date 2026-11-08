"""Exercise create-with-files sequencing and recovery in shipped browser code."""

import json
import shutil
import subprocess

import pytest

from localswim import api_endpoint

DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8')),nodes=new Map(),calls=[],notices=[];
function node(){return {value:'',children:[],disabled:false,classList:{remove(){},add(){}},
 append(...items){this.children.push(...items)},replaceChildren(){this.children=[]},
 addEventListener(type,fn){this[type]=fn},focus(){}}}
function el(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)}
el('mk-subject').value='New';el('mk-detail').value='Draft';el('mk-priority').value='P3';el('mk-owner').value='bot';
let board={revision:1,browserUser:'human',lanes:[{state:'backlog',items:[]}]},fail=true,closed=0;
const ctx=vm.createContext({crypto:require('crypto').webcrypto,Uint8Array,
 document:{getElementById:el,createElement:node},window:{workspaceEpoch:1},API_PREFIX:'/one',
 data:board,authoritativeData:board,localActions:[],
 toast(text){notices.push(text)},relationshipSpecs:()=>[],acceptRevision(){},
 snapshotItem:(b,id)=>b.lanes.flatMap(l=>l.items).find(c=>c.id===id),
 acknowledgeSnapshot(b){ctx.authoritativeData=b;return true},forgetAction(action){ctx.localActions.splice(ctx.localActions.indexOf(action),1)},
 fileBase64:async file=>Buffer.from(await file.arrayBuffer()).toString('base64'),
 apiGet:async()=>JSON.parse(JSON.stringify(board)),
 apiPost:async(path,body,version)=>{
 calls.push({path,body,version});
 if(path.endsWith('/cards')){
  board.lanes[0].items.push({id:body.id,cardVersion:'v1',attachments:[]});board.revision++;
  return {ok:true,status:200,json:async()=>({revision:board.revision})};
 }
 ctx.localActions.push({id:'intent-'+calls.length,prefix:'/one',cardId:body.id||board.lanes[0].items[0].id,kind:'attachment',body});
 await Promise.resolve();
 assert.ok(run('mkFiles.find(entry => entry.status === \"uploading\").actionId'),'intent correlated before awaiting response');
 const card=board.lanes[0].items[0];
 if(input.mode==='navigation'){ctx.window.workspaceEpoch++;return {ok:false,status:0,json:async()=>({workspaceBoardChanged:true})}}
 if(fail && calls.filter(c=>c.version).length===2 && input.mode!=='success'){
  fail=false;return {ok:false,status:['refusal','wrongfile'].includes(input.mode)?409:0,json:async()=>({error:'stopped'})};
 }
 assert.equal(version,card.cardVersion);card.cardVersion='v'+(calls.length);board.revision++;
 card.attachments.push({id:'a'+calls.length,name:body.name,size:1,sha256:require('crypto').createHash('sha256').update(Buffer.from(body.data,'base64')).digest('hex'),by:'human'});
 return {ok:true,status:200,json:async()=>({revision:board.revision})};
 }});
const start=input.page.indexOf('let mkOpen ='),end=input.page.indexOf('// ---- the board ',start);
vm.runInContext(input.page.slice(start,end),ctx);
const confirmStart=input.page.indexOf('function confirmAddIntent(');
vm.runInContext(input.page.slice(confirmStart,input.page.indexOf('// Dismissal',confirmStart)),ctx);
const run=code=>vm.runInContext(code,ctx);
// Keep real cancellation elsewhere; count only successful close in this harness.
ctx.closed=()=>closed++;run('closeMake = () => closed()');
run("mkOpen='backlog'");
function file(name,size=1){return {name,size,lastModified:1,arrayBuffer:async()=>Uint8Array.from([1]).buffer}}
const selected=[file('first.txt'),file('second.txt')];if(input.mode==='oversize')selected[1].size=10485761;
el('mk-files').change({target:{files:selected,value:''}});
(async()=>{
 await run('submitMake()');
 if(input.mode==='oversize'){assert.equal(calls.length,0);assert.equal(run('mkFiles.length'),2);return}
 assert.equal(calls.filter(c=>c.path.endsWith('/cards')).length,1);
 if(input.mode==='success'){assert.equal(closed,1);assert.equal(calls.length,3);return}
 if(input.mode==='navigation'){assert.equal(calls.length,2);assert.equal(closed,0);return}
 assert.equal(closed,0);assert.equal(run('mkFiles[0].status'),'saved');
 assert.equal(run('mkFiles[1].status'),['refusal','wrongfile'].includes(input.mode)?'refused':'unknown');
 if(input.mode==='reload-controls'){
  ctx.restoredFiles=JSON.parse(run('JSON.stringify(mkFiles)'));
  ctx.restoredAttempt=JSON.parse(run('JSON.stringify(mkAttempt)'));
  run('mkAttempt=null;mkFiles=[];restoreMakeFiles(restoredFiles);restoreMakeAttempt(restoredAttempt)');
  assert.equal(el('mk-go').textContent,'Continue uploads');
  assert.ok(el('mk-file-status').children.every(row=>row.children.length===0));
  assert.equal(run('mkAttempt.confirmed'),true);
  assert.equal(calls.filter(c=>c.path.endsWith('/cards')).length,1);return;
 }
 const before=calls.length;
 if(input.mode==='wrongfile'){
  run('mkFiles[1].file=null');
  const wrong=file('second.txt');wrong.arrayBuffer=async()=>Uint8Array.from([2]).buffer;
  el('mk-files').change({target:{files:[wrong],value:''}});
  await run('submitMake()');assert.equal(calls.length,before);
  assert.equal(run('mkFiles[1].file'),null);
  el('mk-files').change({target:{files:[file('second.txt')],value:''}});
 }
 await run('submitMake()');
 assert.equal(calls.filter(c=>c.path.endsWith('/cards')).length,1);
 if(['refusal','wrongfile'].includes(input.mode)){assert.equal(calls.length,before+1);assert.equal(closed,1);return}
 assert.equal(calls.length,before,'negative fresh read must not replay unknown upload');
 // Reload serialization keeps outcomes/metadata without claiming browser File persistence.
 const saved=JSON.parse(run('JSON.stringify(mkFiles)'));assert.equal(saved[1].file,undefined);
 ctx.saved=saved;const originalAttempt=run('mkAttempt');
 const restore=vm.createContext({saved,renderMakeFiles(){}});
 const restoreStart=input.page.indexOf('function restoreMakeFiles(');
 vm.runInContext('let mkFiles=[];'+input.page.slice(restoreStart,input.page.indexOf("document.getElementById('mk-files')",restoreStart)),restore);
 vm.runInContext('restoreMakeFiles(saved)',restore);
 assert.equal(vm.runInContext('mkFiles[1].file',restore),undefined);
 assert.equal(vm.runInContext('mkFiles[1].status',restore),'unknown');
 run('restoreMakeFiles(saved)');assert.equal(run('mkFiles[1].file'),undefined);
 assert.equal(run('mkAttempt'),originalAttempt);
 const pending=saved[1],card=board.lanes[0].items[0];
 card.attachments.push({id:'late',name:pending.name,size:pending.size,sha256:pending.digest,by:pending.actor});
 if(input.mode==='ambiguous')card.attachments.push({...card.attachments.at(-1),id:'also-late'});
 await run('submitMake()');assert.equal(calls.length,before);
 assert.equal(closed,input.mode==='ambiguous'?0:1);
})().then(()=>process.stdout.write('ok')).catch(error=>{console.error(error);process.exitCode=1});
"""


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "refusal",
        "wrongfile",
        "unknown",
        "ambiguous",
        "navigation",
        "oversize",
        "reload-controls",
    ],
)
def test_create_files_preserve_intent_and_partial_results(mode: str) -> None:
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
