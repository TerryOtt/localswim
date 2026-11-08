"""Exercise user paste filtering and guarded existing-card uploads in shipped JS."""

import json
import shutil
import subprocess

import pytest

from localswim import api_endpoint

DRIVER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const page=JSON.parse(fs.readFileSync(0,'utf8')).page,nodes=new Map(),calls=[],refusals=[];
function node(){return {disabled:false,children:[],replaceChildren(){this.children=[]},append(...items){this.children.push(...items)},addEventListener(t,f){this[t]=f}}}
function el(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)}
let writable=true,read=async()=>'',outcome=true,navigate=false,commandGate=null;
const card={id:'original',cardVersion:'v1'};
const ctx=vm.createContext({crypto:require('crypto').webcrypto,File:require('buffer').File,
 document:{getElementById:el,createElement:node},window:{workspaceEpoch:1},API_PREFIX:'/board',openId:'original',mkOpen:null,
 data:{attachmentMaxBytes:10},localActions:[],workspaceCanMutate:()=>writable,
 itemById:id=>id==='original'?card:null,toast(){},attachmentRefusal:reason=>refusals.push(reason),
 fileBase64:file=>read(file),attachmentCommand:async(id,action,body,version)=>{
   calls.push({id,action,body,version});if(commandGate) await commandGate();if(navigate)ctx.window.workspaceEpoch++;
   return outcome;
 },apiGet:async()=>({cardVersion:'v2'}),acknowledgeSnapshot:()=>true,snapshotItem:s=>s});
const start=page.indexOf('function clipboardImages('),end=page.indexOf('// ---- the detail panel',start);
vm.runInContext(page.slice(start,end),ctx);
const run=code=>vm.runInContext(code,ctx);
function event(files,text='',html=''){
 return {prevented:false,preventDefault(){this.prevented=true},clipboardData:{
  getData:type=>type==='text/plain'?text:html,
  items:files.map(file=>({kind:'file',type:file.type,getAsFile:()=>file}))}};
}
const image=new (require('buffer').File)(['x'],'image.png',{type:'image/png'});
ctx.ev=event([image]);assert.equal(run('clipboardImages(ev).length'),1);
for(const e of [event([image],'prose'),event([image],'','<p>prose</p>'),event([]),
 event([new (require('buffer').File)(['x'],'text.txt',{type:'text/plain'})])]){
 ctx.ev=e;assert.equal(run('clipboardImages(ev).length'),0);el('panel').paste(e);assert.equal(e.prevented,false);
}
ctx.ev={defaultPrevented:true,clipboardData:null};assert.equal(run('clipboardImages(ev).length'),0);
ctx.files=[{name:'first.png',size:1},{name:'second.png',size:1}];
(async()=>{
 await run('uploadCardFiles(files)');assert.equal(calls.length,2);
 assert.deepEqual(calls.map(c=>c.version),['v1','v2']);assert.ok(calls.every(c=>c.id==='original'));
 calls.length=0;
 // Delayed reads and writes show immediate stages without locking text input.
 let releaseRead,releaseUpload;
 read=()=>new Promise(resolve=>{releaseRead=resolve});
 commandGate=()=>new Promise(resolve=>{releaseUpload=resolve});
 ctx.files=[{name:'first.png',size:1}];const delayed=run('uploadCardFiles(files)');
 assert.match(el('p-upload-status').textContent,/Preparing attachment 1 of 1/);
 assert.equal(el('p-upload').disabled,true);assert.equal(el('say').disabled,false);
 releaseRead('encoded');while(!releaseUpload) await new Promise(resolve=>setImmediate(resolve));
 assert.match(el('p-upload-status').textContent,/Uploading attachment 1 of 1/);
 releaseUpload();await delayed;
 assert.equal(el('p-upload-status').textContent,'');assert.equal(el('p-upload').disabled,false);
 commandGate=null;
 // Closing and reopening the same card invalidates the older read, including its cleanup.
 let oldRead;read=()=>new Promise(resolve=>{oldRead=resolve});
 const old=run('uploadCardFiles(files)');run('resetCardUploadFeedback()');
 let newRead;read=()=>new Promise(resolve=>{newRead=resolve});
 const newer=run('uploadCardFiles(files)');oldRead('old');await old;
 assert.match(el('p-upload-status').textContent,/Preparing/);assert.equal(el('p-upload').disabled,true);
 assert.equal(calls.length,1);newRead('new');await newer;
 assert.equal(calls.length,2);assert.equal(el('p-upload-status').textContent,'');
 ctx.files=[{name:'first.png',size:1},{name:'second.png',size:1}];
 calls.length=0;read=async()=>'';writable=false;await run('uploadCardFiles(files)');assert.equal(calls.length,0);writable=true;
 ctx.localActions.push({prefix:'/board',cardId:'original',phase:'unknown'});
 await run('uploadCardFiles(files)');assert.equal(calls.length,0);ctx.localActions.length=0;
 el('p-upload').disabled=true;await run('uploadCardFiles(files)');assert.equal(calls.length,0);el('p-upload').disabled=false;
 ctx.files=[{name:'oversize.png',size:11}];await run('uploadCardFiles(files)');assert.equal(calls.length,0);assert.equal(refusals.length,1);
 ctx.files=[{name:'first.png',size:1},{name:'second.png',size:1}];outcome=false;
 await run('uploadCardFiles(files)');assert.equal(calls.length,1);calls.length=0;outcome=true;
 navigate=true;await run('uploadCardFiles(files)');assert.equal(calls.length,1);calls.length=0;navigate=false;
 read=async()=>{ctx.openId='another';return ''};await run('uploadCardFiles(files)');assert.equal(calls.length,0);
 assert.equal(el('p-upload').disabled,false);
 // A refused clipboard intake never interferes with text paste in a busy drawer.
 const prose=event([image],'still prose');el('panel').paste(prose);assert.equal(prose.prevented,false);
 // Reload keeps metadata, and clipboard reselection must be unambiguous and retain identity.
 const makeStart=page.indexOf('let mkOpen =');
 vm.runInContext(page.slice(makeStart,page.indexOf('async function uploadMakeFiles',makeStart)),ctx);
 run("mkOpen='backlog'");writable=false;ctx.images=[image];run("selectMakeFiles(images,'clipboard')");
 assert.equal(run('mkFiles.length'),0);writable=true;
 run("mkBusy=true;selectMakeFiles(images,'clipboard');mkBusy=false");assert.equal(run('mkFiles.length'),0);
 run("selectMakeFiles(images,'clipboard')");const name=run('mkFiles[0].name');
 assert.equal(JSON.parse(run('JSON.stringify(mkFiles)'))[0].file,undefined);
 run("restoreMakeFiles(JSON.parse(JSON.stringify(mkFiles)));selectMakeFiles(images,'clipboard')");
 assert.equal(run('mkFiles.length'),1);assert.equal(run('mkFiles[0].name'),name);assert.equal(run('mkFiles[0].file'),image);
 run("mkAttempt={confirmed:true};mkFiles[0].file=null;mkFiles[0].status='unknown';selectMakeFiles(images,'clipboard')");
 assert.equal(run('mkFiles.length'),1);assert.equal(run('mkFiles[0].file'),null);
 run("mkAttempt=null;mkFiles=[];selectMakeFiles([images[0],images[0]],'clipboard');restoreMakeFiles(JSON.parse(JSON.stringify(mkFiles)));selectMakeFiles(images,'clipboard')");
 assert.equal(run('mkFiles.length'),2);assert.ok(run('mkFiles.every(entry=>!entry.file)'));

})().then(()=>process.stdout.write('ok')).catch(error=>{console.error(error);process.exitCode=1});
"""


def test_clipboard_filters_prose_and_guards_existing_card_uploads() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    result = subprocess.run(
        [node, "-e", DRIVER],
        input=json.dumps({"page": api_endpoint.PAGE}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"
