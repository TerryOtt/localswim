"""Review approval preserves workspace credentials and board scope."""

import json
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from localswim import api_endpoint, board_state
from tests.test_api import request_json
from tests.test_review_approval import APPROVAL, ready
from tests.test_workspace import RunningWorkspace, board_route, existing_writer
from tests.test_workspace import gateway as gateway_fixture

if TYPE_CHECKING:
    import pathlib

gateway = gateway_fixture
API = board_state.API_PREFIX


def seed_review(path: pathlib.Path) -> None:
    board = board_state.load(path)
    ready(board)
    board_state.save(board, path)


@pytest.mark.parametrize("credential", ["missing", "invalid", "upstream_cli", "upstream_browser"])
def test_approval_requires_gateway_credential(gateway: RunningWorkspace, credential: str) -> None:
    seed_review(gateway.first)
    with existing_writer(gateway.first) as descriptor:
        upstream = f"http://127.0.0.1:{descriptor['port']}"
        _, session = request_json(upstream + API + "/browser-session")
        tokens = {
            "missing": None,
            "invalid": "invalid-approval-credential",
            "upstream_cli": str(descriptor["token"]),
            "upstream_browser": str(session["token"]),
        }
        before = gateway.first.read_bytes()
        code, payload = request_json(
            board_route(gateway.base, "alpha/board", "cards/review/approve"),
            token=tokens[credential],
            revision=0,
            body={},
        )
        assert code == 401, payload
        assert gateway.first.read_bytes() == before
        assert board_state.load(gateway.second).revision == 0


def test_approval_proxy_reuses_writer_preserves_revision_actor_and_other_board(
    gateway: RunningWorkspace,
) -> None:
    seed_review(gateway.first)
    seed_review(gateway.second)
    other_before = gateway.second.read_bytes()
    with existing_writer(gateway.first) as descriptor:
        route = board_route(gateway.base, "alpha/board", "cards/1/approve")
        before = gateway.first.read_bytes()
        code, payload = request_json(route, token=gateway.instance.token, body={})
        assert code == 428, payload
        assert gateway.first.read_bytes() == before
        code, payload = request_json(route, token=gateway.instance.token, revision=0, body={})
        assert code == 200, payload
        assert payload["revision"] == 1
        saved = board_state.load(gateway.first)
        item = saved.find("review")
        assert item.state == "completed"
        assert [(comment.text, comment.by) for comment in item.comments] == [(APPROVAL, "terry")]
        assert (item.history[-1].frm, item.history[-1].to, item.history[-1].by) == (
            "ready_for_review",
            "completed",
            "terry",
        )
        assert saved.verify() == []
        committed = gateway.first.read_bytes()
        for revision, expected in [(0, 412), (1, 409)]:
            code, payload = request_json(
                route, token=gateway.instance.token, revision=revision, body={}
            )
            assert code == expected, payload
            assert gateway.first.read_bytes() == committed
        current = json.loads(
            board_state.service_descriptor_path(gateway.first).read_text(encoding="utf-8")
        )
        assert current == descriptor
        assert gateway.second.read_bytes() == other_before
        assert not board_state.service_descriptor_path(gateway.second).exists()


NODE_DRIVER = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
function section(start, end) {
  const a = input.source.indexOf(start), b = input.source.indexOf(end, a);
  if (a < 0 || b < 0) throw Error('Production approval helpers not found');
  return input.source.slice(a, b);
}
const button = {hidden: true, disabled: false, textContent: '', addEventListener() {}};
const events = [], say = {value: 'unsent'};
const elements = {'p-approve': button, say};
for (const id of ['scrim', 'panel']) {
  elements[id] = {classList: {remove(name) {events.push([id, name]);}}};
}
const ctx = vm.createContext({input, button, events, say, JSON, Set, encodeURIComponent,
  document: {getElementById: id => elements[id]}});
vm.runInContext(`
let API_PREFIX = '/alpha', API_TOKEN = 'synthetic', openId = 'review', seen = 'fresh';
let data = {revision: 4, browserUser: 'terry', actors: [{id:'terry', class:'human'}],
  lanes: [{state:'ready_for_review'}, {state:'completed'}]};
let card = {id:'review', state:'ready_for_review', archived:false};
let permit = true, calls = [], toasts = [], reconciled = [], drafts = {review:'unsent'};
let relationshipDraft = {pending:true};
let resolveFetch, rejectFetch;
const window = {workspaceEpoch: 1, WorkspaceUI: {
  reconcile: (...args) => reconciled.push(args)}};
function allowed() { return permit; }
function itemById(id) { return card && card.id === id ? card : null; }
function userLabel() { return 'Terry'; }
function toast(...args) { toasts.push(args); events.push(['toast', seen, data.revision]); }
function fetch(path, options) {
  calls.push({path, options});
  return new Promise((resolve, reject) => {resolveFetch=resolve; rejectFetch=reject;});
}
` + section('async function apiPost(', '// **The repaint counter') +
section('const pendingApprovals =', 'function openCard('), ctx);
vm.runInContext(section('function closeCard()',
  "document.getElementById('close').addEventListener"), ctx);
vm.runInContext(`this.run = async () => {
  renderApproval(card);
  const eligible = !button.hidden && !button.disabled && button.textContent;
  const first = approveReview();
  const pendingDisabled = button.disabled;
  await approveReview();
  const closedBeforeResponse = events.length;
  say.value = 'typed while approval pending';
  if (input.mode === 'board') {API_PREFIX='/beta'; window.workspaceEpoch++; data.revision=99;}
  if (input.mode === 'card') {openId='other'; card={id:'other', state:'ready_for_review'};}
  if (input.mode === 'closed') {openId=null;}
  if (input.mode === 'epoch') {window.workspaceEpoch++;}
  if (input.mode === 'removed') {card=null;}
  if (input.mode === 'board_error') {API_PREFIX='/beta'; window.workspaceEpoch++; data.revision=99;}
  if (input.mode.endsWith('error')) rejectFetch(Error('synthetic connection loss'));
  else resolveFetch({ok: input.mode !== 'refused', json: async () =>
    ({revision:5, result:'accepted', error:'refused'})});
  await first;
  const result = {eligible, pendingDisabled, calls, toasts, reconciled,
    seen, revision:data.revision, drafts, pending:pendingApprovals.size,
    closedBeforeResponse, events, openId, relationshipDraft,
    disabled:button.disabled, hidden:button.hidden};
  for (const kind of ['bot','denied','wrong','archived','target_missing','source_missing']) {
    card={id:'review', state:'ready_for_review'}; permit=true;
    data.actors[0].class='human'; data.lanes=[{state:'ready_for_review'},{state:'completed'}];
    if(kind==='bot') data.actors[0].class='bot';
    if(kind==='denied') permit=false;
    if(kind==='wrong') card.state='in_progress';
    if(kind==='archived') card.archived=true;
    if(kind==='target_missing') data.lanes=[{state:'ready_for_review'}];
    if(kind==='source_missing') {card.state='backlog'; data.lanes=[{state:'completed'}];}
    renderApproval(card); result[kind]=button.hidden;
  }
  return result;
};`, ctx);
ctx.run().then(result => process.stdout.write(JSON.stringify(result)))
  .catch(err => { console.error(err); process.exitCode=1; });
"""


@pytest.mark.parametrize(
    "mode",
    ["stay", "board", "card", "closed", "epoch", "removed", "error", "board_error", "refused"],
)
def test_browser_approval_guards_pending_request_and_navigation(mode: str) -> None:
    """Execute production callbacks with deferred fetch, including late failures."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    result = subprocess.run(
        [node, "-e", NODE_DRIVER],
        input=json.dumps({"source": api_endpoint.PAGE, "mode": mode}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["eligible"] == "Terry Reviewed & Approved"
    assert observed["pendingDisabled"] is True
    assert len(observed["calls"]) == 1
    request = observed["calls"][0]
    assert request["path"] == "/alpha/cards/review/approve"
    assert request["options"]["headers"]["If-Match"] == '"revision-4"'
    assert request["options"]["headers"]["Authorization"] == "Bearer synthetic"
    assert request["options"]["body"] == "{}"
    assert observed["closedBeforeResponse"] == 0
    if mode == "stay":
        assert observed["drafts"] == {"review": "typed while approval pending"}
        assert observed["openId"] is None
        assert observed["relationshipDraft"] is None
        assert observed["events"] == [["toast", None, 5], ["scrim", "show"], ["panel", "show"]]
    else:
        assert observed["drafts"] == {"review": "unsent"}
        assert observed["relationshipDraft"] == {"pending": True}
        assert all(event[0] == "toast" for event in observed["events"])
    assert observed["pending"] == 0
    assert all(
        observed[kind]
        for kind in ["bot", "denied", "wrong", "archived", "target_missing", "source_missing"]
    )
    if mode in {"board", "board_error", "card", "closed", "epoch"}:
        assert observed["toasts"] == []
        assert observed["seen"] == "fresh"
        assert observed["revision"] == (99 if mode.startswith("board") else 4)
    else:
        assert len(observed["toasts"]) == 1
        assert observed["seen"] is None
        assert observed["revision"] == (4 if mode == "error" else 5)
        assert observed["disabled"] is (mode == "stay")
    assert len(observed["reconciled"]) == (1 if mode in {"board", "epoch"} else 0)
    if mode == "error":
        assert "outcome unknown" in observed["toasts"][0][0]
        assert observed["toasts"][0][1] is True
    if mode == "refused":
        assert observed["toasts"] == [["refused", True]]
    if mode == "removed":
        assert observed["hidden"] is True
