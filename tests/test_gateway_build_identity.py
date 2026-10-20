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
if(input.mode==='normalize'){
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
