"""A live, draggable swimlane board over one board JSON, served on loopback.

    uv run --frozen localswim path/to/board.json
    then open the URL it prints

**RFC 2119 keywords, and the capitals are load-bearing.**

## The port comes from the BOARD, not from this file

**Terry, 2026-08-18:** *"I want per-project config JSON that includes TCP port num; I
want to be able to bookmark one board per project. reduces surprise if nothing is
listening at that port."*

**A shared default port is worse than a dead bookmark.** With every project on 8792, a
bookmark opens whichever board happens to be running -- so the failure mode is reading
the WRONG project's work and believing it, which is silent. **A port per project means
the bookmark either shows your board or shows nothing, and nothing is honest.**

`--port` still overrides, for the case where two boards must run at once during a
migration.

## IT WRITES, and that is safe here for a reason Trello could not offer

**Terry drags cards, and either of us can comment.** Both are write paths.

**The same afternoon, the official Trello MCP server was connected and removed within
the hour**, because its OAuth grant authenticates Claude AS Terry -- a card Claude moved
and a card Terry moved were the same event by the same member, so his signoff stopped
being provable.

**This server binds to loopback.** Whoever reaches it is at his machine, so a drag IS
Terry: no identity to forge, no token to leak, and `by` in the history can be trusted.

**Permission is re-checked in `board_state.Board.move`, not here.** The page carries the edge
list only so the cursor can answer without a round trip. **A guard that lives only in
the client is decoration** -- and one that lives only in the server leaves the library
Claude uses wide open, which is exactly what happened on the first version.

## Three staleness edges, and each looks like the server being broken

**A change to the BOARD FILE is live**, picked up within `POLL_MS`.

**A change to THIS FILE or to `board_state.py` needs a RESTART.**

**A change to the PAGE triggers a guarded BROWSER RELOAD.** The open tab still runs the
script it was served, which produces a genuinely confusing halfway state: cards render
correctly because their content comes from `/api/v001/board`, while new CSS and new
counts do not.
The client preserves unsent form state in session storage and reloads itself once.

## The browser rules this had to satisfy

  * **Loopback is exempt from mixed-content blocking, and `127.0.0.1` is the address
    that gets the exemption.** A LAN address is a plain insecure origin and Chrome
    blocks it.
  * **Chrome may gate loopback behind a Local Network Access prompt** on a fresh
    profile. The symptom looks exactly like a server that is not running.
"""

import argparse
import contextlib
import copy
import datetime
import hashlib
import html
import http.server
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING, cast

from . import board_state

if TYPE_CHECKING:
    import socketserver
    from collections.abc import Callable

HOST = "127.0.0.1"
API_PREFIX = board_state.API_PREFIX
API_PARTS = API_PREFIX.strip("/").split("/")
CARD_COMMAND_PARTS = 3
LOCAL_BOARD_DIR = "boards"


def build_id() -> str:
    """What this checkout is, for the stale-page check. Never raises.

    **`git` is asked in api_endpoint.py's OWN directory**, not the working directory, because
    the tool and the board data live in different repositories and the caller is usually
    standing in neither.

    **A dirty tree is marked, and that is the case this exists for.** A developer editing
    the server is exactly who ends up with a stale tab, and a bare hash would claim a
    cleanliness the working tree does not have -- it would go on matching across every
    uncommitted edit, which is the whole failure wearing a shorter name.

    **Failure returns `unknown`, never a lie and never an exception.** A missing hash is a
    real answer and reads as "cannot tell", which is different from "current". Same
    three-state rule this project applies to the toolchain banner: confirmed fresh,
    confirmed stale, and could-not-establish are three outcomes, not two.
    """
    here = pathlib.Path(__file__).resolve().parent
    try:
        head = subprocess.run(  # noqa: S603 -- fixed argv, no shell or untrusted executable
            ["git", "-C", str(here), "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if head.returncode != 0 or not (head.stdout or "").strip():
            return "unknown"
        ident = head.stdout.strip()
        dirty = subprocess.run(  # noqa: S603 -- fixed argv, no shell or untrusted executable
            ["git", "-C", str(here), "status", "--porcelain"],  # noqa: S607
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return "unknown"
    if dirty.returncode == 0 and (dirty.stdout or "").strip():
        # A bare "-dirty" is one bit, not an identity: every uncommitted edit would
        # produce the same build id and an old tab could agree with newly restarted
        # code. Include the loaded Python sources so successive dirty builds differ.
        try:
            sources = (
                pathlib.Path(__file__).resolve(),
                pathlib.Path(board_state.__file__ or __file__).resolve(),
            )
            digest = hashlib.sha256()
            for source in sources:
                digest.update(source.read_bytes())
        except OSError:
            return ident + "-dirty"
        return ident + "-" + digest.hexdigest()[:8] + "-dirty"
    return ident


# **Read at import so it describes the code this process loaded.** A later status poll may
# replace only a dirty identity with a clean commit after the loaded source digests still
# match disk and two Git reads agree. It must never adopt changed source without re-exec.
BUILD = build_id()

# ---------------------------------------------------------------------------
# IS THIS PROCESS RUNNING THE CODE THAT IS ON DISK?
#
# **`BUILD` above cannot answer that, and the reason is the comment right above it.**
# It freezes at import so it can honestly describe the loaded code -- which is
# correct, and which means **a stale SERVER makes the tab and the server AGREE.**
# `drifted` is false and `#stale` stays hidden.
#
# **That is not theoretical. It bit twice on 2026-08-19.** `api_endpoint.py` was edited,
# committed and pushed while the process kept serving the old page with no flag. Then
# `rules.json` gained an actor and the board went on showing the old lane owners --
# **Terry noticed before any instrument did**, and his first guess was that the rule
# had never been written.
#
# **Card #0052, under his standing order: RESOLVE if possible, ELSE alert.** Python
# holds old module objects, so hot-reloading them in place is unsafe. The resolvable
# action is a graceful process re-exec: preflight the changed files in a child Python,
# drain request threads, close the socket, and run the original command again.
#
# **A wrong instruction is worse than none**: reloading re-fetches the same old page
# from the same old process, so it looks like it was followed and nothing changes.
#
# **This warning is about a stale PROCESS, not a stale TAB.** The tab now repairs its
# own mismatch after preserving unsent state. Reloading cannot replace Python module
# objects, so automatic browser recovery MUST NOT be confused with this restart path.
#
# **mtime first, hash only when it moves.** Hashing 120 KB twice a second to answer a
# question that is almost always "no" is waste; a `stat` is not. And hashing rather
# than trusting mtime alone means a `touch`, or an edit reverted before saving, does
# not raise a banner Terry cannot act on -- **the same reason the toolchain check
# refuses to shout on anything it has not confirmed.**
# `module.__file__` is `str | None` to a type checker -- None for a namespace package,
# which `board_state` is not. Falling back to this file keeps the annotation honest without
# pretending the impossible case cannot be typed.
CODE_FILES = (
    pathlib.Path(__file__).resolve(),
    pathlib.Path(board_state.__file__ or __file__).resolve(),
)


def _code_stamp() -> tuple[tuple[float, str], ...]:
    """(mtime, digest) per source file. A digest of "" means it could not be read."""
    out: list[tuple[float, str]] = []
    for path in CODE_FILES:
        try:
            out.append((path.stat().st_mtime, hashlib.sha256(path.read_bytes()).hexdigest()))
        except OSError:
            out.append((0.0, ""))
    return tuple(out)


_BOOT_CODE = _code_stamp()

BUILD_REFRESH_INTERVAL_S = 2.0
_build_refresh_lock = threading.Lock()
_next_build_refresh_at = 0.0


def refresh_build_id_if_clean() -> str:
    """Replace a stale dirty identity only when loaded code matches a clean checkout.

    Status is polled several times per second, so Git checks are throttled while active
    development keeps the checkout dirty. A clean candidate is read twice around the
    source digest comparison. This does not make Git and the filesystem transactional,
    but it closes ordinary commit races and never blesses source bytes this process did
    not load.
    """
    global BUILD, _next_build_refresh_at  # noqa: PLW0603 -- process build metadata

    if not BUILD.endswith("-dirty"):
        return BUILD
    checked_at = time.monotonic()
    with _build_refresh_lock:
        if not BUILD.endswith("-dirty") or checked_at < _next_build_refresh_at:
            return BUILD
        _next_build_refresh_at = checked_at + BUILD_REFRESH_INTERVAL_S
        candidate = build_id()
        if candidate == "unknown" or candidate.endswith("-dirty"):
            return BUILD
        boot_digests = tuple(digest for _mtime, digest in _BOOT_CODE)
        current_digests = tuple(digest for _mtime, digest in _code_stamp())
        if not all(current_digests) or current_digests != boot_digests:
            return BUILD
        if build_id() != candidate:
            return BUILD
        BUILD = candidate
        print(f"  Build ID refreshed after clean commit: {BUILD}", flush=True)
        return BUILD


# **Per-file state, so the answer is REMEMBERED rather than recomputed.** The mtime
# says when to look; these say what was found. Without them a changed file would be
# re-hashed on every poll forever, which is the cost the mtime check exists to avoid.
_seen_code_mtimes = [m for m, _ in _BOOT_CODE]
_code_differs = [False] * len(CODE_FILES)


def code_is_stale() -> bool:
    """True when a source file on disk differs from the one this process loaded.

    **An UNREADABLE file reports stale, which is the safe direction.** The three-state
    doctrine applies to network answers, where offline is not stale; a source file
    that has stopped being readable is a real reason to look rather than an absence of
    evidence.

    **An edit reverted before the next poll CLEARS the flag**, because the verdict is
    the digest comparison rather than "has the mtime ever moved".
    """
    for i, path in enumerate(CODE_FILES):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            _code_differs[i] = True
            continue
        if mtime == _seen_code_mtimes[i]:
            continue
        _seen_code_mtimes[i] = mtime
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            _code_differs[i] = True
            continue
        _code_differs[i] = digest != _BOOT_CODE[i][1]
    return any(_code_differs)


RESTART_DEBOUNCE_S = 0.75
_restart_lock = threading.Lock()
_restart_scheduled = False
_restart_requested = threading.Event()
_restart_refused_stamp: tuple[tuple[float, str], ...] | None = None
_restart_problem: str | None = None


def _source_syntax_problem() -> str | None:
    """Explain why the changed Python cannot safely replace this running process."""
    for path in CODE_FILES:
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            line = f" line {exc.lineno}" if exc.lineno else ""
            return f"{path.name}{line}: {exc.msg}"
        except (OSError, UnicodeError) as exc:
            return f"{path.name}: {exc}"
    return None


def _source_startup_problem() -> str | None:
    """Preflight the changed modules in a child before replacing the healthy process."""
    if problem := _source_syntax_problem():
        return problem
    here = pathlib.Path(__file__).resolve().parent
    try:
        checked = subprocess.run(
            [sys.executable, "-c", "from localswim import api_endpoint, board_state"],
            cwd=here,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"startup check could not run: {exc}"
    if checked.returncode == 0:
        return None
    lines = [line.strip() for line in (checked.stderr or "").splitlines() if line.strip()]
    detail = lines[-1] if lines else f"child Python exited {checked.returncode}"
    return f"startup check failed: {detail}"


def request_code_restart(
    server: socketserver.BaseServer,
) -> tuple[bool, str | None]:
    """Schedule one graceful re-exec for changed, syntax-valid Python source."""
    global _restart_scheduled, _restart_problem  # noqa: PLW0603

    current = _code_stamp()
    with _restart_lock:
        if _restart_scheduled:
            return True, None
        if current == _restart_refused_stamp:
            return False, _restart_problem
        _restart_scheduled = True
        _restart_problem = None

    def restart_when_stable() -> None:
        global _restart_scheduled, _restart_refused_stamp  # noqa: PLW0603
        global _restart_problem  # noqa: PLW0603

        latest = current
        while True:
            time.sleep(RESTART_DEBOUNCE_S)
            observed = _code_stamp()
            if observed == latest:
                break
            latest = observed
        boot_digests = tuple(digest for _mtime, digest in _BOOT_CODE)
        latest_digests = tuple(digest for _mtime, digest in latest)
        if latest_digests == boot_digests:
            with _restart_lock:
                _restart_scheduled = False
            return
        if problem := _source_startup_problem():
            with _restart_lock:
                _restart_problem = problem
                _restart_refused_stamp = latest
                _restart_scheduled = False
            print(f"  AUTO-RESTART PAUSED: {problem}", flush=True)
            return

        print("  Python source changed; restarting server.", flush=True)
        _restart_requested.set()
        server.shutdown()

    threading.Thread(
        target=restart_when_stable,
        name="code-restart",
        daemon=True,
    ).start()
    return True, None


class BoardHttpServer(http.server.ThreadingHTTPServer):
    """Finish active mutations before the process replaces itself."""

    daemon_threads = False
    block_on_close = True


_shutdown_requested = threading.Event()
_command_gate = threading.Lock()


def request_service_shutdown(server: socketserver.BaseServer) -> tuple[bool, str]:
    """Quiesce mutations, flush autopush, and schedule one clean server shutdown."""
    with _command_gate:
        if _shutdown_requested.is_set():
            return True, "shutdown is already in progress"
        ok, detail = flush_autopush_for_shutdown(BOARD_PATH)
        if not ok:
            return False, detail
        _shutdown_requested.set()
        threading.Thread(
            target=server.shutdown,
            name="service-shutdown",
            daemon=True,
        ).start()
        return True, detail


def _reexec_if_requested() -> None:
    """Replace this process after its listening socket and request threads close."""
    if not _restart_requested.is_set():
        return
    print("Re-executing with changed Python source.", flush=True)
    # `argparse` parses these same raw option tokens again in `main`. Do not reuse
    # `sys.argv[0]`: a uv Windows console shim reports an extensionless launcher path,
    # which the Python interpreter cannot open as a script.
    os.execv(  # noqa: S606 -- deliberately replace this process with the same interpreter
        sys.executable,
        [sys.executable, "-m", "localswim.api_endpoint", *sys.argv[1:]],
    )


# ---------------------------------------------------------------------------
# OPT-IN AUTOPUSH: THE BOARD MAY REACH ITS OWN GIT REMOTE AFTER IT GOES QUIET
#
# **It is OFF unless the process starts with `--autopush`.** A board contains
# identities, descriptions, comments, and audit history. The server can validate that
# the board has a usable repository and remote; it cannot prove that remote is private.
# A valid new board may still be untracked, and autopush adopts only that exact path.
#
# **The hole this closes:** the board lived on a NAS and reached GitHub only when
# Claude happened to be in session and happened to run `git push`. **Terry drags ten
# cards in the evening and nothing leaves the laptop.** A manual step is a step that
# gets skipped, and it fails silently -- the exact shape of bug this whole card is
# about.
#
# **It runs on its OWN THREAD, not on the request path.** This server is passive: it
# reads the board only when a browser polls `/api/v001/status`. Hanging the push off
# a request
# would mean a closed tab stops the backups, which is the failure wearing a new hat.
#
# **A `git push` takes seconds and MUST NOT block a request** in any case.
_PUSH_QUIET_S = 5.0
_PUSH_TICK_S = 1.0
_PUSH_TIMEOUT_S = 120

# **Five states, and only ONE of them is allowed to raise a pill.** Same doctrine as
# the toolchain banner: loudness tracks what Terry can act on. `off` is a legitimate
# configuration, `pending` is normal, `ok` is the resting state.
_push_lock = threading.Lock()
_push_operation_lock = threading.Lock()
_push_state: dict[str, object] = {"state": "off", "at": 0.0, "detail": "not started"}
_autopush_enabled = False


def _set_push(state: str, detail: str) -> None:
    """Publish the autopush verdict for the status endpoint to read."""
    with _push_lock:
        _push_state["state"] = state
        _push_state["detail"] = detail
        _push_state["at"] = time.time()


def push_status() -> dict[str, object]:
    """A snapshot of the autopush verdict. Copied under the lock, never handed out live."""
    with _push_lock:
        return dict(_push_state)


def _push_once(board_path: pathlib.Path) -> tuple[bool, str]:
    """Serialize one commit-and-push attempt and publish its result."""
    with _push_operation_lock:
        ok, detail = push_board(board_path)
        _set_push("ok" if ok else "failed", detail)
        return ok, detail


def flush_autopush_for_shutdown(board_path: pathlib.Path) -> tuple[bool, str]:
    """Push the final quiescent board snapshot before an intentional shutdown."""
    if not _autopush_enabled:
        return True, "autopush is disabled"
    _set_push("pending", "performing final shutdown push")
    return _push_once(board_path)


def _git(args: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    """Run one git command. Never raises; a timeout comes back as a failed result."""
    try:
        return subprocess.run(  # noqa: S603 -- argv remains separated and shell-free
            ["git", *args],  # noqa: S607 -- PATH lookup is the supported Git discovery
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_PUSH_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def push_unavailable(board_path: pathlib.Path) -> str:
    """Why autopush cannot run here, or "" when it can.

    **A repository with no remote is COMPLETE without a push**, which the global rule
    already says. So "no remote" is a configuration rather than a failure, and it must
    reach the `off` state rather than the `failed` one.
    """
    repo = _git(["rev-parse", "--show-toplevel"], board_path.parent)
    if repo.returncode != 0:
        return "the board's directory is not a git repository"
    root = pathlib.Path(repo.stdout.strip())
    if _git(["check-ignore", "--quiet", "--", str(board_path)], root).returncode == 0:
        return "the board is ignored by git"
    if not _git(["remote"], board_path.parent).stdout.strip():
        return "the board's repository has no remote"
    return ""


def push_board(board_path: pathlib.Path) -> tuple[bool, str]:
    """Commit the board file and push it. Returns (ok, one-line detail).

    **It stages and commits only the exact board path**, which is the safety property
    rather than a style choice. A stray file in the working tree cannot ride along.

    `git diff` does not report untracked files. The original change check therefore
    returned clean for a newly initialized board, and autopush announced success while
    leaving the only snapshot outside Git. Path-bounded porcelain status sees tracked,
    staged, and untracked changes without widening what the later add may stage.
    """
    cwd = board_path.parent
    status = _git(["status", "--porcelain=v1", "--untracked-files=all", "--", str(board_path)], cwd)
    if status.returncode != 0:
        return False, f"git status failed: {status.stderr.strip()[:200]}"
    if not status.stdout.strip():
        return True, "no board change to commit"
    when = datetime.datetime.now(tz=datetime.UTC).astimezone()
    # Terry's stamp format, from his display preferences: `2026-08-19 02:56pm`.
    stamp = f"{when:%Y-%m-%d %I:%M:%S}" + f"{when:%p}".lower()
    message = (
        f"Board: automatic snapshot {stamp}\n\n"
        "Written by api_endpoint.py's autopush thread, not by a person. The card's own\n"
        "history array carries the per-move audit, so this commit exists for\n"
        "durability rather than for granularity.\n"
    )
    add = _git(["add", "--", str(board_path)], cwd)
    if add.returncode != 0:
        return False, f"git add failed: {add.stderr.strip()[:200]}"
    commit = _git(["commit", "-m", message, "--", str(board_path)], cwd)
    if commit.returncode != 0:
        return False, f"git commit failed: {commit.stderr.strip()[:200]}"
    push = _git(["push"], cwd)
    if push.returncode != 0:
        # **The commit STANDS when the push fails, deliberately.** The change is safe on
        # disk and in local history; the next successful push carries it. Rolling it back
        # would throw away the only durable copy that did succeed.
        return False, f"committed, but push failed: {push.stderr.strip()[:200]}"
    return True, "committed and pushed"


def _push_loop(board_path: pathlib.Path) -> None:
    """Watch the board file and push once it goes quiet.

    **DEBOUNCE, in one sentence: every write restarts a short timer, and the push
    happens when the timer runs out.** Terry drags ten cards in thirty seconds and
    GitHub receives ONE commit about five seconds after the last drag.

    **Without it, ten drags mean ten commits and ten pushes**, each taking seconds, and
    they queue up behind each other.

    **It starts armed.** The first pass reconciles whatever changed while no server was
    running, which is exactly the window this card exists to cover.
    """
    why = push_unavailable(board_path)
    if why:
        _set_push("off", why)
        print(f"  autopush  : OFF -- {why}", flush=True)
        return
    print(f"  autopush  : on, {_PUSH_QUIET_S:.0f}s after the last board write", flush=True)
    _set_push("ok", "armed")
    try:
        seen = board_path.stat().st_mtime
    except OSError:
        seen = 0.0
    dirty_at: float | None = time.time()
    while True:
        time.sleep(_PUSH_TICK_S)
        try:
            now_mtime = board_path.stat().st_mtime
        except OSError:
            continue
        if now_mtime != seen:
            seen = now_mtime
            dirty_at = time.time()
            _set_push("pending", "board changed; waiting for it to go quiet")
            continue
        if dirty_at is None or time.time() - dirty_at < _PUSH_QUIET_S:
            continue
        dirty_at = None
        ok, detail = _push_once(board_path)
        if not ok:
            print(f"  AUTOPUSH FAILED: {detail}", flush=True)


def start_autopush(
    board_path: pathlib.Path,
    *,
    enabled: bool,
) -> threading.Thread | None:
    """Start the optional publisher, or publish an explicit disabled status."""
    global _autopush_enabled  # noqa: PLW0603 -- one service owns one autopush mode
    _autopush_enabled = enabled
    if not enabled:
        _set_push("off", "disabled; start the server with --autopush to enable")
        print("  autopush  : OFF -- enable with --autopush", flush=True)
        return None
    worker = threading.Thread(target=_push_loop, args=(board_path,), name="autopush", daemon=True)
    worker.start()
    return worker


# Far below the time a human takes to switch windows, and a stat() against a local file
# rather than anything on a network.
POLL_MS = 400

# The 12-hour pivot, named so the meridiem arithmetic reads as a rule.
NOON = 12

# Below this an hour is one digit, so a column of times needs a pad to stay aligned.
TWO_DIGIT_HOUR = 10

# **U+2007 FIGURE SPACE: one digit wide in a font with tabular figures.** That is the
# character's entire purpose, and Inter is loaded with 'tnum' 1. It replaces the leading
# zero Terry asked to drop, so the column stays aligned and there is no zero to look at.
#
# **Written as an escape rather than pasted, deliberately.** An invisible character sitting
# in source is unreviewable -- anyone reading this line can see exactly what it is.
FIGURE_SPACE = "\u2007"
# **Inline markdown only, and that is a deliberate scope.** A detail or comment carries
# `code`, **bold** and *italic* and nothing else. A markdown library for one field would
# be a dependency for a job this size.
INLINE = (
    # **` -- ` becomes an em dash. Card #0079**, and Terry's report was exact: *"double
    # dash renders with a gap in the middle. Not sure if it's a property of our typeface
    # or an issue within our control."* **It is both.**
    #
    # **Measured in the browser at 13px:** one hyphen is 8.44px wide, `--` is 16.86px --
    # exactly double, so no ligature fires -- and Inter's hyphen carries wide side
    # bearings, which is the notch he can see. An em dash is 13.00px and solid, so the
    # replacement is NARROWER as well as unbroken.
    #
    # **SPACES ON BOTH SIDES ARE REQUIRED, and that is the whole safety of it.** The
    # board is full of `--verify`, `--set-detail`, `--comment`: 87 of them, and every
    # one keeps its hyphens because nothing follows the `--` but a letter. The two
    # `-->` arrows survive for the same reason. 530 real em dashes are converted.
    #
    # **The DATA is not touched.** Terry writes `--` and the JSON stays ASCII and
    # greppable; only the rendering changes. Storing the glyph would make the file
    # harder to search for the sake of a screen.
    (re.compile(r"(?<=\s)--(?=\s)"), "—"),
    # **A markdown heading becomes a heading. Card #0086's second half.**
    #
    # **This only became worth doing once `pre-wrap` landed.** With newlines collapsed a
    # heading had nowhere to sit, so `## The measurement` read as literal hashes in the
    # middle of a paragraph -- noise in the one place Terry is trying to skim.
    #
    # **Anchored to the START OF A LINE with `re.MULTILINE`**, which is what keeps
    # `#0086` and `#137` alone: a ticket reference has one hash and no space after it,
    # and it is almost never the first thing on a line.
    #
    # **Its own class rather than `<strong>`**, because `**bold**` already means
    # emphasis here. Two different things reading identically would cost the structure
    # this exists to add.
    # **The trailing newline is CONSUMED, and without that the spacing is wrong.**
    # `.mdh` is `display: block`, so it already occupies its own line; leaving the
    # author's newline in as well renders a heading, a blank line, and then another
    # blank line before the body. Absorbing one puts it back to a single gap.
    (re.compile(r"^#{2,6}[ \t]+(.+)$\n?", re.MULTILINE), r'<b class="mdh">\1</b>'),
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"\*([^*]+)\*"), r"<em>\1</em>"),
)

# Set once at startup by `main`. One process serves one board.
BOARD_PATH: pathlib.Path = pathlib.Path("board.json")


def source_checkout_board_problem(path: pathlib.Path) -> str:
    """Refuse sensitive board data outside the ignored directory in this checkout."""
    source = pathlib.Path(__file__).resolve().parent
    repo = _git(["rev-parse", "--show-toplevel"], source)
    if repo.returncode != 0:
        return ""
    root = pathlib.Path(repo.stdout.strip()).resolve()
    try:
        relative = path.resolve().relative_to(root)
    except ValueError:
        return ""
    safe = root / LOCAL_BOARD_DIR
    if not relative.parts or relative.parts[0] != LOCAL_BOARD_DIR:
        return (
            f"refusing board data outside the dedicated local-data directory; "
            f"put this board under {safe}"
        )
    ignored = _git(["check-ignore", "--quiet", "--", str(path)], root)
    if ignored.returncode == 0:
        return ""
    return f"refusing board data because git does not ignore {safe}"


class RevisionConflict(board_state.BoardError):
    """A command was based on a board snapshot that is no longer current."""


class BoardStore:
    """Serialize board commands and publish only snapshots that reached disk."""

    def __init__(
        self,
        path: pathlib.Path,
        policy: board_state.TransitionPolicy | None = None,
    ) -> None:
        self.path = path
        self._external_policy = policy is not None
        self._mutex = threading.RLock()
        self._board = board_state.load(path, policy)
        self.policy = self._board.policy

    def snapshot(self) -> board_state.Board:
        """Return an isolated current snapshot, refreshing valid external edits."""
        with self._mutex:
            board = board_state.load(self.path, self.policy if self._external_policy else None)
            self.policy = board.policy
            self._board = board
            return copy.deepcopy(board)

    def execute(
        self, expected_revision: int, command: Callable[[board_state.Board], str]
    ) -> tuple[str, int]:
        """Apply one command under the file lock and commit one new revision."""
        with self._mutex:
            active = self.policy if self._external_policy else None
            with board_state.edit(self.path, active) as candidate:
                if candidate.revision != expected_revision:
                    raise RevisionConflict(
                        f"board revision is {candidate.revision}, not "
                        f"{expected_revision}; refresh and try again"
                    )
                result = command(candidate)
                problems = candidate.verify()
                if problems:
                    raise board_state.BoardError(
                        "command would leave an invalid board: " + "; ".join(problems)
                    )
                candidate.revision += 1
                committed = candidate
            # `board_state.edit` saves on exit. Publish in memory only after that succeeds.
            self._board = copy.deepcopy(committed)
            return result, committed.revision

    def reload_policy_if_changed(self) -> str | None:
        """Adopt a complete embedded policy edit without changing other stores."""
        with self._mutex:
            if self._external_policy:
                self._board = board_state.load(self.path, self.policy)
                actors = {user.id for user in self._board.users}
                configured = frozenset((self._board.browser_user, self._board.cli_user))
                self.policy, message = self.policy.reload_if_changed(actors, configured)
                self._board.policy = self.policy
                return message
            fresh = board_state.load(self.path)
            changed = fresh.policy.to_json() != self.policy.to_json()
            self._board = fresh
            self.policy = fresh.policy
            return f"embedded policy reloaded: {len(self.policy.states)} lanes" if changed else None


STORE: BoardStore | None = None
BROWSER_TOKEN = secrets.token_urlsafe(32)
CLI_TOKEN = secrets.token_urlsafe(32)


def service_file(path: pathlib.Path) -> pathlib.Path:
    """Machine-local rendezvous file used by the CLI to find this service."""
    return board_state.service_descriptor_path(path)


def publish_service(path: pathlib.Path, port: int) -> None:
    """Atomically publish the CLI endpoint and its process credential."""
    target = service_file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    doc = {
        "schema": 1,
        "pid": os.getpid(),
        "host": HOST,
        "port": port,
        "token": CLI_TOKEN,
        "board": str(path.resolve()),
    }
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        with contextlib.suppress(OSError):
            tmp.chmod(0o600)
        tmp.replace(target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def remove_service(path: pathlib.Path) -> None:
    """Remove only the rendezvous file published by this process."""
    target = service_file(path)
    try:
        raw = cast("board_state.JsonValue", json.loads(target.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return
    if isinstance(raw, dict) and raw.get("token") == CLI_TOKEN:
        target.unlink(missing_ok=True)


# **Inter, bundled rather than linked, under the SIL Open Font License 1.1.**
# `src/localswim/vendor/typefaces/inter/README.md` carries the copyright, license, and
# three conditions that make redistributing it here compliant. Terry raised the
# question himself and proposed this exact layout.
#
# **It is NOT installed on his machine**, checked rather than assumed, so naming it in a
# CSS font stack alone would have fallen back to Segoe UI and looked almost right --
# the kind of failure nobody investigates. And a Google Fonts `<link>` would make a
# LOCAL tool reach the internet to render, breaking the board on a plane.
FONT_DIR = pathlib.Path(__file__).resolve().parent / "vendor" / "typefaces" / "inter"

FONTS = {
    "/fonts/Inter-latin.woff2": "Inter-latin.woff2",
    "/fonts/Inter-latin-ext.woff2": "Inter-latin-ext.woff2",
}


#: The tab icon. **Card #0080**, and Terry called it what it is: "visual nicety".
#:
#: **Three bars of different heights, which is what this board looks like from across
#: the room.** An icon has about 16 CSS pixels to say what a tab is, so it carries the
#: SHAPE of the thing rather than a picture of it -- unequal lanes on a dark field.
#:
#: **SVG rather than an `.ico` file**, so it stays text in the repository, scales to
#: every tab-bar density, and adds no binary to a tool whose whole point is being
#: readable. Every current browser accepts one.
#:
#: **The colors are the board's own.** `--barbg` for the field, then the actor colors
#: and the live green -- so the tab and the page agree without a second palette to keep
#: in step.
#:
#: **THE BLUE IS THE LIGHT VARIANT, and that is not a whim.** Terry looked at the icon
#: and said the blue was too dark against the black. Measured against the `#1D2125`
#: field: `#0052CC` contrasts at **2.37**, while the amber is **7.20** and the green
#: **8.16** -- three times darker, so one bar of three receded. `#85B8FF` lands at
#: **7.94**, between the other two, and the three bars now carry equal weight.
#:
#: **`--terry` stays `#0052CC` everywhere else**, because everywhere else it sits on
#: white. A color is only dark or light relative to what is behind it, and this is the
#: one surface here with a dark ground.
FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#1D2125"/>'
    '<rect x="6"  y="8"  width="5" height="16" rx="2.5" fill="#85B8FF"/>'
    '<rect x="13.5" y="13" width="5" height="11" rx="2.5" fill="#E2A100"/>'
    '<rect x="21" y="18" width="5" height="6"  rx="2.5" fill="#4BCE97"/>'
    "</svg>"
)


def brand_data_uri() -> str:
    """`FAVICON` as a CSS `url()` value, so the mark has exactly ONE definition.

    **Card #0095.** The bar carries the same mark the browser tab does, and the obvious
    way to do that is to paste the SVG into the stylesheet. **That would be the same fact
    stored twice**, and the two copies drift the first time anybody recolors one bar.

    **The `#` MUST be escaped or the URI ends at the first hex color.** A `#` opens the
    fragment, so `fill="#4BCE97"` would truncate the whole document to `<svg ... fill="`
    and the browser draws nothing, silently. `urllib.parse.quote` handles it along with
    the angle brackets, the quotes and the spaces.

    **THE viewBox IS CROPPED TO THE THREE BARS, and that is measured rather than
    aesthetic.** The mark's ground is `#1D2125` and `--barbg` is `#1D2125` -- the same
    hex, so on this bar the rounded square paints nothing while still occupying half the
    box. A 26px element rendered 13px of visible ink, which is why the first attempt
    looked small no matter what the number said. The bars live at `x=6..26, y=8..24`,
    so `viewBox="6 8 20 16"` makes the element's height BE the mark's height.

    **The ground rect stays in the document on purpose.** It is invisible here by
    identity rather than by deletion, so a future bar color makes it appear rather than
    leaving a hole -- and the tab icon keeps the square it needs against a browser's own
    chrome.
    """
    cropped = FAVICON.replace('viewBox="0 0 32 32"', 'viewBox="6 8 20 16"')
    return "data:image/svg+xml," + urllib.parse.quote(cropped, safe="")


def inline(text: str) -> str:
    """Escape HTML, THEN apply the three inline spans.

    **That order is the whole safety of it.** Reversing it would escape the markup this
    function just produced and leave the content raw.
    """
    out = html.escape(text)
    for pattern, repl in INLINE:
        out = pattern.sub(repl, out)
    return out


#: How long a finished card stays on screen. **Terry's number, card #0063**: *"cards in
#: COMPLETED for >= 24 hours should not be shown"*.
OLD_AFTER = datetime.timedelta(hours=24)


def is_old(item: board_state.Item) -> bool:
    """Whether a COMPLETED card has sat there long enough to hide. **Card #0063.**

    **Measured from when it ENTERED `completed`, not from when it was created.** A card
    filed in March and finished this morning is fresh; the question is how long the
    result has been on screen.

    **Only `completed` ages.** Nothing else on this board is finished, so nothing else
    has a reason to disappear -- and a card that vanished from `Blocked` after a day
    would be hiding exactly the thing that needs chasing.

    **An unknown timestamp is NOT old.** `state_since` is `None` for a migrated card, and
    the safe direction is to keep showing it: a card wrongly hidden is invisible, while a
    card wrongly shown is merely one extra row.
    """
    if item.state != "completed":
        return False
    since = item.state_since
    if since is None:
        return False
    return datetime.datetime.now().astimezone() - board_state.parse_stamp(since) >= OLD_AFTER


#: Where sRGB's transfer curve switches from its linear foot to the power segment.
#: **A constant from the specification, not a tuning knob** -- WCAG 2 and sRGB both
#: name this exact value, and changing it would stop these numbers meaning what the
#: standard says they mean.
SRGB_KNEE = 0.04045


def _relative_luminance(color: str) -> float:
    """WCAG relative luminance of `#RRGGBB`."""
    text = color.lstrip("#")
    channels: list[float] = []
    for index in (0, 2, 4):
        c = int(text[index : index + 2], 16) / 255.0
        channels.append(c / 12.92 if c <= SRGB_KNEE else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def on_white(color: str) -> float:
    """Contrast ratio of one color against the page's white ground."""
    return 1.05 / (_relative_luminance(color) + 0.05)


#: WCAG AA for normal text. The names this is used for are 12px, well under the
#: large-text threshold, so the strict bar applies.
TEXT_CONTRAST = 4.5


def readable_on_white(color: str) -> str:
    """The same hue, darkened just enough to be readable as TEXT on white. Card #0089.

    **A configured user color is an ACCENT, and an accent is not automatically text.**
    Claude's `#E2A100` is right on a lane border and renders its owner chip and every
    audit-trail entry at **2.25:1** -- measured in the browser, against a 4.5 bar.

    **Derived rather than hand-picked, so a third user needs no second color.** Somebody
    adding themselves to the board picks one value they like; this finds the readable
    version of it. Hand-tuning a second hex per person is the kind of chore that gets
    skipped, and the skip is invisible.

    **It scales toward black and keeps the hue**, so the darker variant still reads as
    that person's color rather than as a different one. A color that already passes is
    returned untouched -- Terry's blue is 8.6:1 and does not move.
    """
    if on_white(color) >= TEXT_CONTRAST:
        return color

    text = color.lstrip("#")
    r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
    # **Descending, so the FIRST hit is the lightest shade that clears the bar.**
    # Going darker than necessary would throw away the hue this exists to keep.
    for step in range(99, 0, -1):
        scale = step / 100
        shade = f"#{round(r * scale):02X}{round(g * scale):02X}{round(b * scale):02X}"
        if on_white(shade) >= TEXT_CONTRAST:
            return shade
    return "#000000"


def user_css() -> str:
    """One color variable and four rules per configured user. **Card #0072.**

    **The stylesheet named `terry` and `claude` in nine places until this card**, so a
    third person would have rendered with no color at all -- their name gray in the
    audit trail, their owner chip unstyled, their lane accent missing. None of that
    fails loudly; it just looks broken for one person.

    **`--accent` is the browser user's color**, and it is what every brand-colored
    control uses: the post button, the create button, the drop outline. Those were
    `var(--accent)` and they mean *this deployment's primary*, not *Terry*.

    **Generated rather than templated, because the count is unknown.** Two users is
    today; the whole point of the card is that it stops being a fixed number.
    """
    lines: list[str] = []
    for u in board_state.USERS:
        lines.append(f"  --user-{u.id}: {u.color};")
        # **A second variable per user, card #0089.** The accent paints borders and
        # fills, where a light color is fine. The ink paints NAMES on white, where it
        # is not. See `readable_on_white`.
        lines.append(f"  --user-{u.id}-ink: {readable_on_white(u.color)};")
    lines.append(f"  --accent: var(--user-{board_state.BROWSER_USER});")
    out: list[str] = [":root {", *lines, "}"]

    for u in board_state.USERS:
        var = f"var(--user-{u.id})"
        ink = f"var(--user-{u.id}-ink)"
        out += [
            # The lane accent, when a lane belongs to exactly this actor. A BORDER, so
            # the light accent is correct here.
            f'.lane[data-css="{u.id}"] {{ border-top-color: {var}; }}',
            # **Text from here down, so all of it takes the ink.** Claude's amber
            # measured 2.25:1 on white as a name and was unreadable.
            f'.lane[data-css="{u.id}"] .owner {{ color: {ink}; }}',
            # Their name, wherever it appears: the audit trail and a comment head.
            f".trail .who.{u.id} {{ color: {ink}; }}",
            f".comment .head .who.{u.id} {{ color: {ink}; }}",
            # **The chip keeps the ACCENT border and takes the INK label**, so it still
            # reads as that person's color while the word stays legible.
            f"#p-owner.{u.id} {{ color: {ink}; border-color: {var}; }}",
        ]
    return "\n".join(out)


def when(stamp: str) -> str:
    """An ISO stamp as `2026-08-18 2:56pm`, or unchanged if it will not parse.

    **Terry's house format**, which he named in one line: ISO 8601 date, a space, then
    `HH:MM` with a lower-case meridiem closed up. The date sorts and never reads
    ambiguously; the time reads the way a person says it.

    **Built by hand rather than by `strftime`**, because `%p` emits `AM`/`PM` in the
    C locale and the platform decides in others -- so lowering it would be a
    locale-dependent guess. `%I` also zero-pads, which he does not want.

    **Unchanged rather than blank on failure.** A card migrated from the markdown log
    carries whatever its old date column said, and showing that beats showing nothing.
    """
    try:
        moment = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    # `% NOON or NOON` is what makes midnight read `12:05am` and noon `12:00pm`,
    # which a plain modulo gets wrong in both directions.
    hour = moment.hour % NOON or NOON
    meridiem = "am" if moment.hour < NOON else "pm"
    # **A single-digit hour is padded with a FIGURE SPACE, not a zero.** Terry asked
    # whether the leading zero could go and whether that breaks anything: it can, and
    # it would have, so this pads with something invisible instead.
    #
    # **U+2007 FIGURE SPACE is exactly the width of a digit** in a font with tabular
    # figures, which is what it exists for. Inter is loaded with
    # `font-feature-settings: 'tnum' 1`, so ` 2:56pm` and `12:05pm` put their colons
    # in the same column while only one of them shows two characters.
    #
    # **Both halves of his ask survive.** No leading zero to look at, and: "in the
    # name of all that is holy keep the datetimes cleanly aligned so all dates line
    # up and times line up."
    #
    # **It stays a plain string**, which matters -- `at.textContent = h.when` and the
    # comment header both take it as text, and returning markup here would force both
    # onto `innerHTML` for a cosmetic fix.
    pad = FIGURE_SPACE if hour < TWO_DIGIT_HOUR else ""
    return f"{moment:%Y-%m-%d} {pad}{hour}:{moment:%M}{meridiem}"


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>%TITLE%</title>
<!-- **Card #0080.** Named explicitly rather than left to the browser's `/favicon.ico`
     guess, which this server does not serve and would answer 404 on every load.

     **The build hash is in the URL because THIS ICON ALREADY CHANGED ONCE.** Its blue
     was too dark against the field and was relit the same afternoon, and the route
     serves it with `max-age=86400` on purpose. Without a version key Terry would have
     kept seeing yesterday's icon for a day.

     **Keying it on `%BUILD%` makes it self-busting**: any change to the server is a new
     URL. The route ignores the query string, so this costs nothing to serve.

     **It is NOT here to fix a missing icon.** One appeared to be missing and was simply
     Chrome fetching favicons asynchronously -- it arrived a moment later, with the
     server having answered 200 the whole time. -->
<link rel="icon" type="image/svg+xml" href="/favicon.svg?v=%BUILD%">
<style>
  /* Inter, served from this repository. See the FONTS note in api_endpoint.py. */
  @font-face {
    font-family: 'Inter'; font-style: normal; font-weight: 100 900;
    font-display: swap; src: url('/fonts/Inter-latin.woff2') format('woff2');
    unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6,
      U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122,
      U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD;
  }
  @font-face {
    font-family: 'Inter'; font-style: normal; font-weight: 100 900;
    font-display: swap; src: url('/fonts/Inter-latin-ext.woff2') format('woff2');
    unicode-range: U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7,
      U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F,
      U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F,
      U+A720-A7FF;
  }

  /* LIGHT, because Terry asked in exactly those words: "turn off dark mode I hate
     it." No media query and no toggle -- one look, chosen. */
  :root {
    --bg: #F4F5F7; --lane: #EBECF0; --card: #FFFFFF;
    --ink: #172B4D; --dim: #5E6C84; --line: #DFE1E6;
    --handoff: #1F845A; --done: #5E6C84;
    /* **THE BAR IS DARK AND THE APP IS LIGHT.** Terry, 2026-08-19: "the top title
     bar for the app blurs/blends with my chrome bookmarks bar. Go dark (something
     from dark gray to pure black, your call) with high contrast (maybe white?)
     text." One 1px hairline was the entire boundary between the browser and the
     app, and both sides of it were white.

     **`#1D2125` is Atlassian's darkest neutral**, so the bar goes dark without
     importing a color from nowhere -- this whole palette is theirs. **Pure black was
     considered and refused**: it makes a hard edge that pulls the eye UP into the
     browser, and it leaves nowhere darker to go for a more severe state.

     **This is a dark HEADER on a light app, not a dark mode.** The lanes, the cards
     and the drawer stay light. */
  --barbg: #1D2125; --barink: #FFFFFF; --bardim: #9AA0A6;
  /* **The LIVE badge's ground had to lighten, and that is the dark bar's real
     cost.** `#14663F` on `#1D2125` is dark-on-dark: the badge stopped reading as a
     badge, and this is the one element whose job is to be believed at a glance.

     **`#1F845A` was picked by MEASUREMENT, not by eye, because two thresholds pull
     against each other.** A darker green reads better under white text and worse
     against the bar; a lighter one does the reverse. Measured in the page:

         #216E4E   badge vs bar 2.63   white on badge 6.17   FAILS the 3:1 block rule
         #1F845A   badge vs bar 3.48   white on badge 4.66   passes both
         #22A06B   badge vs bar 4.87   white on badge 3.33   FAILS AA on the text

     **`#1F845A` is also already in this palette as `--handoff`**, so the bar gains
     no color from nowhere. */
  --live: #1F845A;
  /* **The dot INVERTS on dark, and a mechanical port would have made it
     invisible.** It idles at `opacity: .10` and breathes up; a dark green at 10%
     on near-black is nothing at all. A bright green is dim when faded and obvious
     when lit, which is what the heartbeat needs. */
  --livedot: #4BCE97;
    /* **The comment badge's ground.** Atlassian's dark neutral, chosen so the badge
     is unmistakably darker than the P3 pill (`#5E6C84`) and cannot be mistaken for
     a priority. White on it measures about 7.5:1. */
  --mark: #42526E;
  /* **A LIGHTNESS RAMP, not only a hue ramp. Card #0088.** Terry asked whether these
     colors were inclusive, after being burned presenting to a red-green colorblind
     colonel.

     **Two defects turned up and the second one was everybody's.** P1, P2, P4 and P5 all
     FAILED WCAG AA against their own white text -- 3.30, 3.75, 3.10 and 1.95. That is a
     readability bug for every reader, not only for a dichromat.

     **Measured under simulated protanopia, deuteranopia and tritanopia**, the old P1 and
     P2 sat 11 apart out of 255. Indistinguishable. P0 and P2 sat 33 apart.

     **Hue is the channel dichromacy takes away; lightness is the one it leaves.** Each
     step is now clearly darker or lighter than its neighbor, and the worst ADJACENT
     separation across all three simulations is 56 rather than 11.

     **The hues barely move**, because Terry reads red-to-amber-to-gray fluently already
     and nothing here asks him to relearn it. The old red simply slides from P0 down to
     P1, and P0 takes a deeper one. */
  --p0: #8C1D12; --p1: #C9372C; --p2: #FFAB00;
    --p3: #5E6C84; --p4: #8993A4; --p5: #C7CDD6;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 14px/1.5 'Inter', 'Segoe UI', system-ui, sans-serif;
         font-feature-settings: 'cv05' 1, 'tnum' 1; }

  /* **`color-scheme: dark` is load-bearing and is NOT decoration.** The alerts
     control is a native `<input type="checkbox">`, and a native control paints
     itself light-on-light against a dark parent unless the scheme is declared. **It
     keeps WORKING, which is exactly why it would have shipped broken.** */
  #bar { position: sticky; top: 0; z-index: 20; display: flex; gap: 14px;
         align-items: center; padding: 9px 14px; background: var(--barbg);
         color: var(--barink); color-scheme: dark;
         border-bottom: 1px solid #000000; font-size: 12px; }
  #bar .grow { flex: 1; }
  /* **The metadata stays dim while #0055 is discussed.** Terry asked for ALL gray
     in this bar to go white; Claude's counter-argument is on that card and it is in
     Needs Terry. **The CTAs went white in #0054 and the metadata did not**, so the
     bar currently has two tiers on purpose rather than by omission. */
  #bar #live, #bar #alerts-wrap, #bar .meta { color: var(--bardim); }
  /* The product wordmark and board name share the approved 15px title typography but
     have different emphasis. `localswim` uses the logo's light blue instead of white,
     so the static mark and word read as one product identity. The board name retains
     bright white because it is the changing context inside that product. */
  #wordmark, #title { font-weight: 700; letter-spacing: -.01em; font-size: 15px;
                      white-space: nowrap; }
  #wordmark { color: #85B8FF; }
  #title { color: var(--barink); margin-left: 22px; }
  /* **ONLY CALLS TO ACTION LIVE HERE.** Terry: "No other stats up there, just
     calls to action." Open counts and in-progress counts were noise -- the lane
     headers already carry them, and a number that never asks for anything trains
     the eye to skip the whole bar.

     **Zero is plain text; non-zero is a hazard pill.** So the bar is quiet when
     there is nothing to do and impossible to miss when there is, which is this
     project's loudness rule applied to a status line. */
  /* **The gap on the left separates the CTAs from the project name**, so they read
     as one group rather than as a continuation of the title. Terry: "CTA's should
     stand in a group." */
  #counts { display: flex; gap: 8px; align-items: center; margin-left: 22px; }
  /* **With every CTA at zero this element has no children and the margin would still
     be there** -- a 22px hole between the project name and nothing. Card #0056 hides
     a zero pill, so the all-quiet board is now a reachable state rather than a
     theoretical one. */
  #counts:empty { margin-left: 0; }
  /* A persistent toggle rather than a button that vanishes. See the note on
     `syncAlerts` for why the browser permission and this preference are two
     different things. */
  #alerts-wrap { font-size: 11px; font-weight: 600; color: var(--dim);
                 display: flex; gap: 4px; align-items: center; cursor: pointer;
                 user-select: none; white-space: nowrap; }
  #alerts-wrap.blocked { cursor: not-allowed; opacity: .55; }
  #alerts { margin: 0; cursor: inherit; }
  /* **A zero-state CTA is WHITE, not dim.** Terry, 2026-08-19: "CTA entries at 0
     should be rendered white to stand out against dark title bar." Card #0054.

     **The bar's rule still holds and its wording needed sharpening.** It reads
     "Zero is plain text; non-zero is a hazard pill" -- and the word carrying the
     distinction is PILL, not the color. A saturated `#FFD400` ground under black
     text against plain white text is a bigger step than two grays ever were.

     **This is the CTA row only.** The metadata beside it stays dim; see #0055. */
  .cta { font-size: 11px; font-weight: 700; letter-spacing: .03em;
         color: var(--barink); padding: 3px 8px; border-radius: 4px; }
  /* **Warning-sign yellow, not highlighter yellow.** Terry asked for higher
     contrast, and the lever is SATURATION rather than contrast ratio -- black on
     the old #F5CD47 already measured about 11:1, well past AAA, so the pill was
     never hard to READ. It just did not read as a hazard. #FFD400 is the pure
     unmuted yellow of a road sign and takes black past 13:1. */
  .cta.hot { background: #FFD400; color: #000000; }
  /* **THE HEARTBEAT.** Terry, 2026-08-18, after being burned by a dashboard at
     work: *"'Written 13:52:53 Reload 1' ain't gonna do it for my brain due to
     emotional trauma."*

     **He is right, and it is the same defect this page exists to prevent.** A
     reload counter only moves when the FILE changes, so `Reload 1` after three
     quiet hours means "nothing happened" and "the poll died at 13:52" equally.
     **A stale view and a healthy view produced the identical pixel.**

     So the dot pulses on every successful poll and the bar counts the seconds
     since the last one. **Proof of life has to be something that MOVES**, because
     anything static is indistinguishable from a frozen page. */
  /* **A CONSTANT-SIZE DOT THAT BREATHES.** Terry: "have it be a constant size but
     do its heartbeat as a fade in/-out. Fade in linear over 1s, then fade out
     linear over 1s. very chillax." Then, having watched it: "go bigger on breather
     radius and slow down to 2s/2s." So 13px and a 4s cycle, linear both ways, no
     scaling -- nothing in the corner of his eye jumping about.

     **The animation runs ONLY while the poll is confirmed live**, and that is not
     decoration. A CSS animation left running unconditionally would keep breathing
     over a dead server, which is precisely the reassuring-but-false signal this
     whole bar exists to kill. `renderLive` owns the class.

     **A 4s cycle is FIFTY TIMES slower than the poll, and that gap is fine.** The
     dot says "alive"; it was never a per-request indicator. `POLL_MS` is the
     guarantee and this is the mood. */
  @keyframes breathe {
    0%   { opacity: .10; }
    50%  { opacity: 1; }
    100% { opacity: .10; }
  }
  #dot { width: 13px; height: 13px; border-radius: 50%; background: var(--livedot);
         flex: 0 0 auto; opacity: .10; transition: background .2s; }
  #dot.alive { animation: breathe 4s linear infinite; }
  /* Solid, not breathing. A stopped heart does not pulse. */
  #dot.stale { background: var(--p0); opacity: 1; animation: none; }
  /* **THE MARK IS STATIC, AND THAT IS THE ANSWER TO CARD #0095.** Terry asked whether
     the heartbeat itself should become the logo, breathing. It should not, and the
     reason is measurable rather than aesthetic: the mark's third bar is `#4BCE97`,
     which IS `--livedot`, and its middle bar is `#E2A100`, a near neighbor of the
     `#FFD400` hazard pills. **Breathing the logo would put the bar's two meaning-bearing
     colors into a decorative object.** At `opacity: .10` three 5-unit bars also vanish
     where a solid disc does not, and a stale logo has no honest red form -- the
     experiment had to swap SHAPES, which makes the indicator two different objects.

     **So the dot keeps its one job and the logo carries identity.** Terry, 2026-08-19,
     having looked at all three: *"I like your second stab better. Keeping the brand
     static looks good."*

     **The mark is locked to the product name, not the board name.** Putting a board's
     title directly beside the logo made the board look like the product. The wrapper
     now contains the mark plus the literal `localswim` wordmark; the board title is a
     separate bright-white sibling.

     **Both sides of the product lockup use the same 36px separation.** `#bar` supplies
     14px between children. The lockup's 22px left margin makes 36px from the breathing
     dot to the logo, and the board title's 22px left margin makes the same 36px from
     the wordmark to the board name. */
  #brandtitle { display: flex; align-items: center; gap: 10px; margin-left: 22px; }
  #brand { width: 28px; height: 22px; flex: 0 0 auto;
           background-image: url("%BRANDURI%");
           background-size: contain; background-repeat: no-repeat; }
  .meta { color: var(--dim); font-variant-numeric: tabular-nums; }
  #live { color: var(--dim); font-variant-numeric: tabular-nums; }

  /* **A BADGE, not colored text.** Terry: "go high contrast green with bold white
     LIVE badge and we're good." White on #14663F is 7.4:1, past WCAG AA for
     small text -- which matters because this is the one element on the page whose
     job is to be believed at a glance. */
  #badge { font-size: 11px; font-weight: 700; letter-spacing: .06em;
           color: #FFFFFF; background: var(--live); border-radius: 4px;
           padding: 3px 8px; flex: 0 0 auto; transition: background .2s; }
  #badge.warn { background: var(--p1); }
  #badge.dead { background: var(--p0); }

  /* **SEVEN LANES SHARE THE WIDTH RATHER THAN OVERFLOWING IT.** Terry works with the
     terminal on the left and the browser on the right, so the viewport is about half a
     screen -- and at a fixed 268px the seventh lane was clipped off the edge with a
     horizontal scrollbar under it. **A board you have to scroll sideways to see is not
     a board**, because the whole point is taking it in at a glance.

     `flex: 1 1 0` divides whatever is there. **The floor is deliberately LOW**, because
     Terry ruled on the trade: *"I'm okay with cards being taller than they are wide;
     vertical scroll is easy and I don't think it'll get deep other than backlog and
     completed."* So a narrow lane wraps its titles rather than pushing a lane off the
     edge -- **the horizontal overflow is the failure, and card height is not.**

     118px keeps all seven visible down to about a 900px viewport. Below that the row
     scrolls, which is the honest behavior for a window too small for the board. */
  #board { display: flex; gap: 8px; padding: 10px; align-items: stretch;
           overflow-x: auto; height: calc(100vh - 44px); }
  .lane { background: var(--lane); border-radius: 8px;
          flex: 1 1 0; min-width: 118px; max-width: 340px;
          display: flex; flex-direction: column;
          border-top: 3px solid var(--dim); }
  /* **Per-user lane accents are GENERATED, card #0072.** See `user_css()`. */
  .lane[data-css="handoff"] { border-top-color: var(--handoff); }
  .lane[data-css="done"]    { border-top-color: var(--done); }

  /* **+25% on the lane titles, 12px to 15px.** Terry asked for +50% first, looked
     at 18px and pulled it back: "that may have overshot for my yes." The count sits
     in a circle sized in `em` of this same rule, so the two track each other and a
     third adjustment needs one number changed rather than two.

     The title, count, and `+` share a top edge. Centering them against the title block
     made both controls sit lower only when a lane name wrapped onto a second line. */
  .lane h2 { margin: 0; padding: 9px 12px 3px; font-size: 15px; font-weight: 700;
             text-transform: uppercase; letter-spacing: .02em;
             display: flex; gap: 8px; align-items: flex-start; line-height: 1.15; }
  /* **Card #0066: the count and the `+` grew into the space under them.** Terry:
     "Increase size of swimlane badge for ticket count and + button... Have a whole
     blank row to grow into below where they are."

     **The header row is what grows, and that is the point.** These are flex children,
     so a taller badge makes `h2` taller and pushes the owner line down -- which is the
     blank row he was pointing at. Nothing else moves.

     `.61em -> .95em` on the text, `1.64em -> 1.8em` on the circle. The circle is sized
     in ITS OWN em, so both terms compound: about 15px across becomes about 26px. */
  .lane h2 .n { margin-left: auto; background: #FFFFFF; color: var(--dim);
                font-size: .95em; font-weight: 700;
                height: 1.8em; min-width: 1.8em; border-radius: 50%;
                display: inline-flex; align-items: center; justify-content: center;
                padding: 0 .4em; box-sizing: border-box; }
  /* Ownership is stated in words under every lane title, not implied by a color.
     Terry: "Real clear ownership per lane." A legend elsewhere would make him
     remember which color meant what. */
  .owner { padding: 0 12px 8px; font-size: 10px; font-weight: 600;
           letter-spacing: .02em; color: var(--dim); }
  /* **Card #0063.** It sits under the owner line rather than in the `h2`, because that
     row is already a flex container holding the title, the count and the `+`, and a
     fourth item there would squeeze the lane name on the narrow columns.

     Sized and colored like the owner line it follows -- this is a lane-level note, not
     a call to action, and the hazard styling on this board is reserved for the three
     counters that genuinely ask Terry for something. */
  .unhide { display: flex; align-items: center; gap: 5px; cursor: pointer;
            padding: 0 12px 8px; margin-top: -4px;
            font-size: 10px; font-weight: 600; color: var(--dim); }
  .unhide input { margin: 0; width: 11px; height: 11px; cursor: pointer; }
  .unhide:hover { color: var(--ink); }

  .lane[data-css="handoff"] .owner { color: var(--handoff); }

  .cards { padding: 0 8px 10px; overflow-y: auto; flex: 1; }
  .lane.over { outline: 2px solid var(--accent); outline-offset: -2px; }
  .lane.deny { outline: 2px dashed var(--p0); outline-offset: -2px; }

  /* **PRIORITY AND TITLE, NOTHING ELSE.** Terry: "for the cards, only show P1-P5 &
     title; I need to click in for description or comment history or audit trail."
     A card is a thing you scan; the panel is a thing you read. */
  .card { background: var(--card); border-radius: 6px; padding: 7px 9px;
          margin-bottom: 7px; box-shadow: 0 1px 1px rgba(9,30,66,.25);
          display: flex; gap: 8px; align-items: flex-start; cursor: pointer; }
  .card.dragging { opacity: .4; }
  /* **A card that MOVED glides from where it was.** Terry asked to watch Claude's
     moves happen: "a motion animation would be fun af for me to watch in realtime."
     The transform is set by the FLIP pass in paint(); this rule only says how it
     travels. `will-change` keeps it on the compositor so a board full of cards does
     not judder. */
  .card.flip { transition: transform .55s cubic-bezier(.22, 1, .36, 1);
               will-change: transform; z-index: 5; position: relative; }
  /* A brief wash of the destination lane's meaning, so the eye lands on the card
     that actually changed rather than hunting for it. */
  @keyframes landed {
    0%   { box-shadow: 0 0 0 3px rgba(0, 82, 204, .55); }
    100% { box-shadow: 0 1px 1px rgba(9, 30, 66, .25); }
  }
  .card.landed { animation: landed 1.1s ease-out; }
  .card:hover { background: #FAFBFC; }
  .pri { font-size: 10px; font-weight: 700; color: #FFFFFF; border-radius: 3px;
         padding: 1px 5px; letter-spacing: .02em; flex: 0 0 auto; margin-top: 1px; }
  /* **The badge PICKS its text color, and that is what unlocked the ramp. Card #0088.**
     Forcing white on every badge is what made four of the six unreadable, and it also
     pinned every color into the narrow dark band where white works -- which is exactly
     the band where red, orange and amber collapse into each other for a dichromat.

     **Letting a light badge carry ink text buys the whole lightness range**, and the
     ramp that defeats color blindness becomes possible. Measured: the worst badge now
     reads at 4.55:1 against 1.95:1 before. */
  .pri.P0 { background: var(--p0); } .pri.P1 { background: var(--p1); }
  .pri.P3 { background: var(--p3); }
  .pri.P2 { background: var(--p2); color: var(--ink); }
  .pri.P4 { background: var(--p4); color: var(--ink); }
  .pri.P5 { background: var(--p5); color: var(--ink); }
  /* **Three rows: badge and ticket, subject, then the comment count.** Terry:
     "display card number on same line as P1/P2 with description below it", then
     "drop the # in front of ticket number, move ticket number to top right and
     make bold", then "move comment count to its own row at the very bottom on
     right and double height of glyph and count; it's too small for me to see."

     **The ticket and the count SWAP places**, which is why those two arrived as
     separate asks and were built in one pass -- doing either alone leaves the top
     right holding two things or nothing. */
  .card { flex-direction: column; gap: 5px; }
  .card .head { display: flex; gap: 7px; align-items: center; width: 100%; }
  .card .tix { margin-left: auto; color: var(--dim); font-size: 12px;
               font-weight: 700; font-variant-numeric: tabular-nums; }
  /* **+50% on the gap above the subject**, Terry's ask, 5px to 7.5px. It is a
     `margin-top` rather than a larger flex `gap` on purpose: raising the gap would
     also push the comment-count row down, and that is a different space he did not
     ask about. */
  .card .subject { font-size: 13px; font-weight: 500; margin-top: 2.5px; }
  /* **A BADGE, because the glyph was bleeding into the card.** Terry, 2026-08-19:
     "the comment glyph is bleeding with the card background, hence adding contrast
     with dark gray badge under it." The speech bubble was a light glyph on a white
     card and read as a smudge rather than an icon.

     **13px, down 25% from 17px, and the badge is what makes that affordable.**
     17px came from #0023, itself a correction of #0019's 2x -- "2x is too damn
     much". **Shrinking it WITHOUT the badge would reopen #0019's original
     complaint**, which was "it's too small for me to see", so the two changes ship
     together or neither does. 25% of 17 is 12.75; 13 keeps `tnum` on a whole pixel.

     **This size has now been set four times: 11, 22, 17, 13.** Every one of them
     passed whatever checks existed. **Render and LOOK.**

     It exists only when a card HAS comments; an empty third row on every card
     would cost real height in a lane that scrolls. */
  .card .marks { align-self: flex-end; color: #FFFFFF; font-size: 13px;
                 font-weight: 700; line-height: 1; display: flex; gap: 5px;
                 align-items: center; background: var(--mark);
                 border-radius: 4px; padding: 3px 7px; }
  /* **Two digits of reserved width, and NO zero padding** -- his instruction, and
     the reason is alignment rather than tidiness. The row is right-aligned, so an
     unreserved number drags the bubble left and right as counts change and a
     column of cards ends up with bubbles at three different x positions.
     `tnum` makes "two digits" an exact width rather than an estimate.

     **The badge inherits that reservation and needs no width of its own.** With the
     pair wrapped, `min-width` on the number sets the badge's minimum too -- so the
     BADGE edge stops moving between cards, which is #0024's defect solved at the
     container instead of at the number. */
  .card .marks .n { min-width: 2ch; text-align: right;
                    font-variant-numeric: tabular-nums; }

  /* **The + sits in the lane header, and ONLY on the lanes a card may be BORN in.**
     `laneEl` asks `data.creatable`, which the server derives from `may_create`, so
     the button appears exactly where the permission model already allows it rather
     than restating the rule in the page. Adding a lane to `create` in `rules.json`
     grows a + with no code change. */
  /* **Card #0066.** `1.05em -> 1.6em`, and a real hit area rather than a glyph with
     4px of padding. A `+` you have to aim at is a `+` Terry reaches for twice. */
  .lane h2 .add { border: 0; background: transparent; color: var(--dim);
                  font-size: 1.6em; font-weight: 700; line-height: 1;
                  cursor: pointer; border-radius: 5px;
                  width: 1.25em; height: 1.25em; padding: 0;
                  display: inline-flex; align-items: center;
                  justify-content: center; }
  .lane h2 .add:hover { background: #FFFFFF; color: var(--accent); }

  /* **A CENTERED MODAL, unlike the card drawer, and the difference is the job.** The
     drawer keeps the board visible because a card is read IN CONTEXT. This form is
     composition -- Terry is looking at the field, not at the lanes -- and a modal
     stops a repaint two columns away from pulling his eye off a paragraph. */
  #mkscrim { position: fixed; inset: 0; background: rgba(9,30,66,.45); display: none;
             z-index: 40; }
  #mkscrim.show { display: block; }
  #mk { position: fixed; top: 50%; left: 50%; transform: translate(-50%,-50%);
        width: 620px; max-width: 94vw; max-height: 88vh; background: #FFFFFF;
        border-radius: 8px; z-index: 41; display: none; flex-direction: column;
        box-shadow: 0 12px 40px rgba(9,30,66,.35); }
  #mk.show { display: flex; }
  #mk header { padding: 16px 20px 12px; border-bottom: 1px solid var(--line); }
  #mk h1 { margin: 0; font-size: 17px; letter-spacing: -.01em; }
  #mk .where { color: var(--dim); font-size: 12px; margin-top: 5px; }
  #mk .body { padding: 16px 20px; overflow-y: auto; flex: 1; }
  #mk label { display: block; font-size: 11px; text-transform: uppercase;
              letter-spacing: .06em; color: var(--dim); font-weight: 700;
              margin: 0 0 5px; }
  #mk .field { margin-bottom: 16px; }
  #mk input[type=text], #mk select, #mk textarea {
        width: 100%; box-sizing: border-box; font: inherit; font-size: 13px;
        border: 1px solid var(--line); border-radius: 5px; padding: 7px 9px;
        background: #FFFFFF; color: var(--ink); }
  #mk textarea { min-height: 190px; resize: vertical; line-height: 1.45; }
  /* **The hint is not decoration.** The comment box posts on Enter and this field
     does not, so two multi-line fields on one page disagree about the same key.
     Terry chose that deliberately -- a comment is one thought, a description is
     several -- and the hand does not read a decision. See cards #0039 and #0040. */
  #mk .hint { font-size: 11px; color: var(--dim); margin-top: 5px; }
  /* **The same hint style on the comment box**, because the two fields state
     OPPOSITE rules and a reader has to be able to compare them at a glance. Same
     size, same color, same place under the field. */
  .keyhint { font-size: 11px; color: var(--dim); margin: 5px 0 8px; }
  #mk footer { padding: 12px 20px 16px; border-top: 1px solid var(--line);
               display: flex; gap: 10px; align-items: center; }
  #mk footer .grow { flex: 1; }
  #mk button { font: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
               border-radius: 5px; padding: 7px 14px; border: 1px solid var(--line);
               background: #FFFFFF; color: var(--ink); }
  #mk button.go { background: var(--accent); border-color: var(--accent);
                  color: #FFFFFF; }
  #mk button:disabled { opacity: .5; cursor: not-allowed; }

  /* The detail panel. A drawer rather than a modal, so the board stays visible and
     a card's lane is still legible while you read it. */
  #scrim { position: fixed; inset: 0; background: rgba(9,30,66,.45); display: none;
           z-index: 30; }
  #scrim.show { display: block; }
  #panel { position: fixed; top: 0; right: 0; bottom: 0; width: 560px;
           max-width: 92vw; background: #FFFFFF; z-index: 31; display: none;
           flex-direction: column; box-shadow: -4px 0 16px rgba(9,30,66,.2); }
  #panel.show { display: flex; }
  #panel header { padding: 16px 20px 12px; border-bottom: 1px solid var(--line);
                  position: relative; }
  /* **The ticket row mirrors `.card .head` deliberately.** Same flex, same
     `margin-left: auto`, same dim bold tabular 12px -- so the number sits in the same
     place and reads the same way whether you are looking at the card or the drawer.
     `padding-right` clears the close button, which is absolute rather than floated
     precisely so this row can be a flex container at all. */
  #panel .head { display: flex; gap: 7px; align-items: center; padding-right: 22px; }
  #p-tix { margin-left: auto; color: var(--dim); font-size: 12px;
           font-weight: 700; font-variant-numeric: tabular-nums; }
  #panel h1 { margin: 6px 0 0; font-size: 18px; letter-spacing: -.01em; }
  #panel .sub { color: var(--dim); font-size: 12px; margin-top: 6px; }
  /* **The owner chip borrows the actor colors the trail already uses**, so `Terry` is
     `--terry` blue and `Claude` is `--claude` amber wherever they appear. One meaning,
     one color. Card #0053. */
  #panel .own { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
  #panel .own-k { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
                  color: var(--dim); font-weight: 700; }
  #p-owner { font: inherit; font-size: 12px; font-weight: 700; cursor: pointer;
             border: 1px solid var(--line); border-radius: 4px; padding: 2px 9px;
             background: #FFFFFF; }

  #p-owner:disabled { opacity: .5; cursor: wait; }
  /* **The priority SELECT wears the chip's colors, card #0062.** It inherits `.pri`
     for the background and the weight; these rules only undo what a browser adds to a
     `<select>` -- its own border, its native font, and the arrow well.

     `appearance: none` removes the platform arrow. **A `<select>` is still keyboard
     and screen-reader complete without it**, which a hand-built dropdown would not
     be, and that is why this is a real select rather than a styled div. */
  /* **The caret is not decoration -- without it the control is undiscoverable.**
     `appearance: none` takes the platform arrow away, and the first version of this
     shipped looking exactly like the read-only chip it replaced. Terry would have had
     to click a thing that gave him no reason to.

     **It hangs off a WRAPPER rather than off the select**, because `::before` and
     `::after` on a `<select>` do not render in any current browser -- the control is
     replaced. `pointer-events: none` keeps the glyph from swallowing the click that
     opens the list. */
  #panel .pri-wrap { position: relative; display: inline-flex; align-items: center; }
  #panel .pri-wrap::after { content: '\\25BE'; position: absolute; right: 5px;
                            color: #FFFFFF; font-size: 9px; line-height: 1;
                            pointer-events: none; }
  #p-pri { appearance: none; -webkit-appearance: none; font: inherit; font-size: 10px;
           font-weight: 700; border: 0; cursor: pointer;
           padding: 2px 16px 2px 6px; line-height: 1.35; }
  #p-pri:disabled { opacity: .5; cursor: wait; }
  /* The list itself is drawn by the OS and cannot inherit a white-on-color chip, so
     the options are given readable defaults rather than left half-styled. */
  #p-pri option { color: var(--ink); background: #FFFFFF; font-weight: 600; }
  #panel .body { overflow-y: auto; padding: 16px 20px 24px; flex: 1; }
  #panel h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
              color: var(--dim); margin: 22px 0 8px; }
  #panel h3:first-child { margin-top: 0; }
  /* **Absolute, not floated.** A float is not laid out around a flex container, so
     the ticket row above would have run underneath it. Taking the button out of the
     flow entirely is what lets that row exist. */
  #close { position: absolute; top: 14px; right: 16px; border: 0;
           background: transparent; font-size: 20px;
           cursor: pointer; color: var(--dim); line-height: 1; }
  /* **Cards #0081 and #0082: editing in place.**

     The title input is styled to MATCH the h1 it replaces rather than to look like a
     form field. A control that jumps in size when you click it makes you lose your
     place on the line you are about to retype. */
  #p-subject { cursor: text; }
  #p-subject-edit { font: inherit; font-size: 18px; font-weight: 700;
                    letter-spacing: -.01em; width: 100%; box-sizing: border-box;
                    margin: 6px 0 0; padding: 1px 3px; color: var(--ink);
                    border: 1px solid var(--accent); border-radius: 4px; }
  /* A text button that reads as a link. It sits inside an `h3`, so it has to shed the
     heading's own transform and spacing rather than inherit them. */
  .linky { font: inherit; font-size: 10px; font-weight: 700; letter-spacing: .06em;
           text-transform: uppercase; background: none; border: 0; cursor: pointer;
           color: var(--accent); padding: 0 0 0 6px; }
  .linky:hover { text-decoration: underline; }
  #p-detail { cursor: text; }
  #p-detail-text { width: 100%; box-sizing: border-box; min-height: 160px;
                   font: inherit; font-size: 13px; padding: 7px 9px; resize: vertical;
                   border: 1px solid var(--line); border-radius: 5px; color: var(--ink); }
  .editrow { display: flex; gap: 10px; align-items: center; margin-top: 8px; }
  .editrow .go { font: inherit; font-size: 12px; font-weight: 700; cursor: pointer;
                 background: var(--accent); color: #FFFFFF; border: 0;
                 border-radius: 5px; padding: 7px 12px; }
  .editrow .go:disabled { opacity: .5; cursor: wait; }
  /* **`pre-wrap` KEEPS THE BLANK LINES. Card #0086, and it was defeating a standing
     order.** Terry, 2026-08-19: *"don't be afraid to add blank lines to break things
     up... use blank lines to help me digest."*

     **The structure survived all the way to the browser and died at the last step.**
     The newlines are in the HTML -- `innerHTML` holds real `\\n\\n` -- and
     `white-space: normal` collapses every run of whitespace to one space. Months of
     paragraph breaks rendered as one wall.

     **`pre-wrap` rather than `pre`**, because `pre` would stop long lines wrapping and
     put a horizontal scrollbar in a 380px drawer. */
  .detail-text { font-size: 13px; white-space: pre-wrap; }
  .detail-text code { font-family: 'Cascadia Mono', Consolas, monospace;
                      font-size: 11.5px; background: #F4F5F7; padding: 0 3px;
                      border-radius: 3px; }
  .empty { color: var(--dim); font-style: italic; font-size: 12.5px; }
  /* **Card #0071.** The kind is a quiet label and the card is the thing to click, so
     the weight goes on the target rather than on the word describing it. */
  .rel { display: flex; align-items: baseline; gap: 8px; padding: 3px 0; }
  .rel-k { flex: 0 0 92px; font-size: 11px; text-transform: uppercase;
           letter-spacing: .06em; color: var(--dim); font-weight: 700; }
  .rel-go { flex: 1; text-align: left; font: inherit; font-size: 12.5px;
            background: none; border: 0; padding: 0; cursor: pointer;
            color: var(--link, #0B5CD5); }
  .rel-go:hover { text-decoration: underline; }

  /* The audit trail and the comments are visually DIFFERENT on purpose. One is what
     the machine recorded and nobody typed; the other is what a person chose to say.
     Making them look alike would suggest the trail is editable. */
  /* **THREE COLUMNS: datetime, actor, action.** Terry's order and his layout --
     "change audit trail to be datetime then user then action in three columns."
     A grid rather than three spans in a sentence, so every column starts at the
     same x whatever the content.

     **`tabular-nums` plus a zero-padded hour is what actually aligns the times.**
     The grid aligns the COLUMN; without the pad, `02:56pm` and `12:05pm` differ by
     a digit inside it. Between the two, no monospace face is needed -- which is
     why Roboto Mono and JetBrains Mono stayed on the shelf. */
  .trail { border-left: 2px solid var(--line); padding-left: 12px;
           display: grid; grid-template-columns: max-content max-content 1fr;
           gap: 5px 14px; font-size: 12px; }
  .trail .at { color: var(--dim); font-variant-numeric: tabular-nums;
               white-space: nowrap; }
  .trail .who { font-weight: 600; }
  /* Per-user name colors are generated -- `user_css()`, card #0072. */
  .trail .what { color: var(--dim); }
  .trail .empty { grid-column: 1 / -1; }

  .comment { background: #F4F5F7; border-radius: 6px; padding: 8px 10px;
             margin-bottom: 8px; font-size: 13px; }
  .comment .head { font-size: 11px; color: var(--dim); margin-bottom: 3px; }
  .comment .head .who { font-weight: 700; }
  /* **Card #0086, same fix as the description.** A comment is where the blank lines
     matter most -- it is the surface Terry's ELI5 order actually governs. */
  .comment .text { white-space: pre-wrap; }
  /* **A rendered `##` heading, card #0086.** Uppercase and dim, matching the `h3`
     headings the drawer already uses, so a heading inside a comment reads like a
     heading rather than like more bold text. */
  .mdh { display: block; font-size: 11px; text-transform: uppercase;
         letter-spacing: .06em; color: var(--dim); font-weight: 700; }

  #say { width: 100%; min-height: 68px; font: inherit; font-size: 13px;
         padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px;
         resize: vertical; }
  #post { margin-top: 8px; background: var(--accent); color: #FFFFFF; border: 0;
          border-radius: 5px; padding: 7px 14px; font: inherit; font-weight: 600;
          font-size: 13px; cursor: pointer; }

  #banner { display: none; margin: 12px; padding: 14px 16px; background: #FFEBE6;
            border: 1px solid var(--p0); border-radius: 6px; font-size: 13px; }
  #banner.show { display: block; }
  .hint { color: var(--dim); font-size: 12px; margin-top: 4px; }

  /* A move writes a file. Saying so, briefly, is what tells Terry the drag took --
     a card that snapped back and a card that saved look identical. */
  #toast { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
           background: var(--ink); color: #FFFFFF; padding: 9px 15px;
           border-radius: 6px; font-size: 13px; opacity: 0; transition: opacity .18s;
           pointer-events: none; z-index: 40; max-width: 70vw; }
  #toast.show { opacity: 1; }
  #toast.bad { background: var(--p0); }

  /* The stale-page flag is now an EXCEPTION rather than the ordinary remedy. A normal
     tab/server mismatch reloads itself. This appears only if the guarded reload already
     ran and the returned page is still stale. */
  /* **A CLASS toggles this, never `style.display`.** Setting the inline style back to
     `''` to reveal it does not work: the element falls straight back to this `none`, so
     the flag stays invisible while every inline-style assertion reports it shown. That
     is what happened on the first cut, and only the render caught it. */
  #stale { display: none; background: var(--p0); color: #FFFFFF; font-weight: 700;
           padding: 2px 8px; border-radius: 4px; white-space: nowrap; }
  #stale.show { display: inline-block; }
  /* **A DIFFERENT staleness needs a DIFFERENT color as well as a different word.**
     `#stale` is `--p0` red and means automatic tab recovery failed. This one means
     "restart the process", which a reload cannot achieve. `--p1` orange is the next
     step down the hazard ramp this palette already uses. Card #0052. */
  #restart { display: none; background: var(--p1); color: #000000; font-weight: 700;
             padding: 2px 8px; border-radius: 4px; white-space: nowrap; }
  #restart.show { display: inline-block; }
  /* **THE BOARD IS NOT BACKED UP. That is a `--p0` matter, and it earns red.**
     `#stale` and `#restart` describe a stale VIEW -- annoying, and nothing is lost.
     This one means the only copy of the board is on a NAS. Card #0013.
     **It appears on `failed` and on NOTHING else.** `off`, `pending` and `ok` are all
     states Terry cannot act on, and a pill he cannot act on is the scenery the
     loudness rule exists to prevent. */
  #push { display: none; background: var(--p0); color: #FFFFFF; font-weight: 700;
          padding: 2px 8px; border-radius: 4px; white-space: nowrap; }
  #push.show { display: inline-block; }
  /* **THE SEARCH BOX LIVES IN THE BAR, NOT IN THE BOARD, and that is load-bearing.**
     `paint()` calls `replaceChildren()` on `#board` alone, so anything inside it is
     destroyed on every repaint. **A query typed and not yet acted on is unsent input** --
     the exact class of loss card #0029 was raised for -- and putting the field in the bar
     means no repaint can reach it. Card #0061. */
  #find { background: #2C3238; color: var(--barink); border: 1px solid #3C444C;
          border-radius: 4px; padding: 3px 9px; font: inherit; font-size: 12px;
          width: 190px; outline: none; }
  #find::placeholder { color: var(--bardim); }
  #find:focus { border-color: #6B7684; background: #333A41; }
  #findn { color: var(--barink); font-size: 12px; font-weight: 700; white-space: nowrap; }
  /* Hidden rather than removed, so `playFlip` measures a stable set and a card does not
     animate in from nowhere when the query is cleared. */
  .card.nomatch { display: none; }
  /* **Generated per configured user, card #0072.** One color variable and four rules
     each, plus `--accent` for the browser user. It goes LAST so a per-user rule wins
     over the generic ones above, and it is built by `user_css()` rather than written
     here because the number of people is configuration rather than code. */
%USERCSS%
</style>
</head>
<body>
  <div id="bar">
    <span id="dot"></span>
    <span id="brandtitle"><span id="brand"></span><span id="wordmark">localswim</span></span>
    <span id="title">%TITLE%</span>
    <span id="counts"></span>
    <span class="grow"></span>
    <input id="find" type="search" autocomplete="off" spellcheck="false"
      placeholder="Find in cards   /"
      title="Search titles, descriptions and comments. Press / to focus, Escape to clear.">
    <span id="findn"></span>
    <span id="live">connecting...</span>
    <span id="stale"
      title="This tab stayed stale after one automatic reload."></span>
    <span id="restart"
      title="The server is preflighting changed Python and will restart itself. A broken
edit leaves the current process running and reports the failing check here."></span>
    <span id="push"></span>
    <label id="alerts-wrap"><input type="checkbox" id="alerts"> Alerts</label>
    <span id="badge"
      title="Green means Python read and parsed the board file in the last 5s.">LIVE</span>
  </div>
  <div id="banner">
    <strong id="banner-head"></strong>
    <div class="hint" id="banner-body"></div>
  </div>
  <div id="board"></div>

  <div id="scrim"></div>
  <aside id="panel">
    <header>
      <button id="close" title="Close">&times;</button>
      <div class="head">
        <!-- **A SELECT rather than a toggle, and the count is the reason.** Owner has
             exactly two values so a click that flips it is honest; priority has six,
             and cycling P0 to P5 one click at a time would be six clicks to undo a
             mis-click. Card #0062.

             **It keeps the `pri` class**, so it wears the same color here as the chip
             on the card face. One meaning, one color -- the rule the owner chip
             already follows. -->
        <span class="pri-wrap"><select class="pri" id="p-pri"
          title="Priority. Either of us can change it, and the change is recorded in
the audit trail."></select></span>
        <span id="p-tix"></span>
      </div>
      <!-- **Card #0081.** The title is a button-shaped nothing until you click it: no
           pencil icon competing with the text, and the whole line is the target. The
           `title` attribute is the only affordance, which is the same weight the owner
           chip carries. -->
      <h1 id="p-subject" title="Click to rename. Enter saves, Escape cancels."></h1>
      <input id="p-subject-edit" hidden aria-label="Card title">
      <div class="sub" id="p-sub"></div>
      <!-- **OWNER IS A LABEL, and the control says so by being a plain toggle rather
           than living beside anything that grants power.** Card #0053. Either actor
           may reassign either way, so there is no state in which this is disabled. -->
      <div class="own">
        <span class="own-k">Owner</span>
        <button id="p-owner" type="button"
          title="Whose plate this is on. A label, not a permission -- either of us can
move any card whoever owns it. Click to hand it over."></button>
      </div>
    </header>
    <div class="body">
      <!-- **Card #0082.** The heading carries the control, so the description itself
           stays a clean block of prose. -->
      <h3>Description <button class="linky" id="p-detail-edit" type="button">edit</button></h3>
      <div class="detail-text" id="p-detail"
           title="Click to edit, or use the button above."></div>
      <!-- **Enter adds a NEWLINE here and the button submits**, exactly like the
           new-card dialog and exactly unlike the comment box. Terry drew that line on
           cards #0039 and #0040: a comment is one thought, a description is several. -->
      <div id="p-detail-editor" hidden>
        <textarea id="p-detail-text" aria-label="Card description"></textarea>
        <div class="editrow">
          <button class="go" id="p-detail-save" type="button">Save description</button>
          <button class="linky" id="p-detail-cancel" type="button">Cancel</button>
        </div>
      </div>
      <!-- **Card #0071. Hidden entirely when a card has no relationships**, which is
           most of them. A heading with nothing under it is noise on 70 cards to serve
           the few that have links. -->
      <h3 id="p-rel-h">Related</h3>
      <div id="p-rel"></div>
      <h3>Comments</h3>
      <div id="p-comments"></div>
      <textarea id="say" placeholder="Leave a note on this card..."></textarea>
      <!-- **The hint is not decoration.** This box posts on Enter and the new-card
           dialog's description does not, so two multi-line fields on one page
           disagree about the same key. Terry chose that deliberately -- a comment
           is one thought, a description is several -- and the HAND does not read a
           decision. Cards #0039 and #0040. -->
      <div class="keyhint">Enter posts. Shift+Enter adds a line break.</div>
      <button id="post">Comment</button>
      <h3>Audit trail</h3>
      <div class="trail" id="p-trail"></div>
    </div>
  </aside>
  <div id="mkscrim"></div>
  <!-- **NO <form> ELEMENT, and that is the mechanism rather than an omission.**
       Terry, 2026-08-19: the description takes Enter as a newline and only the
       button submits. A <form> gives a single-line input an implicit submit on
       Enter that has to be fought with preventDefault in a handler somebody can
       delete. **No form means the key has nothing to submit.** Card #0040. -->
  <div id="mk" role="dialog" aria-modal="true" aria-labelledby="mk-title">
    <header>
      <h1 id="mk-title">New card</h1>
      <div class="where" id="mk-where"></div>
    </header>
    <div class="body">
      <div class="field">
        <label for="mk-subject">Title</label>
        <input type="text" id="mk-subject" maxlength="200"
               placeholder="What needs doing, in one line">
      </div>
      <div class="field">
        <label for="mk-priority">Priority</label>
        <select id="mk-priority"></select>
      </div>
      <!-- **Card #0069.** Terry had no way to say who a new card belongs to, so every
           one landed on the bot and had to be reassigned afterwards. He requested an
           owner picker and delegated the implementation details.
           **The bot is the default because that is the existing behavior**, not because
           it is the better guess. Changing the default in the same breath as exposing
           the control would hide which of the two behaviors moved. -->
      <div class="field">
        <label for="mk-owner">Owner</label>
        <select id="mk-owner"></select>
      </div>
      <div class="field">
        <label for="mk-detail">Description</label>
        <textarea id="mk-detail"
                  placeholder="The reasoning, the constraints, the traps..."></textarea>
        <div class="hint">Enter adds a new line. Use Add card to submit.</div>
      </div>
    </div>
    <footer>
      <span class="grow"></span>
      <button id="mk-cancel">Cancel</button>
      <button id="mk-go" class="go">Add card</button>
    </footer>
  </div>
  <div id="toast"></div>

<script>
const API_TOKEN = '%TOKEN%';
const API_PREFIX = '%API_PREFIX%';

async function apiPost(path, body) {
  return fetch(path, {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer ' + API_TOKEN,
      'Content-Type': 'application/json',
      'If-Match': '"revision-' + data.revision + '"',
    },
    body: JSON.stringify(body),
  });
}

function acceptRevision(out) {
  if (Number.isInteger(out.revision)) data.revision = out.revision;
}

// **The repaint counter is GONE, deliberately.** It only moved when the file
// changed, so a healthy quiet board and a dead poll showed the same number --
// which is the exact confusion the LIVE badge replaced. Two signals telling the
// same story badly is worse than one telling it well.
let seen = null, openId = null;

// **Unsent comment text, per card id. It is the ONLY state the repaint may not own.**
//
// Terry, 2026-08-18: "I've had to race claude several times and got comments wiped 3-4
// times in a row." The drawer repaints on every board write so a new comment appears
// without reopening the card -- and Claude writes to this board constantly, so the
// refresh that keeps the panel honest was destroying whatever he was mid-sentence on.
//
// **A draft is the one thing on this page the SERVER does not have a copy of.** Every
// other pixels can be rebuilt from /api/v001/board; typed text that has not been posted
// exists
// nowhere else, so the automatic stale-code reload carries it through sessionStorage.
const STALE_RELOAD_STATE_KEY = 'localswim-stale-reload-state-v1';
const STALE_RELOAD_QUERY = '_localswim_build';
let staleReloadState = null;
try {
  const saved = sessionStorage.getItem(STALE_RELOAD_STATE_KEY);
  if (saved) staleReloadState = JSON.parse(saved);
  sessionStorage.removeItem(STALE_RELOAD_STATE_KEY);
} catch (err) {
  // Storage can be disabled. Reload still works; there is simply no saved UI state.
  staleReloadState = null;
}
let drafts = (staleReloadState && staleReloadState.drafts
              && typeof staleReloadState.drafts === 'object')
  ? staleReloadState.drafts : {};
let data = {lanes: [], edges: [], counts: {}, error: null};
// **Card #0063. Off on every load, which is what "by default" means.** It lives here
// rather than in localStorage on purpose: a board that remembered being unhidden would
// need a second decision about when to forget, and Terry asked for a default rather
// than a preference.
let unhideOld = !!(staleReloadState && staleReloadState.unhideOld);

// **Card #0072. The page knows no names.** `data.actors` comes from `rules.json`, so
// every label here is configuration rather than markup.
//
// **An UNKNOWN id falls back to the id itself rather than to a name.** A card whose
// history names somebody since removed from the config would otherwise render as
// whichever person the old ternary happened to pick -- attributing one person's move to
// another, silently. Showing the raw id is uglier and true.
function userLabel(id) {
  const u = (data.actors || []).find(a => a.id === id);
  return u ? u.label : (id || '?');
}

// **The owner control CYCLES rather than flips**, because there may be more than two.
// The old line read `owner === 'terry' ? 'claude' : 'terry'`, which is a flip and is
// correct only for exactly two actors.
function nextOwner(current) {
  const ids = (data.actors || []).map(a => a.id);
  if (!ids.length) return current;
  return ids[(ids.indexOf(current) + 1) % ids.length];
}

// **Cards #0081 and #0082. Which field, if any, is being edited right now.**
//
// **One variable rather than two booleans**, because "both open at once" is a state
// nobody wants and a pair of flags is a state machine that permits it.
let editing = null;

function closeEditors() {
  editing = null;
  document.getElementById('p-subject').hidden = false;
  document.getElementById('p-subject-edit').hidden = true;
  document.getElementById('p-detail').hidden = false;
  document.getElementById('p-detail-editor').hidden = true;
}

function startSubjectEdit() {
  const it = itemById(openId);
  if (!it || editing) return;
  editing = 'subject';
  const box = document.getElementById('p-subject-edit');
  // **Seeded from `data`, never from the rendered heading.** The heading is text the
  // browser has already normalized; `it.subject` is what the board actually holds.
  box.value = it.subject;
  document.getElementById('p-subject').hidden = true;
  box.hidden = false;
  box.focus();
  box.select();
}

async function saveSubject() {
  const box = document.getElementById('p-subject-edit');
  const wanted = box.value.trim();
  if (!wanted) { toast('A card needs a title', true); return; }
  const it = itemById(openId);
  if (it && wanted === it.subject) { closeEditors(); return; }
  box.disabled = true;
  try {
    const res = await apiPost(API_PREFIX + '/cards/' + encodeURIComponent(openId) + '/subject',
                              {subject: wanted});
    const out = await res.json();
    acceptRevision(out);
    if (!res.ok) { toast(out.error || 'Rename refused', true); return; }
    toast(out.result);
    seen = null;
    closeEditors();
  } catch (err) {
    toast('Could not reach the board: ' + err, true);
  } finally {
    box.disabled = false;
  }
}

function startDetailEdit() {
  const it = itemById(openId);
  if (!it || editing) return;
  editing = 'detail';
  // **`detailRaw`, never `detail`.** The rendered form has been through `inline()` --
  // escaped, with the markdown-ish bits turned into tags -- and editing THAT would
  // store HTML, one round trip from unreadable.
  document.getElementById('p-detail-text').value = it.detailRaw || '';
  document.getElementById('p-detail').hidden = true;
  document.getElementById('p-detail-editor').hidden = false;
  document.getElementById('p-detail-text').focus();
}

async function saveDetail() {
  const area = document.getElementById('p-detail-text');
  const save = document.getElementById('p-detail-save');
  const wanted = area.value;
  if (!wanted.trim()) { toast('A description cannot be blank', true); return; }
  save.disabled = true;
  try {
    const res = await apiPost(API_PREFIX + '/cards/' + encodeURIComponent(openId) + '/detail',
                              {detail: wanted});
    const out = await res.json();
    acceptRevision(out);
    if (!res.ok) { toast(out.error || 'Edit refused', true); return; }
    toast(out.result);
    seen = null;
    closeEditors();
  } catch (err) {
    toast('Could not reach the board: ' + err, true);
  } finally {
    save.disabled = false;
  }
}

// **Turn ` -- ` into an em dash AS IT IS TYPED. Card #0092.**
//
// **Terry's call, and he overruled mine.** #0079 converted on RENDER and deliberately
// left the stored text as ASCII, on the argument that the JSON stays greppable. His
// answer: *"I will NEVER grep on punctuation like --. I'd love to have the UI do
// something smart and immediately turn ' -- ' into something not-fugly."*
//
// **So the em dash is now what gets STORED**, and the render-time rule stays as the
// safety net for every comment written before today.
//
// **It fires on the TRAILING SPACE, which is what makes it feel right.** Converting on
// the second hyphen would rewrite `--verify` under his fingers halfway through the word.
// Waiting for the space means the pattern is unambiguously the em-dash usage, and it
// lands at the moment he moves on to the next word.
//
// **The caret is moved by the number of replacements BEFORE it**, not reset. ` -- ` is
// four characters and ` — ` is three, so each one earlier in the string pulls the
// caret back by one. Assigning `value` without this sends the cursor to the end, which
// is the classic version of this feature that everybody hates.
const DASH_RUN = / -- /g;

function emDashAsYouType(field) {
  const before = field.value;
  if (!DASH_RUN.test(before)) return;
  DASH_RUN.lastIndex = 0;

  const caret = field.selectionStart;
  let shift = 0;
  for (const hit of before.matchAll(DASH_RUN)) {
    if (hit.index + hit[0].length <= caret) shift += 1;
  }

  const after = before.replace(DASH_RUN, ' — ');
  // **Only touch the field if something actually changed.** A no-op assignment still
  // disturbs the selection and breaks undo, twice a keystroke.
  if (after === before) return;
  field.value = after;
  const at = caret - shift;
  field.setSelectionRange(at, at);
}

function toast(msg, bad) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.toggle('bad', !!bad);
  t.classList.add('show');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), 2800);
}

function allowed(from, to) {
  return data.edges.some(e => e[0] === from && e[1] === to);
}

function itemById(id) {
  for (const lane of data.lanes) {
    for (const it of lane.items) if (it.id === id) return it;
  }
  return null;
}

// **Card #0071. The model landed on #0028 and nothing drew it**, so the relationships
// existed and Terry could not see one.
//
// **The wire already carries labels, not slugs** -- `{kind, ticket, subject}` -- because
// he says "#0028" out loud. Nothing here needs a lookup table.
const REL_WORD = {
  blocks: 'Blocks',
  blocked_by: 'Blocked by',
  duplicates: 'Duplicates',
  duplicated_by: 'Duplicated by',
  references: 'References',
  referenced_by: 'Referenced by',
  relates_to: 'Relates to',
};

function relRow(word, ref) {
  const row = document.createElement('div');
  row.className = 'rel';
  const k = document.createElement('span');
  k.className = 'rel-k';
  k.textContent = word;
  // **A relationship is a CARD REFERENCE, so it opens that card.** A list you cannot
  // follow is a list you have to go and look things up from, which is the work the
  // relationship was recorded to save.
  const a = document.createElement('button');
  a.type = 'button';
  a.className = 'rel-go';
  a.textContent = ref.ticket + '  ' + ref.subject;
  a.title = 'Open ' + ref.ticket;
  a.addEventListener('click', () => {
    // **Ticket, not slug.** `find()` on the server takes either, and `itemById` here
    // wants the slug -- so the lookup goes through the lanes by ticket label, which is
    // the only identifier the wire carries for the OTHER card.
    for (const lane of data.lanes) {
      for (const it of lane.items) {
        if (it.ticket === ref.ticket) { openCard(it.id); return; }
      }
    }
    toast(ref.ticket + ' is not on the board', true);
  });
  row.append(k, a);
  return row;
}

function paintRelations(it) {
  const box = document.getElementById('p-rel');
  const head = document.getElementById('p-rel-h');
  box.replaceChildren();
  const rows = [];
  // **Hierarchy first, then relations.** Parent and children say where a card SITS;
  // the rest say what it touches, and the first question is usually the first one.
  if (it.parent) rows.push(relRow('Parent', it.parent));
  for (const kid of it.children || []) rows.push(relRow('Child', kid));
  for (const l of it.links || []) rows.push(relRow(REL_WORD[l.kind] || l.kind, l));
  // **Hidden entirely rather than showing "none".** Most cards have no relationships,
  // and an empty section on every one of them is noise paid for by the few that do.
  const any = rows.length > 0;
  head.style.display = any ? '' : 'none';
  box.style.display = any ? '' : 'none';
  for (const r of rows) box.appendChild(r);
}

// ---- the detail panel ----------------------------------------------------

function openCard(id) {
  const it = itemById(id);
  if (!it) return;

  // **OPENING a card and REFRESHING one are different acts, and this line is what
  // separates them.** paint() calls this on every board change, so an unconditional
  // clear at the end of this function wiped Terry's draft on somebody else's write.
  // The comparison MUST happen BEFORE openId is reassigned, or every call looks like
  // a refresh and a genuinely new card inherits the previous card's text.
  if (openId !== id) {
    const box = document.getElementById('say');
    if (openId) drafts[openId] = box.value;
    box.value = drafts[id] || '';
    // **A different card means the editors close.** Leaving one open would show card
    // A's title in an input attached to card B, and saving it would rename the wrong
    // one. Cards #0081 and #0082.
    closeEditors();
  }
  openId = id;
  // **The priority control, card #0062.** Options come from `data.priorities`, which
  // the server builds from `rules.json` -- the same source `lanes()` ranks by, so a
  // value offered here can never be one the sort does not know.
  const pri = document.getElementById('p-pri');
  const prios = (data.priorities || []);
  if (pri.options.length !== prios.length) {
    pri.replaceChildren();
    for (const p of prios) {
      const o = document.createElement('option');
      o.value = p.id;
      o.textContent = p.id + '  \\u00b7  ' + p.label;
      pri.appendChild(o);
    }
  }
  // **NEVER write to this control while Terry is inside it.** paint() calls openCard()
  // on every board change, twice a second, and assigning `.value` to a select whose
  // dropdown is open closes it. That is #0029's defect wearing a different hat: a
  // repaint may redraw anything the SERVER has a copy of, and an interaction in
  // progress is the one thing it does not.
  if (document.activeElement !== pri) pri.value = it.priority;
  pri.className = 'pri ' + it.priority;
  pri.title = it.priorityLabel;
  // **The ticket moved OUT of the sub line and up to the top right**, matching what
  // #0021 did for the card face. Leaving it in both places would print the same number
  // twice in a header two lines tall.
  // **No leading hash, for the same reason the card face strips it** -- #0021. The two
  // MUST agree; a number that gains a `#` when you open the card is two conventions.
  document.getElementById('p-tix').textContent = it.ticket.replace('#', '');
  // **#0029's rule, third surface.** paint() reopens the card twice a second, so a
  // field being edited MUST NOT be rewritten underneath the person typing in it. The
  // priority select guards on focus; these guard on `editing`, because a textarea can
  // lose focus to its own Save button and still hold unsent text.
  if (editing !== 'subject') {
    document.getElementById('p-subject').textContent = it.subject;
  }
  document.getElementById('p-sub').textContent =
    it.laneLabel + '  \\u00b7  ' + it.id;

  const own = document.getElementById('p-owner');
  own.textContent = userLabel(it.owner);
  own.className = it.owner;
  own.disabled = false;

  const detail = document.getElementById('p-detail');
  if (editing !== 'detail') {
    if (it.detail) { detail.innerHTML = it.detail; detail.className = 'detail-text'; }
    else { detail.textContent = 'No description.'; detail.className = 'empty'; }
  }

  paintRelations(it);

  const cs = document.getElementById('p-comments');
  cs.replaceChildren();
  if (!it.comments.length) {
    const e = document.createElement('div');
    e.className = 'empty';
    e.textContent = 'No comments yet.';
    cs.appendChild(e);
  }
  for (const c of it.comments) {
    const d = document.createElement('div');
    d.className = 'comment';
    d.innerHTML = '<div class="head"><span class="who ' + c.by + '"></span>'
      + '<span class="at"></span></div><div class="text">' + c.text + '</div>';
    d.querySelector('.who').textContent = userLabel(c.by);
    d.querySelector('.at').textContent = '  \\u00b7  ' + c.when;
    cs.appendChild(d);
  }

  // Three cells per entry, appended straight into the grid -- no row wrapper, so
  // the columns line up across every entry rather than within each one.
  const tr = document.getElementById('p-trail');
  tr.replaceChildren();
  if (!it.history.length) {
    const e = document.createElement('div');
    e.className = 'empty';
    e.textContent = 'No recorded history \\u2014 migrated before the trail existed.';
    tr.appendChild(e);
  }
  for (const h of it.history) {
    const at = document.createElement('div');
    at.className = 'at';
    at.textContent = h.when;

    const who = document.createElement('div');
    who.className = 'who ' + h.by;
    who.textContent = userLabel(h.by);

    const what = document.createElement('div');
    what.className = 'what';
    // **An ownership entry moves no lane, so `ownerTo` is the discriminant.** Without
    // this branch it would render as `' \\u2192 '` with two empty labels -- a blank
    // row in a permanent record, which is worse than a missing one. Card #0053.
    //
    // **Terry's own wording for the line**: "Ticket ownership change: Terry -> Claude".
    const name = userLabel;
    if (h.ownerTo) {
      what.textContent = 'Ticket ownership change: '
        + name(h.ownerFrom) + ' \\u2192 ' + name(h.ownerTo);
    } else if (h.priorityTo) {
      // **A THIRD entry shape, card #0070.** Terry dropped #0037 to P5, watched it move
      // up its lane, opened the card and found nothing: "no breadcrumbs of it in Audit
      // Log." A card that moves for no visible reason is what an audit trail exists to
      // prevent.
      what.textContent = 'Ticket priority change: '
        + h.priorityFrom + ' \\u2192 ' + h.priorityTo;
    } else {
      what.textContent = h.from ? (h.fromLabel + ' \\u2192 ' + h.toLabel)
                                : ('created in ' + h.toLabel);
    }

    tr.append(at, who, what);
  }

  document.getElementById('scrim').classList.add('show');
  document.getElementById('panel').classList.add('show');
  // **Nothing touches the textarea here.** The element is never replaced -- the
  // comment list is a sibling -- so leaving its value alone also preserves focus
  // and caret position through a repaint, for free.
}

function closeCard() {
  // **Escape is one keypress away at all times.** Dropping the draft on close is the
  // same defect as dropping it on repaint, just slower to notice.
  if (openId) drafts[openId] = document.getElementById('say').value;
  openId = null;
  document.getElementById('scrim').classList.remove('show');
  document.getElementById('panel').classList.remove('show');
}

document.getElementById('close').addEventListener('click', closeCard);
document.getElementById('scrim').addEventListener('click', closeCard);
document.addEventListener('keydown', ev => {
  const find = document.getElementById('find');
  const typing = ev.target instanceof HTMLInputElement
              || ev.target instanceof HTMLTextAreaElement;
  // **`/` jumps to the search box. Card #0061.** The convention every code host uses,
  // and it costs nothing -- but it MUST NOT fire while he is typing a comment, or a
  // slash in a file path steals the caret mid-sentence.
  if (ev.key === '/' && !typing && !mkOpen) {
    ev.preventDefault();
    find.focus();
    find.select();
    return;
  }
  // **The new-card dialog wins while it is open**, because it sits on top and it is
  // the only thing here holding text the server has no copy of. `closeMake(false)`
  // asks before discarding; `closeCard()` never needs to, because it STASHES the
  // comment draft rather than dropping it.
  if (ev.key !== 'Escape') return;
  if (mkOpen) { closeMake(false); return; }
  // **Escape clears the search BEFORE it closes a card**, because a filtered board with
  // an open card is a state where Escape has two plausible meanings, and the narrower
  // one is what the eye is on.
  if (document.activeElement === find && query) {
    find.value = '';
    query = '';
    applyFilter();
    return;
  }
  closeCard();
});

document.getElementById('find').addEventListener('input', ev => {
  query = ev.target.value;
  applyFilter();
});

// **ONE submit path, called from the button AND from Enter.** A second copy on the
// key handler would drift, and the line that drifts is `delete drafts[openId]` --
// the one whose own comment says dropping it makes the text come back on reopen and
// "invites Terry to send it twice". Card #0039.
async function postComment() {
  const text = document.getElementById('say').value.trim();
  // **This guard is why Enter on an empty box is safe for free.** It predates the
  // key binding and covers it without a second check.
  if (!openId || !text) return;
  const res = await apiPost(API_PREFIX + '/cards/' + encodeURIComponent(openId) + '/comment',
                            {text: text});
  const out = await res.json();
  acceptRevision(out);
  if (!res.ok) { toast(out.error || 'Comment refused', true); return; }
  document.getElementById('say').value = '';
  // **The stash MUST be dropped too, or the text just sent comes back on reopen** --
  // which reads as the comment having failed, and invites Terry to send it twice.
  delete drafts[openId];
  toast(out.result);
  seen = null;
}

document.getElementById('post').addEventListener('click', postComment);

// **Cards #0081 and #0082.** Click the text to edit it -- the whole line is the target,
// so no icon competes with the title for the eye.
// **Card #0092. Every field where Terry writes PROSE gets it.** The comment box, the
// description editor and the new-card description. **The title input does NOT** -- a
// card title is a name, and a name with an em dash in it is a name he did not type.
for (const id of ['say', 'p-detail-text', 'mk-detail']) {
  const field = document.getElementById(id);
  if (field) field.addEventListener('input', () => emDashAsYouType(field));
}

document.getElementById('p-subject').addEventListener('click', startSubjectEdit);
document.getElementById('p-detail').addEventListener('click', startDetailEdit);
document.getElementById('p-detail-edit').addEventListener('click', startDetailEdit);
document.getElementById('p-detail-save').addEventListener('click', saveDetail);
document.getElementById('p-detail-cancel').addEventListener('click', closeEditors);

// **Enter SAVES a title and Escape abandons it.** A title is one line, so the comment
// box's rule applies rather than the description's -- cards #0039 and #0040 drew that
// distinction and this follows it rather than inventing a third convention.
document.getElementById('p-subject-edit').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') { ev.preventDefault(); saveSubject(); }
  if (ev.key === 'Escape') { ev.preventDefault(); closeEditors(); }
});

// **Escape abandons the description too, and Enter does NOT save it.** A description is
// several thoughts, so Enter is a newline and only the button submits.
document.getElementById('p-detail-text').addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') { ev.preventDefault(); closeEditors(); }
});

// **Hand the card over. A toggle, because there are exactly two actors.** Card #0053.
//
// **It reads the CURRENT owner off `data` rather than off the button's own text**, so
// a repaint between opening the card and clicking cannot flip it the wrong way.
document.getElementById('p-owner').addEventListener('click', async () => {
  if (!openId) return;
  const it = itemById(openId);
  if (!it) return;
  const own = document.getElementById('p-owner');
  own.disabled = true;
  try {
    const res = await apiPost(API_PREFIX + '/cards/' + encodeURIComponent(openId) + '/assign',
                              {owner: nextOwner(it.owner)});
    const out = await res.json();
    acceptRevision(out);
    if (!res.ok) { toast(out.error || 'Reassign refused', true); return; }
    toast(out.result);
    seen = null;
  } catch (err) {
    toast('Could not reach the board: ' + err, true);
  } finally {
    own.disabled = false;
  }
});

// **Change the priority. Card #0062.**
//
// **`change`, not `input`.** On a `<select>` the two fire together in every current
// browser, and `change` is the one that means "a choice was made" -- so a future
// keyboard-driven select that fires `input` per arrow key cannot POST five times on
// the way from P0 to P5.
//
// **The old value is read off `data`, never off the control**, for the same reason
// the owner toggle does it: a repaint between opening the card and choosing cannot
// make this send the wrong thing. On refusal the control is put back from `data`
// rather than left showing a value the board never accepted.
document.getElementById('p-pri').addEventListener('change', async (ev) => {
  if (!openId) return;
  const pri = ev.target;
  const wanted = pri.value;
  pri.disabled = true;
  try {
    const res = await apiPost(API_PREFIX + '/cards/' + encodeURIComponent(openId) + '/priority',
                              {priority: wanted});
    const out = await res.json();
    acceptRevision(out);
    if (!res.ok) {
      toast(out.error || 'Priority change refused', true);
      const it = itemById(openId);
      if (it) pri.value = it.priority;
      return;
    }
    toast(out.result);
    seen = null;
  } catch (err) {
    toast('Could not reach the board: ' + err, true);
    const it = itemById(openId);
    if (it) pri.value = it.priority;
  } finally {
    pri.disabled = false;
  }
});

// **Enter POSTS here and adds a NEWLINE in the new-card dialog, and the two are
// deliberate opposites.** Terry: a comment is one thought, a description is
// several. Cards #0039 and #0040.
//
// **Bound on the TEXTAREA, never on `document`.** A document-level handler would
// fire while the drawer is closed and while a card is being dragged; there is
// already one of those for Escape and it should stay the only one.
document.getElementById('say').addEventListener('keydown', ev => {
  // **`isComposing` FIRST.** Committing an IME candidate sends Enter, and without
  // this the comment posts mid-word with the composition half-finished.
  if (ev.key !== 'Enter' || ev.isComposing) return;
  // **Shift+Enter lets the DEFAULT run rather than inserting a newline by hand.**
  // The browser already places the caret and keeps undo history intact; doing it
  // manually breaks both.
  if (ev.shiftKey) return;
  ev.preventDefault();
  postComment();
});

// ---- the new-card dialog -------------------------------------------------
//
// **THE DRAFT IS THE WHOLE RISK, and it is the same one #0029 cost him four
// comments over.** A description is several paragraphs where a comment is a
// sentence, so the fuse is longer and the loss is bigger.
//
// **This form is SAFE BY CONSTRUCTION rather than by a guard**, and that is worth
// more than a rule somebody has to remember. `paint()` rebuilds `#board` and
// nothing else. `#mk` lives outside `#board`, so a repaint cannot reach it -- the
// element is never replaced, so its values, its focus and its caret all survive a
// poll for free. Same property the `#say` textarea already relies on.
//
// **`mkOpen` is still kept**, because the lane a card is being written for has to
// outlive the repaint too.
let mkOpen = null;

function mkDirty() {
  return !!(document.getElementById('mk-subject').value.trim()
         || document.getElementById('mk-detail').value.trim());
}

function openMake(lane) {
  mkOpen = lane.state;
  document.getElementById('mk-where').textContent =
    'It will be created in ' + lane.label + ', by ' + userLabel(data.browserUser) + '.';
  const pri = document.getElementById('mk-priority');
  // **Priorities come from `rules.json` via /api/v001/board.** P0 exists and a range
  // typed
  // into the page would be a second copy of the list. Card #0047.
  pri.innerHTML = '';
  for (const p of data.priorities) {
    const o = document.createElement('option');
    o.value = p.id;
    o.textContent = p.id + '  ' + p.label;
    if (p.id === data.defaultPriority) o.selected = true;
    pri.appendChild(o);
  }
  // **Owners come from the SERVER too, for the same reason the priorities do.** Card
  // #0069. A pair of hard-coded options would be a second copy of the actor list, and
  // card #0072 is about to make that list configurable -- so a page that names Terry and
  // Claude in markup would go stale the day somebody else deploys this.
  const own = document.getElementById('mk-owner');
  own.innerHTML = '';
  for (const a of data.actors || []) {
    const o = document.createElement('option');
    o.value = a.id;
    o.textContent = a.label;
    if (a.id === data.defaultOwner) o.selected = true;
    own.appendChild(o);
  }
  document.getElementById('mkscrim').classList.add('show');
  document.getElementById('mk').classList.add('show');
  document.getElementById('mk-subject').focus();
}

function closeMake(force) {
  // **Cancelling with text in it ASKS FIRST.** The whole card is about not losing
  // typing, and a stray Escape is one keypress from a paragraph.
  if (!force && mkDirty()
      && !window.confirm('Discard this card? What you typed will be lost.')) {
    return;
  }
  mkOpen = null;
  document.getElementById('mk-subject').value = '';
  document.getElementById('mk-detail').value = '';
  document.getElementById('mkscrim').classList.remove('show');
  document.getElementById('mk').classList.remove('show');
}

document.getElementById('mkscrim').addEventListener('click', () => closeMake(false));
document.getElementById('mk-cancel').addEventListener('click', () => closeMake(false));

// **Enter in the TITLE moves to the description. It does not submit.** A
// single-line input's default is to submit its form, which is exactly the
// behavior card #0040 refuses -- and there is no form here, so the default is
// already nothing. This makes the key do something USEFUL instead of nothing,
// and it matches how the form is actually filled.
document.getElementById('mk-subject').addEventListener('keydown', ev => {
  if (ev.key === 'Enter' && !ev.isComposing) {
    ev.preventDefault();
    document.getElementById('mk-detail').focus();
  }
});

async function submitMake() {
  const subject = document.getElementById('mk-subject').value.trim();
  if (!mkOpen || !subject) {
    toast('A card needs a title', true);
    document.getElementById('mk-subject').focus();
    return;
  }
  const go = document.getElementById('mk-go');
  go.disabled = true;
  try {
    const res = await apiPost(API_PREFIX + '/cards', {
        state: mkOpen,
        subject: subject,
        detail: document.getElementById('mk-detail').value,
        priority: document.getElementById('mk-priority').value,
        owner: document.getElementById('mk-owner').value,
    });
    const out = await res.json();
    acceptRevision(out);
    if (!res.ok) { toast(out.error || 'Card refused', true); return; }
    // **Only cleared once the server has it.** Clearing on click would throw the
    // text away on a refusal, which is the same defect as the repaint wipe
    // arriving through the submit path instead.
    closeMake(true);
    toast(out.result);
    seen = null;
  } catch (err) {
    toast('Could not reach the board: ' + err, true);
  } finally {
    go.disabled = false;
  }
}

document.getElementById('mk-go').addEventListener('click', submitMake);

// ---- the board -----------------------------------------------------------

function card(item) {
  const d = document.createElement('div');
  d.className = 'card';
  d.draggable = !!item.draggable;
  d.dataset.id = item.id;
  d.dataset.state = item.state;
  d.innerHTML = '<div class="head"><span class="pri"></span>'
    + '<span class="tix"></span></div>'
    + '<span class="subject"></span>'
    + (item.comments.length ? '<span class="marks"></span>' : '');
  // **No leading hash.** Display only -- find() still takes 0003, #0003, 3 or the
  // slug, because he may paste a hash from an older message or a commit.
  d.querySelector('.tix').textContent = item.ticket.replace('#', '');
  const pri = d.querySelector('.pri');
  pri.textContent = item.priority;
  pri.className = 'pri ' + item.priority;
  pri.title = item.priorityLabel;
  // Set as text, so a stray angle bracket in a subject cannot become markup.
  d.querySelector('.subject').textContent = item.subject;
  // A card with discussion on it should say so without being opened. At 11px on
  // the top row Terry could not see it -- "it's too small for me to see" -- so it
  // is 22px on its own bottom row now, and absent entirely when there is nothing
  // to say.
  // **Bubble first, then the count**, per Terry. The number sits in its own span so
  // CSS can reserve two digits and right-align it without padding the value.
  if (item.comments.length) {
    const marks = d.querySelector('.marks');
    marks.innerHTML = '<span class="b">\\u{1F4AC}</span><span class="n"></span>';
    marks.querySelector('.n').textContent = item.comments.length;
  }

  d.addEventListener('click', () => openCard(item.id));
  d.addEventListener('dragstart', ev => {
    ev.dataTransfer.setData('text/plain',
      JSON.stringify({id: item.id, from: item.state}));
    ev.dataTransfer.effectAllowed = 'move';
    d.classList.add('dragging');
  });
  d.addEventListener('dragend', () => d.classList.remove('dragging'));
  return d;
}

// **ONE place decides which cards are on screen. Card #0063.**
//
// **This is not tidiness -- two copies were WRONG within a minute of being written.**
// `laneEl` filtered the old completed cards out and set the badge to what it had drawn;
// `applyFilter` then ran, as `paint()` always makes it, and reset every badge to
// `lane.items.length`. The first render showed a lane reading 8 above zero cards.
//
// The search half had the same hole in the other direction: it counted a match on a
// card that had been hidden and therefore had no element to highlight.
function visibleItems(lane) {
  return unhideOld ? lane.items : lane.items.filter(i => !i.old);
}

function laneEl(lane) {
  const el = document.createElement('section');
  el.className = 'lane';
  el.dataset.lane = lane.state;
  el.dataset.css = lane.css;
  el.innerHTML = '<h2><span class="nm"></span><span class="n"></span></h2>'
    + '<div class="owner"></div><div class="cards"></div>';
  el.querySelector('.nm').textContent = lane.label;
  el.querySelector('.owner').textContent = lane.ownerLabel;
  // **The + exists only where the SERVER says a card may be born.** `lane.creatable`
  // comes from `may_create("terry", state)`, so this asks the permission model
  // rather than carrying a copy of it. Card #0038.
  if (lane.creatable) {
    const add = document.createElement('button');
    add.className = 'add';
    add.type = 'button';
    add.textContent = '+';
    add.title = 'Add a card to ' + lane.label;
    add.addEventListener('click', ev => { ev.stopPropagation(); openMake(lane); });
    el.querySelector('h2').appendChild(add);
  }
  // **Card #0063. Old finished work is hidden by default.** Terry: "cards in COMPLETED
  // for >= 24 hours should not be shown", with a checkbox to bring them back, and his
  // word for it: "I like 'Unhide' vs 'Show' or 'Display' here."
  //
  // **The state is in memory, not in localStorage, and that is the point of "by
  // default".** paint() runs twice a second and MUST NOT re-hide what Terry just
  // unhid, which `unhideOld` handles. A reload starting hidden again is the requirement
  // rather than a shortcoming.
  const hiding = (lane.oldCount || 0) > 0;
  if (hiding) {
    const wrap = document.createElement('label');
    wrap.className = 'unhide';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = unhideOld;
    box.addEventListener('change', () => { unhideOld = box.checked; paint(); });
    const text = document.createElement('span');
    text.textContent = 'Unhide old (' + lane.oldCount + ')';
    wrap.appendChild(box);
    wrap.appendChild(text);
    wrap.title = 'Cards finished more than 24 hours ago are hidden. '
      + 'Tick to bring them back for this visit.';
    el.querySelector('.owner').after(wrap);
  }

  const shown = visibleItems(lane);
  // **The badge counts what is ON SCREEN.** A lane reading 8 above zero visible cards
  // is a number that describes something the eye cannot find.
  el.querySelector('.n').textContent = shown.length;

  const cards = el.querySelector('.cards');
  for (const item of shown) cards.appendChild(card(item));

  el.addEventListener('dragover', ev => {
    const src = document.querySelector('.card.dragging');
    if (!src || src.dataset.state === lane.state) return;
    if (allowed(src.dataset.state, lane.state)) {
      ev.preventDefault();
      el.classList.add('over');
    } else {
      el.classList.add('deny');
    }
  });
  el.addEventListener('dragleave', () => {
    el.classList.remove('over'); el.classList.remove('deny');
  });
  el.addEventListener('drop', async ev => {
    ev.preventDefault();
    el.classList.remove('over'); el.classList.remove('deny');
    let payload;
    try { payload = JSON.parse(ev.dataTransfer.getData('text/plain')); }
    catch (e) { return; }
    if (!allowed(payload.from, lane.state)) {
      toast('Not yours to drop there: ' + payload.from + ' \\u2192 ' + lane.state, true);
      return;
    }
    const res = await apiPost(API_PREFIX + '/cards/' + encodeURIComponent(payload.id) + '/move',
                              {to: lane.state});
    const out = await res.json();
    acceptRevision(out);
    if (!res.ok) { toast(out.error || 'Move refused', true); return; }
    toast(out.result);
    seen = null;
  });
  return el;
}

// **One shared card horizon when the completed-lane control takes a row.** The
// `Unhide old` control exists only while old cards are available, so permanently
// reserving its height would waste space on every other board. Measuring the rendered
// list edges avoids copying a font-dependent pixel height and also absorbs a wrapped
// lane title. Clearing the previous padding first makes resize realignment idempotent.
function alignCardStarts() {
  const board = document.getElementById('board');
  const lists = Array.from(board.querySelectorAll('.cards'));
  for (const list of lists) list.style.paddingTop = '';
  if (!lists.length || !board.querySelector('.unhide')) return;

  const target = Math.max(...lists.map(list => list.getBoundingClientRect().top));
  for (const list of lists) {
    const gap = Math.max(0, target - list.getBoundingClientRect().top);
    if (gap) list.style.paddingTop = gap + 'px';
  }
}

window.addEventListener('resize', alignCardStarts);

// **FLIP: First, Last, Invert, Play.** Terry wanted to watch Claude's moves happen
// rather than see a card teleport: "a motion animation would be fun af for me to
// watch in realtime."
//
// **The board repaints wholesale, so the old elements are gone by the time the new
// ones exist.** FLIP works anyway because it compares POSITIONS rather than nodes:
// measure every card's rectangle before the rebuild, measure again after, then
// translate each survivor back to where it was and let CSS carry it home.
//
// **Cards are matched by `data-id`**, which is why stable ids mattered beyond
// bookkeeping. Under the old positional row numbers a signoff renumbered everything
// and every card would have appeared to move.
function measureCards() {
  const seen = new Map();
  for (const el of document.querySelectorAll('.card[data-id]')) {
    seen.set(el.dataset.id, el.getBoundingClientRect());
  }
  return seen;
}

function playFlip(before) {
  if (!before.size) return;   // First paint. Nothing moved; it all just arrived.
  for (const el of document.querySelectorAll('.card[data-id]')) {
    const was = before.get(el.dataset.id);
    if (!was) continue;       // A brand new card fades in rather than flying in.
    const now = el.getBoundingClientRect();
    const dx = was.left - now.left;
    const dy = was.top - now.top;
    // Sub-pixel drift from a reflow is not a move. Two pixels is the floor.
    if (Math.abs(dx) < 2 && Math.abs(dy) < 2) continue;

    el.style.transform = 'translate(' + dx + 'px, ' + dy + 'px)';
    // **Two frames, not one.** Setting the transform and removing it in the same
    // frame collapses to no animation at all, because the browser never renders
    // the inverted position.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.classList.add('flip');
      el.style.transform = '';
      el.addEventListener('transitionend', () => {
        el.classList.remove('flip');
        el.classList.add('landed');
      }, {once: true});
    }));
  }
}

// **A card can ask for Terry and he can be in another window.** His words: "with no
// email those could get missed." Three escalating tells, none of which needs a mail
// server:
//
//   1. **The tab title.** Free, always on, survives a backgrounded tab. `(2) FGA
//      board` is visible in the tab strip from any other window.
//   2. **A desktop notification**, if he grants it once. Fires only when a count
//      GOES UP, never on a repaint, so a board sitting at two does not nag.
//   3. **A push to his phone**, which is Claude's job rather than the page's --
//      Claude sends one when it moves a card to Needs Terry.
//
// **It fires on a RISE, not on a non-zero value.** A notification that repeats every
// 400ms while a count sits at one is the boy-who-cried-wolf failure this project
// keeps guarding against, and it would be worse than silence.
let lastCta = null;
let boardTitle = document.title;

function announce(c) {
  const asking = (c.needs_terry_action || 0) + (c.blocked || 0);
  document.title = asking > 0 ? '(' + asking + ') ' + boardTitle : boardTitle;

  if (lastCta !== null && asking > lastCta && alertsOn()) {
    const n = new Notification('FGA board needs you', {
      body: (c.needs_terry_action || 0) + ' waiting for Terry, '
          + (c.blocked || 0) + ' blocked',
      tag: 'fga-cta',          // Replaces its predecessor rather than stacking.
    });
    n.onclick = () => { window.focus(); n.close(); };
  }
  lastCta = asking;
}

// **THE BROWSER PERMISSION AND THIS PREFERENCE ARE TWO DIFFERENT THINGS**, and
// conflating them is what made the old button vanish forever after one click.
//
// A permission is granted once and CANNOT be revoked from script. A preference is
// ours, lives in `localStorage` beside `fga-hide-landed`, and can be turned off.
// Terry asked for a checkbox precisely because the button gave him no way to see
// whether alerts were on, or to switch them off again.
//
// **Three states, rendered honestly:**
//   `default` -- unchecked; ticking it prompts.
//   `granted` -- checked or not, per the stored preference.
//   `denied`  -- unchecked and DISABLED. **A checkbox that silently does nothing
//                is worse than one that admits it cannot.**
const ALERTS_KEY = 'fga-alerts';
const alertsBox = document.getElementById('alerts');
const alertsWrap = document.getElementById('alerts-wrap');

function alertsOn() {
  return ('Notification' in window)
    && Notification.permission === 'granted'
    && localStorage.getItem(ALERTS_KEY) === '1';
}

function syncAlerts() {
  if (!('Notification' in window)) {
    alertsWrap.hidden = true;
    return;
  }
  const denied = Notification.permission === 'denied';
  alertsBox.disabled = denied;
  alertsWrap.classList.toggle('blocked', denied);
  alertsWrap.title = denied
    ? 'Your browser has blocked notifications for this site.'
    : 'Desktop notification when a card starts waiting on you.';
  alertsBox.checked = alertsOn();
}

alertsBox.addEventListener('change', async () => {
  if (!alertsBox.checked) {
    localStorage.setItem(ALERTS_KEY, '0');
    syncAlerts();
    return;
  }
  // **The prompt only appears behind this gesture.** Chrome refuses
  // `requestPermission` without one, and an auto-request on load is the pattern
  // people reflexively deny -- which would leave the page permanently unable to
  // ask again.
  if (Notification.permission === 'default') {
    await Notification.requestPermission();
  }
  // **Stored only if the browser actually said yes.** Otherwise the box unticks
  // itself, which is the honest answer to "I asked and was refused".
  localStorage.setItem(ALERTS_KEY,
    Notification.permission === 'granted' ? '1' : '0');
  syncAlerts();
});

syncAlerts();

// **SEARCH. Card #0061.** Terry: "This board state will become my memory. 'Didn't we do
// something like that before?' a search window to find substrings in other tickets would
// be nice."
//
// **It searches the DESCRIPTION and the COMMENTS, not just the title.** That is the whole
// point: the reasoning he is trying to find again lives in the body of a card, and a
// title-only search would answer "no" to most of the questions he actually asks.
//
// **Entirely client-side.** `/api/v001/board` already carries `detail` and `comments`,
// so no
// request is made and the result appears while he types.
let query = (staleReloadState && typeof staleReloadState.query === 'string')
  ? staleReloadState.query : '';
const HAY = new Map();

function hay(item) {
  // **Built once per card and cached**, because a repaint plus a keystroke would
  // otherwise rebuild every haystack on every character.
  //
  // **Tags are stripped.** `detail` arrives as HTML from `inline()`, so a search for
  // "span" would otherwise match every card that contains a line break.
  let text = HAY.get(item.id);
  if (text === undefined) {
    text = [item.ticket, item.subject, item.detail || '',
            ...(item.comments || []).map(c => c.text || '')]
      .join(' ').replace(/<[^>]*>/g, ' ').toLowerCase();
    HAY.set(item.id, text);
  }
  return text;
}

function applyFilter() {
  const board = document.getElementById('board');
  const findn = document.getElementById('findn');
  const q = query.trim().toLowerCase();
  // **Both branches count `visibleItems`, never `lane.items`.** Card #0063: a card
  // hidden for being old has no element to highlight, so counting it here would report
  // matches the eye cannot find -- the same disagreement, arriving through the search.
  if (!q) {
    for (const el of board.querySelectorAll('.card.nomatch')) el.classList.remove('nomatch');
    for (const lane of data.lanes || []) {
      const el = board.querySelector('.lane[data-lane="' + lane.state + '"] .n');
      if (el) { el.textContent = visibleItems(lane).length; el.title = ''; }
    }
    findn.textContent = '';
    return;
  }
  let total = 0;
  for (const lane of data.lanes || []) {
    let shown = 0;
    const pool = visibleItems(lane);
    for (const item of pool) {
      const el = board.querySelector('.card[data-id="' + CSS.escape(item.id) + '"]');
      const hit = hay(item).includes(q);
      if (hit) shown += 1;
      if (el) el.classList.toggle('nomatch', !hit);
    }
    total += shown;
    const n = board.querySelector('.lane[data-lane="' + lane.state + '"] .n');
    if (n) {
      n.textContent = shown;
      n.title = shown + ' of ' + pool.length + ' match';
    }
  }
  // **Zero is stated, not left blank.** "No cards match" and "the search has not run" are
  // different answers, and a blank counter says both.
  findn.textContent = total ? total + ' found' : 'no match';
}

function paint() {
  // The project name is board data, not page-build data. Update both title surfaces on
  // every changed-board paint so `--set-project` reaches an already-open tab without a
  // reload. An error payload has no project and must not erase the last known name.
  if (typeof data.project === 'string' && data.project) {
    boardTitle = data.project;
    document.getElementById('title').textContent = boardTitle;
  }
  // **"Comment as Terry" was in the MARKUP until card #0072**, and dropping the name
  // rather than configuring it would have lost something real: the button says who the
  // comment gets attributed to, which is the one thing a shared board must not leave to
  // guesswork.
  document.getElementById('post').textContent =
    'Comment as ' + userLabel(data.browserUser);

  const board = document.getElementById('board');
  const before = measureCards();
  board.replaceChildren();
  for (const lane of data.lanes) board.appendChild(laneEl(lane));
  alignCardStarts();
  playFlip(before);
  // **REAPPLIED AFTER EVERY REPAINT, and forgetting this is the obvious bug.** `paint()`
  // rebuilds every card, so a filtered board would silently un-filter itself the next
  // time Terry moved a card or Claude wrote a comment -- 400 ms later, with his query
  // still sitting in the box. Card #0061.
  applyFilter();

  const banner = document.getElementById('banner');
  banner.classList.toggle('show', !!data.error);
  if (data.error) {
    document.getElementById('banner-head').textContent = 'The board did not load.';
    document.getElementById('banner-body').textContent = data.error;
  }

  // **Three counters, and every one is something TERRY must do.** Nothing else
  // belongs here -- the lane headers already carry every other number, and a stat
  // that never asks for anything teaches the eye to skip past the ones that do.
  //
  // **Zero is plain text; non-zero is a hazard pill.** Quiet when there is nothing
  // to do, impossible to miss when there is.
  const c = data.counts || {};
  const counts = document.getElementById('counts');
  counts.replaceChildren();
  // **Ranked worst first, left to right.** Terry's order. Blocked means neither of
  // us can move it; waiting-for-Terry means Claude is stopped until he answers;
  // needs-signoff means the work is done and a tick is outstanding. **Reading order
  // is severity order**, so the leftmost pill that is lit is the worst thing on the
  // board.
  // **A ZERO CTA IS NOT SHOWN AT ALL.** Terry, 2026-08-19: "if any of the CTA's on
  // title bar are zero count, do not show them. Having a CTA that cannot be actioned
  // defeats the fucking purpose." Card #0056.
  //
  // **This is his own doctrine, applied where it was previously only stated.** The bar
  // already said "a number that never asks for anything trains the eye to skip the
  // whole bar" -- and a zero is exactly a number that never asks for anything.
  //
  // **THE COUNT MUST BE KNOWN, NOT MERELY FALSY, and that distinction is the whole
  // guard.** When the board cannot be read, `payload()` returns `counts: {}` -- so
  // `c.blocked || 0` would render 0, and hiding on 0 would make EVERY pill vanish and
  // the bar read as "nothing to do" at the exact moment nothing is known.
  //
  // **A CTA that is absent because it is zero must not be confusable with one that is
  // absent because the number never arrived.** So a missing key is skipped rather
  // than treated as zero, and the dead poll is reported by the dot and the badge --
  // which are the elements whose job that is.
  for (const pair of [
    ['BLOCKED', c.blocked],
    ['WAITING FOR TERRY', c.needs_terry_action],
    ['NEEDS SIGNOFF', c.ready_for_review],
  ]) {
    if (typeof pair[1] !== 'number' || pair[1] <= 0) continue;
    const s = document.createElement('span');
    s.className = 'cta hot';
    s.textContent = pair[0] + ': ' + pair[1];
    counts.appendChild(s);
  }

  announce(c);

  // Repaint the drawer too, so a comment posted a moment ago appears without the
  // card having to be reopened.
  if (openId) openCard(openId);
}

// ---- proof of life -------------------------------------------------------
//
// **Terry had a dashboard whose backend had crashed, and the page looked fine.**
// His words: "past date/time does not mean it's the latest." A timestamp answers
// "when did I last get data", which reads IDENTICALLY to "when did everything
// stop" -- and the second one is the case you needed to notice.
//
// **So liveness is measured by the CLIENT against its own clock**, and it counts
// UP. `lastOk` is the only thing this trusts: the moment a fetch actually
// returned. Nothing the server sends can fake it, because a server that sends
// nothing cannot move it.
let lastOk = 0;
// The board file's own mtime, in client-clock milliseconds. Both processes are on
// this machine, so the two clocks are the same clock.
let fileMs = 0;

// **`lastOk` moves ONLY when Python confirms it read and parsed the board.** The
// `/api/v001/status` route performs a real `board_state.load()`, and its `ok` field
// is what this keys on -- not the HTTP status, because a socket answering proves the
// process is up and nothing about the file.
const LIVE_MS  = 5000;   // Terry's number: "read that file within last 5 seconds".

// **What THIS TAB is running, frozen at load time.** The server stamps it into the HTML,
// so it describes the JavaScript the browser actually has -- not what the checkout says
// now. Every poll compares it against the server's live answer.
//
// **This exists because a patched server and an unpatched page look IDENTICAL.** On
// 2026-08-18 a P0 was fixed, verified, and reported still broken, because the tab had
// been open across the restart and was running the code from before it. Both of us read
// the symptom as a bad fix, and twenty minutes went into a defect that was not there.
const PAGE_BUILD = '%BUILD%';
let serverBuild = null;
// **Whether the SERVER is running older code than the files on disk.** A different
// question from `serverBuild !== PAGE_BUILD`, and one nothing measured until card
// #0052 -- both of those ids freeze at their own start, so a stale server makes them
// AGREE and the reload flag stays hidden.
let codeStale = false;
let restarting = false;
let restartProblem = null;
let pushState = null;
const WARN_MS  = 15000;  // ~37 polls missed at 400ms. Nothing benign lasts this long.

// **The dot's 2s breathing cycle is CSS, not JavaScript, and it is cosmetic.**
// **Do not "fix" the poll rate to match it.** The badge claims Python read the
// file within 5 seconds; polling at the animation's pace would leave two or three
// chances to notice a failure before the claim is already false. The animation is
// how it feels; POLL_MS is what it guarantees.

// **One absolute clock, everything else relative.** Terry's call: the two
// timestamps became "N seconds/minutes ago" and only `Currently` stays a wall
// clock. **The absolute one is the tick; the relative ones are the answer.** A
// past HH:MM:SS makes you subtract before you know whether to worry, and doing
// arithmetic under stress is how a stale dashboard gets believed.
function clockOf(ms) {
  // **`2:09:29pm`, lower case and closed up.** Terry's preference, stated as one:
  // "for AM/PM go all lower case and no space between second and am/pm."
  //
  // **Built by hand rather than by `toLocaleTimeString`, and that is the safer
  // choice here.** Chrome emits `2:09:29 PM` with U+202F NARROW NO-BREAK SPACE
  // before the meridiem, not an ordinary space -- so the obvious `.replace(' PM')`
  // silently does nothing on a machine with current ICU, and a regex for it needs
  // escapes that this Python template would have to double.
  const d = new Date(ms);
  const h24 = d.getHours();
  const h12 = ((h24 + 11) % 12) + 1;
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  return h12 + ':' + mm + ':' + ss + (h24 < 12 ? 'am' : 'pm');
}

function agoOf(ms) {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return s + (s === 1 ? ' second ago' : ' seconds ago');
  const m = Math.round(s / 60);
  if (m < 60) return m + (m === 1 ? ' minute ago' : ' minutes ago');
  const h = Math.round(m / 60);
  return h + (h === 1 ? ' hour ago' : ' hours ago');
}

let staleReloadStarted = false;

function saveStaleReloadState() {
  if (openId) drafts[openId] = document.getElementById('say').value;
  const saved = {
    drafts: drafts,
    openId: openId,
    editing: editing,
    query: query,
    unhideOld: unhideOld,
  };
  if (editing === 'subject') {
    saved.editValue = document.getElementById('p-subject-edit').value;
  } else if (editing === 'detail') {
    saved.editValue = document.getElementById('p-detail-text').value;
  }
  if (mkOpen) {
    saved.make = {
      state: mkOpen,
      subject: document.getElementById('mk-subject').value,
      detail: document.getElementById('mk-detail').value,
      priority: document.getElementById('mk-priority').value,
      owner: document.getElementById('mk-owner').value,
    };
  }
  try {
    sessionStorage.setItem(STALE_RELOAD_STATE_KEY, JSON.stringify(saved));
  } catch (err) {
    // Storage can be disabled. Reload still works; there is simply no restored UI.
  }
}

function restoreStaleReloadState() {
  const saved = staleReloadState;
  if (!saved) return;
  staleReloadState = null;

  document.getElementById('find').value = query;
  if (saved.openId && itemById(saved.openId)) {
    openCard(saved.openId);
    if (saved.editing === 'subject') {
      startSubjectEdit();
      document.getElementById('p-subject-edit').value = saved.editValue || '';
    } else if (saved.editing === 'detail') {
      startDetailEdit();
      document.getElementById('p-detail-text').value = saved.editValue || '';
    }
  }
  if (saved.make) {
    const lane = (data.lanes || []).find(item => item.state === saved.make.state);
    if (lane) {
      openMake(lane);
      document.getElementById('mk-subject').value = saved.make.subject || '';
      document.getElementById('mk-detail').value = saved.make.detail || '';
      document.getElementById('mk-priority').value = saved.make.priority || '';
      document.getElementById('mk-owner').value = saved.make.owner || '';
    }
  }
}

function reloadForServerBuild(build) {
  const next = new URL(window.location.href);
  if (next.searchParams.get(STALE_RELOAD_QUERY) === build) return false;
  if (staleReloadStarted) return true;
  staleReloadStarted = true;
  saveStaleReloadState();
  // The build query cache-busts the page and proves one reload already happened.
  // If the returned page still disagrees, the UI reports that instead of looping.
  next.searchParams.set(STALE_RELOAD_QUERY, build);
  window.location.replace(next.toString());
  return true;
}

function clearStaleReloadMarker() {
  const clean = new URL(window.location.href);
  if (!clean.searchParams.has(STALE_RELOAD_QUERY)) return;
  clean.searchParams.delete(STALE_RELOAD_QUERY);
  window.history.replaceState(null, '', clean.toString());
}

// **Terry specified this bar himself:** "last update: HH:MM:SS - Currently:
// HH:MM:SS [green circle]".
//
// **It is better than a computed age, and the reason is that it needs no
// interpretation.** `Currently` is a wall clock the browser ticks every half
// second. If it FREEZES, the page is dead. If the GAP between the two grows,
// the server is dead. Both failures are visible without anyone trusting a
// number this code calculated -- which is the whole complaint, since the
// dashboard that burned him showed a perfectly plausible timestamp.
function renderLive() {
  const el = document.getElementById('live');
  const badge = document.getElementById('badge');
  const dot = document.getElementById('dot');
  const nowMs = Date.now();
  const nowTxt = clockOf(nowMs);

  // **Checked BEFORE the lastOk guard**, because a stale tab and a dead poll are
  // independent failures. A confirmed mismatch repairs itself after preserving every
  // unsent field; the red pill is reserved for a failed automatic recovery.
  //
  // **Silence here means the two ids AGREE.** An unknown build on either side is not a
  // mismatch -- it is "cannot tell", and shouting on it would fire this every time git
  // is unavailable, which is how a warning becomes wallpaper.
  const stale = document.getElementById('stale');
  const known = serverBuild && serverBuild !== 'unknown' && PAGE_BUILD !== 'unknown';
  const drifted = known && serverBuild !== PAGE_BUILD;
  if (drifted && reloadForServerBuild(serverBuild)) return;
  if (known && !drifted) clearStaleReloadMarker();
  const reloadFailed = drifted;
  if (reloadFailed) {
    stale.textContent = 'AUTO-RELOAD FAILED  \\u00b7  page ' + PAGE_BUILD
      + '  \\u00b7  server ' + serverBuild;
  }
  stale.classList.toggle('show', reloadFailed);

  // **A SECOND, DIFFERENT STALENESS, and it needs a DIFFERENT INSTRUCTION.** Card
  // #0052.
  //
  // The tab/server mismatch above repairs itself. This compares the SERVER to the code
  // on DISK, and **a browser reload cannot fix it** -- the tab would re-fetch the same
  // page from the same process holding the same old modules.
  //
  // The server owns the graceful re-exec. This pill reports the short transition or,
  // if preflight refuses the changed source, the specific problem that needs fixing.
  const restart = document.getElementById('restart');
  restart.textContent = restartProblem
    ? 'AUTO-RESTART PAUSED  \\u00b7  ' + restartProblem
    : (restarting ? 'RESTARTING SERVER  \\u00b7  code changed on disk'
                  : 'RESTART SERVER  \\u00b7  code changed on disk');
  restart.classList.toggle('show', !!codeStale);

  // **A THIRD flag, and this one is about the DATA rather than the view.** Card #0013.
  //
  // `#stale` and `#restart` both say "what you are looking at is out of date". Nothing
  // is lost in either case. **This one says the board exists in exactly one place**,
  // which is the condition the private remote was created to end.
  //
  // **It shows on `failed` and on nothing else.** `off` is a legitimate configuration,
  // `pending` is the normal state five seconds a day, and `ok` is the resting state --
  // **none of the three is something Terry can act on**, and a pill he cannot act on
  // becomes scenery. Same loudness rule the toolchain banner runs on.
  //
  // **It is placed BEFORE the `lastOk` return, like `#restart`.** A dead poll and an
  // unpushed board are independent, and hiding this one during the other is exactly
  // the bug that put the restart flag here.
  const push = document.getElementById('push');
  const pushFailed = !!pushState && pushState.state === 'failed';
  if (pushFailed) {
    push.textContent = 'NOT BACKED UP  \\u00b7  push failed';
    push.title = String(pushState.detail || 'git push failed');
  }
  push.classList.toggle('show', pushFailed);

  if (!lastOk) {
    // **`Build` leads here TOO, and it did not before.** This branch never reached
    // the `parts` list, so the build id vanished exactly when the poll was dead --
    // the moment somebody most needs to know which code they are looking at.
    // Cards #0043 and #0049.
    el.textContent = 'Build ' + PAGE_BUILD + '  ·  Last JSON file read: never  ·  ' + nowTxt;
    badge.className = 'dead';
    badge.textContent = 'NO DATA';
    dot.classList.add('stale');
    dot.classList.remove('alive');
    return;
  }

  // **All three in ONE span, so every separator is the same separator.** Terry
  // asked for the dot between the first two to match the one already between the
  // last two; two spans plus a flex gap could never quite line up with a `·`
  // typed into the middle of a string.
  //
  // The file's age is a DIFFERENT fact from the poll's, and both belong here: a
  // board nobody has touched for two hours is normal, and a poll that has not
  // answered for two hours is not.
  // **The wall clock carries no label.** Terry: "drop 'Currently:', it's obvious
  // its current local time." The two ages are labelled because a bare duration
  // could be either; a running clock explains itself.
  const age = nowMs - lastOk;
  const parts = [];
  // **`Build` LEADS, and it is capitalized.** Terry, 2026-08-19: "Move 'build (hash)'
  // to left of 'File written'. Separate with dot to stay consistent, capitalize
  // 'Build'." Card #0043. The separator was already the dot he wanted.
  //
  // **It shows even when it AGREES with the server, and that is deliberate.** A check
  // that is invisible while healthy is indistinguishable from one that was never wired
  // up -- the failure this whole bar exists to refuse. It sits in the proof-of-life
  // span rather than beside the counts, because it is EVIDENCE and not a call to
  // action.
  parts.push('Build ' + PAGE_BUILD);
  // **Both labels renamed, and the pair gets DISAMBIGUATED.** Card #0049. One said
  // *written* and the other said *update* -- two words for one idea, neither saying
  // WHAT was written or updated.
  //
  // **The distinction was already documented here and invisible on screen:** the
  // file's age and the poll's age are different facts. A board nobody has touched for
  // two hours is normal; a poll that has not answered for two hours is not. **One
  // going quiet is fine and the other is a fault, and now the labels say which.**
  if (fileMs) parts.push('Last card update: ' + agoOf(fileMs));
  parts.push('Last JSON file read: ' + agoOf(lastOk));
  parts.push(nowTxt);
  el.textContent = parts.join('  ·  ');

  // **The badge flips at LIVE_MS, not at WARN_MS.** It carries a single claim --
  // "Python read that file within the last 5 seconds" -- and a badge that stays
  // green while quietly becoming false is the whole problem being fixed.
  if (age < LIVE_MS) {
    badge.className = '';
    badge.textContent = 'LIVE';
  } else if (age < WARN_MS) {
    badge.className = 'warn';
    badge.textContent = 'NO ANSWER ' + Math.round(age / 1000) + 's';
  } else {
    const s = Math.round(age / 1000);
    badge.className = 'dead';
    badge.textContent = 'STALE ' + (s < 90 ? s + 's' : Math.round(s / 60) + 'm');
  }
  // **The dot and the badge key off the SAME `lastOk` and the same threshold**, so
  // they cannot disagree. Breathing green and a red STALE badge side by side would
  // be worse than either alone.
  const live = age < LIVE_MS;
  dot.classList.toggle('stale', !live);
  dot.classList.toggle('alive', live);
}

// **POLLING IS THE DESIGN, not a fallback for missing file watching.** Terry:
// "I hope inotify or equivalent works and we get realtime update, but the timer
// also actively polling will keep blood pressure low."
//
// **A push socket would be worse here, and for his own reason.** A WebSocket or
// SSE stream that goes quiet is indistinguishable from one that is working and
// has nothing to say -- which is the crashed-dashboard failure again, wearing a
// different protocol. **A poll that must answer every 400ms cannot go quiet
// without the circle noticing.**
//
// 400ms is realtime as far as an eye is concerned, and the cost is one `stat`
// plus a 12 KB JSON parse against a local file.
async function tick() {
  try {
    const meta = await (await fetch(API_PREFIX + '/status', {cache: 'no-store'})).json();
    // **Captured before the ok check**, because a stale tab and an unreadable board
    // are independent failures and either can be true while the other is.
    if (meta.build) serverBuild = meta.build;
    // **Captured beside `build`, on both the ok and the failure path**, because a
    // stale process and an unreadable board are independent and either can be true
    // while the other is.
    codeStale = !!meta.codeStale;
    restarting = !!meta.restarting;
    restartProblem = meta.restartProblem || null;
    // **Captured on BOTH paths for the third time, and for the same reason.** A board
    // that cannot be read and a board that cannot be pushed are independent failures.
    pushState = meta.push || null;
    // **`ok: false` does NOT refresh `lastOk`.** The server answered, but it
    // could not read the board -- and a reachable server serving an unreadable
    // file is exactly the state that must not look healthy.
    if (meta.ok === false) { renderLive(); return; }
    fileMs = meta.mtime * 1000;
    if (meta.mtime !== seen) {
      data = await (await fetch(API_PREFIX + '/board', {cache: 'no-store'})).json();
      // **The search cache MUST die with the data that filled it.** Card #0061. A card
      // that gains a comment keeps its id, so a cache keyed on the id alone would go on
      // answering from the text that card had a minute ago -- and a search for a
      // sentence Terry just wrote would report "no match".
      HAY.clear();
      seen = meta.mtime;
      paint();
      restoreStaleReloadState();
    }
    // Only a real answer moves this. It is the whole signal.
    lastOk = Date.now();
  } catch (e) {
    // Deliberately does NOT touch lastOk. The age keeps climbing, and that is
    // what turns the bar red on its own.
  }
  renderLive();
}
tick();
setInterval(tick, %POLL%);
// **A second timer, and it is not redundant.** If `tick` itself wedges -- a
// hung fetch, a thrown error in paint -- the age would freeze at whatever it
// last rendered and the page would look healthy forever. This one only reads
// the clock, so it keeps counting when everything else has stopped.
setInterval(renderLive, 500);
</script>
</body>
</html>
"""


def _report_rule_gaps(policy: board_state.TransitionPolicy) -> None:
    """Say how many permission rows still owe a description. Card #0064.

    **Terry wants the rules pedantic enough to pull a sentence out of a human**: *"Tell
    me why actor X should be able to make this card movement."*

    **Printed rather than enforced.** Refusing to start over an unwritten sentence would
    have Claude filling seventeen of them in to unblock itself, and filler reads as
    considered. **A number he sees at every start is the pressure**; a blocked server is
    just a blocked server.
    """
    blank, shared = board_state.rules_gaps(document=policy.to_json())
    if not blank and not shared:
        return
    print("  embedded policy, edge descriptions still owed:")
    if blank:
        print(f"      {len(blank)} edge(s) carry NO description")
    if shared:
        print(f"      {len(shared)} edge(s) share ONE description across both actors")
    print("      Why may THIS actor make THIS move? Each row answers for itself.")


def payload() -> bytes:
    """The board as JSON for the page, or an `error` the page renders as a banner.

    **A load failure is DATA, not a 500.** The page stays up and says what is wrong,
    because a blank tab and a broken parser look identical and one of them is a lie.
    """
    try:
        board = STORE.snapshot() if STORE is not None else board_state.load(BOARD_PATH)
    except (board_state.BoardError, OSError, json.JSONDecodeError) as exc:
        return json.dumps({"lanes": [], "edges": [], "counts": {}, "error": str(exc)}).encode(
            "utf-8"
        )

    # **Drift is reported to the page, not swallowed.** `verify()` replays each item's
    # history; a mismatch means something changed a state without going through
    # `move()`, and that is exactly the news a board must not keep to itself.
    drift = board.verify()
    lanes = board.lanes()
    policy = board.policy
    browser_edges = policy.edges_for(board.browser_user)

    # **Card #0028. Relationships reach the page as LABELS, never as slugs.** The stored
    # row names `id`s because those are stable; Terry says "#0028" out loud, so the wire
    # carries what he says. `subject` rides along so a link is readable without a lookup.
    by_id = {i.id: i for i in board.items}

    def related(item_id: str) -> list[dict[str, str]]:
        """Every relationship this card has, including the derived direction."""
        return [
            {"kind": kind, "ticket": by_id[other].label, "subject": by_id[other].subject}
            for kind, other in board.links_for(item_id)
            if other in by_id
        ]

    kids: dict[str, list[dict[str, str]]] = {}
    for child in board.items:
        if child.parent:
            kids.setdefault(child.parent, []).append(
                {"ticket": child.label, "subject": child.subject}
            )
    counts: dict[str, int] = {lane.state: len(lane.items) for lane in lanes}
    counts["open"] = sum(len(lane.items) for lane in lanes if lane.state != "completed")

    return json.dumps(
        {
            "revision": board.revision,
            "project": board.project,
            "lanes": [
                {
                    "state": lane.state,
                    "label": lane.label,
                    "css": lane.css,
                    "ownerLabel": lane.owner_label,
                    # **Where the `+` appears, decided by the same predicate the write path
                    # enforces.** `may_create` is what `/create` calls, so the button cannot
                    # offer a lane the POST would refuse. Adding a lane to `create` in
                    # `rules.json` grows a `+` with no code change here or in the page.
                    "creatable": policy.may_create(board.browser_user, lane.state),
                    # **Card #0063. Counted on the server, beside the flag it counts**, so the
                    # checkbox label and the hiding can never disagree about how many.
                    "oldCount": sum(1 for i in lane.items if is_old(i)),
                    "items": [
                        {
                            "id": item.id,
                            "ticket": item.label,
                            "state": item.state,
                            "laneLabel": lane.label,
                            "subject": item.subject,
                            "priority": item.priority,
                            "priorityLabel": policy.priority_label.get(item.priority, ""),
                            # **Card #0063.** Computed on the SERVER so one clock decides it, and
                            # so the 24-hour rule lives in exactly one place. The page re-fetches
                            # twice a second, so a card ages out on its own without a reload.
                            **({"old": True} if is_old(item) else {}),
                            "detail": inline(item.detail),
                            # **The RAW text as well as the rendered form, card #0082.** `detail`
                            # has already been through `inline()` -- escaped, with the markdown-ish
                            # bits turned into tags -- and putting THAT into an editor would hand
                            # Terry HTML to edit and then store it, one round trip from unreadable.
                            "detailRaw": item.detail,
                            # Card #0028. Omitted when empty so a board of unrelated cards stays
                            # the same size on the wire as it was before relationships existed.
                            **({"links": related(item.id)} if board.links_for(item.id) else {}),
                            **(
                                {
                                    "parent": {
                                        "ticket": by_id[item.parent].label,
                                        "subject": by_id[item.parent].subject,
                                    }
                                }
                                if item.parent and item.parent in by_id
                                else {}
                            ),
                            **({"children": kids[item.id]} if item.id in kids else {}),
                            # **A LABEL, never a permission.** Card #0053, Terry: *"It's just a
                            # label, not permissions model."* Nothing in the page may branch on
                            # this to allow or refuse a move -- `draggable` below and `allowed()`
                            # in the script both read the permission table and MUST keep doing so.
                            "owner": item.owner,
                            # **Computed on the SERVER from the same table the server enforces**,
                            # so the cursor and the answer cannot disagree.
                            "draggable": any(a == item.state for a, _ in browser_edges),
                            "comments": [
                                {"by": c.by, "when": when(c.at), "text": inline(c.text)}
                                for c in item.comments
                            ],
                            # **Newest first.** Terry: "needs to be newest at top (most
                            # relevant) to oldest at bottom." The Completed lane already
                            # reverses for the same reason -- the interesting end of a
                            # permanent record is the recent end, and a long trail otherwise
                            # buries the entry you opened the card to read.
                            #
                            # **Reversed HERE rather than in `board_state.py`.** The stored order is
                            # chronological and `verify()` replays it forwards; flipping the
                            # model to suit a drawer would break the audit.
                            # **Ownership entries ride the SAME list**, because the trail is one
                            # chronological thing to read. `ownerTo` is the discriminant and is
                            # absent on a lane move. Card #0053.
                            "history": [
                                {
                                    "by": h.by,
                                    "when": when(h.at),
                                    "from": h.frm,
                                    "fromLabel": policy.lane_label.get(h.frm or "", ""),
                                    "toLabel": policy.lane_label.get(h.to, h.to),
                                    "ownerFrom": h.owner_frm,
                                    "ownerTo": h.owner_to,
                                    # Card #0070, the third entry shape.
                                    "priorityFrom": h.priority_frm,
                                    "priorityTo": h.priority_to,
                                }
                                for h in reversed(item.history)
                            ],
                        }
                        for item in lane.items
                    ],
                }
                for lane in lanes
            ],
            "edges": sorted(browser_edges),
            # **The priority list ships from `rules.json`, never typed into the page.**
            # A range written in HTML is a second copy that goes stale silently -- and it
            # already nearly did: card #0047 was specified as "P1-P5" while `P0` exists.
            "priorities": [
                {"id": p, "label": policy.priority_label.get(p, "")} for p in policy.priorities
            ],
            "defaultPriority": policy.default_priority,
            # **The actor list ships from the server, never typed into the page.** Card
            # #0069, and the same rule the priorities already follow. **Card #0072 made it
            # configurable**, so a page naming Terry and Claude in markup would go stale the
            # day somebody else deploys this.
            #
            # **The LABEL now comes from config rather than from `a.capitalize()`.** That
            # call worked for two lowercase ids and would have rendered `mcallister` as
            # `Mcallister`, which is a name spelled wrong on every card that person touches.
            "actors": [
                {"id": u.id, "label": u.label, "class": u.user_class, "color": u.color}
                for u in board.users
            ],
            # **Who the PAGE writes as.** It labels the comment button and the new-card
            # note, both of which said "Terry" in markup until this card.
            "browserUser": board.browser_user,
            "defaultOwner": board.default_owner,
            "counts": counts,
            "error": "; ".join(drift) if drift else None,
        }
    ).encode("utf-8")


SLUG_MAX = 48


def slug_for(board: board_state.Board, subject: str) -> str:
    """A card id derived from its title, unique on this board.

    **The CLI makes Claude type a slug and the web form MUST NOT.** Terry is writing
    a title, and asking him for a second machine-readable name would be asking him to
    do the computer's job -- `find()` already accepts the ticket number, which is the
    handle he actually says out loud.

    **A collision is broken with the ticket number the card is ABOUT to get**, not
    with a counter of its own. `-2` would be a second numbering scheme that means
    nothing; the ticket is already on the card and already unique. So a second
    `Dark status bar` becomes `dark-status-bar-0051`.

    **Zero-padded, because the house rule says pad anything that will ever sort.**
    """
    base = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:SLUG_MAX].strip("-")
    # A title of nothing but punctuation still needs an id. The caller has already
    # refused an EMPTY subject, so this is the "###" case rather than the blank one.
    base = base or "card"
    if not any(item.id == base for item in board.items):
        return base
    return f"{base}-{board.next_ticket:04d}"


def _apply_http_command(  # noqa: PLR0911, PLR0912 -- one explicit branch per REST action
    board: board_state.Board,
    command_parts: list[str],
    body: board_state.JsonObject,
    actor: board_state.Actor,
    policy: board_state.TransitionPolicy,
) -> str:
    """Translate one validated REST route into one domain mutation."""
    if command_parts == ["cards"]:
        state = str(body["state"])
        subject = str(body["subject"]).strip()
        if not subject:
            raise board_state.BoardError("a card needs a title")
        item_id = str(body.get("id") or slug_for(board, subject))
        return board.create(
            item_id,
            subject,
            state,
            actor,
            priority=str(body.get("priority") or policy.default_priority),
            detail=str(body.get("detail") or ""),
            owner=board_state.as_actor(str(body.get("owner") or board_state.DEFAULT_OWNER)),
        )

    if command_parts == ["board", "project"]:
        was = board.project
        name = str(body["project"]).strip()
        if not name:
            raise board_state.BoardError("a project needs a name")
        board.project = name
        return f"project renamed: {was!r} -> {name!r}"

    ref, action = command_parts[1], command_parts[2]
    if action == "move":
        return board.move(ref, str(body["to"]), actor)
    if action == "comment":
        return board.comment(ref, str(body["text"]), actor)
    if action == "assign":
        return board.assign(ref, board_state.as_actor(str(body["owner"])), actor)
    if action == "priority":
        return board.set_priority(ref, str(body["priority"]).upper(), actor)
    if action == "subject":
        return board.set_subject(ref, str(body["subject"]), actor)
    if action == "detail":
        return board.set_detail(ref, str(body["detail"]), actor)
    if action == "parent":
        parent = body.get("parent")
        return board.set_parent(ref, str(parent) if parent else None, actor)
    if action == "link":
        kind, other = str(body["kind"]).lower(), str(body["other"])
        if body.get("remove"):
            return board.unlink(ref, kind, other, actor)
        return board.link(ref, kind, other, actor)
    raise board_state.BoardError(f"unknown card command {action!r}")


class Handler(http.server.BaseHTTPRequestHandler):
    """The page, the board, a timestamp to poll, the fonts, and three write routes."""

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # A cached copy of the previous write looks exactly like an edit that did not
        # happen, which is the one thing a live view must never show.
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: object, code: int = 200) -> None:
        self._send(json.dumps(obj).encode("utf-8"), "application/json", code)

    def _redirect(self, location: str) -> None:
        self.send_response(307)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()

    def _read_json(self) -> board_state.JsonObject | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = cast("board_state.JsonValue", json.loads(self.rfile.read(length) or b"{}"))
        except ValueError, TypeError:
            return None
        return body if isinstance(body, dict) else None

    def _actor(self) -> board_state.Actor | None:
        """Map a bearer credential to a configured actor; never trust body data."""
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {BROWSER_TOKEN}":
            return board_state.BROWSER_USER
        if auth == f"Bearer {CLI_TOKEN}":
            return board_state.CLI_USER
        return None

    def _expected_revision(self) -> int | None:
        raw = self.headers.get("If-Match", "").strip().strip('"')
        if raw.startswith("revision-"):
            raw = raw.removeprefix("revision-")
        return int(raw) if raw.isdigit() else None

    def do_GET(self) -> None:
        route = self.path.partition("?")[0]
        if route in ("/v1/status", "/mtime"):
            # A pre-upgrade page needs one successful poll to discover the new build
            # and reload itself. This redirect preserves that migration path without
            # retaining a second implementation of the status endpoint.
            self._redirect(API_PREFIX + "/status")
        elif route == API_PREFIX + "/status":
            # **THE GREEN CIRCLE MEANS "PYTHON READ THAT FILE JUST NOW", so this
            # route actually reads it.** Terry set the bar: *"green circle means
            # python has actively polled and read that file within last 5 seconds."*
            #
            # **A `stat()` would have been cheaper and would have lied.** It proves
            # the directory entry exists, not that the board is readable -- a
            # truncated or half-written file passes `stat` and fails `load`. And a
            # server answering HTTP proves only that the socket is up.
            #
            # **`ok` is the field the dot keys on.** Parsing 12 KB of JSON at 2.5 Hz
            # is nothing, and it is the difference between a signal that means
            # something and one that looks reassuring.
            # **THE EMBEDDED POLICY IS RE-READ HERE TOO.** Terry's standing order is
            # that a tool able to detect its own staleness resolves it. Schema 4 keeps
            # lane labels, IDs, and permissions in the same snapshot as the state, so
            # one validated board read refreshes them together.
            reloaded = (
                STORE.reload_policy_if_changed()
                if STORE is not None
                else board_state.reload_rules_if_changed()
            )
            if reloaded:
                print(f"  {reloaded}", flush=True)

            code_stale = code_is_stale()
            current_build = BUILD if code_stale else refresh_build_id_if_clean()
            restarting, restart_problem = (
                request_code_restart(self.server) if code_stale else (False, None)
            )
            code_meta = {
                "build": current_build,
                "codeStale": code_stale,
                "restarting": restarting,
                **({"restartProblem": restart_problem} if restart_problem else {}),
            }

            try:
                mtime = BOARD_PATH.stat().st_mtime
                if STORE is not None:
                    STORE.snapshot()
                else:
                    board_state.load(BOARD_PATH)
            except (OSError, board_state.BoardError, json.JSONDecodeError) as exc:
                # **`build` rides on the FAILURE path too.** An unreadable board and a
                # stale tab are independent problems, and the page must be able to tell
                # them apart while one of them is happening.
                self._json(
                    {
                        "ok": False,
                        "mtime": 0,
                        "stamp": "unreadable",
                        **code_meta,
                        "push": push_status(),
                        "error": str(exc),
                    }
                )
                return
            stamp = (
                datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC)
                .astimezone()
                .strftime("%H:%M:%S")
            )
            # **`codeStale` rides on BOTH paths, like `build` and for the same
            # reason.** An unreadable board and a stale process are independent
            # failures, and either can be true while the other is.
            self._json(
                {"ok": True, "mtime": mtime, "stamp": stamp, **code_meta, "push": push_status()}
            )
        elif route == API_PREFIX + "/board":
            self._send(payload(), "application/json")
        elif route == "/favicon.svg":
            # **Cached, unlike everything else here.** The no-store rule on `_send`
            # exists so a live view never shows a stale board; an icon is not the board
            # and re-fetching it twice a second would be silly.
            blob = FAVICON.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(blob)
        elif route in FONTS:
            # The font is immutable and 133 KB; letting the browser cache it is the
            # one thing on this server that SHOULD be cached.
            try:
                blob = (FONT_DIR / FONTS[route]).read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "font/woff2")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(blob)
        elif route in ("/", "/index.html"):
            # **A broken board still serves its page.** The title falls back and
            # `/api/v001/board` carries the real error to the banner.
            title = "Work board"
            with contextlib.suppress(board_state.BoardError, OSError, json.JSONDecodeError):
                title = board_state.load(BOARD_PATH).project or title
            # **`%BUILD%` is baked into the page at request time**, so the constant the
            # JavaScript holds describes the code THIS tab loaded -- which is the whole
            # comparison. Fetching it later would just re-read the server and always agree.
            page = (
                PAGE.replace("%POLL%", str(POLL_MS))
                .replace("%BUILD%", html.escape(BUILD))
                .replace("%TOKEN%", BROWSER_TOKEN)
                .replace("%API_PREFIX%", API_PREFIX)
                # **The bar's mark comes from the same `FAVICON` the tab icon
                # uses.** Card #0095. One definition, so a recolor cannot land in
                # one place and miss the other.
                .replace("%BRANDURI%", brand_data_uri())
                # **Built per request, not once at import**, so a board cast change
                # reaches the next page load without a restart.
                .replace("%USERCSS%", user_css())
                .replace("%TITLE%", html.escape(title))
            )
            self._send(page.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: PLR0911 -- BaseHTTPRequestHandler contract
        """Execute one authenticated, revision-checked domain command."""
        route = self.path.partition("?")[0]
        if route == API_PREFIX + "/shutdown":
            if self.headers.get("Authorization", "") != f"Bearer {CLI_TOKEN}":
                self._json({"error": "missing or invalid service credential"}, 401)
                return
            ok, detail = request_service_shutdown(self.server)
            if not ok:
                self._json({"error": f"final autopush failed: {detail}"}, 503)
                return
            self._json({"result": "board service shutdown scheduled", "push": detail})
            return
        parts = [urllib.parse.unquote(part) for part in route.strip("/").split("/")]
        command_parts = parts[len(API_PARTS) :] if parts[: len(API_PARTS)] == API_PARTS else []
        is_create = command_parts == ["cards"]
        is_project = command_parts == ["board", "project"]
        is_card_command = len(command_parts) == CARD_COMMAND_PARTS and command_parts[0] == "cards"
        if not (is_create or is_project or is_card_command):
            self.send_error(404)
            return

        actor = self._actor()
        if actor is None:
            self._json({"error": "missing or invalid service credential"}, 401)
            return
        expected = self._expected_revision()
        if expected is None:
            self._json({"error": "If-Match must name the expected board revision"}, 428)
            return
        body = self._read_json()
        if body is None:
            self._json({"error": "bad request body"}, 400)
            return

        with _command_gate:
            if _shutdown_requested.is_set():
                self._json({"error": "board service shutdown is in progress"}, 503)
                return
            store = STORE
            if store is None:
                self._json({"error": "board store is not initialized"}, 409)
                return

            def mutate(board: board_state.Board) -> str:
                return _apply_http_command(board, command_parts, body, actor, store.policy)

            try:
                result, revision = store.execute(expected, mutate)
            except KeyError as exc:
                self._json({"error": f"missing field {exc}"}, 400)
                return
            except RevisionConflict as exc:
                self._json({"error": str(exc)}, 412)
                return
            except (board_state.BoardError, OSError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, 409)
                return

        print(f"  {result}", flush=True)
        self._json({"result": result, "revision": revision})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002, ARG002
        # **The parameter names are the base class's and MUST NOT be renamed.** A
        # parameter name is part of an override's contract, because a caller may pass
        # it by keyword. Both lint codes here are forced by a signature this code does
        # not own.
        #
        # The poll runs twice a second, so logging every request would bury anything
        # worth reading. Only a real data fetch prints.
        #
        # **`args[0]` is NOT always a string.** `send_error` routes through `log_error`
        # with `("code %d, message %s", 404, ...)`, an int first, and an unguarded `in`
        # test raises inside the handler and closes the socket with no response.
        first = args[0] if args else ""
        if isinstance(first, str) and API_PREFIX + "/board" in first:
            print(f"  Served {first.split()[1] if ' ' in first else first}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the service command line, including opt-in external side effects."""
    ap = argparse.ArgumentParser(description="Serve a localswim board.")
    ap.add_argument("board", type=pathlib.Path, help="path to the board JSON")
    ap.add_argument("--port", type=int, default=None, help="override the port in the board file")
    ap.add_argument(
        "--autopush",
        action="store_true",
        help="commit and push quiet board changes to the board repository's remote",
    )
    return ap.parse_args(argv)


def main() -> None:
    global BOARD_PATH, STORE  # noqa: PLW0603 -- one process serves one board
    args = parse_args()
    _shutdown_requested.clear()
    BOARD_PATH = args.board.resolve()

    if problem := source_checkout_board_problem(BOARD_PATH):
        raise SystemExit(f"ERROR: {problem}")

    port = args.port or board_state.DEFAULT_PORT
    print(f"Serving {BOARD_PATH}")
    board: board_state.Board | None = None
    try:
        board = board_state.load(BOARD_PATH)
        STORE = BoardStore(BOARD_PATH)
        if args.port is None:
            port = board.port
        print(f"  project   : {board.project or '(unnamed)'}")
        for lane in board.lanes():
            if lane.items:
                print(f"      {lane.label:<20} {len(lane.items):>2}  [{lane.owner_label}]")
        drift = board.verify()
        if drift:
            print(f"  DRIFT: {len(drift)} item(s) disagree with their own history:")
            for problem in drift:
                print(f"      {problem}")
        _report_rule_gaps(board.policy)
    except (board_state.BoardError, OSError, json.JSONDecodeError) as exc:
        # **Loud, and it still serves.** The page renders the same message, so the
        # failure is visible in both places rather than as an empty board.
        print(f"  WARNING: {exc}")
        print("  The page will say so rather than look empty.")

    bad_edges = board.policy.check_edges() if board is not None else []
    if bad_edges:
        print(f"  PERMISSION TABLE INCONSISTENT, {len(bad_edges)} problem(s):")
        for problem in bad_edges:
            print(f"      {problem}")

    if not FONT_DIR.is_dir():
        # Inter is bundled. Without it the page silently falls back to Segoe UI and
        # looks almost right, which is the kind of failure nobody investigates.
        print(f"  WARNING: {FONT_DIR} is missing, so Inter will not load.")

    print(f"  view      : http://{HOST}:{port}/")
    print(f"  polling   : every {POLL_MS} ms, repaints only when the file changes")
    print(f"  {board_state.USER_LABEL[board_state.BROWSER_USER]} may drag:")
    for a, b in sorted(board_state.BROWSER_EDGES):
        print(f"      {a} -> {b}")
    # **A DAEMON thread, so Ctrl+C still stops the server.** A push in flight is a
    # `git` subprocess that finishes on its own; the worst case at shutdown is a commit
    # that lands without its push, and the next --autopush start reconciles it because
    # the loop begins armed.
    start_autopush(BOARD_PATH, enabled=args.autopush)

    server = BoardHttpServer((HOST, port), Handler)
    publish_service(BOARD_PATH, port)
    print(f"Listening on {HOST}:{port}. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        remove_service(BOARD_PATH)
    _reexec_if_requested()


if __name__ == "__main__":
    main()
