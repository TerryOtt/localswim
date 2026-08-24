# localswim

A small, local swimlane board for one human and one automation agent. It serves a
draggable browser UI over a single JSON file, binds only to loopback, and has no runtime
dependencies outside Python's standard library.

## Quick start

Requirements:

- [uv](https://docs.astral.sh/uv/) 0.12.5 (it installs the pinned Python 3.14.7)
- A modern browser
- Git only if you want build identifiers or opt-in automatic pushes

Clone the repository and create a private board from the checked description and
name-based permission examples:

~~~text
uv run --frozen localswim-cli boards/my-project.json --init examples/board-description.example.json examples/permissions.example.json
~~~

Then start the service:

~~~console
uv python install
uv sync --locked
uv run --frozen localswim boards/my-project.json
~~~

Open the URL it prints, normally **http://127.0.0.1:8792/**. The port comes from the
board file so different projects can have stable bookmarks. The --port option provides
a temporary override.

The boards/ directory is ignored by Git and is the only permitted location for board
files kept inside this public checkout. A board may instead live in a separate
repository or directory.

## Board configuration

The checked [board description](examples/board-description.example.json) lists users,
priorities, and lane display names. The separate
[permission input](examples/permissions.example.json) names people and lanes as a human
would. Initialization resolves those names, generates readable lane slugs once, rejects
collisions, and persists the resolved policy inside the new board. The checked
[example board](examples/board.example.json) is the resulting valid, empty snapshot.

The generic example stays deliberately small. A second checked
[Terry workflow description](examples/board-description.terry-workflow.json),
[permission input](examples/permissions.terry-workflow.json), and generated
[board](examples/board.terry-workflow.json) preserve Terry's preferred seven-lane
workflow, also used by FGA:

~~~console
uv run --frozen localswim-cli boards/my-project.json --init examples/board-description.terry-workflow.json examples/permissions.terry-workflow.json
~~~

The important fields are:

| Field | Meaning |
|---|---|
| schema | Board format version; currently 4. |
| project | Name shown in the browser. |
| port | Loopback TCP port; defaults to 8792 if omitted. |
| policy | Resolved lanes, priorities, creation rights, and transition edges. |
| users | Valid identities, labels, classes (human or bot), and UI colors. |
| browserUser | Identity used for browser changes. |
| cliUser | Identity used for CLI changes. |
| defaultOwner | Owner assigned when a new card does not specify one. |
| revision | Monotonic write version; start a new board at 0. |
| nextTicket | Next display number; start an empty board at 1. |
| items | Cards; start an empty board with an empty list. |

User IDs are case-sensitive. The embedded policy's two transition actors MUST exactly
match browserUser and cliUser. Malformed JSON, ambiguous names, slug collisions, and
invalid fields fail with a path, line, or field-specific explanation.

## Daily use

The browser supports creating, moving, editing, assigning, prioritizing and relating
cards. The CLI can inspect the snapshot while the service is stopped or running:

~~~console
uv run --frozen localswim-cli boards/my-project.json
uv run --frozen localswim-cli boards/my-project.json --verify
uv run --frozen localswim-cli boards/my-project.json --json
~~~

Inspect one card by stable ID or ticket number without dumping the board:

~~~console
uv run --frozen localswim-cli boards/my-project.json --show 137
uv run --frozen localswim-cli boards/my-project.json --show '#0137' --json
uv run --frozen localswim-cli boards/my-project.json --show upload-retry --include-prose
~~~

The focused report includes state, priority, owner, comment count, parent, children,
and directional relationships with each related card's current status. Its default
human and JSON forms omit detail and comment text. `--include-prose` is the explicit
opt-in for that selected card's description and comments; bounded activity remains the
audit-history interface.

Time-bounded activity reports combine card creation, movement, assignment, priority,
and comment events into one chronological stream:

~~~console
uv run --frozen localswim-cli boards/my-project.json --activity-since 2026-08-23T10:53:09-04:00
uv run --frozen localswim-cli boards/my-project.json --activity-between 2026-08-23T10:53:09-04:00 2026-08-23T11:09:02-04:00 --json
~~~

Bounds are inclusive and require RFC 3339 timestamps with an explicit UTC offset.
Output is intentionally safe for coordination: it identifies the card, event, actor,
and changed lane, owner, or priority, but never emits card prose or comment text.
Comment events expose only their character count. Events recorded in different history
arrays at the same instant have deterministic output order, not a recoverable causal
order.

CLI mutations use the running REST service so browser and CLI writes share validation,
locking and revision checks:

~~~console
uv run --frozen localswim-cli boards/my-project.json --create docs "Write setup docs" --state ready_for_work
uv run --frozen localswim-cli boards/my-project.json --comment docs "First draft is ready"
uv run --frozen localswim-cli boards/my-project.json --move docs in_progress
~~~

Run **uv run --frozen localswim-cli --help** for the complete command list. CLI changes are
attributed to cliUser; browser changes are attributed to browserUser. Request bodies
cannot choose another identity. CLI stdout and stderr are always UTF-8, including when
Windows redirects them through a legacy system encoding.

Schema 4 embeds the complete resolved policy so a board's lane identities and
permissions travel atomically with its state. With the service stopped, upgrade a
schema-3 board with the policy it already uses:

~~~console
uv run --frozen localswim-cli boards/my-project.json --embed-policy path/to/rules.json
~~~

The earlier schema-2 Ready For Work migration also produces a schema-4 board:

~~~console
uv run --frozen localswim-cli boards/my-project.json --migrate-lane <old-lane-id> ready_for_work
~~~

Every structural migration refuses a listening board port, validates and replays the
complete result before replacing the file, and increments the board revision.

Display renames and identity migrations are deliberately different operations:

~~~console
uv run --frozen localswim-cli boards/my-project.json --rename-lane-label ready_for_work "Selected Work"
uv run --frozen localswim-cli boards/my-project.json --migrate-lane-id ready_for_work selected_work
~~~

The first changes only presentation. The second atomically rewrites the embedded
policy, current card states, and lane-history endpoints. Neither operation derives an
existing ID from the current label.

## Transition permissions

The embedded policy is the allow-list for lanes, priorities, card creation, and
movement. Its generated edges are grouped under exactly two actor IDs:

~~~json
"edges": {
  "terry": [
    {
      "from": "backlog",
      "to": "ready_for_work",
      "description": "Why Terry may make this move."
    },
    {
      "from": "ready_for_work",
      "to": "in_progress",
      "description": "Terry may start selected work directly."
    }
  ],
  "bot": [
    {
      "from": "ready_for_work",
      "to": "in_progress"
    }
  ]
}
~~~

Each edge MUST contain string from and to lane IDs, MAY contain a string description,
and MUST contain no other fields. Unknown lanes, self-loops, duplicate actor edges and
duplicate JSON keys are rejected. Initialization inputs may use display names, but the
persisted policy and every runtime request use stable IDs.

## Data safety and Git

`localswim.api_endpoint` is the sole writer while a board is live. Every mutation
validates the model, increments revision, flushes a temporary file in the board's
directory and atomically replaces the previous snapshot. Stale writes receive HTTP 412
instead of overwriting newer state.

Board JSON contains identities, descriptions, comments and audit history. Keep it out
of this public source repository.

Automatic Git commits and pushes are OFF by default. Enable them only for a board stored
in an appropriate Git repository:

~~~console
uv run --frozen localswim --autopush path/to/board.json
~~~

When enabled, the server commits only the board path after five quiet seconds and pushes
the board repository's current branch. A valid new board that is not yet tracked is
adopted on the first pass; unrelated tracked or untracked files remain untouched. The
server refuses ignored boards, non-repositories and repositories without a remote; it
cannot prove that a configured remote is private.

Stop a live service through its authenticated CLI rendezvous rather than terminating
its process:

~~~console
localswim-cli path/to/board.json --shutdown
~~~

The command stops accepting mutations, performs one final commit-and-push when
`--autopush` is enabled, and waits for the service descriptor to be removed. A failed
final push refuses shutdown so the live service and its diagnostic state remain
available.

## REST API

The browser and CLI use these loopback routes:

~~~text
GET  /api/v001/status
GET  /api/v001/board
POST /api/v001/cards
POST /api/v001/cards/<id>/{move,comment,assign,priority,subject,detail,link,parent}
POST /api/v001/board/project
POST /api/v001/shutdown
~~~

Only the `/api/v001` routes are API endpoints. For seamless upgrades of already-open
tabs, `/v1/status` and `/mtime` issue no-cache redirects to
`/api/v001/status`. Earlier board and mutation routes are not retained.

Mutations require the per-process bearer credential and an
If-Match: "revision-N" header. Credentials are published in a user-local temporary
service descriptor; they are not stored in the board. The shutdown route accepts only
the CLI credential and does not change the board revision.

## Live code updates

The server watches its installed `api_endpoint` and `board_state` modules. A valid
change is debounced, preflighted in a child Python process and applied by gracefully
re-executing the server. An invalid change leaves the healthy process running and
reports the preflight error.

A service started from a dirty checkout retains that truthful Build ID while work
remains. Once the checkout becomes clean, status polling checks at most once every two
seconds and adopts the clean commit only after two Git reads agree and both loaded
Python source digests still match disk. Changed source continues through the guarded
re-exec path; the metadata refresh never hides or overrides real code drift.

Open tabs detect the new build and reload once. Comment drafts, active editors, a
partially written card, search text and view state survive in per-tab session storage.

## Development

Before changing the implementation, read the
[contributor orientation](docs/ORIENTATION.md). It maps the components, data and
request flows, schemas, safety invariants, state-policy caveats, test boundaries, and
Windows/Codex environment details that are intentionally more technical than this user
guide. The branch and pull-request policy lives in
[CONTRIBUTING.md](CONTRIBUTING.md), and automation-specific working rules live in
[AGENTS.md](AGENTS.md). The reasoning and pins behind the development tools are in
[docs/TOOLING.md](docs/TOOLING.md).

Install the exact Python and locked development environment:

~~~console
uv python install
uv sync --locked
~~~

Run the complete gate:

~~~console
uv run --frozen python check.py
~~~

The gate runs LF line-ending validation, Ruff `ALL`, Ruff formatting, strict Pyright,
pytest, actionlint with ShellCheck, and the project's US English vocabulary check. The
vocabulary table comes from the public FlickrGroupAddr/backend-api repository; pass its
path with **--word-table path/to/claude-dirty-words.py** if it is not in a neighboring
checkout. actionlint 1.7.12 and ShellCheck 0.11.0 must be on `PATH`; installation details
are in [docs/TOOLING.md](docs/TOOLING.md).

Build reproducible source and wheel distributions with the declared backend, ignoring
any local uv dependency-source overrides:

~~~console
uv build --no-sources --clear
~~~

Enable the local pre-commit hook once per clone:

~~~console
git config core.hooksPath .githooks
~~~

GitHub Actions runs the same gate on pushes and pull requests. Text files are enforced
as UTF-8 with LF endings by .editorconfig, .gitattributes and the gate.

## License

MIT. See LICENSE.
