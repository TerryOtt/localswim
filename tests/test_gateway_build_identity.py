"""Gateway page identity must remain independent from selected board processes."""

import json
import pathlib
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from localswim import api_endpoint
from tests.test_api import request_json
from tests.test_workspace import gateway as _gateway_fixture

gateway = _gateway_fixture

if TYPE_CHECKING:
    from tests.test_workspace import RunningWorkspace

NODE_DRIVER = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
(async()=>{
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {textContent:'', className:'', value:'draft',
    classList:{values:new Set(),toggle(k,v){v?this.values.add(k):this.values.delete(k)},
      add(k){this.values.add(k)},remove(k){this.values.delete(k)}}});
  return elements.get(id);
}
let href=input.url || 'http://localhost/board/alpha/board';
const navigations=[], stored={};
const location={get href(){return href},get search(){return new URL(href).search},
  get hash(){return new URL(href).hash},replace(url){navigations.push(url);href=url}};
const history={replaceState(a,b,url){href=new URL(url,href).href}};
const window={location,localswimWorkspace:!!input.workspace,history,
  WorkspaceUI:input.workspace?{pageStatus:()=>input.status,capture(){}}:null};
const context=vm.createContext({window,location,URL,Date,document:{getElementById:element},
  sessionStorage:{setItem(k,v){stored[k]=v}},serverBuild:input.boardBuild || null,
  codeStale:!!input.boardStale,restarting:!!input.boardRestarting,restartProblem:input.boardProblem||null,
  pushState:null,lastOk:input.lastOk?Date.now():0,fileMs:0,LIVE_MS:5000,WARN_MS:15000,
  drafts:{},openId:null,editing:null,query:'needle',unhideOld:false,mkOpen:null,
  STALE_RELOAD_STATE_KEY:'saved',STALE_RELOAD_QUERY:'_localswim_build',staleReloadStarted:false,
  PAGE_BUILD:'aaaaaaa',clockOf:()=>'',agoOf:()=>''});
function section(source,start,end) {
  const a=source.indexOf(start),b=source.indexOf(end,a);
  if(a<0||b<0)throw Error('Production helper boundary not found: '+start);
  return source.slice(a,b);
}
if(input.mode==='drop'){
  const assert=require('assert/strict');
  function node(){return {dataset:{},listeners:{},classList:{values:new Set(['over','deny']),
    add(k){this.values.add(k)},remove(k){this.values.delete(k)}},
    querySelector(){return node()},addEventListener(k,fn){this.listeners[k]=fn}}}
  context.document.createElement=node;
  let posts=0,refreshes=0,accepted=0;const messages=[];
  Object.assign(context,{visibleItems:()=>[],API_PREFIX:'/api/v001',seen:42,
    allowed:()=>input.allowed!==false,toast:message=>messages.push(message),
    acceptRevision:()=>accepted++,tick:async()=>{refreshes++;
      if(input.refreshFailure)throw Error('refresh unavailable')},
    apiPost:async()=>{posts++;if(input.failure==='network')throw Error('lost response');
      return {ok:input.ok!==false,json:async()=>{
        if(input.failure==='read')throw Error('unreadable response');return input.response}}}});
  vm.runInContext(section(input.page,'function laneEl(', '\n// Match headings'),context);
  const lane=vm.runInContext('laneEl({state:"ready",label:"Ready",ownerLabel:"Owner"})',context);
  await lane.listeners.drop({preventDefault(){},dataTransfer:{getData(){return input.payload}}});
  assert.equal(lane.classList.values.size,0);
  process.stdout.write(JSON.stringify({posts,refreshes,accepted,messages,seen:context.seen}));
}else if(input.mode==='alerts'){
  const assert=require('assert/strict');let permission=input.permission||'granted';
  const messages=[],deliveries=[],listeners={};let requests=0,focus=0,closed=0;
  for(const id of ['alerts','alerts-wrap'])element(id).addEventListener=(k,fn)=>listeners[k]=fn;
  context.document.title='Synthetic board';
  function Notification(title,options){deliveries.push(options.body);
    if(input.failure==='constructor')throw Error('delivery unavailable');
    if(input.failure==='callback-set')Object.defineProperty(this,'onclick',{
      set(){throw Error('callback unavailable')}});
    this.close=()=>{closed++;if(input.failure==='close')throw Error('close unavailable')};
    messages.push(this)}
  Object.defineProperty(Notification,'permission',{get(){
    if(input.failure==='permission')throw Error('permission unavailable');return permission}});
  Notification.requestPermission=async()=>{requests++;
    if(input.failure==='request')throw Error('permission rejected');permission='granted'};
  if(!input.missing){window.Notification=Notification;context.Notification=Notification}
  window.focus=()=>{focus++;if(input.failure==='focus')throw Error('focus unavailable')};
  context.localStorage={getItem(){if(input.failure==='get')throw Error('read unavailable');
    return input.optedOut?'0':'1'},setItem(){
      if(input.failure==='set')throw Error('write unavailable')}};
  vm.runInContext(section(input.page,'let lastCta = null;', '\n// **SEARCH.'),context);
  // This marker must still execute after setup even under optional API failures.
  vm.runInContext('globalThis.setupContinued=true',context);
  for(const id of ['board','counts']){
    element(id).replaceChildren=()=>{};element(id).appendChild=()=>{};
  }
  Object.assign(context,{userLabel:()=> 'Synthetic reader',measureCards:()=>new Map(),
    alignCardStarts(){},playFlip(){},applyFilter(){}});
  context.document.createElement=()=>({});
  vm.runInContext(section(input.page,'function paint()', '\n// ---- proof of life'),context);
  if(input.gesture){element('alerts').checked=true;await listeners.change()}
  for(const count of [0,1,1,2]){
    context.data={project:'Synthetic board',browserUser:'reader',lanes:[],counts:{blocked:count}};
    vm.runInContext('paint()',context);
  }
  for(const notification of messages)if(notification.onclick)notification.onclick();
  if(input.optOutGesture){element('alerts').checked=false;await listeners.change()}
  assert.equal(context.document.title,'(2) Synthetic board');
  assert.equal(element('post').textContent,'Comment as Synthetic reader');
  assert.equal(element('title').textContent,'Synthetic board');
  assert.equal(vm.runInContext('lastCta',context),2);
  process.stdout.write(JSON.stringify({deliveries,requests,focus,closed,continued:context.setupContinued,
    checked:element('alerts').checked,disabled:element('alerts').disabled,
    hidden:element('alerts-wrap').hidden,title:element('alerts-wrap').title}));
}else if(input.mode==='drawer'){
  const assert=require('assert/strict');
  function node(tag){
    return {tag,children:[],dataset:{},hidden:false,disabled:false,value:'',listeners:{},
      classList:{contains:()=>true},
      append(...children){this.children.push(...children)},
      replaceChildren(){this.children=[]},setAttribute(k,v){this[k]=v},
      addEventListener(k,v){this.listeners[k]=v},
      focus(){context.document.activeElement=this},select(){},
      contains(target){return this===target||this.children.some(c=>c.contains(target))},
      querySelector(){return this.children.flatMap(function walk(c){
        return [c,...c.children.flatMap(walk)]}).find(c=>c.tag==='button'&&c.disabled)}};
  }
  elements.clear();
  context.document.getElementById=id=>{
    if(!elements.has(id))elements.set(id,node(id));return elements.get(id)};
  context.document.createElement=node;
  context.document.body=node('body');context.document.activeElement=context.document.body;
  const get=context.document.getElementById, calls=[];
  let resolve;
  Object.assign(context,{API_PREFIX:'/api/a',seen:1,
    itemById:id=>({id,subject:'Original'}),toast:()=>{},acceptRevision:()=>{},
    apiPost:(path,body)=>{calls.push({path,body});return new Promise(r=>{resolve=r})}});
  vm.runInContext(section(input.page,'let editing = null;', 'function startDetailEdit()'),context);
  vm.runInContext(section(input.page,'function attachmentImage(', 'function fileBase64('),context);
  vm.runInContext(section(input.page,"document.getElementById('p-subject-edit').addEventListener('keydown'",
    "document.getElementById('p-detail-text').addEventListener"),context);
  const key=(key,isComposing=false)=>{
    const ev={key,isComposing,prevented:false,stopped:false,
      preventDefault(){this.prevented=true},stopPropagation(){this.stopped=true}};
    get('p-subject-edit').listeners.keydown(ev);return ev};
  context.openId='a';vm.runInContext('startSubjectEdit()',context);
  get('p-subject-edit').value='New title';
  assert.equal(key('Enter',true).prevented,false);assert.equal(calls.length,0);
  assert.equal(key('Enter').prevented,true);key('Enter');assert.equal(calls.length,1);
  resolve({ok:true,json:async()=>({result:'saved'})});await new Promise(setImmediate);
  assert.equal(context.document.activeElement,get('p-subject'));
  vm.runInContext('startSubjectEdit()',context);
  assert.equal(key('Escape').stopped,true);
  assert.equal(context.document.activeElement,get('p-subject'));
  vm.runInContext('startSubjectEdit()',context);get('p-subject-edit').value='Failed';key('Enter');
  context.document.activeElement=context.document.body;
  resolve({ok:false,json:async()=>({error:'refused'})});await new Promise(setImmediate);
  assert.equal(get('p-subject-edit').hidden,false);assert.equal(get('p-subject-edit').disabled,false);
  assert.equal(context.document.activeElement,get('p-subject-edit'));
  key('Enter');context.openId='b';vm.runInContext('closeEditors(false);startSubjectEdit()',context);
  get('p-subject-edit').value='B draft';
  resolve({ok:true,json:async()=>({result:'saved A'})});await new Promise(setImmediate);
  assert.equal(get('p-subject-edit').value,'B draft');
  assert.equal(get('p-subject-edit').hidden,false);
  assert.equal(get('p-subject-edit').disabled,false);
  get('p-subject-edit').value='pending B';key('Enter');
  vm.runInContext('closeEditors(false);startSubjectEdit()',context);
  get('p-subject-edit').value='Fresh B session';
  resolve({ok:true,json:async()=>({result:'saved previous session'})});
  await new Promise(setImmediate);
  assert.equal(get('p-subject-edit').hidden,false);
  assert.equal(get('p-subject-edit').value,'Fresh B session');
  get('close').focus();key('Escape');
  assert.equal(context.document.activeElement,get('close')); // no unrelated focus theft
  vm.runInContext('startSubjectEdit()',context);
  const beforeNoop=calls.length;key('Enter');
  assert.equal(calls.length,beforeNoop);
  assert.equal(context.document.activeElement,get('p-subject'));
  for(const action of ['cover','attachment'])for(const ok of [true,false]){
    context.attachmentRefusal=()=>{};
    const a={id:'a',attachments:[{id:'file-a',name:'A.png',url:'/a',size:8,mediaType:'image/png'}]};
    const b={id:'b',attachments:[{id:'file-b',name:'B.png',url:'/b',size:8,mediaType:'image/png'}]};
    context.card=a;vm.runInContext('paintAttachments(card)',context);
    const box=get('p-attachments'), row=box.children[0], actions=row.children.at(-1);
    const button=actions.children[action==='cover'?0:1];button.focus();
    const pending=button.listeners.click();const count=calls.length;
    context.card={...a,coverAttachment:'file-a'};vm.runInContext('paintAttachments(card)',context);
    assert.equal(box.children[0],row); // same-card pending/focus is preserved
    context.card=b;vm.runInContext('paintAttachments(card)',context);
    assert.notEqual(box.children[0],row);assert.equal(box.children[0].children[1].textContent,'B.png');
    resolve({ok,json:async()=>({result:'done',error:'refused'})});await pending;
    assert.equal(button.disabled,false);
    assert.equal(calls.length,count);assert.equal(calls.at(-1).path,'/api/a/cards/a/'+action);
    assert.equal(box.children[0].children[1].textContent,'B.png');
    const bRow=box.children[0];bRow.children.at(-1).children[0].focus();
    context.card={...b,coverAttachment:'file-b'};vm.runInContext('paintAttachments(card)',context);
    assert.equal(box.children[0],bRow); // same-card focused control is preserved
    context.API_PREFIX='/api/other';vm.runInContext('paintAttachments(card)',context);
    assert.notEqual(box.children[0],bRow);context.API_PREFIX='/api/a';
  }
  process.stdout.write(JSON.stringify({verified:true,calls:calls.length}));
}else if(input.mode==='rendering'){
  // These deliberately small DOM seams verify production helper plumbing. The
  // synthetic browser smoke covers the real template parser and DOM boundaries.
  function node(tag){
    return {tag,className:'',textContent:'',children:[],htmlWrites:[],
      append(...children){this.children.push(...children)},
      set innerHTML(value){this.htmlWrites.push(value);this.html=value;
        if(tag==='template'){
          let text=value.replace(/<b class="mdh">([^]*?)<\/b>/g,'$1 ')
            .replace(/<[^>]*>/g,'');
          text=text.replace(/&(?:amp|lt|gt|quot|#x27|#39);/g,
            entity=>({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"',
              '&#x27;':"'",'&#39;':"'"}[entity]));
          this.content={textContent:text,querySelectorAll(){return []}};
        }},get innerHTML(){return this.html}};
  }
  context.document.createElement=node;
  context.data={actors:input.actors};
  vm.runInContext(section(input.page,'function userLabel(',
    '// **The owner control CYCLES'),context);
  vm.runInContext(section(input.page,'const HAY =','function paint()'),context);
  context.items=input.items;
  const haystacks=vm.runInContext('items.map(hay)',context);
  const cached=vm.runInContext(`items[0].subject='replacement';hay(items[0])`,context);
  // A changed payload must invalidate the existing ID through the actual poll handler.
  Object.assign(context,{pollingEpoch:null,seen:0,API_PREFIX:'/api/v001',
    apiGet:async path=>path.endsWith('/status')?{ok:true,mtime:1}:
      {actors:input.actors,lanes:[{state:'completed',items:input.items}]},
    paint(){},restoreStaleReloadState(){},renderLive(){}});
  context.window.workspaceEpoch=1;
  vm.runInContext(section(input.page,'async function tick()', '\ntick();'),context);
  await vm.runInContext('tick()',context);
  const refreshed=vm.runInContext('hay(items[0])',context);
  context.comments=input.comments;
  const headers=vm.runInContext('comments.map(renderComment)',context);
  context.query='needle';
  context.CSS={escape:value=>value};
  context.visibleItems=lane=>lane.items.filter(item=>!item.hidden);
  context.data.lanes=[{state:'completed',items:input.filterItems}];
  element('board').querySelector=selector=>element(selector);
  element('board').querySelectorAll=()=>[];
  vm.runInContext('applyFilter()',context);
  const filtered={found:element('findn').textContent,
    count:element('.lane[data-lane="completed"] .n').textContent};
  context.query='';vm.runInContext('applyFilter()',context);
  process.stdout.write(JSON.stringify({haystacks,cached,refreshed,headers,filtered,
    unfiltered:element('.lane[data-lane="completed"] .n').textContent}));
}else if(input.mode==='normalize'){
  vm.runInContext(section(input.workspaceSource,
    '  function normalizePageStatus(', '\n  function '),context);
  process.stdout.write(JSON.stringify(input.values.map(value=>vm.runInContext('normalizePageStatus('+JSON.stringify(value)+')',context))));
}else if(input.mode==='route'){
  vm.runInContext(section(input.workspaceSource,'  function route(', '\n  function '),context);
  process.stdout.write(JSON.stringify(vm.runInContext('route("beta/board")',context)));
}else if(input.mode==='roundtrip'){
  // A Node VM checks shipped state plumbing; these DOM seams are not a browser.
  const helpers=section(input.page,'function saveStaleReloadState()',
    '// **Terry specified this bar himself:');
  vm.runInContext(helpers,context);
  Object.assign(context,{drafts:{'other-card':'other unsent comment'},openId:'card-17',
    editing:'detail',query:'urgent café',unhideOld:true,mkOpen:'ready_for_work'});
  for(const [id,value] of Object.entries({'say':'unsent comment\nsecond line',
    'p-detail-text':'unsaved **detail**','mk-subject':'new draft subject',
    'mk-detail':'new draft detail\nwith newline','mk-priority':'P3','mk-owner':'bot'}))
    element(id).value=value;
  vm.runInContext('saveStaleReloadState();',context);
  const saved=JSON.parse(stored.saved), freshElements=new Map(), storageCalls=[];
  function freshElement(id){
    if(!freshElements.has(id))freshElements.set(id,{value:''});
    return freshElements.get(id);
  }
  // No saved-value globals cross the reload boundary. The shipped reader and
  // declarations must populate drafts, query and completed visibility themselves.
  const fresh=vm.createContext({document:{getElementById:freshElement},
    sessionStorage:{getItem(key){storageCalls.push(['get',key]);
      return input.emptyStored?null:stored.saved},
      removeItem(key){storageCalls.push(['remove',key])}}});
  vm.runInContext(section(input.page,"const STALE_RELOAD_STATE_KEY =",
    '// **Card #0072.'),fresh);
  vm.runInContext(section(input.page,'let query =','const HAY ='),fresh);
  vm.runInContext(`let openId=null,editing=null,mkOpen=null;
    data.lanes=[{state:'ready_for_work'}];
    function itemById(id){return id==='card-17'?{id}:null}
    function openCard(id){openId=id;document.getElementById('say').value=drafts[id]||''}
    function startDetailEdit(){editing='detail'}
    function startSubjectEdit(){editing='subject'}
    function openMake(lane){mkOpen=lane.state}`,fresh);
  vm.runInContext(helpers+'\nrestoreStaleReloadState();',fresh);
  const restored=vm.runInContext(
    '({drafts,query,unhideOld,openId,editing,mkOpen,staleReloadState})',fresh);
  process.stdout.write(JSON.stringify({saved,restored,storageCalls,
    fields:Object.fromEntries([...freshElements].map(([key,value])=>[key,value.value]))}));
}else{
  const helpers=section(input.page,'function saveStaleReloadState()',
    '// **Terry specified this bar himself:');
  const live=section(input.page,'function renderLive()', '// **POLLING IS THE DESIGN,');
  vm.runInContext(helpers+live+'\nrenderLive();',context);
  if(input.repeat)vm.runInContext('renderLive();',context);
  process.stdout.write(JSON.stringify({navigations,href,stale:element('stale').textContent,
    staleShown:element('stale').classList.values.has('show'),restart:element('restart').textContent,
    restartShown:element('restart').classList.values.has('show'),stored}));
}
})().catch(error=>{process.stderr.write(String(error));process.exitCode=1});
"""


def browser_helpers(**values: Any) -> Any:  # noqa: ANN401 - JSON browser test boundary
    """Execute actual shipped helpers with only browser surfaces stubbed."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    payload = {
        "page": api_endpoint.PAGE,
        "workspaceSource": pathlib.Path(api_endpoint.__file__)
        .with_name("workspace.js")
        .read_text(encoding="utf-8"),
        **values,
    }
    result = subprocess.run(
        [node, "-e", NODE_DRIVER],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def test_drawer_attachment_identity_and_subject_keyboard_handlers() -> None:
    """Production handlers under narrow DOM seams; real keyboard evidence is separate."""
    assert browser_helpers(mode="drawer")["verified"] is True


@pytest.mark.parametrize(
    "payload",
    [
        "broken",
        "null",
        "[]",
        '"text"',
        "42",
        "{}",
        '{"id":1,"from":"backlog"}',
        '{"id":"a","from":null}',
        '{"id":"","from":"backlog"}',
        '{"card":"a","state":"backlog"}',
    ],
)
def test_drop_ignores_malformed_payload_before_request(payload: str) -> None:
    result = browser_helpers(mode="drop", payload=payload)
    assert result["posts"] == result["refreshes"] == result["accepted"] == 0
    assert result["seen"] == 42


@pytest.mark.parametrize(
    ("options", "message", "accepted"),
    [
        ({"failure": "network"}, "outcome unknown", 0),
        ({"failure": "read"}, "outcome unknown", 0),
        ({"response": None}, "outcome unknown", 0),
        ({"response": []}, "outcome unknown", 0),
        ({"ok": False, "failure": "read"}, "Move refused", 0),
        ({"ok": False, "response": {"error": "Stale revision"}}, "Stale revision", 1),
        ({"response": {"result": "Moved"}}, "Moved", 1),
        ({"response": {"workspaceBoardChanged": True}}, None, 0),
    ],
)
@pytest.mark.parametrize("refresh_failure", [False, True])
def test_drop_contains_unknown_outcomes_refusal_and_refresh(
    options: dict[str, Any], message: str | None, accepted: int, *, refresh_failure: bool
) -> None:
    result = browser_helpers(
        mode="drop",
        payload='{"id":"a","from":"backlog"}',
        refreshFailure=refresh_failure,
        **options,
    )
    assert result["posts"] == result["refreshes"] == 1
    assert result["accepted"] == accepted
    assert result["seen"] is None
    if message:
        assert message in result["messages"][0]
    if refresh_failure:
        assert "refresh failed" in result["messages"][-1]


def test_drop_permission_refusal_never_submits() -> None:
    result = browser_helpers(mode="drop", payload='{"id":"a","from":"backlog"}', allowed=False)
    assert result["posts"] == result["refreshes"] == 0
    assert "Not yours" in result["messages"][0]


@pytest.mark.parametrize(
    "failure",
    ["get", "set", "permission", "request", "constructor", "callback-set", "focus", "close", None],
)
def test_optional_alert_failures_preserve_setup_title_and_baseline(failure: str | None) -> None:
    result = browser_helpers(
        mode="alerts",
        failure=failure,
        gesture=failure in {"get", "set", "request"},
        permission="default" if failure == "request" else "granted",
    )
    assert result["continued"]
    assert len(result["deliveries"]) <= 2
    if failure in {"permission", "request"}:
        assert result["deliveries"] == []
        assert result["disabled"]
        assert not result["checked"]
        assert "unavailable" in result["title"]
    elif failure in {"constructor", "callback-set"}:
        assert len(result["deliveries"]) == 1
        assert result["disabled"]
        assert not result["checked"]
    else:
        assert len(result["deliveries"]) == 2
    if failure in {"get", "set"}:
        assert "this page only" in result["title"]


@pytest.mark.parametrize("permission", ["default", "denied", "granted"])
def test_alert_permission_and_preference_stay_distinct(permission: str) -> None:
    result = browser_helpers(mode="alerts", permission=permission, optedOut=True)
    assert result["deliveries"] == []
    assert result["requests"] == 0
    assert not result["checked"]
    assert result["disabled"] == (permission == "denied")
    opted_in = browser_helpers(
        mode="alerts", permission=permission, gesture=True, optOutGesture=True
    )
    assert opted_in["requests"] == (1 if permission == "default" else 0)
    assert not opted_in["checked"]
    assert len(opted_in["deliveries"]) == (0 if permission == "denied" else 2)


def test_unavailable_notifications_are_visible_and_do_not_interrupt_setup() -> None:
    result = browser_helpers(mode="alerts", missing=True)
    assert result["continued"]
    assert result["disabled"]
    assert not result["hidden"]
    assert not result["checked"]
    assert "unavailable" in result["title"]
    assert result["deliveries"] == []


def test_subject_rename_uses_native_named_button_inside_heading() -> None:
    assert '<h1><button id="p-subject" type="button" aria-label="Rename card title"' in (
        api_endpoint.PAGE
    )


def test_rendered_search_and_comment_header_boundaries() -> None:
    actor = 'reviewer " data-probe="inert"><i id="inert-header">markup</i>'
    result = browser_helpers(
        mode="rendering",
        actors=[{"id": actor, "label": "Configured reviewer"}, {"id": "bot", "label": "Bot"}],
        items=[
            {
                "id": "literal",
                "ticket": "#0166",
                "subject": "x < y > z",
                "detail": api_endpoint.inline("## Header\n`a * b ** c` & readable <literal>"),
                "detailRaw": "raw-only-artifact",
                "comments": [
                    {"text": api_endpoint.inline("**café** & 'quote' <script>inert</script>")}
                ],
            }
        ],
        comments=[
            {"by": actor, "when": "now", "text": api_endpoint.inline("**formatted** `a*b`")},
            {"by": "bot", "when": "earlier", "text": "ordinary"},
            {"by": "unknown with spaces", "when": "oldest", "text": "fallback"},
        ],
        filterItems=[
            {"id": "visible", "subject": "needle"},
            {"id": "hidden", "subject": "needle", "hidden": True},
        ],
    )
    hay = result["haystacks"][0]
    for phrase in (
        "#0166",
        "x < y > z",
        "a * b ** c",
        "header a * b ** c & readable",
        "café & 'quote'",
    ):
        assert phrase in hay
    for artifact in ("<strong>", "&amp;", "&lt;", "raw-only-artifact", 'class="mdh"'):
        assert artifact not in hay
    assert result["cached"] == hay
    assert "replacement" in result["refreshed"]
    assert "x < y > z" not in result["refreshed"]
    first, ordinary, fallback = result["headers"]
    head, body = first["children"]
    assert first["htmlWrites"] == head["htmlWrites"] == []
    assert [child["tag"] for child in head["children"]] == ["span", "span"]
    who, at = head["children"]
    assert who["className"] == "who " + actor
    assert who["textContent"] == "Configured reviewer"
    assert who["htmlWrites"] == at["htmlWrites"] == []
    assert at["textContent"] == "  ·  now"
    assert body["htmlWrites"] == ["<strong>formatted</strong> <code>a*b</code>"]
    assert ordinary["children"][0]["children"][0]["textContent"] == "Bot"
    assert fallback["children"][0]["children"][0]["textContent"] == "unknown with spaces"
    assert result["filtered"] == {"found": "1 found", "count": 1}
    assert result["unfiltered"] == 1


@pytest.mark.parametrize("board_build", ["bbbbbbb", "ccccccc", None])
@pytest.mark.parametrize("last_ok", [False, True])
def test_gateway_identity_ignores_selected_board_build(
    board_build: str | None, *, last_ok: bool
) -> None:
    result = browser_helpers(
        workspace=True, status={"build": "aaaaaaa"}, boardBuild=board_build, lastOk=last_ok
    )
    assert result["navigations"] == []
    assert not result["staleShown"]


@pytest.mark.parametrize("status", [None, {}, {"build": None}, {"build": "unknown"}])
def test_unknown_gateway_never_uses_old_board_identity(status: Any) -> None:  # noqa: ANN401 - malformed JSON cases
    result = browser_helpers(workspace=True, status=status, boardBuild="bbbbbbb")
    assert result["navigations"] == []
    assert not result["staleShown"]


def test_gateway_upgrade_reloads_once_and_preserves_failed_proof() -> None:
    result = browser_helpers(workspace=True, status={"build": "bbbbbbb"}, repeat=True)
    assert len(result["navigations"]) == 1
    assert "_localswim_build=bbbbbbb" in result["href"]
    assert result["staleShown"]
    assert "AUTO-RELOAD FAILED" in result["stale"]
    assert "saved" in result["stored"]


def test_reload_state_roundtrip_executes_shipped_storage_bootstrap_in_fresh_vm() -> None:
    """Node VM evidence only: DOM opening/rendering is represented by narrow stubs."""
    result = browser_helpers(mode="roundtrip")
    assert result["saved"] == {
        "drafts": {"other-card": "other unsent comment", "card-17": "unsent comment\nsecond line"},
        "openId": "card-17",
        "editing": "detail",
        "editValue": "unsaved **detail**",
        "query": "urgent café",
        "unhideOld": True,
        "make": {
            "state": "ready_for_work",
            "subject": "new draft subject",
            "detail": "new draft detail\nwith newline",
            "priority": "P3",
            "owner": "bot",
        },
    }
    assert result["restored"] == {
        "drafts": result["saved"]["drafts"],
        "query": "urgent café",
        "unhideOld": True,
        "openId": "card-17",
        "editing": "detail",
        "mkOpen": "ready_for_work",
        "staleReloadState": None,
    }
    assert result["fields"] == {
        "find": "urgent café",
        "say": "unsent comment\nsecond line",
        "p-detail-text": "unsaved **detail**",
        "mk-subject": "new draft subject",
        "mk-detail": "new draft detail\nwith newline",
        "mk-priority": "P3",
        "mk-owner": "bot",
    }
    assert result["storageCalls"] == [
        ["get", "localswim-stale-reload-state-v1"],
        ["remove", "localswim-stale-reload-state-v1"],
    ]


def test_reload_state_empty_storage_control_cannot_restore_seeded_drafts() -> None:
    result = browser_helpers(mode="roundtrip", emptyStored=True)
    assert result["saved"]["openId"] == "card-17"
    assert result["restored"] == {
        "drafts": {},
        "query": "",
        "unhideOld": False,
        "openId": None,
        "editing": None,
        "mkOpen": None,
        "staleReloadState": None,
    }
    assert result["fields"] == {}


def test_matching_gateway_clears_completed_reload_proof() -> None:
    result = browser_helpers(
        workspace=True,
        status={"build": "aaaaaaa"},
        url="http://localhost/board/alpha/board?_localswim_build=aaaaaaa&other=yes#anchor",
    )
    assert "_localswim_build" not in result["href"]
    assert "other=yes" in result["href"]
    assert result["href"].endswith("#anchor")


def test_standalone_keeps_its_own_build_authority() -> None:
    result = browser_helpers(workspace=False, boardBuild="bbbbbbb")
    assert len(result["navigations"]) == 1
    assert "_localswim_build=bbbbbbb" in result["href"]


@pytest.mark.parametrize("workspace_mode", [False, True])
def test_restart_refusal_remains_visible(*, workspace_mode: bool) -> None:
    result = browser_helpers(
        workspace=workspace_mode,
        status={
            "build": "aaaaaaa",
            "codeStale": True,
            "restarting": False,
            "restartProblem": "invalid changed source",
        },
        boardBuild="aaaaaaa",
        boardStale=True,
        boardProblem="invalid changed source",
    )
    assert result["restartShown"]
    assert "AUTO-RESTART PAUSED" in result["restart"]
    assert "invalid changed source" in result["restart"]


def test_workspace_navigation_preserves_reload_proof_and_url_state() -> None:
    result = browser_helpers(
        mode="route", url="http://localhost/?_localswim_build=bbbbbbb&other=yes#anchor"
    )
    assert result == "/board/beta/board?_localswim_build=bbbbbbb&other=yes#anchor"


@pytest.mark.parametrize("build", [None, "", "unknown", 17, {}, "bad build", "<html>"])
def test_malformed_gateway_build_becomes_unknown(build: Any) -> None:  # noqa: ANN401 - malformed JSON cases
    result = browser_helpers(mode="normalize", values=[{"build": "aaaaaaa"}, {"build": build}])
    assert result[0]["build"] == "aaaaaaa"
    assert result[1]["build"] is None


def test_gateway_catalog_exposes_restart_refusal(
    gateway: RunningWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_restart(_server: object) -> tuple[bool, str]:
        return False, "fixture refusal"

    monkeypatch.setattr(api_endpoint, "code_is_stale", lambda: True)
    monkeypatch.setattr(api_endpoint, "request_code_restart", refuse_restart)
    code, payload = request_json(gateway.base + "/api/v001/workspace")
    assert code == 200
    assert payload["codeStale"] is True
    assert payload["restarting"] is False
    assert payload["restartProblem"] == "fixture refusal"


def test_board_restart_warning_remains_independent_of_gateway() -> None:
    result = browser_helpers(
        workspace=True,
        status={"build": "aaaaaaa", "codeStale": False},
        boardBuild="bbbbbbb",
        boardStale=True,
        boardProblem="board launch gate busy",
    )
    assert result["navigations"] == []
    assert result["restartShown"]
    assert "BOARD" in result["restart"]
    assert "board launch gate busy" in result["restart"]
    assert "GATEWAY" not in result["restart"]


@pytest.mark.parametrize("build", ["c154c0b", "deadbee-12345678-dirty", " release_1.2 "])
def test_normalized_gateway_accepts_supported_build_tokens(build: str) -> None:
    result = browser_helpers(mode="normalize", values=[{"build": build}])
    assert result[0]["build"] == build.strip()


def test_gateway_restart_flags_require_boolean_values() -> None:
    result = browser_helpers(
        mode="normalize",
        values=[
            {
                "build": "aaaaaaa",
                "codeStale": "false",
                "restarting": 1,
                "restartProblem": {"unexpected": "object"},
            }
        ],
    )
    assert result[0] == {
        "build": "aaaaaaa",
        "codeStale": False,
        "restarting": False,
        "restartProblem": None,
    }
