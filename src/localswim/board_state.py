"""The data model for a human/bot swimlane board, stored as ONE JSON file.

**RFC 2119 keywords, and the capitals are load-bearing.** MUST and MUST NOT are
absolute. SHOULD is a strong default a good argument may overrule. MAY is optional.

## Why JSON, and why dataclasses over the JSON

**Terry, 2026-08-18: *"'database' needs to be a JSON file, not md."*** He is applying
his own standing order -- JSON by default for structured data -- and the markdown table
this replaced had already started paying for the exception.

**A table makes every reader re-derive the record from text.** The parser it needed grew
`OPEN_RE`, `LANDED_RE`, a suspect-line detector for each, a renumbering pass and a
migration the day a column was added. **Every one of those existed only because the
storage had no types**, and it lost a signoff silently on its first real use.

**Then `TypedDict` was tried and dropped the same afternoon.** Every optional key makes
every access unsafe, so pyright objected at each use site and two of them were papered
over with `.get`. **A type that makes the checker cry wolf trains you to ignore it.**
Terry: *"how do you feel about Python dataclasses?"* -- and then *"yeah I found I loved
them too."* A field with a default simply exists.

## The three things a dataclass bought that the dict did not

  * **Defaults that apply.** `priority: str = DEFAULT_PRIORITY` needs no `.get`.
  * **Validation at one boundary.** `from_json` is the only place a malformed board can
    enter, so everything past it is known-good.
  * **Methods where the data is.** `Item.current_state()` recomputes state from history,
    which is what makes `verify()` possible at all.

## HOW THE PERMISSION MODEL IS ENFORCED, and why there is no state machine library

**`move()` checks `RULES` and raises.** That covers every caller that uses the API.

**`verify()` covers the ones that do not.** Python cannot stop `item.state = "completed"`,
and a guard that only protects the path you thought of protects nothing -- proven here
the same day, when the check lived in the server's POST handler and the library that
Claude uses was wide open. So `verify()` REPLAYS each item's history and refuses a board
whose stored state disagrees with its own audit trail. **A hand edit to the JSON is
caught by the same mechanism**, because the trail is the authority.

**`python-statemachine` 3.2.1 was surveyed and refused**, and the survey is recorded in
the README. It is genuinely good -- zero runtime dependencies, pushed the day before --
but the transition table here is already declarative data, `explain_refusal` writes
better errors than a generic guard, and the library cannot stop a direct attribute write
either. **It would have added a dependency and moved the rules, not enforced them.**
"""

import argparse
import contextlib
import copy
import datetime
import hashlib
import io
import itertools
import json
import os
import pathlib
import re
import socket
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Self, cast

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

type JsonValue = bool | int | float | str | list[JsonValue] | JsonObject | None
type JsonObject = dict[str, JsonValue]

# **`dataclasses.asdict` was considered and REFUSED**, which is why every class here
# hand-writes `to_json`. `asdict` walks nested dataclasses blindly: it would emit `frm`
# instead of `from`, and it would write an empty `comments` list onto every card.
# **The file is JSON so a person can read the diff**, and that is worth two dozen lines.

SCHEMA = 4
PREVIOUS_BOARD_SCHEMA = 2
EMBEDDED_POLICY_PREVIOUS_SCHEMA = 3
DESCRIPTION_SCHEMA = 1
PERMISSIONS_SCHEMA = 1
API_PREFIX = "/api/v001"

# **A SECOND, INDEPENDENT NUMBER, and splitting it was the first thing card #0064 had to
# do.** One `SCHEMA` used to gate both `board.json` and `rules.json`, so flattening the
# rules would have bumped the number every board is checked against and **rejected every
# board on disk** -- a data outage caused by a change to a different file.
#
# **Two files with two shapes get two version numbers.** They change for unrelated
# reasons and neither should be able to invalidate the other.
#
# **Schema 6 groups permitted edges under their actor.** The actor used to be repeated
# in every row; grouping makes one person's complete permission set readable in one
# place. Edge `note` also became the optional, clearer `description`.
RULES_SCHEMA = 6
REQUIRED_EDGE_ACTORS = 2


class BoardError(ValueError):
    """The board, or a request against it, is not something this version accepts.

    **MOVED ABOVE `_load_rules`'s call site by card #0072, and it was a latent bug.**
    The class used to be defined AFTER the module-level `_load_rules(RULES_PATH)` line.
    Python resolves the name at raise time rather than at def time, so a well-formed
    file never noticed -- and **every refusal path in the loader would have raised
    `NameError: name 'BoardError' is not defined` instead of the message it wrote.**

    **Found by bumping the schema**, which made the loader take one of those paths for
    the first time. The error handling had never been executed, which is the same shape
    as a check that cannot fail: it read as covered and was not.
    """


def _unique_json_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    """Build one JSON object while refusing duplicate keys instead of losing one."""
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise BoardError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _read_json(path: pathlib.Path) -> JsonValue:
    """Read JSON with useful syntax locations and no silently collapsed keys."""
    try:
        with path.open(encoding="utf-8") as fh:
            return cast("JsonValue", json.load(fh, object_pairs_hook=_unique_json_object))
    except json.JSONDecodeError as exc:
        raise BoardError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


# **The default port, and it is a DEFAULT rather than the port.** Terry, 2026-08-18:
# *"I want per-project config JSON that includes TCP port num; I want to be able to
# bookmark one board per project."*
#
# **A shared port is worse than a dead bookmark.** With every project on 8792, a
# bookmark opens whichever board happens to be running -- so the failure mode is
# reading the WRONG project's work and believing it. A port per project means the
# bookmark either shows your board or shows nothing, and nothing is honest.
DEFAULT_PORT = 8792

# The TCP port range, named so the validation reads as a rule rather than as two
# numbers somebody typed.
MIN_PORT, MAX_PORT = 1, 65535

# **`LANES`, `STATES`, `PRIORITIES`, `PRIORITY_LABEL` and `DEFAULT_PRIORITY` are all
# LOADED FROM `rules.json`**, further down, once `LaneRules` exists to hold them.
#
# **They used to be literals here and both copies briefly survived the move.** The
# loader ran after them so the right table won, and the file quietly lied to anyone
# reading the top of it -- a dead literal that looks authoritative is worse than no
# literal at all.
#
# **P0 is on fire. P5 is only if there is nothing else.** Terry's scale, verbatim:
# *"P0 is [...] on fire emergency and P5 is 'only if you have nothing else to work'."*
# Six levels rather than three, because he wanted room to rank a long backlog without
# every item collapsing to "medium". **The list itself is in `rules.json`.**

# **`Actor` WAS `Literal["terry", "claude"]` UNTIL CARD #0072**, and losing that is the
# price of being deployable by anybody else. Terry, 2026-08-19: *"This is an open source
# code repo and my buddy Scott is thinking about using it... The users and user class
# should be part of per-project cfg like rules JSON."*
#
# **A `Literal` cannot name values that are read from a file at run time**, so the type
# is now a plain `str` and `as_actor` is the only guard. **That is not a loss of safety,
# it is a relocation of it** -- and the docstring below records why the runtime check was
# always the thing doing the work.
Actor = str

HUMAN = "human"
BOT = "bot"
USER_CLASSES: tuple[str, ...] = (HUMAN, BOT)


@dataclass(frozen=True)
class User:
    """One person or program that may act on this board. **Card #0072.**

    **`user_class` is a real distinction rather than a label.** Card #0073 gates comment
    auto-linking on it: Terry types `#0028` when he means that card, and Claude types it
    while explaining something, so the feature has to tell a human from a bot. **It is
    not derivable from a name**, which is exactly why it is configured.

    **`color` is emitted as a CSS variable per user**, so a third person needs no
    stylesheet edit. The page painted `--terry` blue and `--claude` amber from hard-coded
    rules until this card.
    """

    id: str
    label: str
    user_class: str
    color: str


def as_actor(value: str) -> Actor:
    """Turn a caller-supplied string into a known actor, or refuse it. Card #0013's
    side finding.

    **THE ONE PLACE A STRING BECOMES AN ACTOR**, and it exists because pyright found
    the same defect from two directions at once on 2026-08-19.

    `api_endpoint.py`'s `/assign` route passed `str(body["owner"])` straight into `assign()`,
    whose parameter is annotated `Actor`. **The annotation was a lie at that call
    site**, and the browser could name any owner it liked. Meanwhile pyright reported
    `assign()`'s own `if owner not in ACTORS` guard as UNREACHABLE -- because the
    annotation promised the check could never fire.

    **Those are one bug**, and only one of them was ever load-bearing: the runtime guard
    was the thing keeping the route safe, and the type system had been told it was
    redundant. **Card #0072 removed the `Literal` and this check is unchanged**, so the
    protection that mattered is exactly where it was.

    **It returns the CONFIGURED id rather than the caller's string.** Case and
    surrounding whitespace are normalized away, so `" Terry "` and `terry` are one actor
    and the board never stores two spellings of one person.
    """
    name = value.strip().lower()
    for user in USERS:
        if user.id == name:
            return user.id
    raise BoardError(f"unknown actor {value!r}; want one of {', '.join(ACTORS)}")


# ---------------------------------------------------------------------------
# RELATIONSHIPS BETWEEN CARDS. Card #0028.
#
# **Terry: *"update data model for status board to be able to note ticket relationships
# 'related, parent, child, referenced by, etc.'"*** He asked for the set to come from a
# survey rather than from taste, and the survey changed the shape of the answer.
#
# **Jira, Linear and GitHub all SPLIT hierarchy from relations**, and none of them models
# a parent as one more link type. GitHub caps a sub-issue at one parent and an open
# request to allow several is still unfulfilled. Terry approved the split: *"recommendation
# accepted and approved for work."*
#
# **So `parent` is a FIELD on the card and everything here is a symmetric pair.** A tree
# needs "one parent" and "no cycles", and neither is expressible in a symmetric table.
#
# `clones` is deliberately absent. It exists in Jira because Jira has a Clone button, and
# a relationship naming a feature this board does not have would never be written.
LINK_INVERSE: dict[str, str] = {
    "blocks": "blocked_by",
    "blocked_by": "blocks",
    "duplicates": "duplicated_by",
    "duplicated_by": "duplicates",
    "references": "referenced_by",
    "referenced_by": "references",
    # **Its own inverse, and that is not a special case to remove.** "A relates to B"
    # and "B relates to A" are the same claim, which is why all three products ship one
    # symmetric `related` rather than a pair.
    "relates_to": "relates_to",
}

# **One direction of each pair is STORED and the other is DERIVED.** `--link 5 blocked_by
# 28` is normalized to `28 blocks 5` at the door, so the file only ever holds one spelling.
LINK_CANONICAL: tuple[str, ...] = ("blocks", "duplicates", "references", "relates_to")


@dataclass(frozen=True)
class Link:
    """One relationship, stored ONCE. Card #0028.

    **TERRY'S HARDEST REQUIREMENT DISSOLVES HERE RATHER THAN BEING ENFORCED.** His words:
    *"the two halves RFC-MUST be done atomically while holding file lock. Both halves get
    relationship or neither get it. Inconsistent relationships where only one of the two
    get updated MUST NOT be allowed to be possible."*

    **He described writing a copy onto each card**, which needs a lock, an atomic write,
    and an API shaped so that "add one half" cannot be expressed. All three are real work
    and all three can be got wrong.

    **A single row has no halves.** The other direction is computed by `LINK_INVERSE` when
    something reads it, so a one-sided link is not merely forbidden -- there is nowhere to
    put one. **That is the same lesson `rules.json` is being flattened for on card #0064**:
    the bug class disappears when the fact stops being stored twice.
    """

    frm: str
    kind: str
    to: str

    def to_json(self) -> dict[str, str]:
        return {"from": self.frm, "kind": self.kind, "to": self.to}

    @classmethod
    def from_json(cls, raw: JsonValue, where: str) -> Self:
        if not isinstance(raw, dict):
            raise BoardError(f"{where}: a link is {type(raw).__name__}, want object")
        missing = [key for key in ("from", "kind", "to") if not raw.get(key)]
        if missing:
            raise BoardError(f"{where}: link is missing {', '.join(missing)}")
        frm, kind, to = raw["from"], raw["kind"], raw["to"]
        if not all(isinstance(value, str) for value in (frm, kind, to)):
            raise BoardError(f"{where}: link endpoints and kind must be strings")
        frm, kind, to = str(frm), str(kind), str(to)
        if kind not in LINK_CANONICAL:
            raise BoardError(
                f"{where}: link kind {kind!r} is not stored form; "
                f"want one of {', '.join(LINK_CANONICAL)}"
            )
        return cls(frm=frm, kind=kind, to=to)


# **THREE STATES MEAN "NOT MOVING", AND TERRY DREW THE LINES HIMSELF.** They get
# confused constantly, and the whole value of the board is that a stalled card says WHO
# is holding it.
#
# | State | Who can move it | Terry, 2026-08-18 |
# |---|---|---|
# | `needs_terry_action` | **Terry** | *"that's 'need a judgement call'"* |
# | `blocked` | **Nobody** | *"neither of us can action it (eg 'awaiting license key')"* |
# | `ready_for_review` | **Terry** | Claude finished. Waiting on the signoff |
#
# **`blocked` MUST NOT be used for "waiting on a decision" and MUST NOT be used for
# "hard".** If Terry could unstick it by answering, it is `needs_terry_action`.


@dataclass(frozen=True)
class LaneRules:
    """Who may CREATE a card here, who may move one IN, and who may move one OUT.

    `inbound` and `outbound` map the OTHER lane to the actors allowed on that edge.
    **Naming the other lane is what an actor set alone could not do**: it is the
    difference between *"Terry may take cards out of Backlog"* and *"Terry may promote a
    Backlog card to Ready For Work, and nowhere else."*

    Each edge is declared once under its actor in `rules.json`. The loader derives both
    indexes, and `check_edges()` verifies that the in-memory views remain symmetrical.
    """

    create: frozenset[str]
    inbound: Mapping[str, frozenset[str]]
    outbound: Mapping[str, frozenset[str]]


# **`TERRY` and `CLAUDE` frozensets lived here and were DELETED by card #0072.**
# Nothing had read either one since edges moved out of Python -- they were left over
# from the version that spelled the permission model in Python. `NOBODY` is still used.
NOBODY: frozenset[str] = frozenset()

# **THE RULES LIVE IN `rules.json`, NOT HERE.** Terry, 2026-08-18: *"can we move
# rules outside code? I'd like that to be like a JSON so it acts more like a rules
# engine. I hate to recompile code when rules for rules engine change."*
#
# **The stronger argument is the one he gave second:** *"also get version history
# isolated to JUST perms changes."* Today's history proves it -- permission edits are
# tangled inside commits about heartbeat CSS and lane title sizes. Split out,
# `git log rules.json` is only ever the rules.
#
# **JSON has no comments, and the reasoning is the most valuable part of that table**,
# so every lane carries a `note` and every edge may carry a `description`. He offered
# `.jsonc` as the
# alternative and it is REFUSED: `jq` cannot read JSONC, and it fails dishonestly --
# `jq -e '.name' wrangler.jsonc` reports `Invalid numeric literal at line 6, column 4`
# where line 6 is the first `//` comment. **The error names the wrong cause**, and he
# specifically wants jq on these files.
#
# **The explanations are IN the data rather than in a companion document.** Two copies
# of one fact is the drift this project keeps paying for.
RULES_PATH = pathlib.Path(__file__).resolve().parent / "rules.json"


def _index_edges(  # noqa: PLR0912 -- every branch rejects one malformed input shape
    edges_raw: JsonObject,
    known: set[str],
    path: pathlib.Path,
) -> tuple[
    dict[str, dict[str, set[str]]],
    dict[str, dict[str, set[str]]],
    frozenset[str],
]:
    """Validate actor-grouped edges and index them by source and destination.

    **`inbound` and `outbound` are still BUILT, and that is why nothing else changed.**
    The file groups rows for readability; this index still answers both questions, so
    enforcement does not care how the JSON presents them.

    **Extracted from `_load_rules`**, which ruff correctly called too branchy once the
    edge validation landed in it -- the same call it made on `Board.from_json` an hour
    earlier, for the same reason.
    """
    inbound: dict[str, dict[str, set[str]]] = {lane: {} for lane in known}
    outbound: dict[str, dict[str, set[str]]] = {lane: {} for lane in known}
    seen: set[tuple[str, str, str]] = set()

    for actor, actor_edges in edges_raw.items():
        if not actor or actor != actor.strip():
            raise BoardError(
                f"{path}: edges actor ids must be nonempty and have no outer whitespace"
            )
        if not isinstance(actor_edges, list):
            raise BoardError(f"{path}: edges.{actor} is not a list")
        for index, edge in enumerate(actor_edges):
            spot = f"edges.{actor}[{index}]"
            if not isinstance(edge, dict):
                raise BoardError(f"{path}: {spot} is not an object")
            missing = [key for key in ("from", "to") if key not in edge]
            if missing:
                raise BoardError(f"{path}: {spot} is missing {', '.join(missing)}")
            unexpected = sorted(set(edge) - {"from", "to", "description"})
            if unexpected:
                raise BoardError(f"{path}: {spot} has unknown field(s) {', '.join(unexpected)}")
            if "description" in edge and not isinstance(edge["description"], str):
                raise BoardError(f"{path}: {spot}.description is not a string")

            for required in ("from", "to"):
                value = edge[required]
                if not isinstance(value, str) or not value.strip():
                    raise BoardError(f"{path}: {spot}.{required} is not a nonempty string")

            frm, to = str(edge["from"]), str(edge["to"])
            for end in (frm, to):
                if end not in known:
                    raise BoardError(f"{path}: {spot} names unknown lane {end!r}")
            if frm == to:
                raise BoardError(f"{path}: {spot} joins {frm!r} to itself")
            if (actor, frm, to) in seen:
                raise BoardError(f"{path}: {spot} repeats {actor} on {frm} -> {to}")
            seen.add((actor, frm, to))
            outbound[frm].setdefault(to, set()).add(actor)
            inbound[to].setdefault(frm, set()).add(actor)
    return inbound, outbound, frozenset(edges_raw)


# **THE PERMISSION TABLE. Terry dictated it lane by lane on 2026-08-18**, in the shape
# he asked for: *"I like that the perms are (in/out, actor, source/dest)."* **The FILE was
# grouped by actor in schema 6** -- each person's complete permission set is together,
# while the source/destination indexes remain derived in memory.
@dataclass(frozen=True)
class Rules:
    """Everything `rules.json` declares, in one object. **Card #0072.**

    **It replaced a five-wide return tuple**, which users, `browserUser`, `cliUser` and
    `defaultOwner` would have taken to nine. A tuple that long is positional trivia at
    every call site, and `reload_rules` unpacks it twice.
    """

    lanes: tuple[tuple[str, str], ...]
    table: Mapping[str, LaneRules]
    priorities: tuple[str, ...]
    priority_label: Mapping[str, str]
    default_priority: str
    edge_actors: frozenset[str]
    document: JsonObject
    # **The cast is NOT here, card #0083.** It moved to the board file, which is
    # per-project; this object describes `rules.json`, which is per-tool.


def _parse_users(doc: JsonObject, path: pathlib.Path) -> tuple[User, ...]:
    """Read and validate the `users` block. **Card #0072.**

    **Every field is mandatory and none is defaulted.** A user with no class would make
    card #0073's auto-link gate silently pick a side, and a user with no color would
    paint as whatever the browser inherits -- both are the reassuring-but-wrong shape
    this repository keeps refusing.
    """
    raw = doc.get("users")
    if not isinstance(raw, list) or not raw:
        raise BoardError(f"{path}: 'users' is missing or empty")

    users: list[User] = []
    for index, entry in enumerate(raw):
        spot = f"users[{index}]"
        if not isinstance(entry, dict):
            raise BoardError(f"{path}: {spot} is not an object")
        missing = [k for k in ("id", "label", "class", "color") if not entry.get(k)]
        if missing:
            raise BoardError(f"{path}: {spot} is missing {', '.join(missing)}")
        if entry["class"] not in USER_CLASSES:
            raise BoardError(
                f"{path}: {spot} class {entry['class']!r}; want one of {', '.join(USER_CLASSES)}"
            )
        # **Ids are the key everything else joins on**, so a duplicate would make one
        # user's permissions silently shadow the other's.
        if any(u.id == entry["id"] for u in users):
            raise BoardError(f"{path}: {spot} repeats id {entry['id']!r}")
        users.append(
            User(
                id=str(entry["id"]),
                label=str(entry["label"]),
                user_class=str(entry["class"]),
                color=str(entry["color"]),
            )
        )
    return tuple(users)


def _pick_role(
    doc: JsonObject,
    key: str,
    users: tuple[User, ...],
    want: str,
    path: pathlib.Path,
) -> str:
    """Resolve `browserUser` or `cliUser`: configured if named, derived if unambiguous.

    **It REFUSES rather than guesses when there is a choice.** Terry's open question on
    card #0072 is exactly this: *"api_endpoint.py posts browser comments as terry because
    loopback proves it is him. With two humans configured, that assumption breaks. Who is
    the browser?"*

    **So a single human needs no configuration and two humans MUST be told.** That
    answers the question without building a login nobody asked for, and it fails loudly
    at startup rather than attributing one person's comment to another.
    """
    named = doc.get(key)
    if named:
        if not any(u.id == named for u in users):
            raise BoardError(f"{path}: {key} names unknown user {named!r}")
        return str(named)

    candidates = [u.id for u in users if u.user_class == want]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise BoardError(f"{path}: no user has class {want!r}, so {key} cannot be derived")
    raise BoardError(
        f"{path}: {len(candidates)} users have class {want!r} ({', '.join(candidates)}), "
        f"so {key} MUST be set explicitly -- this board will not guess who is acting"
    )


def _parse_lanes(
    doc: JsonObject, path: pathlib.Path
) -> tuple[list[JsonObject], tuple[tuple[str, str], ...], set[str]]:
    """Validate lane rows and return their typed records, display order, and ids."""
    lanes_raw = doc.get("lanes")
    if not isinstance(lanes_raw, list) or not lanes_raw:
        raise BoardError(f"{path}: 'lanes' is missing or empty")

    lanes: list[JsonObject] = []
    order: list[tuple[str, str]] = []
    labels: set[str] = set()
    for index, lane in enumerate(lanes_raw):
        spot = f"lanes[{index}]"
        if not isinstance(lane, dict):
            raise BoardError(f"{path}: {spot} is not an object")
        lane_id, label, create = lane.get("id"), lane.get("label"), lane.get("create")
        if not isinstance(lane_id, str) or not lane_id:
            raise BoardError(f"{path}: {spot}.id is not a nonempty string")
        if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", lane_id) is None:
            raise BoardError(f"{path}: {spot}.id {lane_id!r} is not a canonical lane slug")
        if not isinstance(label, str) or not label:
            raise BoardError(f"{path}: {spot}.label is not a nonempty string")
        folded_label = label.casefold()
        if folded_label in labels:
            raise BoardError(f"{path}: lane labels are not unique: {label!r}")
        labels.add(folded_label)
        if not isinstance(create, list) or not all(isinstance(actor, str) for actor in create):
            raise BoardError(f"{path}: {spot}.create is not a list of actor ids")
        lanes.append(lane)
        order.append((lane_id, label))

    known = {lane_id for lane_id, _label in order}
    if len(known) != len(lanes):
        raise BoardError(f"{path}: lane ids are not unique")
    return lanes, tuple(order), known


def _parse_priorities(
    doc: JsonObject, path: pathlib.Path
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Validate priority rows and return their order and display labels."""
    priorities_raw = doc.get("priorities")
    if not isinstance(priorities_raw, list) or not priorities_raw:
        raise BoardError(f"{path}: 'priorities' is missing or empty")
    rows: list[tuple[str, str]] = []
    for index, priority in enumerate(priorities_raw):
        spot = f"priorities[{index}]"
        if not isinstance(priority, dict):
            raise BoardError(f"{path}: {spot} is not an object")
        priority_id, label = priority.get("id"), priority.get("label")
        if not isinstance(priority_id, str) or not priority_id:
            raise BoardError(f"{path}: {spot}.id is not a nonempty string")
        if not isinstance(label, str) or not label:
            raise BoardError(f"{path}: {spot}.label is not a nonempty string")
        rows.append((priority_id, label))
    return tuple(priority_id for priority_id, _label in rows), dict(rows)


def _parse_rules(doc: JsonObject, path: pathlib.Path) -> Rules:
    """Validate one rules document into the shapes the rest of this module uses.

    **It REFUSES rather than repairs**, exactly like `Board.from_json`. A rules file
    naming an unknown actor or a lane that does not exist is a bug in whoever edited
    it, and defaulting past that would hide the edit that broke the board.

    **Lane `note` and edge `description` fields do not grant permission.** They exist
    for the person reading the diff; only actor, source, and destination affect the
    transition index.
    """
    if doc.get("schema") != RULES_SCHEMA:
        raise BoardError(
            f"{path}: rules schema {doc.get('schema')!r}, want {RULES_SCHEMA}. "
            "schema 6 groups edges under actor ids and renames edge note to the "
            "optional description field."
        )

    # **ACTORS ARE NOT VALIDATED HERE ANY MORE. Card #0083.** The user list moved to
    # the board file, which is not loaded yet -- the board path is a command-line
    # argument. `install_users` does the check the moment both halves exist.
    lanes, order, known = _parse_lanes(doc, path)

    edges_raw = doc.get("edges")
    if not isinstance(edges_raw, dict) or not edges_raw:
        raise BoardError(f"{path}: 'edges' is missing, empty, or not an object")
    if len(edges_raw) != REQUIRED_EDGE_ACTORS:
        raise BoardError(
            f"{path}: 'edges' contains {len(edges_raw)} actors; "
            f"exactly {REQUIRED_EDGE_ACTORS} distinct actors are required"
        )

    inbound, outbound, edge_actors = _index_edges(edges_raw, known, path)

    table: dict[str, LaneRules] = {}
    for lane in lanes:
        lane_id = str(lane["id"])
        create = cast("list[JsonValue]", lane["create"])
        table[lane_id] = LaneRules(
            create=frozenset(actor for actor in create if isinstance(actor, str)),
            inbound={src: frozenset(who) for src, who in inbound[lane_id].items()},
            outbound={dst: frozenset(who) for dst, who in outbound[lane_id].items()},
        )

    priorities, labels = _parse_priorities(doc, path)
    default = doc.get("defaultPriority", priorities[len(priorities) // 2])
    if not isinstance(default, str) or default not in priorities:
        raise BoardError(f"{path}: defaultPriority {default!r} is not in the list")

    return Rules(
        lanes=order,
        table=table,
        priorities=priorities,
        priority_label=labels,
        default_priority=default,
        edge_actors=edge_actors,
        document=copy.deepcopy(doc),
    )


def _load_rules(path: pathlib.Path) -> Rules:
    """Read and validate one standalone rules JSON file."""
    raw_doc = _read_json(path)
    if not isinstance(raw_doc, dict):
        raise BoardError(f"{path}: rules document is not an object")
    return _parse_rules(raw_doc, path)


@dataclass(frozen=True)
class TransitionPolicy:
    """Immutable, reloadable view of the board transition configuration."""

    rules: Rules
    path: pathlib.Path
    observed_mtime_ns: int

    def __post_init__(self) -> None:
        table = MappingProxyType(
            {
                state: LaneRules(
                    rules.create,
                    MappingProxyType(dict(rules.inbound)),
                    MappingProxyType(dict(rules.outbound)),
                )
                for state, rules in self.rules.table.items()
            }
        )
        frozen = Rules(
            self.rules.lanes,
            table,
            self.rules.priorities,
            MappingProxyType(dict(self.rules.priority_label)),
            self.rules.default_priority,
            self.rules.edge_actors,
            copy.deepcopy(self.rules.document),
        )
        object.__setattr__(self, "rules", frozen)

    def __deepcopy__(self, _memo: dict[int, object]) -> Self:
        return self

    @classmethod
    def load(cls, path: pathlib.Path = RULES_PATH) -> Self:
        path = pathlib.Path(path)
        rules = _load_rules(path)
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = 0
        policy = cls(rules, path, stamp)
        if problems := policy.check_edges():
            raise BoardError("; ".join(problems))
        return policy

    @classmethod
    def from_json(cls, document: JsonObject, where: str) -> Self:
        """Build a policy from the resolved document embedded in a board snapshot."""
        path = pathlib.Path(where)
        policy = cls(_parse_rules(document, path), path, 0)
        if problems := policy.check_edges():
            raise BoardError("; ".join(problems))
        return policy

    def to_json(self) -> JsonObject:
        """Return an isolated, serializer-ready copy of the resolved policy."""
        return copy.deepcopy(self.rules.document)

    @property
    def lanes(self) -> tuple[tuple[str, str], ...]:
        return self.rules.lanes

    @property
    def table(self) -> Mapping[str, LaneRules]:
        return self.rules.table

    @property
    def states(self) -> tuple[str, ...]:
        return tuple(state for state, _ in self.lanes)

    @property
    def lane_label(self) -> dict[str, str]:
        return dict(self.lanes)

    @property
    def priorities(self) -> tuple[str, ...]:
        return self.rules.priorities

    @property
    def priority_label(self) -> Mapping[str, str]:
        return self.rules.priority_label

    @property
    def default_priority(self) -> str:
        return self.rules.default_priority

    def may_move(self, actor: str, from_state: str, to_state: str) -> bool:
        rules = self.table.get(from_state)
        return rules is not None and actor in rules.outbound.get(to_state, NOBODY)

    def may_create(self, actor: str, state: str) -> bool:
        rules = self.table.get(state)
        return rules is not None and actor in rules.create

    def explain_refusal(self, actor: str, from_state: str, to_state: str) -> str:
        rules = self.table.get(from_state)
        if rules is None:
            return f"{from_state} is not a lane"
        if to_state not in self.table:
            return f"{to_state} is not a lane"
        if self.may_move(actor, from_state, to_state):
            return ""
        allowed_here = sorted(dst for dst, actors in rules.outbound.items() if actor in actors)
        if allowed_here:
            return (
                f"{actor.capitalize()} has out permission on {from_state}, but not "
                f"to {to_state}. From here {actor} may go to: " + ", ".join(allowed_here)
            )
        others = sorted({a for actors in rules.outbound.values() for a in actors})
        if others:
            return (
                f"{from_state} is not {actor}'s to move out of. "
                f"That belongs to: {', '.join(others)}"
            )
        return f"Nothing moves out of {from_state}"

    def edges_for(self, actor: str) -> frozenset[tuple[str, str]]:
        return frozenset(
            (a, b)
            for a in self.states
            for b in self.states
            if a != b and self.may_move(actor, a, b)
        )

    def actors_in(self, state: str) -> frozenset[str]:
        rules = self.table.get(state)
        if rules is None or not rules.inbound:
            return NOBODY
        return frozenset[str]().union(*rules.inbound.values())

    def actors_out(self, state: str) -> frozenset[str]:
        rules = self.table.get(state)
        if rules is None or not rules.outbound:
            return NOBODY
        return frozenset[str]().union(*rules.outbound.values())

    def check_edges(self) -> list[str]:
        problems: list[str] = []
        for state, rules in self.table.items():
            for src, actors in rules.inbound.items():
                other = self.table.get(src)
                if other is None:
                    problems.append(f"{state}.inbound names unknown lane {src!r}")
                    continue
                mirror = other.outbound.get(state)
                if mirror is None:
                    problems.append(f"{src} -> {state} was indexed inbound but not outbound")
                elif mirror != actors:
                    problems.append(
                        f"{src} -> {state}: inbound says {sorted(actors)}, "
                        f"outbound says {sorted(mirror)}"
                    )
            for dst, actors in rules.outbound.items():
                other = self.table.get(dst)
                if other is None:
                    problems.append(f"{state}.outbound names unknown lane {dst!r}")
                elif state not in other.inbound:
                    problems.append(
                        f"{state} -> {dst} was indexed outbound but not inbound "
                        f"for {sorted(actors)}"
                    )
        return problems

    def validate_actors(
        self,
        actors: set[str],
        where: str,
        configured_edge_actors: frozenset[str] | None = None,
    ) -> None:
        named = set(self.rules.edge_actors)
        for rules in self.table.values():
            named |= set(rules.create)
            for who in (*rules.inbound.values(), *rules.outbound.values()):
                named |= set(who)
        if unknown := sorted(named - actors):
            raise BoardError(
                f"{self.path} names actor(s) " + ", ".join(unknown) + f" that {where} "
                "does not list as users; the board user list is authoritative"
            )
        if configured_edge_actors is not None and self.rules.edge_actors != configured_edge_actors:
            expected = ", ".join(sorted(configured_edge_actors))
            actual = ", ".join(sorted(self.rules.edge_actors))
            raise BoardError(
                f"{self.path} edges actors ({actual}) do not exactly match "
                f"{where} browserUser and cliUser ({expected})"
            )

    def reload_if_changed(
        self,
        actors: set[str] | None = None,
        configured_edge_actors: frozenset[str] | None = None,
    ) -> tuple[Self, str | None]:
        """Return a fresh policy after a complete valid edit, otherwise keep this one."""
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            return self, None
        if stamp == self.observed_mtime_ns:
            return self, None
        try:
            fresh = type(self).load(self.path)
            if actors is not None:
                fresh.validate_actors(actors, str(self.path), configured_edge_actors)
        except (BoardError, OSError, ValueError) as exc:
            seen = type(self)(self.rules, self.path, stamp)
            return seen, (f"rules.json changed and was REFUSED: {exc}. Keeping the loaded table.")
        return fresh, f"rules.json reloaded: {len(fresh.states)} lanes"


_POLICY = TransitionPolicy.load(RULES_PATH)
_RULES = _POLICY.rules

LANES = _RULES.lanes
RULES = _RULES.table
PRIORITIES = _RULES.priorities
PRIORITY_LABEL = _RULES.priority_label
DEFAULT_PRIORITY = _RULES.default_priority


def _board_policy(
    raw: JsonObject, where: str, supplied: TransitionPolicy | None
) -> TransitionPolicy:
    """Select an explicit test/migration policy or validate the embedded document."""
    if supplied is not None:
        return supplied
    policy_raw = raw.get("policy")
    if not isinstance(policy_raw, dict):
        raise BoardError(f"{where}: 'policy' is missing or not an object")
    return TransitionPolicy.from_json(policy_raw, f"{where}: policy")


# **THE CONFIGURED CAST. Cards #0072 and #0083.**
#
# **EMPTY UNTIL A BOARD LOADS, and that is the whole change #0083 made.** The users
# moved out of `rules.json` -- which lives next to the CODE -- and into the board file,
# which is per-project. So they cannot be known at import: the board path is a
# command-line argument.
#
# **`Board.from_json` calls `install_users`**, and every entry point loads a board
# before it needs a name.
USERS: tuple[User, ...] = ()
ACTORS: tuple[str, ...] = ()
USER_LABEL: dict[str, str] = {}
USER_CLASS: dict[str, str] = {}
USER_COLOR: dict[str, str] = {}

#: Who `api_endpoint.py` writes as. Loopback proves the request came from this machine.
BROWSER_USER: str = ""
#: Who `board_state.py` writes as. **There is no flag to say otherwise** -- see `main`.
CLI_USER: str = ""
#: Who a new card lands on when nobody says.
DEFAULT_OWNER: str = ""


def install_users(  # noqa: PLR0913, PLR0917 -- one complete user configuration
    users: tuple[User, ...],
    browser: str,
    cli: str,
    default_owner: str,
    where: str,
    policy: TransitionPolicy | None = None,
) -> None:
    """Bind the cast, and check `rules.json` only names people this board knows.

    **Card #0083. This is where the two config files meet.** Lanes and edges are the
    TOOL's behavior and stay beside the code; the people are the DEPLOYMENT's and live
    with the data. **So the actor validation that `_load_rules` used to do moved here**,
    which is the first moment both halves exist.

    **A rules file naming somebody the board has never heard of is REFUSED**, not
    ignored. That edge would silently grant nothing to nobody, and a permission that
    quietly does not exist is worse than one that fails.
    """
    global USERS, ACTORS, USER_LABEL, USER_CLASS, USER_COLOR  # noqa: PLW0603
    global BROWSER_USER, CLI_USER, DEFAULT_OWNER, BROWSER_EDGES  # noqa: PLW0603

    configured_edge_actors = frozenset((browser, cli))
    if len(configured_edge_actors) != REQUIRED_EDGE_ACTORS:
        raise BoardError(f"{where}: browserUser and cliUser MUST be two different actor ids")

    active = policy or _POLICY
    active.validate_actors({u.id for u in users}, where, configured_edge_actors)

    USERS = users
    ACTORS = tuple(u.id for u in users)
    USER_LABEL = {u.id: u.label for u in users}
    USER_CLASS = {u.id: u.user_class for u in users}
    USER_COLOR = {u.id: u.color for u in users}
    BROWSER_USER, CLI_USER, DEFAULT_OWNER = browser, cli, default_owner
    BROWSER_EDGES = edges_for(browser)


def is_human(actor: str) -> bool:
    """Whether this actor is a person. **Card #0073's gate, and #0072's reason for
    carrying a class at all.**

    **An unknown actor is NOT a human.** The one caller is comment auto-linking, and
    the safe direction there is to do nothing rather than to invent relationships.
    """
    return USER_CLASS.get(actor) == HUMAN


STATES: tuple[str, ...] = tuple(state for state, _ in LANES)
LANE_LABEL: dict[str, str] = dict(LANES)


def _rules_gap_document(path: pathlib.Path, document: JsonObject | None) -> JsonValue:
    """Return an explicit embedded policy or best-effort standalone rules document."""
    if document is not None:
        return document
    try:
        return _read_json(path)
    except BoardError, OSError:
        return None


def rules_gaps(
    path: pathlib.Path = RULES_PATH, document: JsonObject | None = None
) -> tuple[list[str], list[str]]:
    """`(edges with no description, edges sharing one across actors)`.

    **Terry: *"Want the rules to be VERY pedantic to ENCOURAGE humans to comment them.
    'Tell me why actor X should be able to make this card movement'."***

    **A missing description MUST NOT fail the load, and that is deliberate.** Refusing to
    start over an unwritten sentence would mean Claude filling 17 of them with
    plausible filler to unblock itself -- which is worse than a blank, because filler
    reads as considered.

    **So it is counted and shown instead.** `api_endpoint.py` prints it at startup, where Terry
    already reads the permission table.

    **A SHARED description is reported separately from a missing one.** They are different
    states: nobody has explained this edge at all, against somebody explained the edge
    and not the two actors on it.
    """
    doc = _rules_gap_document(path, document)
    if not isinstance(doc, dict):
        return [], []
    edges = doc.get("edges")
    if not isinstance(edges, dict):
        return [], []
    rows: list[tuple[str, str, str, str]] = []
    for actor, actor_edges in edges.items():
        if not isinstance(actor_edges, list):
            continue
        for edge in actor_edges:
            if not isinstance(edge, dict):
                continue
            frm, to, description = (
                edge.get("from"),
                edge.get("to"),
                edge.get("description", ""),
            )
            if isinstance(frm, str) and isinstance(to, str) and isinstance(description, str):
                rows.append((actor, frm, to, description.strip()))
    blank = [f"{actor}: {frm} -> {to}" for actor, frm, to, description in rows if not description]
    by_edge: dict[tuple[str, str], set[str]] = {}
    for _actor, frm, to, description in rows:
        if description:
            by_edge.setdefault((frm, to), set()).add(description)
    counts: dict[tuple[str, str], int] = {}
    for _actor, frm, to, description in rows:
        if description:
            key = (frm, to)
            counts[key] = counts.get(key, 0) + 1
    shared = [
        f"{frm} -> {to}"
        for (frm, to), n in counts.items()
        if n > 1 and len(by_edge[(frm, to)]) == 1
    ]
    return blank, sorted(shared)


def check_edges() -> list[str]:
    """Every inconsistency in the derived permission index. Empty means consistent.

    **THIS FUNCTION LOST ITS ORIGINAL JOB ON 2026-08-19, and that is the win.** Card
    #0064. It was 32 lines that compared the two stored copies of every edge --
    `laneA.out.laneB` against `laneB.in.laneA` -- because `rules.json` held each fact
    twice and the two could disagree.

    **`rules.json` now stores each edge once**, so there is no second copy to contradict
    the first. The comparison it used to make cannot fail.

    **It is kept rather than deleted, with a narrower job**, because `inbound` and
    `outbound` are still BUILT as two dictionaries in `_load_rules`, and a future edit
    there could still fill one and not the other. **The check moved from guarding the
    FILE to guarding the derivation.**

    **Callers MUST surface a non-empty result.** A table that contradicts itself behaves
    as whichever half a given code path reads, and the two halves are read by different
    code.
    """
    return _POLICY.check_edges()


def may_move(actor: str, from_state: str, to_state: str) -> bool:
    """Whether `actor` may move a card along this one edge.

    **One lookup, because the edge names both ends.** An earlier model asked two
    separate questions -- may this actor leave that lane, may this actor enter this one
    -- and answered yes to combinations nobody intended, `backlog -> completed` among
    them.

    **THIS FUNCTION IS NOT THE WHOLE RULE FOR ONE EDGE, AND THAT IS DELIBERATE.**
    `backlog -> ready_for_work` carries the `bot` actor, so this returns True for it.
    **Automation MUST NOT use it without Terry's explicit per-ticket instruction.** Terry,
    2026-08-19: *"claude MUST NOT move out of backlog until/unless Terry gives explicit
    guidance for one specific ticket."*

    **The table cannot express that constraint** -- the grant is PER TICKET and verbal,
    and a permission model grants an edge or it does not. The edge exists so Terry can
    say *"promote #0027"* and it happens without him reaching for the mouse.

    **So a caller that trusts this function alone will get that one edge wrong.** The
    restraint lives in that edge's `description`, in FlickrGroupAddr's
    `CLAUDE.md`, and in its `docs/ORIENTATION.md`. **Every other edge here IS the whole
    rule.**
    """
    return _POLICY.may_move(actor, from_state, to_state)


def may_create(actor: str, state: str) -> bool:
    """Whether `actor` may create a NEW card in this lane."""
    return _POLICY.may_create(actor, state)


def explain_refusal(actor: str, from_state: str, to_state: str) -> str:
    """Why this move is refused, naming the ACTUAL cause. Empty when it is allowed.

    **Terry wrote the target wording himself:** *"Terry does have out perms on ready for
    review but not where you tried to drop that card."*

    **A single "not allowed" flattens three situations**, and each needs a different
    next action: the lane is not yours at all, it is yours but not to that destination,
    or nobody may take that edge. **The middle one is the interesting case and the one a
    generic message hides.**
    """
    return _POLICY.explain_refusal(actor, from_state, to_state)


def edges_for(actor: str) -> frozenset[tuple[str, str]]:
    """Every move `actor` may make, derived from `RULES` rather than listed."""
    return _POLICY.edges_for(actor)


# **Card #0072: named for the ROLE, not for the person.** `TERRY_EDGES` was what the
# page asks to decide whether a card is draggable, and the answer is "the edges the
# browser's user may take" -- which stops being Terry's the day somebody else deploys
# this. **`CLAUDE_EDGES` was DELETED rather than renamed**: nothing read it, in either
# file, since the permission table moved into JSON.
BROWSER_EDGES: frozenset[tuple[str, str]] = frozenset()

# **The mtime the table above was built from.** `_rules_mtime` is what makes a live
# reload possible without asking the filesystem to re-read an unchanged file.
_rules_mtime: float = RULES_PATH.stat().st_mtime if RULES_PATH.exists() else 0.0


def reload_rules_if_changed() -> str | None:
    """Re-read `rules.json` when it has changed on disk. Returns a message, or None.

    **Terry's standing order, 2026-08-19: a tool that can DETECT its own staleness
    MUST resolve it where it can, and alert only where it cannot.** `rules.json` is
    data, so this end of the problem is resolvable and gets resolved silently.

    **It bit twice in one afternoon before this existed.** The rules gained a
    `claude` actor and the board went on showing the old lane owners; Terry noticed
    before any instrument did, and his first guess was that the rule had never been
    written. **A server holding a table it loaded at import cannot see that it is
    wrong.**

    **EVERY DERIVED GLOBAL IS REBOUND TOGETHER, and that is the whole difficulty.**
    Seven names come out of `_load_rules` or are computed from it, and a table that
    is half-new contradicts itself -- which is exactly what `check_edges()` exists to
    catch, arriving from a new direction.

    **A BAD FILE KEEPS THE OLD TABLE.** A `rules.json` saved mid-edit is a real
    state, and half a permission model is worse than a stale one. So the new table is
    built and validated COMPLETELY before anything is rebound; on any failure this
    returns a message and changes nothing.

    **`_rules_mtime` advances even on a rejected file.** Otherwise a broken save
    would be re-read, re-parsed and re-rejected on every single poll -- twice a
    second, forever -- and the log would be the only thing that noticed.
    """
    # **Nine globals rebound, and `global` is correct here rather than a smell.** The
    # rest of this module reads these names directly, and every caller reaches them as
    # `board_state.RULES`, so **rebinding the module attribute IS the delivery mechanism.**
    #
    # **PLW0603 is suppressed for one function, deliberately.** The lint is right in
    # general: global rebinding is hard to reason about. The alternative here is a
    # mutable container -- `_state.rules` -- which means touching every reference in
    # two files to satisfy a rule about a single function that exists precisely to
    # rebind them. **The blast radius of the fix exceeds the blast radius of the
    # finding**, and the function is short, documented, and the only writer.
    # **The first line carries no `noqa` and the second does, which looks wrong and is
    # not.** PLW0603 does not fire on names bound only by TUPLE UNPACKING, and every
    # name on the first line is. Adding a matching directive there is an unused `noqa`,
    # which `RUF100` then reports -- so the asymmetry is ruff's, not a slip.
    global LANES, RULES, PRIORITIES, PRIORITY_LABEL, DEFAULT_PRIORITY  # noqa: PLW0603
    global STATES, LANE_LABEL, BROWSER_EDGES, _rules_mtime  # noqa: PLW0603
    global _POLICY, _RULES  # noqa: PLW0603
    fresh_policy, message = _POLICY.reload_if_changed(
        set(ACTORS) if ACTORS else None,
        frozenset((BROWSER_USER, CLI_USER)) if ACTORS else None,
    )
    if fresh_policy is not _POLICY:
        _POLICY = fresh_policy
        _RULES = fresh_policy.rules
        LANES, RULES = fresh_policy.lanes, fresh_policy.table
        PRIORITIES = fresh_policy.priorities
        PRIORITY_LABEL = fresh_policy.priority_label
        DEFAULT_PRIORITY = fresh_policy.default_priority
        STATES = fresh_policy.states
        LANE_LABEL = fresh_policy.lane_label
        BROWSER_EDGES = fresh_policy.edges_for(BROWSER_USER)
        _rules_mtime = fresh_policy.observed_mtime_ns / 1_000_000_000
    if message and message.startswith("rules.json reloaded"):
        return message + f", {len(BROWSER_EDGES)} draggable edges"
    return message


def actors_in(state: str) -> frozenset[str]:
    """Every actor who may move a card INTO this lane, from anywhere.

    **A summary for display, never a permission check.** `may_move` asks about one
    edge; this collapses all of them.
    """
    return _POLICY.actors_in(state)


def actors_out(state: str) -> frozenset[str]:
    """Every actor who may move a card OUT of this lane, to anywhere."""
    return _POLICY.actors_out(state)


def lane_class(
    state: str,
    policy: TransitionPolicy | None = None,
    browser_user: str | None = None,
) -> str:
    """A coarse class for styling: a USER ID, `handoff`, `done` or `shared`.

    **Derived from the permission table, never stored.** A lane whose `in` and `out`
    name different actors IS a handoff -- that is the definition rather than a list, so
    both handoff lanes qualify without either being special-cased.

    **It returned the literal `terry` or `claude` until card #0072**, which meant the
    stylesheet needed one hard-coded rule per person. It now returns the id of the SOLE
    actor when there is one, and `api_endpoint.py` emits a CSS variable per configured user --
    so a third person gets their color with no stylesheet edit.

    **A lane SEVERAL actors share falls back to the browser's own user, and that is
    deliberately the OLD behavior rather than a better one.** The previous line read
    `return "claude" if into == {"claude"} else "terry"`, so a lane both of them could
    work already painted in Terry's blue -- and since he gained actors on eight edges on
    2026-08-19, that is most of this board.

    **Returning a new `shared` class here was written first and then withdrawn.** It is
    arguably more honest, and it would have recolored five lanes on a board Terry was
    looking at, for a card about configuration. **A refactor that changes what the screen
    looks like is not a refactor.** If a distinct shared color is wanted, that is a
    one-line change and its own card.
    """
    active = policy or _POLICY
    into, out = active.actors_in(state), active.actors_out(state)
    if not out:
        return "done"
    if into != out:
        return "handoff"
    if len(into) == 1:
        return next(iter(into))
    return browser_user if browser_user is not None else BROWSER_USER


def lane_owner_label(
    state: str,
    policy: TransitionPolicy | None = None,
) -> str:
    """`IN: x · OUT: y`, which is what a lane header shows.

    **Two halves rather than one word.** Terry asked for *"real clear ownership per
    lane"*, and a single label is exactly what fails on a boundary lane -- calling
    `ready_for_review` "Claude's" or "Terry's" is wrong either way.
    """
    active = policy or _POLICY

    def actors_in(lane: str) -> frozenset[str]:
        return active.actors_in(lane)

    def actors_out(lane: str) -> frozenset[str]:
        return active.actors_out(lane)

    def who(names: frozenset[str]) -> str:
        return " + ".join(n.capitalize() for n in sorted(names)) if names else "nobody"

    return f"IN: {who(actors_in(state))}  ·  OUT: {who(actors_out(state))}"


def now() -> str:
    """An ISO 8601 stamp in this machine's local zone, offset included.

    **Local rather than UTC, and the offset is what makes that safe.** Terry reads
    these; a UTC stamp would make him do arithmetic to answer *"did I sign that off
    before dinner"*. The offset keeps it unambiguous for anything that parses it.
    """
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


#: Safe fallback for an absent or unreadable event timestamp. Aware rather than naive,
#: because a naive value cannot be compared with real stamps carrying an offset.
_BEGINNING_OF_TIME = datetime.datetime.min.replace(tzinfo=datetime.UTC)


def item_order_key(
    item: Item,
    policy: TransitionPolicy,
) -> tuple[int, int]:
    """Return the one deterministic card order used by lanes and focused queries.

    The order is policy priority, then ticket number. Tickets are allocated
    monotonically, so their numeric order is creation order without parsing mutable or
    legacy audit timestamps. Keeping this comparator shared prevents a CLI triage query
    from disagreeing with the same cards on the board.
    """
    rank = (
        policy.priorities.index(item.priority)
        if item.priority in policy.priorities
        else len(policy.priorities)
    )
    return (rank, item.ticket)


def parse_stamp(raw: str | None) -> datetime.datetime:
    """One history `at` string as an aware datetime, or `_BEGINNING_OF_TIME`.

    **It NEVER raises.** A malformed stamp returns the fallback, because the board is
    rendered every 400 ms and one bad string MUST NOT take a lane off the screen.
    """
    if raw is None:
        return _BEGINNING_OF_TIME

    text = raw.strip()
    parsed: datetime.datetime | None = None

    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        # **A trailing zone WORD, as on #0012.** `fromisoformat` handles a numeric
        # offset and refuses an abbreviation, and abbreviations are ambiguous besides
        # -- so the word is dropped and the remainder read as local time.
        head = text.rsplit(" ", 1)[0] if " " in text else text
        try:
            parsed = datetime.datetime.fromisoformat(head)
        except ValueError:
            parsed = None

    if parsed is None:
        return _BEGINNING_OF_TIME

    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def report_sort_health(board: Board) -> list[str]:
    """Check the shared total order, print the outcome, and return failures.

    **Extracted from `main()` rather than inlined**, because inlining it pushed that
    function to 14 branches and `ruff` refused at 12. The limit is doing its job here:
    `main()` is a dispatcher, and a block that prints a report is a different concern.
    """
    problems = total_order_problems(board)
    if problems:
        print(f"  SORT IS BROKEN, {len(problems)} problem(s):")
        for problem in problems:
            print(f"      {problem}")
    else:
        print("  Sort self-test passed, and every lane is strictly ordered.")
    return problems


def total_order_problems(board: Board) -> list[str]:
    """Report any lane whose cards do not compare STRICTLY increasing. **Card #0047.**

    **This is the requirement the card states in capitals**, and it is about the screen
    rather than about tidiness. The page repaints every 400 ms; two cards that compare
    equal can swap between frames, which is a card moving under Terry's cursor as he
    reaches for it.

    **Checked against the real board rather than asserted in a comment**, because the
    key is only total while `ticket` stays unique -- and nothing else here enforces that.
    """
    problems: list[str] = []

    for lane in board.lanes():
        keys = [item_order_key(item, board.policy) for item in lane.items]
        problems.extend(
            f"{lane.label}: #{before[1]:04d} and #{after[1]:04d} do not order"
            for before, after in itertools.pairwise(keys)
            if before >= after
        )

    return problems


@dataclass
class Change:
    """One entry in an item's history. Appended, never edited, never removed.

    **`frm` is `None` on a creation entry**, which is how a reader tells creation from
    a move without a type field. It serializes as `"from"`, because that is what the
    JSON should read like -- `from` is a Python keyword and cannot be an attribute.
    """

    at: str
    to: str
    by: str
    frm: str | None = None
    # **An OWNERSHIP change has no lane transition**, so `to` is empty on those and
    # these two carry the actors instead. Card #0053.
    #
    # **This keeps the history a log of CHANGES rather than turning it into a general
    # event log**, which is what Terry's clarification bought: *"initial ticket
    # ownership assignment is NOT to be in the audit log, that's clear by ticket
    # creation timestamp."* Only a REASSIGNMENT is an event, so these entries are rare
    # and no existing card needs a synthetic one.
    owner_frm: str | None = None
    owner_to: str | None = None
    # **A PRIORITY change is the third shape, added 2026-08-19 on card #0070.** Terry
    # dropped #0037 to P5, watched it move up the lane in the page, opened it, and found
    # nothing explaining why: *"no breadcrumbs of it in Audit Log."*
    #
    # **`set_priority` used to write no entry on purpose**, and this reverses that. The
    # old reasoning was that `verify()` replays LANES and a third shape would have to be
    # taught to it -- true, and it valued the checker's simplicity over the one reader
    # the log exists for.
    priority_frm: str | None = None
    priority_to: str | None = None

    @property
    def kind(self) -> str:
        """`"lane"`, `"owner"` or `"priority"`. **The one place the shape is decided.**

        **This replaced `is_owner_change`, and the reason is a bug that already
        happened.** `replayed_state()` read `history[-1].to` and returned `""` for any
        card whose last event was a reassignment -- a wrong lane, silently. The fix there
        was a filter, and the filter had to be repeated at each reader.

        **A second discriminant would have meant checking two booleans everywhere**, and
        the third shape is exactly when that stops being remembered. Readers now ask for
        the kind they want.
        """
        if self.owner_to is not None:
            return "owner"
        if self.priority_to is not None:
            return "priority"
        return "lane"

    def to_json(self) -> dict[str, str]:
        out = {"at": self.at, "to": self.to, "by": self.by}
        if self.frm is not None:
            out["from"] = self.frm
        if self.owner_to is not None:
            out["ownerTo"] = self.owner_to
        if self.owner_frm is not None:
            out["ownerFrom"] = self.owner_frm
        if self.priority_to is not None:
            out["priorityTo"] = self.priority_to
        if self.priority_frm is not None:
            out["priorityFrom"] = self.priority_frm
        return out

    @classmethod
    def from_json(cls, raw: JsonObject, where: str) -> Self:
        owner_to = raw.get("ownerTo")
        priority_to = raw.get("priorityTo")
        # **`to` is required on a LANE entry and absent on the other two.** Checking it
        # unconditionally would refuse every board written after either change.
        moves_lane = not isinstance(owner_to, str) and not isinstance(priority_to, str)
        at, to, by = raw.get("at"), raw.get("to", ""), raw.get("by")
        required = {"at": at, "by": by}
        if moves_lane:
            required["to"] = to
        for key, value in required.items():
            if not isinstance(value, str) or not value:
                raise BoardError(f"{where}: history entry has no {key}")
        if not isinstance(at, str) or not isinstance(to, str) or not isinstance(by, str):
            raise BoardError(f"{where}: history entry contains a non-string field")
        frm = raw.get("from")
        owner_frm = raw.get("ownerFrom")
        priority_frm = raw.get("priorityFrom")
        return cls(
            at=at,
            to=to,
            by=by,
            frm=frm if isinstance(frm, str) else None,
            owner_frm=owner_frm if isinstance(owner_frm, str) else None,
            owner_to=owner_to if isinstance(owner_to, str) else None,
            priority_frm=priority_frm if isinstance(priority_frm, str) else None,
            priority_to=priority_to if isinstance(priority_to, str) else None,
        )


@dataclass
class Comment:
    """A note either of us leaves on a card.

    **Terry asked for these alongside the audit trail:** *"I need to click in for
    description or comment history (from either of us) or audit trail."*

    **They are SEPARATE from `history` on purpose.** History is what the machine
    recorded and nobody typed; comments are what a person chose to say. Mixing them
    would make the audit trail editable, which is the one thing it must not be.
    """

    at: str
    by: str
    text: str

    def to_json(self) -> dict[str, str]:
        return {"at": self.at, "by": self.by, "text": self.text}

    @classmethod
    def from_json(cls, raw: JsonObject, where: str) -> Self:
        values = {key: raw.get(key) for key in ("at", "by", "text")}
        for key, value in values.items():
            if not isinstance(value, str):
                raise BoardError(f"{where}: comment has no {key}")
        return cls(at=str(values["at"]), by=str(values["by"]), text=str(values["text"]))


@dataclass(frozen=True)
class ActivityEvent:
    """One sanitized, time-addressable board event for CLI inspection.

    Comment text is deliberately absent. A board-write monitor needs to identify what
    changed, not copy private prose into a terminal transcript. The source timestamp is
    retained verbatim for audit output while `instant` supplies a comparable value.
    """

    ticket: int
    item_id: str
    kind: str
    at: str
    instant: datetime.datetime
    by: str
    lane_from: str | None = None
    lane_to: str | None = None
    owner_from: str | None = None
    owner_to: str | None = None
    priority_from: str | None = None
    priority_to: str | None = None
    comment_chars: int | None = None
    sequence: int = 0

    def to_json(self) -> JsonObject:
        """Return the public CLI shape without the internal sort fields."""
        out: JsonObject = {
            "ticket": self.ticket,
            "id": self.item_id,
            "kind": self.kind,
            "at": self.at,
            "by": self.by,
        }
        if self.lane_from is not None:
            out["from"] = self.lane_from
        if self.lane_to is not None:
            out["to"] = self.lane_to
        if self.owner_from is not None:
            out["ownerFrom"] = self.owner_from
        if self.owner_to is not None:
            out["ownerTo"] = self.owner_to
        if self.priority_from is not None:
            out["priorityFrom"] = self.priority_from
        if self.priority_to is not None:
            out["priorityTo"] = self.priority_to
        if self.comment_chars is not None:
            out["commentChars"] = self.comment_chars
        return out

    def describe(self) -> str:
        """One compact human-readable line with no card or comment prose."""
        base = f"{self.at}  #{self.ticket:04d} {self.item_id}  {self.kind} by {self.by}"
        if self.kind in {"created", "moved"}:
            source = self.lane_from if self.lane_from is not None else "(new)"
            return f"{base}  {source} -> {self.lane_to}"
        if self.kind == "assigned":
            return f"{base}  {self.owner_from} -> {self.owner_to}"
        if self.kind == "prioritized":
            return f"{base}  {self.priority_from} -> {self.priority_to}"
        return f"{base}  {self.comment_chars} character(s)"


@dataclass
class Item:
    """One card.

    **`id` is stable and MUST NOT be reused.** It is how the board, the harness panel
    and any future consumer agree on which card is which, and it survives reordering,
    renaming and signoff. The markdown version numbered rows positionally, so signing
    one off renumbered the rest while the panel matched by position.

    **`ticket` is the handle a PERSON uses**, and it is not decoration on top of the
    slug. Terry named the use case exactly: *"Human brains will want to do shit like
    'wtf is up with ticket 137, Claude? you high today?'"* -- and then the other half,
    *"we don't like ticket summary"*, because quoting a long subject out loud is
    miserable.

    **So the two identifiers have different jobs.** The slug is the machine's; the
    number is the conversation's. Both are permanent, and `find()` accepts either.
    """

    id: str
    subject: str
    state: str
    ticket: int = 0
    priority: str = DEFAULT_PRIORITY
    detail: str = ""
    # **EXACTLY ONE OWNER, and it is a LABEL rather than a permission.** Terry,
    # 2026-08-19: *"It's just a label, not permissions model."* Card #0053.
    #
    # **NO CODE PATH MAY CONSULT THIS TO ALLOW OR REFUSE ANYTHING.** Not `may_move`,
    # not `may_create`, not the drag handler, not `/create`. `rules.json` answers *who
    # may do what*; this answers *who is carrying it*. **Joining them would create a
    # second authorization mechanism that `check_edges()` cannot see**, contradicting
    # the one that is actually enforced.
    #
    # **Terry can move a card Claude owns, and Claude can move a card Terry owns.**
    # That is not a bug to close.
    #
    # **Defaults to `claude` on his standing order**: *"if in doubt, assign to claude
    # and it'll get fixed as ticket progresses."* An error that lands on Claude gets
    # corrected the moment the work starts; one that lands on Terry sits in his lane
    # until he notices.
    owner: str = ""
    # **HIERARCHY IS A FIELD, NOT A RELATIONSHIP. Card #0028.**
    #
    # Jira, Linear and GitHub all keep parent/child out of their relationship table, and
    # Terry approved following them. **A tree needs two rules a symmetric table cannot
    # state**: a card has at most ONE parent, and the chain must not loop. Both are
    # checked in `Board.from_json` and in `set_parent`.
    #
    # It holds the parent's `id`, and `None` means top level.
    parent: str | None = None
    history: list[Change] = field(default_factory=list[Change])
    comments: list[Comment] = field(default_factory=list[Comment])

    @property
    def created_at(self) -> str | None:
        """The raw `at` string of this card's creation entry, or `None`.

        **A creation entry is a LANE change with no `frm`.** `Change.frm` is documented
        as `None` exactly there, so no type field is needed and none is invented here.

        **Ownership and priority entries are skipped**, because both carry `frm = None`
        too and neither is a creation. That is the same trap `replayed_state()` already
        fell into once -- a reassignment read as a lane event returned a wrong lane
        silently. Asking for `kind == "lane"` is what makes this immune.
        """
        for change in self.history:
            if change.kind == "lane" and change.frm is None:
                return change.at
        return None

    @property
    def state_since(self) -> str | None:
        """When this card last ENTERED the lane it is in, or `None`. **Card #0063.**

        **The LAST lane entry, not the first**, and the difference is the whole point: a
        card completed, reopened and completed again entered `completed` twice, and only
        the second one says how long it has been sitting there.

        **Ownership and priority entries are skipped.** Both carry no lane, so counting
        one would report a card as freshly arrived because somebody renamed its owner.

        `None` when the history has no lane entry -- a migrated card, or one whose trail
        predates the mechanism. Callers MUST treat that as unknown rather than as recent.
        """
        for change in reversed(self.history):
            if change.kind == "lane":
                return change.at
        return None

    @property
    def label(self) -> str:
        """`#0016`. **Four digits, zero-padded**, per Terry's standing order.

        *"Zero-pad anything that will ever sort."* Unpadded numbers sort `1, 10, 2`,
        and a reference cited in a commit message cannot be cheaply changed later.
        Above 9999 it simply grows rather than truncating.
        """
        return f"#{self.ticket:04d}"

    def replayed_state(self) -> str | None:
        """The state this item's own history says it should be in.

        `None` when there is no history to replay -- migrated cards have none, and an
        absent trail is not evidence of a wrong state.

        **OWNERSHIP ENTRIES ARE SKIPPED, and forgetting that was a real defect.** Card
        #0053 gave `Change` an ownership form that carries no lane, so `to` is `""` on
        those. This read `history[-1].to` and returned `""` for any card whose most
        recent event was a reassignment -- **a wrong lane, silently, rather than an
        error.**

        **Caught 2026-08-19 on card #0003**, whose last entry was Terry handing it
        back. `verify()` was never affected because it filters the same entries before
        its own replay; **the filter was applied there and not here**, which is the
        instance being fixed rather than the class.
        """
        lanes = [c for c in self.history if c.kind == "lane"]
        return lanes[-1].to if lanes else None

    def to_json(self) -> JsonObject:
        history: list[JsonValue] = [cast("JsonValue", change.to_json()) for change in self.history]
        out: JsonObject = {
            "id": self.id,
            "ticket": self.ticket,
            "priority": self.priority,
            "state": self.state,
            "subject": self.subject,
            "detail": self.detail,
            "owner": self.owner,
            "history": history,
        }
        # Omitted at top level, so 60-odd parentless cards stay readable.
        if self.parent:
            out["parent"] = self.parent
        # Omitted when empty, so a board full of comment-less cards stays readable.
        if self.comments:
            out["comments"] = [cast("JsonValue", comment.to_json()) for comment in self.comments]
        return out

    @classmethod
    def from_json(
        cls,
        raw: JsonObject,
        where: str,
        policy: TransitionPolicy | None = None,
        actors: set[str] | None = None,
        default_owner: str | None = None,
    ) -> Self:
        active = policy or _POLICY
        required = {key: raw.get(key) for key in ("id", "state", "subject")}
        for key, value in required.items():
            if not isinstance(value, str) or not value:
                raise BoardError(f"{where} has no {key}")
        item_id, state, subject = (str(required[key]) for key in ("id", "state", "subject"))
        if state not in active.states:
            raise BoardError(f"{where}: unknown state {state!r}")
        priority = raw.get("priority", active.default_priority)
        if not isinstance(priority, str) or priority not in active.priorities:
            raise BoardError(f"{where}: unknown priority {priority!r}")
        ticket = raw.get("ticket", 0)
        if not isinstance(ticket, int) or ticket < 0:
            raise BoardError(f"{where}: ticket {ticket!r} is not a positive integer")
        # **"Exactly one owner" is ENFORCED here rather than assumed.** An unknown
        # actor is refused outright: a card owned by nobody, or by a name no lane
        # header can render, is a data error and `from_json` REFUSES rather than
        # repairs -- the same contract the rest of this class keeps.
        owner = raw.get("owner", default_owner or DEFAULT_OWNER)
        if not isinstance(owner, str) or owner not in (
            actors if actors is not None else set(ACTORS)
        ):
            raise BoardError(f"{where}: unknown owner {owner!r}")
        detail = raw.get("detail", "")
        if not isinstance(detail, str):
            raise BoardError(f"{where}: detail is not a string")
        parent = raw.get("parent")
        if parent is not None and not isinstance(parent, str):
            raise BoardError(f"{where}: parent is not a string")
        history_raw = raw.get("history", [])
        if not isinstance(history_raw, list) or not all(
            isinstance(change, dict) for change in history_raw
        ):
            raise BoardError(f"{where}: history is not a list of objects")
        comments_raw = raw.get("comments", [])
        if not isinstance(comments_raw, list) or not all(
            isinstance(comment, dict) for comment in comments_raw
        ):
            raise BoardError(f"{where}: comments is not a list of objects")
        return cls(
            id=item_id,
            subject=subject,
            state=state,
            ticket=ticket,
            priority=priority,
            detail=detail,
            # **A card with no `owner` reads as Claude's**, which is the migration for
            # the 51 cards written before this field existed. Terry's standing order:
            # *"if in doubt, assign to claude."* No synthetic history entry is written
            # for them, because initial ownership is not an audit event.
            owner=owner,
            # **Existence and cycles are checked in `Board.from_json`, not here.** An item
            # cannot see its siblings, so this only records what the file said.
            parent=parent,
            history=[
                Change.from_json(change, where)
                for change in history_raw
                if isinstance(change, dict)
            ],
            comments=[
                Comment.from_json(comment, where)
                for comment in comments_raw
                if isinstance(comment, dict)
            ],
        )


def _links_from_json(raw: JsonValue, known: set[str], where: str) -> list[Link]:
    """Validate the board's link table. **Extracted from `Board.from_json`**, which ruff
    correctly called too branchy once this landed in it. Card #0028."""
    if not isinstance(raw, list):
        raise BoardError(f"{where}: 'links' is not a list")
    links: list[Link] = []
    pairs: set[tuple[str, str, str]] = set()
    for index, link_raw in enumerate(raw):
        link = Link.from_json(link_raw, f"{where}: links[{index}]")
        for end in (link.frm, link.to):
            if end not in known:
                raise BoardError(f"{where}: links[{index}] names unknown card {end!r}")
        if link.frm == link.to:
            raise BoardError(f"{where}: links[{index}] joins {link.frm!r} to itself")
        # **A duplicate is checked in BOTH directions, because `relates_to` is its own
        # inverse.** `a relates_to b` and `b relates_to a` are one claim written two
        # ways, and letting both in would show the relationship on each card twice.
        key = (link.frm, link.kind, link.to)
        mirror = (link.to, link.kind, link.frm)
        if key in pairs or (link.kind == "relates_to" and mirror in pairs):
            raise BoardError(f"{where}: links[{index}] repeats an existing link")
        pairs.add(key)
        links.append(link)
    return links


def _check_parents(board: Board, known: set[str], where: str) -> None:
    """Refuse a parent that does not exist or that closes a loop. Card #0028.

    **It runs once the whole board exists**, because a parent is a sibling and no item
    can validate its own.
    """
    for item in board.items:
        if item.parent is None:
            continue
        if item.parent not in known:
            raise BoardError(f"{where}: {item.label} names unknown parent {item.parent!r}")
        if cycle := board.parent_cycle(item):
            raise BoardError(f"{where}: parent cycle {' -> '.join(cycle)}")


@dataclass
class Lane:
    state: str
    label: str
    css: str
    owner_label: str
    items: list[Item]


@dataclass
class Board:
    """A whole board: the project it belongs to, the port it is served on, its cards."""

    project: str = ""
    policy: TransitionPolicy = field(default_factory=lambda: _POLICY, repr=False, compare=False)
    port: int = DEFAULT_PORT
    items: list[Item] = field(default_factory=list[Item])

    # Monotonic optimistic-concurrency token. Older schema-2 boards predate the field
    # and enter at revision zero; the first service mutation writes revision one.
    revision: int = 0

    # **The next ticket to hand out. It only ever goes UP.**
    #
    # **A number MUST NOT be reused, even after a card is archived or deleted.** The
    # whole point is that Terry can say "ticket 137" and mean one thing forever; two
    # pieces of work sharing a reference in git history would destroy that.
    #
    # **Stored rather than derived.** `len(items) + 1` and `max(ticket) + 1` both
    # look correct and both collide the moment anything is removed -- the first
    # immediately, the second as soon as the highest-numbered card goes.
    next_ticket: int = 1

    # **Every relationship on the board, each stored ONCE.** See `Link` for why this is
    # here rather than a copy on each card. Card #0028.
    links: list[Link] = field(default_factory=list[Link])

    # **THE CAST LIVES WITH THE DATA, NOT WITH THE CODE. Card #0083.**
    #
    # Card #0072 put users in `rules.json`, which sits next to `board_state.py` inside the
    # TOOL repository. **Terry called that "per-project cfg" and it is really
    # per-DEPLOYMENT**: a second person adding themselves would be editing a file inside
    # a repository they cloned, carrying that edit across every `git pull` forever.
    #
    # **`port` and `project` were already here and are already per-project**, which is
    # the precedent this follows rather than inventing one.
    #
    # **Schema 4 brought lanes and edges into the board too.** Identity, permissions,
    # users, and state now travel as one validated and atomically replaced snapshot.
    users: tuple[User, ...] = ()
    browser_user: str = ""
    cli_user: str = ""
    default_owner: str = ""

    # ---- serialization -------------------------------------------------------

    def to_json(self) -> JsonObject:
        # **THE USERS MUST BE WRITTEN BACK OR THE FIRST SAVE DELETES THEM.** Card #0083.
        # `save()` rewrites the WHOLE file from this dict, and `api_endpoint.py` pushes it five
        # seconds later -- so a forgotten key here would erase the cast, commit the
        # erasure, and the next load would refuse to start.
        users: list[JsonValue] = [
            {"id": u.id, "label": u.label, "class": u.user_class, "color": u.color}
            for u in self.users
        ]
        items: list[JsonValue] = [item.to_json() for item in self.items]
        out: JsonObject = {
            "schema": SCHEMA,
            "revision": self.revision,
            "project": self.project,
            "port": self.port,
            "policy": self.policy.to_json(),
            "users": users,
            "browserUser": self.browser_user,
            "cliUser": self.cli_user,
            "defaultOwner": self.default_owner,
            "nextTicket": self.next_ticket,
            "items": items,
        }
        # **Omitted while empty, which is also the migration.** Every board written before
        # card #0028 simply has no `links` key, and reads back as a board with no links.
        if self.links:
            out["links"] = [cast("JsonValue", link.to_json()) for link in self.links]
        return out

    @classmethod
    def from_json(
        cls,
        raw: JsonValue,
        where: str,
        policy: TransitionPolicy | None = None,
    ) -> Self:
        """Validate and build. **The ONLY place a malformed board can enter.**

        **It refuses rather than repairs.** Silently normalizing an unknown state or a
        duplicate id would hide a bug in whatever wrote it, which is exactly how the
        markdown version lost a signoff.
        """
        if not isinstance(raw, dict):
            raise BoardError(f"{where}: top level is {type(raw).__name__}, want object")
        if raw.get("schema") != SCHEMA:
            raise BoardError(f"{where}: schema {raw.get('schema')!r}, this build reads {SCHEMA}")
        items_raw = raw.get("items")
        if not isinstance(items_raw, list):
            raise BoardError(f"{where}: 'items' is missing or not a list")

        port = raw.get("port", DEFAULT_PORT)
        if not isinstance(port, int) or not MIN_PORT <= port <= MAX_PORT:
            raise BoardError(f"{where}: port {port!r} is not a TCP port number")

        revision = raw.get("revision", 0)
        if not isinstance(revision, int) or revision < 0:
            raise BoardError(f"{where}: revision {revision!r} is not a non-negative integer")

        active_policy = _board_policy(raw, where, policy)

        # **Users are parsed and INSTALLED before the items**, because `Item.from_json`
        # validates each card's owner against the cast. Card #0083.
        users = _parse_users(raw, pathlib.Path(where))
        browser_user = _pick_role(raw, "browserUser", users, HUMAN, pathlib.Path(where))
        cli_user = _pick_role(raw, "cliUser", users, BOT, pathlib.Path(where))
        default_owner = raw.get("defaultOwner") or cli_user
        if default_owner not in {u.id for u in users}:
            raise BoardError(f"{where}: defaultOwner names unknown user {default_owner!r}")
        install_users(users, browser_user, cli_user, str(default_owner), where, active_policy)

        items: list[Item] = []
        seen: set[str] = set()
        tickets: set[int] = set()
        for index, item_raw in enumerate(items_raw):
            spot = f"{where}: items[{index}]"
            if not isinstance(item_raw, dict):
                raise BoardError(f"{spot} is not an object")
            item = Item.from_json(
                item_raw, spot, active_policy, {u.id for u in users}, str(default_owner)
            )
            if item.id in seen:
                raise BoardError(f"{spot}: duplicate id {item.id!r}")
            seen.add(item.id)
            # **A duplicate ticket is refused at the door.** The number's whole value
            # is that "ticket 137" means one thing forever, and two cards sharing one
            # would break every reference in git and in conversation at once.
            if item.ticket and item.ticket in tickets:
                raise BoardError(f"{spot}: duplicate ticket {item.label}")
            tickets.add(item.ticket)
            items.append(item)

        next_ticket = raw.get("nextTicket", max(tickets, default=0) + 1)
        if not isinstance(next_ticket, int) or next_ticket < 1:
            raise BoardError(f"{where}: nextTicket {next_ticket!r} is not positive")
        # **The counter MUST be ahead of every ticket in the file.** A hand edit that
        # rewinds it would hand out a number already in use -- caught here rather
        # than discovered when two cards collide.
        if tickets and next_ticket <= max(tickets):
            raise BoardError(
                f"{where}: nextTicket is {next_ticket} but ticket "
                f"#{max(tickets):04d} already exists -- the counter went backwards"
            )

        board = cls(
            project=str(raw.get("project", "")),
            port=port,
            items=items,
            revision=revision,
            next_ticket=next_ticket,
            links=_links_from_json(raw.get("links", []), seen, where),
            users=users,
            browser_user=browser_user,
            cli_user=cli_user,
            default_owner=str(default_owner),
        )
        board.policy = active_policy
        _check_parents(board, seen, where)
        return board

    # ---- reading -------------------------------------------------------------

    def find(self, ref: str) -> Item:
        """A card, by slug OR by ticket number. **If you can say it, you can type it.**

        Terry's use case is spoken -- *"wtf is up with ticket 137"* -- so the CLI
        accepts `137`, `#137` and `0137` as readily as `implement-lrc-plug-as`.
        Making him look up a slug to act on a number he just said out loud would
        waste the handle the number exists to be.

        **The slug is tried first.** A slug is unambiguous; a bare number could in
        principle be one, and the explicit identifier should win.
        """
        for item in self.items:
            if item.id == ref:
                return item

        digits = ref.lstrip("#").lstrip("0") or "0"
        if digits.isdigit():
            wanted = int(digits)
            for item in self.items:
                if item.ticket == wanted:
                    return item
            raise BoardError(f"no card with ticket #{wanted:04d}")

        raise BoardError(f"no item with id {ref!r}")

    def lanes(self) -> list[Lane]:
        """Return lanes whose cards use the shared priority-then-ticket order.

        Policy priority is primary. The monotonically allocated ticket number is both
        creation order and the unique tie-breaker. This total comparator keeps cards
        stable across the browser's frequent repaints and is shared with CLI triage.
        """

        buckets: dict[str, list[Item]] = {state: [] for state in self.policy.states}
        for item in self.items:
            buckets.setdefault(item.state, []).append(item)
        return [
            Lane(
                state,
                label,
                lane_class(state, self.policy, self.browser_user),
                lane_owner_label(state, self.policy),
                sorted(
                    buckets.get(state, []),
                    key=lambda item: item_order_key(item, self.policy),
                ),
            )
            for state, label in self.policy.lanes
        ]

    def verify(self) -> list[str]:
        """Replay every item's history and report anything the transition policy forbids.

        **THIS IS THE ENFORCEMENT THAT SURVIVES A DIRECT WRITE.** `move()` guards every
        caller that uses the API; nothing can stop `item.state = "completed"` or a hand
        edit to the JSON. **The history is the authority**, so replaying it catches an
        out-of-band change on the next load, whoever made it and however.

        **Terry asked for exactly this emphasis:** *"I'd rather the state machine guard
        sanity."* So it checks four things rather than one, and only the first was
        here before:

        1. The stored `state` matches where the history ends.
        2. **Every recorded transition was LEGAL for the actor that claimed it.** A
           forged `by` on an edge that actor may not take is now caught.
        3. **The chain is unbroken** -- each entry leaves where the previous one
           arrived. A spliced or deleted entry shows up as a gap.
        4. The first entry is a creation, in a lane that permits creation by its actor.

        **What it still cannot catch, stated plainly: a LEGAL edge with a forged
        actor.** If a `ready_for_review -> completed` entry claims `terry`, the state
        machine agrees, because that is exactly what Terry is allowed to do. No amount
        of checking here fixes that; only signing would, and signing a local board is
        absurd. **The defense is that the CLI cannot emit `by: terry` and the server
        cannot emit `by: claude`**, so forging one takes a deliberate hand edit rather
        than a flag.

        **An item with no history is skipped rather than flagged.** The twelve cards
        migrated from the markdown log carry none, and inventing a trail for them would
        have been fabricating evidence.
        """
        problems: list[str] = []
        for item in self.items:
            if not item.history:
                continue

            # **ONLY LANE ENTRIES ARE REPLAYED.** Cards #0053 and #0070. Replaying an
            # ownership or priority entry as a transition would break the chain check on
            # every reassignment, and a model that refuses the board after somebody
            # changed a label is worse than not recording the label at all.
            #
            # **Asking for the kind rather than excluding known others is what makes a
            # FOURTH shape safe.** `not c.is_owner_change` silently began replaying
            # priority entries the moment they existed; `== "lane"` cannot.
            #
            # **The other kinds are checked on their own terms instead**, below.
            lane_history = [c for c in item.history if c.kind == "lane"]
            problems.extend(
                f"{item.id}: history reassigns to {c.owner_to!r}, which is not an actor"
                for c in item.history
                if c.kind == "owner" and c.owner_to not in {user.id for user in self.users}
            )
            problems.extend(
                f"{item.id}: history sets priority {c.priority_to!r}, which is not one"
                for c in item.history
                if c.kind == "priority" and c.priority_to not in self.policy.priorities
            )
            if not lane_history:
                continue

            first = lane_history[0]
            if first.frm is None and not self.policy.may_create(first.by, first.to):
                problems.append(
                    f"{item.id}: history says {first.by} created it in {first.to}, "
                    f"which {first.by} may not do"
                )

            where: str | None = None
            for index, change in enumerate(lane_history):
                if change.frm is None:
                    if index > 0:
                        problems.append(
                            f"{item.id}: history[{index}] has no 'from', so it reads "
                            f"as a second creation"
                        )
                elif where is not None and change.frm != where:
                    problems.append(
                        f"{item.id}: history[{index}] leaves {change.frm!r} but the "
                        f"previous entry arrived at {where!r} -- the chain is broken"
                    )
                elif not self.policy.may_move(change.by, change.frm, change.to):
                    problems.append(
                        f"{item.id}: history[{index}] records {change.by} moving "
                        f"{change.frm} -> {change.to}, which the permission table "
                        f"forbids"
                    )
                where = change.to

            if where is not None and where != item.state:
                problems.append(
                    f"{item.id}: stored state is {item.state!r} but its history ends "
                    f"at {where!r} -- something changed it without going through "
                    f"move()"
                )
        return problems

    # ---- writing -------------------------------------------------------------

    def create(  # noqa: PLR0913 -- see below
        self,
        item_id: str,
        subject: str,
        state: str,
        by: Actor,
        *,
        priority: str | None = None,
        detail: str = "",
        owner: Actor = "",
    ) -> str:
        """Add a new card, with its first history entry already on it.

        **`PLR0913` is suppressed and the suggested fix would be worse.** Collapsing
        these into a dict would move the field names off the call site, where they are
        the only thing making the call readable. The last two are keyword-only, so the
        positional count is within the rule's real concern.

        **Creation is an EVENT, not an initial condition.** Without an entry here a
        card's earliest record would be the day somebody happened to touch it.
        """
        if not self.policy.may_create(by, state):
            allowed = sorted(s for s in self.policy.states if self.policy.may_create(by, s))
            raise BoardError(
                f"{by} may not create in {state}. "
                + (
                    f"{by} may create in: {', '.join(allowed)}"
                    if allowed
                    else f"{by} may not create anywhere"
                )
            )
        if any(item.id == item_id for item in self.items):
            raise BoardError(f"duplicate id {item_id!r}")
        priority = priority or self.policy.default_priority
        if priority not in self.policy.priorities:
            raise BoardError(f"unknown priority {priority!r}")
        chosen_owner = owner or self.default_owner or DEFAULT_OWNER
        if chosen_owner not in {user.id for user in self.users}:
            raise BoardError(f"unknown owner {chosen_owner!r}")
        # **Taken from the counter and the counter advances**, never derived from the
        # items. See `next_ticket` for why both obvious derivations collide.
        # **NO ownership entry, even though the owner is now chosen rather than assumed.**
        # Card #0069. Terry drew this line on #0053: *"initial ticket ownership assignment
        # is NOT to be in the audit log, that's clear by ticket creation timestamp."*
        # **Picking an owner at creation is still an initial condition, not an event** --
        # the creation entry already carries the moment, and only a REASSIGNMENT is a
        # change to record.
        item = Item(
            id=item_id,
            subject=subject,
            state=state,
            ticket=self.next_ticket,
            priority=priority,
            detail=detail,
            owner=chosen_owner,
            history=[Change(at=now(), to=state, by=by)],
        )
        self.next_ticket += 1
        self.items.append(item)
        return f"created {item.label} {item_id} in {state} (by {by})"

    def set_priority(self, item_id: str, priority: str, by: Actor) -> str:
        """Change one card's priority, and RECORD IT. Cards #0060 and #0070.

        **This used to write no history entry, and Terry caught it.** He dropped #0037
        to P5, saw it move up its lane in the page, opened the card, and found nothing
        explaining the move: *"no breadcrumbs of it in Audit Log."*

        **The old reasoning was real and it was outweighed.** An entry with neither a
        lane nor an owner is a third shape for `verify()` to learn -- true, and it valued
        the checker's simplicity over the one reader the log exists for. **A card that
        moves for no visible reason is exactly what an audit trail is supposed to
        prevent.**

        **Write the log first, then the field**, for the same reason `move()` and
        `assign()` do: a change that never reached the trail is indistinguishable from
        tampering.

        **A no-op writes NOTHING.** Setting P3 on a P3 card is not an event, and the
        same rule already governs reassignment.

        **Either actor may set it, and there is no permission check.** Terry decides what
        matters; Claude files cards and gets the guess wrong sometimes. A permission here
        would only make a correction need a round trip.
        """
        if priority not in self.policy.priorities:
            raise BoardError(f"unknown priority {priority!r}; want one of {', '.join(PRIORITIES)}")
        item = self.find(item_id)
        was = item.priority
        if was == priority:
            return f"{item.label} is already {priority}"
        item.history.append(Change(at=now(), to="", by=by, priority_frm=was, priority_to=priority))
        item.priority = priority
        return f"{item.label} priority: {was} -> {priority} (by {by})"

    # ---- relationships, card #0028 -------------------------------------------

    def parent_cycle(self, item: Item) -> list[str] | None:
        """The loop this card's parent chain falls into, or None. Card #0028.

        **A tree is the one shape a symmetric relationship table cannot express**, and
        this is half the reason `parent` is a field rather than a link kind. The other
        half is "at most one parent", which the field gives for free.

        **It walks rather than recurses, and it stops on the first repeat**, so a board
        that somehow reaches disk with a loop reports it instead of hanging.
        """
        by_id = {i.id: i for i in self.items}
        seen: list[str] = [item.id]
        at = item.parent
        while at is not None:
            if at in seen:
                return [*seen, at]
            seen.append(at)
            nxt = by_id.get(at)
            if nxt is None:
                return None
            at = nxt.parent
        return None

    def set_parent(self, child_id: str, parent_ref: str | None, by: Actor) -> str:
        """Give a card a parent, or clear it with `None`. Card #0028.

        **No history entry**, for the reason `set_priority` gives: `verify()` replays
        LANES and OWNERS, and a third shape would have to be taught to it. Git is the
        trail.
        """
        child = self.find(child_id)
        if parent_ref is None:
            if child.parent is None:
                return f"{child.label} already has no parent"
            was = child.parent
            child.parent = None
            return f"{child.label} parent cleared (was {was}, by {by})"

        parent = self.find(parent_ref)
        if parent.id == child.id:
            raise BoardError(f"{child.label} cannot be its own parent")
        was_parent = child.parent
        child.parent = parent.id
        # **Set it, then check, then put it back.** Testing the loop before the write
        # would have to simulate the new edge anyway, and this way the check reads the
        # same board every other caller does.
        if cycle := self.parent_cycle(child):
            child.parent = was_parent
            raise BoardError(f"that would make a parent cycle: {' -> '.join(cycle)}")
        return f"{child.label} parent: {was_parent or 'none'} -> {parent.id} (by {by})"

    def link(self, a_ref: str, kind: str, b_ref: str, by: Actor) -> str:
        """Relate two cards. **There is no way to write one half.** Card #0028.

        **It takes BOTH cards, which is the structural answer to Terry's requirement**
        that an inconsistent relationship *"MUST NOT be allowed to be possible"*. A
        function taking one card and one direction could write a dangling half; this one
        cannot express that.

        **An inverse spelling is normalized to the stored one.** `--link 5 blocked_by 28`
        becomes `28 blocks 5`, so the file holds exactly one spelling of each fact.
        """
        if kind not in LINK_INVERSE:
            raise BoardError(
                f"unknown relationship {kind!r}; want one of {', '.join(sorted(LINK_INVERSE))}"
            )
        a, b = self.find(a_ref), self.find(b_ref)
        if a.id == b.id:
            raise BoardError(f"{a.label} cannot be related to itself")
        frm, to, stored = a, b, kind
        if kind not in LINK_CANONICAL:
            frm, to, stored = b, a, LINK_INVERSE[kind]
        if self.find_link(frm.id, stored, to.id) is not None:
            return f"{a.label} already {kind} {b.label}"
        self.links.append(Link(frm=frm.id, kind=stored, to=to.id))
        return f"{a.label} {kind} {b.label} (by {by})"

    def find_link(self, frm: str, kind: str, to: str) -> Link | None:
        """The stored row for this relationship, in either spelling, or None."""
        for link in self.links:
            if link.kind != kind:
                continue
            if (link.frm, link.to) == (frm, to):
                return link
            # `relates_to` is its own inverse, so the row may be written either way.
            if kind == "relates_to" and (link.frm, link.to) == (to, frm):
                return link
        return None

    def unlink(self, a_ref: str, kind: str, b_ref: str, by: Actor) -> str:
        """Remove a relationship. Removing one half removes the whole thing, because
        there is only one row. Card #0028."""
        if kind not in LINK_INVERSE:
            raise BoardError(f"unknown relationship {kind!r}")
        a, b = self.find(a_ref), self.find(b_ref)
        frm, to, stored = a, b, kind
        if kind not in LINK_CANONICAL:
            frm, to, stored = b, a, LINK_INVERSE[kind]
        found = self.find_link(frm.id, stored, to.id)
        if found is None:
            return f"{a.label} is not {kind} {b.label}"
        self.links.remove(found)
        return f"{a.label} no longer {kind} {b.label} (by {by})"

    def links_for(self, item_id: str) -> list[tuple[str, str]]:
        """`(relationship as this card sees it, other card's id)`, sorted. Card #0028.

        **This is where the second half comes from.** A row saying `28 blocks 5` is read
        by card 5 as `blocked_by 28`, computed through `LINK_INVERSE` rather than stored.
        """
        out: list[tuple[str, str]] = []
        for link in self.links:
            if link.frm == item_id:
                out.append((link.kind, link.to))
            elif link.to == item_id:
                out.append((LINK_INVERSE[link.kind], link.frm))
        return sorted(out)

    def set_detail(self, item_id: str, detail: str, by: Actor) -> str:
        """Replace one card's description. Card #0028's second lesson.

        **A description was WRITE-ONCE until 2026-08-19, and that was an accident.**
        Terry read card #0028 and answered *"wall of text ELI5, try again in human
        readable fashion"*. Nothing could try again. Claude could only apologize in a
        comment under the wall, which leaves the wall.

        **No history entry, for the reason `set_priority` gives.** `verify()` replays
        LANES and OWNERS, and prose is neither. **The audit trail for the text is git**
        -- the board file is committed after every write, so the old description is one
        `git diff` away and never rides in the JSON twice.

        **An empty description is REFUSED.** Blanking a card is far more likely to be a
        shell that ate the argument than a thing somebody meant, and the old text is
        gone either way.
        """
        text = detail.rstrip("\n")
        if not text.strip():
            raise BoardError("refusing to blank a description; pass real text")
        item = self.find(item_id)
        was = len(item.detail)
        if item.detail == text:
            return f"{item.label} description is already that text"
        item.detail = text
        return f"{item.label} description: {was} -> {len(text)} chars (by {by})"

    def set_subject(self, item_id: str, subject: str, by: Actor) -> str:
        """Rename one card. **Card #0081.** Terry: *"Sometimes I want to change ticket
        titles, and I have no way to do that currently."*

        **No history entry, for the reason `set_detail` gives.** `verify()` replays LANES
        and OWNERS, and a title is neither. **The audit trail for text is git** -- the
        board file is committed after every write, so the old title is one `git diff`
        away and never rides in the JSON twice.

        **THE `id` AND THE `ticket` DO NOT MOVE, and that is the whole safety of this.**
        The slug was derived from the ORIGINAL title and stays put: Terry says *"ticket
        137"* out loud, commit messages cite `#0016`, and `links` join on `id`. **A
        rename that renumbered or re-slugged would break every one of those**, which is
        exactly why `Item.id` is documented as stable and MUST NOT be reused.

        **So a renamed card keeps a slug that no longer describes it.** That is
        deliberate and it is the cheaper half of the trade.

        **An empty title is REFUSED**, like an empty description: a card with no name is
        unfindable in every view, and the old text is gone either way.
        """
        text = " ".join(subject.split())
        if not text:
            raise BoardError("refusing to blank a title; pass real text")
        item = self.find(item_id)
        if item.subject == text:
            return f"{item.label} is already called that"
        was = item.subject
        item.subject = text
        return f"{item.label}: {was!r} -> {text!r} (by {by})"

    def assign(self, item_id: str, owner: Actor, by: Actor) -> str:
        """Reassign one card's owner, appending to its history. Card #0053.

        **EITHER ACTOR MAY REASSIGN EITHER WAY, and that is not an oversight.** Terry:
        *"Terry and Claude MUST be able to reassign ownership between the two"*, and
        *"It's just a label, not permissions model."* **So there is no `may_` check
        here and there MUST NOT be one** -- adding a permission to this path would be
        exactly the second authorization mechanism the field is defined not to be.

        **A no-op reassignment writes NOTHING.** An audit trail that records "Terry set
        the owner to Terry" is noise in the one place noise is expensive, and Terry
        signalling he is working a card can arrive repeatedly.

        **Write the log first, then the field**, for the same reason `move()` does: a
        state change that never reached the trail is indistinguishable from tampering.
        """
        if owner not in {user.id for user in self.users}:
            raise BoardError(f"unknown owner {owner!r}")
        item = self.find(item_id)
        was = item.owner
        if was == owner:
            return f"{item.label} is already owned by {owner}"
        item.history.append(Change(at=now(), to="", by=by, owner_frm=was, owner_to=owner))
        item.owner = owner
        return f"{item.label} ownership change: {was} -> {owner} (by {by})"

    def move(self, item_id: str, to_state: str, by: Actor) -> str:
        """Move one card, appending to its history. Returns a one-line description.

        **THE PERMISSION CHECK LIVES HERE, not in the callers.** The first version had
        it only in the server's POST handler, so the browser was guarded and the library
        was not -- and the library is what Claude uses. A test written the same hour
        walked a card `ready_for_review -> completed` as `claude` and then
        `completed -> in_progress` as `terry`: the one edge Claude must never take, and
        a breach of append-only, both accepted in silence.

        **A guard that covers only the path you had in mind covers nothing.**
        """
        if to_state not in self.policy.states:
            raise BoardError(f"unknown state {to_state!r}")
        item = self.find(item_id)
        was = item.state
        if was == to_state:
            return f"{item_id} is already {to_state}"
        if not self.policy.may_move(by, was, to_state):
            raise BoardError(self.policy.explain_refusal(by, was, to_state))

        # **WRITE THE LOG FIRST, THEN THE STATE.** Terry: *"I'd rather log fail and
        # then we abort vs write file succeed and succeed THEN log fails. Leaves you
        # in a bad spot."*
        #
        # **The bad spot is a card whose state nothing explains.** `verify()` treats
        # the history as the authority, so a state change that never made it into the
        # trail does not read as a missing log entry -- it reads as tampering, and it
        # is indistinguishable from the real thing.
        #
        # **So the entry goes on first and is rolled back if anything downstream
        # refuses.** In memory the two lines are adjacent and nothing can fail
        # between them, which is exactly why the ordering costs nothing and is worth
        # having anyway: the next person to add a step between them inherits the safe
        # order rather than discovering it.
        entry = Change(at=now(), frm=was, to=to_state, by=by)
        item.history.append(entry)
        item.state = to_state

        # **Abort rather than half-apply.** If the result would not survive its own
        # audit, undo both and raise -- a board that fails `verify()` on the next load
        # is worse than a move that plainly did not happen.
        broken = [p for p in self.verify() if p.startswith(f"{item_id}:")]
        if broken:
            item.history.pop()
            item.state = was
            raise BoardError("; ".join(broken))
        return f"{item_id}: {was} → {to_state} (by {by})"

    def comment(self, item_id: str, text: str, by: Actor) -> str:
        """Leave a note on a card. **Either of us, any card, any state.**

        **Commenting is NOT permission-checked, and that is deliberate.** A comment
        changes nothing about who owns the work; refusing one would only stop the two of
        us talking on the card where the talking belongs.
        """
        if not text.strip():
            raise BoardError("a comment needs text")
        item = self.find(item_id)
        item.comments.append(Comment(at=now(), by=by, text=text.strip()))
        made, missing = self._links_from_text(item, text, by)
        note = ""
        if made:
            note += f"; references {', '.join(made)}"
        # **A TYPO IS REPORTED, NOT SWALLOWED.** Card #0028 asks what happens when the
        # named ticket does not exist, and silence is the wrong answer: the comment
        # stands either way, so a quiet miss looks exactly like a link that worked.
        if missing:
            note += f"; NO SUCH TICKET: {', '.join(missing)}"
        return f"{item_id}: comment by {by}{note}"

    # **`#0028` and `#28` both count, and a bare `28` does not.** Requiring the hash keeps
    # ordinary prose -- "28 tests", "step 12" -- from silently wiring cards together.
    TICKET_MENTION = re.compile(r"#(\d{1,6})\b")

    def _links_from_text(self, item: Item, text: str, by: Actor) -> tuple[list[str], list[str]]:
        """Add a `references` link for each `#nnnn` a comment names. Card #0028.

        **Terry's example, verbatim:** *"If I tag ticket 9876 in a comment with 'See
        #9876' that should add a 'references #9876' in source ticket auto add a
        'referenced by' relationship to ticket 9876."*

        **Only one row is written**, and card 9876 reads it as `referenced_by` through
        `LINK_INVERSE`. The second half needs no code.

        **A comment is append-only, so a link it created is never retracted here.** That
        answers the card's second open question by construction rather than by policy:
        there is no edit path that could trigger a retraction.

        **IT RUNS FOR HUMANS AND NOT FOR BOTS**, which is Terry's call and the right
        one: *"Can we turn auto link off for Claude and not Terry?"*

        **It asked `by != "terry"` until card #0073**, which was the same rule written as
        a name. That spelling silently excluded every future person: a second human's
        comments would have linked nothing, for no reason anybody could see. **The class
        comes from `rules.json` now** -- see `is_human` and card #0072.

        **The feature demonstrated the problem on its own first use.** Claude's closing
        comment on card #0028 mentioned four ticket numbers in passing -- one inside a
        code example -- and linked all four. **A person types `#0028` when they mean that
        card. A bot types it while explaining something**, in tables, in examples, in
        prose about other work.

        **A bot keeps `--link`, which is explicit.** Nothing is lost except the ability
        to create a relationship by accident.

        **An UNKNOWN actor is treated as a bot**, so a comment attributed to somebody no
        longer in the config links nothing rather than linking wrongly.
        """
        made: list[str] = []
        missing: list[str] = []
        if not any(user.id == by and user.user_class == HUMAN for user in self.users):
            return made, missing
        for hit in self.TICKET_MENTION.finditer(text):
            ref = hit.group(1)
            try:
                other = self.find(ref)
            except BoardError:
                label = f"#{int(ref):04d}"
                if label not in missing:
                    missing.append(label)
                continue
            if other.id == item.id:
                continue
            if self.find_link(item.id, "references", other.id) is not None:
                continue
            self.links.append(Link(frm=item.id, kind="references", to=other.id))
            made.append(other.label)
        return made, missing


def load(path: pathlib.Path, policy: TransitionPolicy | None = None) -> Board:
    """Read, parse and VALIDATE a board file."""
    raw = _read_json(pathlib.Path(path))
    return Board.from_json(raw, str(path), policy)


def service_descriptor_path(path: pathlib.Path) -> pathlib.Path:
    """Stable machine-local rendezvous path for one resolved board file."""
    identity = str(path.resolve()).casefold().encode("utf-8")
    name = hashlib.sha256(identity).hexdigest()[:20] + ".json"
    return pathlib.Path(tempfile.gettempdir()) / "claude-status" / name


# **How long to wait for another writer, and when to call a lock abandoned.**
# A real edit is a file read, a dict mutation and a 13 KB write -- microseconds. A
# second of patience covers any honest contention; ten means the holder is dead.
LOCK_WAIT_S = 1.0
LOCK_STALE_S = 10.0


@contextlib.contextmanager
def locked(path: pathlib.Path) -> Generator[None]:
    """Hold an exclusive lock on a board for the whole read-modify-write.

    **Terry asked whether this was needed:** *"are we (do we need to?) file locking
    as it's our 'Database'? ... if it's cheap seems like peace of mind to get atomic
    test and set."* **It is needed, and the race is real rather than theoretical.**

    **Two writers exist and both rewrite the WHOLE file.** Terry's drag goes through
    the server's `do_POST`; Claude's edits go through `board_state.py --move`. Each loads
    the entire board, mutates it and saves it. Overlap them and the second save
    silently discards everything the first one did -- a lost update, and the atomic
    rename in `save()` makes that outcome CLEANER rather than safer, because the
    file left behind is perfectly valid and simply missing a card's move.

    **`O_CREAT | O_EXCL` is the primitive**, because it is the one atomic
    test-and-set every filesystem agrees on, including the SMB share `X:` lives on.
    No dependency, and nothing to configure.

    **A stale lock is stolen rather than waited on forever.** A process killed
    mid-edit would otherwise wedge the board permanently, and a board that cannot be
    written is a worse failure than the race this prevents. The holder's pid is
    written into the file so an abandoned one can be identified.
    """
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # **Steal only a lock that is provably old.** Age is read from the lock
            # file itself, so a live holder refreshing nothing still keeps it for
            # LOCK_STALE_S -- far longer than any real edit takes.
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue  # It vanished between the two calls. Try again.
            if age > LOCK_STALE_S:
                lock.unlink(missing_ok=True)
                continue
            if time.monotonic() > deadline:
                raise BoardError(
                    f"{path.name} is locked by another writer "
                    f"(held {age:.1f}s). Nothing was changed."
                ) from None
            time.sleep(0.02)
            continue
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


@contextlib.contextmanager
def edit(
    path: pathlib.Path,
    policy: TransitionPolicy | None = None,
) -> Generator[Board]:
    """Load, hand over the board, then save -- all under one lock.

    **This is the ONLY correct way to change a board**, and both writers use it. A
    bare `load` / mutate / `save` is the lost-update race with extra steps.

    **The board is loaded INSIDE the lock**, which is the whole point: reading before
    acquiring would hand out a snapshot that another writer can invalidate before the
    save lands.
    """
    with locked(path):
        board = load(path, policy)
        yield board
        save(board, path)


def save(board: Board, path: pathlib.Path) -> None:
    """Write the board ATOMICALLY. The reader sees the old file or the new one.

    **`indent=2` and a trailing newline are not cosmetic.** A single-line JSON file
    turns every edit into one enormous diff, which throws away the reason the record
    lives in git at all.

    **The write goes to a temp file and is then RENAMED over the target**, because
    the obvious version destroys the board on a bad day. `open(path, "w")` truncates
    first and writes second, so a crash, a full disk or a killed process between
    those two leaves a half file -- and the loser is not the one change in flight, it
    is every card ever recorded.

    **`Path.replace` is atomic on Windows and POSIX alike**, which is why it is used
    rather than `shutil.move` or an unlink-then-rename.

    **`fsync` before the rename is the part people skip.** Without it the rename can
    reach the disk before the bytes do, and a power loss leaves a correctly named
    empty file -- the worst of both outcomes. The board is 13 KB and this happens on
    a human's drag, so the cost is irrelevant.

    **The temp file is in the SAME DIRECTORY on purpose.** A rename across
    filesystems is not atomic, and `X:` is an SMB share while the temp directory is
    not.
    """
    text = json.dumps(board.to_json(), indent=2, ensure_ascii=False) + "\n"
    target = pathlib.Path(path)
    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


# **MEASURED, not guessed: 7 failures in 400 saves, 1.75%.** Reproduced 2026-08-19 by
# hammering `save()` on the SMB share while a `api_endpoint.py` polled the same file.
#
# Roughly 47 saves a second there, so the window is small and real. `X:` is an SMB
# share and the server opens `board.json` every `POLL_MS = 400`; on Windows
# `os.replace` fails with `ERROR_ACCESS_DENIED` when the target is open in another
# process without `FILE_SHARE_DELETE`, which Python's `open()` does not request.
RENAME_TRIES = 6
RENAME_PAUSE = 0.04


def _replace_with_retry(tmp: pathlib.Path, target: pathlib.Path) -> None:
    """Rename the temp file over the target, retrying a transient WinError 5.

    **BOUNDED, and it RAISES at the end.** A swallowed failure would be a lost write
    reporting success, which is exactly the defect card #0032 exists to prevent.

    **It MUST NOT fall back to a non-atomic write.** A refused save is recoverable --
    the caller sees an exception and the old file is untouched. A half-written
    `board.json` is not, and it would take every card ever recorded with it.

    **Only `PermissionError` is retried.** A missing directory or a full disk will not
    fix itself in 40 ms, and retrying those would turn a clear failure into a slow one.
    """
    for attempt in range(RENAME_TRIES):
        try:
            tmp.replace(target)
        except PermissionError:
            if attempt == RENAME_TRIES - 1:
                raise
            time.sleep(RENAME_PAUSE)
        else:
            return


def _require_board_port_stopped(target: pathlib.Path, raw: JsonObject) -> None:
    """Refuse an offline migration while anything is listening on the board port."""
    port = raw.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or not MIN_PORT <= port <= MAX_PORT:
        raise BoardError(f"{target}: port {port!r} is not a TCP port number")
    try:
        connection = socket.create_connection(("127.0.0.1", port), timeout=0.25)
    except OSError:
        return
    connection.close()
    raise BoardError(f"board port {port} is listening; stop the service before migration")


def lane_slug(label: str) -> str:
    """Return the readable ASCII lane ID generated once during board initialization."""
    normalized = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", normalized.casefold()).strip("_")
    if not slug:
        raise BoardError(f"lane name {label!r} does not produce a nonempty ASCII slug")
    return slug


def _validate_lane_id(value: JsonValue, where: str) -> str:
    """Accept only the same canonical slug form initialization generates."""
    if not isinstance(value, str) or not value:
        raise BoardError(f"{where} is not a nonempty string")
    if lane_slug(value) != value:
        raise BoardError(f"{where} {value!r} is not a canonical lower-case underscore slug")
    return value


def _description_lanes(
    description: JsonObject, path: pathlib.Path
) -> tuple[list[JsonObject], dict[str, str]]:
    """Resolve lane display names to generated-once or explicitly imported IDs."""
    raw = description.get("lanes")
    if not isinstance(raw, list) or not raw:
        raise BoardError(f"{path}: 'lanes' is missing or empty")
    lanes: list[JsonObject] = []
    name_to_id: dict[str, str] = {}
    ids: set[str] = set()
    folded_names: set[str] = set()
    for index, row in enumerate(raw):
        where = f"{path}: lanes[{index}]"
        if not isinstance(row, dict):
            raise BoardError(f"{where} is not an object")
        unknown = set(row) - {"name", "id", "note"}
        if unknown:
            raise BoardError(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BoardError(f"{where}.name is not a nonempty string")
        name = name.strip()
        folded = name.casefold()
        if folded in folded_names:
            raise BoardError(f"{path}: lane names are not unique: {name!r}")
        folded_names.add(folded)
        lane_id = (
            _validate_lane_id(row.get("id"), f"{where}.id") if "id" in row else lane_slug(name)
        )
        if lane_id in ids:
            raise BoardError(f"{path}: lane slug collision at {name!r}: {lane_id!r}")
        ids.add(lane_id)
        name_to_id[name] = lane_id
        lane: JsonObject = {"id": lane_id, "label": name, "create": []}
        if "note" in row:
            note = row.get("note")
            if not isinstance(note, str):
                raise BoardError(f"{where}.note is not a string")
            lane["note"] = note
        lanes.append(lane)
    return lanes, name_to_id


def _permission_actor_map(
    description: JsonObject, path: pathlib.Path
) -> tuple[tuple[User, ...], dict[str, str], str, str, str]:
    """Resolve permission-file display names to stable board user IDs."""
    users = _parse_users(description, path)
    labels: dict[str, str] = {}
    folded: set[str] = set()
    for user in users:
        key = user.label.casefold()
        if key in folded:
            raise BoardError(f"{path}: user labels are not unique: {user.label!r}")
        folded.add(key)
        labels[user.label] = user.id
    browser = _pick_role(description, "browserUser", users, HUMAN, path)
    cli = _pick_role(description, "cliUser", users, BOT, path)
    default_owner = description.get("defaultOwner") or cli
    if not isinstance(default_owner, str) or default_owner not in {u.id for u in users}:
        raise BoardError(f"{path}: defaultOwner names unknown user {default_owner!r}")
    actor_ids = {browser, cli}
    permission_labels = {u.label: u.id for u in users if u.id in actor_ids}
    return users, permission_labels, browser, cli, default_owner


def _resolve_create_permissions(
    permissions: JsonObject,
    lanes: list[JsonObject],
    lane_ids: dict[str, str],
    actor_ids: dict[str, str],
    path: pathlib.Path,
) -> None:
    """Attach explicit name-based card-creation permissions to resolved lane rows."""
    create_raw = permissions.get("create")
    if not isinstance(create_raw, dict):
        raise BoardError(f"{path}: 'create' is missing or not an object")
    expected = set(lane_ids)
    if set(create_raw) != expected:
        details: list[str] = []
        if missing := sorted(expected - set(create_raw)):
            details.append("missing " + ", ".join(missing))
        if extra := sorted(set(create_raw) - expected):
            details.append("unknown " + ", ".join(extra))
        raise BoardError(f"{path}: create lanes do not match description ({'; '.join(details)})")
    lanes_by_name = {str(lane["label"]): lane for lane in lanes}
    for lane_name, actors_raw in create_raw.items():
        if not isinstance(actors_raw, list) or not all(
            isinstance(actor, str) for actor in actors_raw
        ):
            raise BoardError(f"{path}: create.{lane_name} is not a list of user names")
        actor_names = cast("list[str]", actors_raw)
        if unknown := [actor for actor in actor_names if actor not in actor_ids]:
            raise BoardError(
                f"{path}: create.{lane_name} names unknown actor(s) " + ", ".join(unknown)
            )
        lanes_by_name[lane_name]["create"] = cast(
            "list[JsonValue]", [actor_ids[actor] for actor in actor_names]
        )


def _resolve_move_permissions(
    permissions: JsonObject,
    lane_ids: dict[str, str],
    actor_ids: dict[str, str],
    path: pathlib.Path,
) -> JsonObject:
    """Map display-name permission endpoints to their persisted IDs once."""
    moves_raw = permissions.get("moves")
    if not isinstance(moves_raw, dict) or set(moves_raw) != set(actor_ids):
        raise BoardError(f"{path}: moves must contain exactly: {', '.join(sorted(actor_ids))}")
    edges: JsonObject = {}
    for actor_name, entries_raw in moves_raw.items():
        if not isinstance(entries_raw, list):
            raise BoardError(f"{path}: moves.{actor_name} is not a list")
        resolved: list[JsonValue] = []
        for index, entry in enumerate(entries_raw):
            where = f"{path}: moves.{actor_name}[{index}]"
            if not isinstance(entry, dict):
                raise BoardError(f"{where} is not an object")
            unknown = set(entry) - {"from", "to", "description"}
            if unknown:
                raise BoardError(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")
            source, destination = entry.get("from"), entry.get("to")
            if not isinstance(source, str) or not isinstance(destination, str):
                raise BoardError(f"{where} must contain string from and to names")
            if source not in lane_ids or destination not in lane_ids:
                raise BoardError(f"{where} must name lanes from the description")
            edge: JsonObject = {"from": lane_ids[source], "to": lane_ids[destination]}
            if "description" in entry:
                edge["description"] = entry["description"]
            resolved.append(edge)
        edges[actor_ids[actor_name]] = resolved
    return edges


def _resolved_policy(
    description: JsonObject,
    permissions: JsonObject,
    description_path: pathlib.Path,
    permissions_path: pathlib.Path,
) -> tuple[JsonObject, tuple[User, ...], str, str, str]:
    """Resolve human-readable initialization inputs into one persisted policy."""
    if description.get("schema") != DESCRIPTION_SCHEMA:
        raise BoardError(
            f"{description_path}: description schema {description.get('schema')!r}, "
            f"want {DESCRIPTION_SCHEMA}"
        )
    if permissions.get("schema") != PERMISSIONS_SCHEMA:
        raise BoardError(
            f"{permissions_path}: permissions schema {permissions.get('schema')!r}, "
            f"want {PERMISSIONS_SCHEMA}"
        )
    lanes, lane_ids = _description_lanes(description, description_path)
    users, actor_ids, browser, cli, default_owner = _permission_actor_map(
        description, description_path
    )
    _resolve_create_permissions(permissions, lanes, lane_ids, actor_ids, permissions_path)
    edges = _resolve_move_permissions(permissions, lane_ids, actor_ids, permissions_path)
    priorities = description.get("priorities")
    policy: JsonObject = {
        "schema": RULES_SCHEMA,
        "note": (
            "Resolved once from board description and name-based permissions during initialization."
        ),
        "priorities": copy.deepcopy(priorities),
        "lanes": cast("list[JsonValue]", lanes),
        "edges": edges,
    }
    if "defaultPriority" in description:
        policy["defaultPriority"] = description["defaultPriority"]
    TransitionPolicy.from_json(policy, f"{description_path}: resolved policy")
    return policy, users, browser, cli, default_owner


def initialize_board(
    target: pathlib.Path, description_path: pathlib.Path, permissions_path: pathlib.Path
) -> str:
    """Create one self-contained board without ever regenerating its lane IDs."""
    target = target.resolve()
    if target.exists():
        raise BoardError(f"{target}: refusing to overwrite an existing board")
    description_raw = _read_json(description_path)
    permissions_raw = _read_json(permissions_path)
    if not isinstance(description_raw, dict):
        raise BoardError(f"{description_path}: description is not an object")
    if not isinstance(permissions_raw, dict):
        raise BoardError(f"{permissions_path}: permissions document is not an object")
    policy, users, browser, cli, default_owner = _resolved_policy(
        description_raw, permissions_raw, description_path, permissions_path
    )
    board_raw: JsonObject = {
        "schema": SCHEMA,
        "revision": 0,
        "project": description_raw.get("project"),
        "port": description_raw.get("port", DEFAULT_PORT),
        "policy": policy,
        "users": [
            {
                "id": user.id,
                "label": user.label,
                "class": user.user_class,
                "color": user.color,
            }
            for user in users
        ],
        "browserUser": browser,
        "cliUser": cli,
        "defaultOwner": default_owner,
        "nextTicket": 1,
        "items": [],
    }
    board = Board.from_json(board_raw, str(target))
    with locked(target):
        if target.exists():
            raise BoardError(f"{target}: another initializer created the board first")
        save(board, target)
    return f"initialized {target} with {len(board.policy.states)} stable lane IDs"


def embed_policy(path: pathlib.Path, policy_path: pathlib.Path) -> str:
    """Upgrade a schema-3 board by atomically embedding its resolved policy."""
    target = path.resolve()
    policy = TransitionPolicy.load(policy_path)
    with locked(target):
        raw = _read_json(target)
        if not isinstance(raw, dict):
            raise BoardError(f"{target}: top level is not an object")
        if raw.get("schema") != EMBEDDED_POLICY_PREVIOUS_SCHEMA:
            raise BoardError(
                f"{target}: policy embedding reads schema {EMBEDDED_POLICY_PREVIOUS_SCHEMA}, "
                f"found {raw.get('schema')!r}"
            )
        _require_board_port_stopped(target, raw)
        raw["schema"] = SCHEMA
        raw["policy"] = policy.to_json()
        migrated = Board.from_json(raw, str(target))
        if problems := migrated.verify():
            raise BoardError(
                f"{target}: embedded-policy board does not verify: {'; '.join(problems)}"
            )
        migrated.revision += 1
        save(migrated, target)
    return (
        f"embedded {len(policy.states)} lanes; schema 3 -> {SCHEMA}; revision {migrated.revision}"
    )


def _policy_document(raw: JsonObject, target: pathlib.Path) -> JsonObject:
    policy = raw.get("policy")
    if not isinstance(policy, dict):
        raise BoardError(f"{target}: 'policy' is missing or not an object")
    return policy


def rename_lane_label(path: pathlib.Path, lane_id: str, label: str) -> str:
    """Rename only a lane's display label, preserving its identity and audit history."""
    target = path.resolve()
    label = label.strip()
    if not label:
        raise BoardError("lane label is empty")
    with locked(target):
        raw = _read_json(target)
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            raise BoardError(f"{target}: label rename needs a schema-{SCHEMA} board")
        _require_board_port_stopped(target, raw)
        policy = _policy_document(raw, target)
        lanes = policy.get("lanes")
        if not isinstance(lanes, list):
            raise BoardError(f"{target}: policy.lanes is not a list")
        matches = [lane for lane in lanes if isinstance(lane, dict) and lane.get("id") == lane_id]
        if len(matches) != 1:
            raise BoardError(f"{target}: policy has no unique lane id {lane_id!r}")
        previous = matches[0].get("label")
        matches[0]["label"] = label
        changed = Board.from_json(raw, str(target))
        changed.revision += 1
        save(changed, target)
    return f"lane {lane_id}: label {previous!r} -> {label!r}; revision {changed.revision}"


def _rewrite_policy_lane_id(
    policy: JsonObject, target: pathlib.Path, source: str, destination: str
) -> int:
    """Rewrite one lane identity and all exact permission endpoints in memory."""
    lanes = policy.get("lanes")
    if not isinstance(lanes, list):
        raise BoardError(f"{target}: policy.lanes is not a list")
    lane_rows = [lane for lane in lanes if isinstance(lane, dict)]
    if any(lane.get("id") == destination for lane in lane_rows):
        raise BoardError(f"{target}: destination lane id {destination!r} already exists")
    matches = [lane for lane in lane_rows if lane.get("id") == source]
    if len(matches) != 1:
        raise BoardError(f"{target}: policy has no unique lane id {source!r}")
    matches[0]["id"] = destination
    replacements = 1
    edges = policy.get("edges")
    if not isinstance(edges, dict):
        raise BoardError(f"{target}: policy.edges is not an object")
    for entries in edges.values():
        if not isinstance(entries, list):
            raise BoardError(f"{target}: policy edge actor value is not a list")
        for edge in entries:
            if not isinstance(edge, dict):
                raise BoardError(f"{target}: policy edge is not an object")
            for key in ("from", "to"):
                if edge.get(key) == source:
                    edge[key] = destination
                    replacements += 1
    return replacements


def migrate_lane_id(path: pathlib.Path, source: str, destination: str) -> str:
    """Atomically migrate one embedded lane ID across policy, state, and audit history."""
    target = path.resolve()
    destination = _validate_lane_id(destination, "destination lane id")
    if source == destination:
        raise BoardError("lane migration needs two different IDs")
    with locked(target):
        raw = _read_json(target)
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            raise BoardError(f"{target}: lane-ID migration needs a schema-{SCHEMA} board")
        _require_board_port_stopped(target, raw)
        policy = _policy_document(raw, target)
        policy_replacements = _rewrite_policy_lane_id(policy, target, source, destination)
        board_replacements = _rewrite_lane_values(target, raw, source, destination)
        migrated = Board.from_json(raw, str(target))
        if problems := migrated.verify():
            raise BoardError(f"{target}: migrated history does not replay: {'; '.join(problems)}")
        migrated.revision += 1
        save(migrated, target)
    return (
        f"lane id {source} -> {destination}; replaced {policy_replacements} policy and "
        f"{board_replacements} board value(s); revision {migrated.revision}"
    )


def _rewrite_lane_values(
    target: pathlib.Path, raw: JsonObject, source: str, destination: str
) -> int:
    """Replace exact current-state and lane-history endpoint values in memory."""
    items = raw.get("items")
    if not isinstance(items, list):
        raise BoardError(f"{target}: 'items' is missing or not a list")
    replacements = 0
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            raise BoardError(f"{target}: items[{item_index}] is not an object")
        if item.get("state") == source:
            item["state"] = destination
            replacements += 1
        history = item.get("history", [])
        if not isinstance(history, list):
            raise BoardError(f"{target}: items[{item_index}].history is not a list")
        for change_index, change in enumerate(history):
            if not isinstance(change, dict):
                raise BoardError(
                    f"{target}: items[{item_index}].history[{change_index}] is not an object"
                )
            for key in ("from", "to"):
                if change.get(key) == source:
                    change[key] = destination
                    replacements += 1
    return replacements


def migrate_lane(path: pathlib.Path, source: str, destination: str) -> str:
    """Migrate one schema-2 lane ID and its audit endpoints while the service is off.

    This is deliberately an offline, explicit CLI migration. Lane IDs occur in both
    each card's current state and the append-only lane history, so changing only the
    current state would make verification fail and changing only the policy would make
    the board unreadable. The original file remains untouched unless the complete
    schema-4 result with its embedded policy validates and replays cleanly.
    """
    target = pathlib.Path(path)
    if not source or not destination or source == destination:
        raise BoardError("lane migration needs two different nonempty lane IDs")
    if source in _POLICY.states:
        raise BoardError(f"source lane {source!r} is still active; refusing ambiguous migration")
    if destination not in _POLICY.states:
        raise BoardError(f"destination lane {destination!r} is not in the active rules")

    with locked(target):
        raw = _read_json(target)
        if not isinstance(raw, dict):
            raise BoardError(f"{target}: top level is not an object")
        if raw.get("schema") != PREVIOUS_BOARD_SCHEMA:
            raise BoardError(
                f"{target}: migration reads schema {PREVIOUS_BOARD_SCHEMA}, "
                f"found {raw.get('schema')!r}"
            )
        _require_board_port_stopped(target, raw)
        replacements = _rewrite_lane_values(target, raw, source, destination)
        raw["schema"] = SCHEMA
        raw["policy"] = _POLICY.to_json()
        migrated = Board.from_json(raw, str(target))
        if problems := migrated.verify():
            raise BoardError(f"{target}: migrated history does not replay: {'; '.join(problems)}")
        migrated.revision += 1
        save(migrated, target)

    return (
        f"migrated schema {PREVIOUS_BOARD_SCHEMA} -> {SCHEMA}; replaced "
        f"{replacements} lane value(s); revision {migrated.revision}"
    )


def _service_descriptor(path: pathlib.Path) -> JsonObject:
    """Read and validate the local service rendezvous file."""
    service = service_descriptor_path(path)
    try:
        raw = cast("JsonValue", json.loads(service.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise BoardError(
            f"board service is not running ({service} is unavailable); nothing changed"
        ) from exc
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise BoardError(f"{service}: unsupported service descriptor")
    host, port, token = raw.get("host"), raw.get("port"), raw.get("token")
    if host != "127.0.0.1" or not isinstance(port, int) or not isinstance(token, str):
        raise BoardError(f"{service}: invalid loopback service endpoint")
    return raw


def _service_json(
    url: str,
    token: str,
    *,
    body: dict[str, object] | None = None,
    revision: int | None = None,
    timeout: float = 5,
) -> JsonObject:
    """Make one authenticated service request and turn refusals into BoardError."""
    headers = {"Authorization": f"Bearer {token}"}
    data: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        if revision is not None:
            headers["If-Match"] = f'"revision-{revision}"'
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 -- URL comes from validated loopback data
        url, data=data, headers=headers, method="POST" if body is not None else "GET"
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 -- only the loopback descriptor is accepted
            request, timeout=timeout
        ) as response:
            raw = cast("JsonValue", json.loads(response.read()))
    except urllib.error.HTTPError as exc:
        try:
            error_raw = cast("JsonValue", json.loads(exc.read()))
            error = error_raw.get("error", str(exc)) if isinstance(error_raw, dict) else str(exc)
        except ValueError:
            error = str(exc)
        raise BoardError(f"board service refused the command: {error}") from exc
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise BoardError(f"could not reach board service; nothing changed: {exc}") from exc
    if not isinstance(raw, dict):
        raise BoardError("board service returned a non-object response")
    return raw


def shutdown_service(path: pathlib.Path) -> str:
    """Ask one live board service to flush autopush and stop, then await cleanup."""
    service_path = service_descriptor_path(path)
    if not service_path.exists():
        return "board service is already stopped"
    descriptor = _service_descriptor(path)
    base = f"http://{descriptor['host']}:{descriptor['port']}"
    response = _service_json(
        base + API_PREFIX + "/shutdown",
        str(descriptor["token"]),
        body={},
        timeout=130,
    )
    result = response.get("result")
    if not isinstance(result, str):
        raise BoardError("board service returned no shutdown result")
    deadline = time.monotonic() + 10
    while service_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if service_path.exists():
        raise BoardError("board service accepted shutdown but did not stop cleanly")
    return result


def _remote_apply(path: pathlib.Path, args: argparse.Namespace) -> str:  # noqa: PLR0912
    """Translate one CLI mutation into the same REST command the browser uses."""
    descriptor = _service_descriptor(path)
    base = f"http://{descriptor['host']}:{descriptor['port']}"
    token = str(descriptor["token"])
    snapshot = _service_json(base + API_PREFIX + "/board", token)
    revision = snapshot.get("revision")
    if not isinstance(revision, int):
        raise BoardError("board service response has no revision")

    route: str
    body: dict[str, object]

    def quote(value: object) -> str:
        return urllib.parse.quote(str(value), safe="")

    if args.create:
        route = API_PREFIX + "/cards"
        body = {
            "id": args.create[0],
            "subject": args.create[1],
            "state": args.state,
            "priority": args.priority,
            "detail": _detail_text(args),
            "owner": args.owner or DEFAULT_OWNER,
        }
    elif args.set_project:
        route, body = API_PREFIX + "/board/project", {"project": args.set_project}
    elif args.move:
        route, body = f"{API_PREFIX}/cards/{quote(args.move[0])}/move", {"to": args.move[1]}
    elif args.comment:
        route, body = (
            f"{API_PREFIX}/cards/{quote(args.comment[0])}/comment",
            {"text": args.comment[1]},
        )
    elif args.assign:
        route, body = (
            f"{API_PREFIX}/cards/{quote(args.assign[0])}/assign",
            {"owner": args.assign[1]},
        )
    elif args.set_priority:
        route, body = (
            f"{API_PREFIX}/cards/{quote(args.set_priority[0])}/priority",
            {"priority": args.set_priority[1]},
        )
    elif args.set_detail:
        route, body = (
            f"{API_PREFIX}/cards/{quote(args.set_detail)}/detail",
            {"detail": _detail_text(args)},
        )
    elif args.set_subject:
        route, body = (
            f"{API_PREFIX}/cards/{quote(args.set_subject[0])}/subject",
            {"subject": args.set_subject[1]},
        )
    elif args.link or args.unlink:
        values = args.link or args.unlink
        route, body = (
            f"{API_PREFIX}/cards/{quote(values[0])}/link",
            {"kind": values[1], "other": values[2], "remove": bool(args.unlink)},
        )
    elif args.set_parent:
        route, body = (
            f"{API_PREFIX}/cards/{quote(args.set_parent[0])}/parent",
            {"parent": args.set_parent[1]},
        )
    elif args.clear_parent:
        route, body = (f"{API_PREFIX}/cards/{quote(args.clear_parent)}/parent", {"parent": None})
    else:
        raise BoardError("no mutation requested")

    response = _service_json(base + route, token, body=body, revision=revision)
    result = response.get("result")
    if not isinstance(result, str):
        raise BoardError("board service returned no result")
    return result


def _add_offline_arguments(parser: argparse.ArgumentParser) -> None:
    """Keep setup and structural-migration flags out of the daily-use parser body."""
    parser.add_argument(
        "--init",
        nargs=2,
        type=pathlib.Path,
        metavar=("DESCRIPTION", "PERMISSIONS"),
        help="initialize a new self-contained board from name-based JSON inputs",
    )
    parser.add_argument(
        "--embed-policy",
        type=pathlib.Path,
        metavar="RULES",
        help="offline schema-3 upgrade that embeds one resolved rules file",
    )
    parser.add_argument(
        "--migrate-lane",
        nargs=2,
        metavar=("OLD", "NEW"),
        help="offline schema-2 lane-ID and embedded-policy migration",
    )
    parser.add_argument(
        "--rename-lane-label",
        nargs=2,
        metavar=("ID", "LABEL"),
        help="offline display-label rename that preserves the lane ID",
    )
    parser.add_argument(
        "--migrate-lane-id",
        nargs=2,
        metavar=("OLD", "NEW"),
        help="offline atomic lane-ID migration across policy, cards, and history",
    )


def _add_activity_arguments(parser: argparse.ArgumentParser) -> None:
    """Keep the mutually exclusive read-window flags together."""
    activity = parser.add_mutually_exclusive_group()
    activity.add_argument(
        "--activity-since",
        metavar="RFC3339_TIMESTAMP",
        help="list sanitized events at or after one RFC 3339 timestamp",
    )
    activity.add_argument(
        "--activity-between",
        nargs=2,
        metavar=("START", "END"),
        help="list sanitized events in one inclusive RFC 3339 time window",
    )


def _positive_count(raw: str) -> int:
    """Parse a positive CLI result limit."""
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _add_inspection_arguments(parser: argparse.ArgumentParser) -> None:
    """Keep focused reads together and make detail/comment exposure explicit."""
    parser.add_argument(
        "--show",
        metavar="REF",
        help="show one card by stable ID or ticket number, including relationships",
    )
    parser.add_argument(
        "--search",
        metavar="QUERY",
        help="find cards by ID, ticket, or subject; --include-prose searches prose too",
    )
    parser.add_argument(
        "--next",
        type=_positive_count,
        metavar="N",
        help="show N cards from --lanes in policy-priority then ticket order",
    )
    parser.add_argument(
        "--lanes",
        nargs="+",
        metavar="LANE",
        help="lane IDs included in a --next or --search query",
    )
    parser.add_argument(
        "--include-prose",
        action="store_true",
        help="include detail and comment text in focused inspection output",
    )


def _activity_requested(args: argparse.Namespace) -> bool:
    """Whether this invocation selects either activity report."""
    return bool(args.activity_since or args.activity_between)


def _validate_activity_arguments(args: argparse.Namespace) -> None:
    """Refuse activity queries combined with any write or integrity operation."""
    if not _activity_requested(args):
        return
    if (
        args.verify
        or any(getattr(args, name) for name in MUTATIONS)
        or any(
            (
                args.init,
                args.embed_policy,
                args.migrate_lane,
                args.rename_lane_label,
                args.migrate_lane_id,
                args.state,
                args.priority,
                args.owner,
                args.detail,
                args.detail_file,
                args.show,
                args.search is not None,
                args.next,
                args.lanes,
                args.include_prose,
            )
        )
    ):
        raise BoardError("activity queries cannot be combined with writes, migrations, or --verify")


def _validate_inspection_arguments(args: argparse.Namespace) -> None:
    """Keep focused inspection read-only and require an explicit content request."""
    if args.show and (args.next is not None or args.search is not None or args.lanes):
        if args.search is not None:
            raise BoardError("--show and --search cannot be combined")
        raise BoardError("--show and --next cannot be combined")
    if args.next is not None and args.search is not None:
        raise BoardError("--next and --search cannot be combined")
    if args.next is None and args.search is None and args.lanes:
        raise BoardError("--lanes requires --next or --search")
    if args.next is not None and not args.lanes:
        raise BoardError("--next requires --lanes")
    if args.include_prose and not (args.show or args.next is not None or args.search is not None):
        raise BoardError("--include-prose requires --show, --next, or --search")

    shared_conflict = (
        args.shutdown
        or args.verify
        or _activity_requested(args)
        or any(getattr(args, name) for name in MUTATIONS)
        or any(
            (
                args.init,
                args.embed_policy,
                args.migrate_lane,
                args.rename_lane_label,
                args.migrate_lane_id,
                args.state,
                args.priority,
                args.owner,
                args.detail,
                args.detail_file,
            )
        )
    )
    if args.show and shared_conflict:
        raise BoardError("--show cannot be combined with writes, migrations, activity, or --verify")
    if args.next is not None and shared_conflict:
        raise BoardError("--next cannot be combined with writes, migrations, activity, or --verify")
    if args.search is not None and shared_conflict:
        raise BoardError(
            "--search cannot be combined with writes, migrations, activity, or --verify"
        )


def _run_offline_operation(args: argparse.Namespace) -> str | None:
    """Validate and execute exactly one requested offline operation."""
    operations = (
        args.init,
        args.embed_policy,
        args.migrate_lane,
        args.rename_lane_label,
        args.migrate_lane_id,
    )
    if not any(operations):
        return None
    conflict = (
        sum(bool(operation) for operation in operations) != 1
        or args.shutdown
        or args.json
        or args.verify
        or any(getattr(args, name) for name in MUTATIONS)
        or args.state
        or args.priority
        or args.owner
        or args.detail
        or args.detail_file
        or args.activity_since
        or args.activity_between
        or args.show
        or args.search is not None
        or args.next
        or args.lanes
        or args.include_prose
    )
    if conflict:
        raise BoardError("offline board setup and migration flags cannot be combined")
    if args.init:
        return initialize_board(args.board, *args.init)
    if args.embed_policy:
        return embed_policy(args.board, args.embed_policy)
    if args.migrate_lane:
        return migrate_lane(args.board, *args.migrate_lane)
    if args.rename_lane_label:
        return rename_lane_label(args.board, *args.rename_lane_label)
    return migrate_lane_id(args.board, *args.migrate_lane_id)


def _run_shutdown_operation(args: argparse.Namespace) -> str | None:
    """Validate and execute the mutually exclusive live-service shutdown operation."""
    if not args.shutdown:
        return None
    conflict = (
        args.json
        or args.verify
        or any(getattr(args, name) for name in MUTATIONS)
        or any(
            (
                args.init,
                args.embed_policy,
                args.migrate_lane,
                args.rename_lane_label,
                args.migrate_lane_id,
                args.state,
                args.priority,
                args.owner,
                args.detail,
                args.detail_file,
                args.activity_since,
                args.activity_between,
                args.show,
                args.search is not None,
                args.next,
                args.lanes,
                args.include_prose,
            )
        )
    )
    if conflict:
        raise BoardError("--shutdown cannot be combined with another operation")
    return shutdown_service(args.board.resolve())


def _handle_shutdown_operation(args: argparse.Namespace) -> bool:
    """Run and report shutdown when requested, returning whether CLI work is complete."""
    try:
        result = _run_shutdown_operation(args)
    except BoardError as exc:
        raise SystemExit(f"  ERROR: {exc}") from None
    if result is None:
        return False
    print(f"  {result}")
    return True


def _handle_remote_mutation(args: argparse.Namespace, parser: argparse.ArgumentParser) -> bool:
    """Validate, execute, and report one live-service mutation when requested."""
    if args.create and not args.state:
        parser.error("--create needs --state")
    if not any(getattr(args, name) for name in MUTATIONS):
        return False
    try:
        result = _remote_apply(args.board.resolve(), args)
    except BoardError as exc:
        raise SystemExit(f"  ERROR: {exc}") from None
    print(f"  {result}")
    return True


def _handle_inspection_report(
    board: Board,
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> bool:
    """Run one focused inspection report when requested."""
    try:
        if args.show:
            _report_item(
                board,
                str(args.show),
                as_json=bool(args.json),
                include_prose=bool(args.include_prose),
            )
            return True
        if args.search is not None:
            _report_search_items(
                board,
                str(args.search),
                cast("list[str] | None", args.lanes),
                as_json=bool(args.json),
                include_prose=bool(args.include_prose),
            )
            return True
        if args.next is not None:
            _report_next_items(
                board,
                cast("list[str]", args.lanes),
                int(args.next),
                as_json=bool(args.json),
                include_prose=bool(args.include_prose),
            )
            return True
    except BoardError as exc:
        parser.error(str(exc))
    return False


def _configure_cli_streams() -> None:
    """Make every CLI result representable regardless of the inherited locale.

    localswim deliberately prints Unicode, including the move arrow. On Windows a
    redirected console process can inherit CP1252; the service may then commit a move
    successfully before the CLI raises ``UnicodeEncodeError`` while reporting it. The
    command owns its text protocol, so establish UTF-8 before argparse or any result
    writes to stdout or stderr.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="strict")


def main() -> None:
    """A small CLI, so a board can be inspected and moved without the browser."""
    _configure_cli_streams()
    ap = argparse.ArgumentParser(description="Inspect or update a localswim board.")
    ap.add_argument("board", type=pathlib.Path, help="path to the board JSON")
    ap.add_argument(
        "--shutdown",
        action="store_true",
        help="flush autopush and stop the live board service",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="emit JSON for the selected report, or dump the parsed board",
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="replay every history and refuse a state it disagrees with",
    )
    _add_activity_arguments(ap)
    _add_inspection_arguments(ap)
    _add_offline_arguments(ap)
    ap.add_argument("--move", nargs=2, metavar=("ID", "STATE"), help="move one card")
    ap.add_argument(
        "--comment", nargs=2, metavar=("ID", "TEXT"), help="leave a comment on one card"
    )
    # **Both actors are offered, unlike `--state` above.** Ownership is a LABEL rather
    # than a permission -- card #0053 -- so either actor may be assigned either way and
    # there is no lane-style restriction to encode here.
    ap.add_argument(
        "--assign",
        nargs=2,
        metavar=("ID", "OWNER"),
        help="reassign one card's owner to a configured user",
    )
    # **Board METADATA gets a flag rather than a hand edit.** Card #0050. The standing
    # order is that Claude writes to the board THROUGH THE LIBRARY, and the usual
    # reasons -- `may_create`, `nextTicket`, the creation history entry -- do not reach
    # a metadata field. **The rule still wins, for a different reason:** `--verify`
    # replays card histories and cannot catch a bad metadata edit at all, which is an
    # argument for keeping hands out of the file rather than a license to reach in.
    #
    # **`port` has the same shape and will want the same treatment.**
    ap.add_argument("--set-project", metavar="NAME", help="rename the board's project field")
    # **Priority was set-once until 2026-08-19.** `--priority` above only decorates
    # `--create`, so a card filed at the wrong priority could not be corrected from the
    # CLI at all -- and the drawer has no control for it either. Terry asked to move a
    # card to P1 and there was no way to do it. Card #0060.
    ap.add_argument(
        "--set-priority", nargs=2, metavar=("ID", "PRIORITY"), help="change one card's priority"
    )
    # **A card's description was WRITE-ONCE until 2026-08-19.** Terry read one and
    # said *"wall of text ELI5, try again in human readable fashion"* -- and there was
    # no way to try again. Only comments could be added, so a bad description could be
    # apologized for and never fixed.
    #
    # **Takes its text from `--detail` or `--detail-file`, the same pair `--create`
    # uses**, so the shell-quoting lesson is inherited rather than repeated.
    ap.add_argument(
        "--set-detail",
        metavar="ID",
        help="replace one card's description; use --detail or --detail-file",
    )
    # **Card #0081.** The web drawer is the surface Terry asked for; this exists so the
    # CLI is not the one place a rename is impossible, which is the asymmetry `/priority`
    # already closed in the other direction.
    ap.add_argument(
        "--set-subject",
        nargs=2,
        metavar=("ID", "TEXT"),
        help="rename one card; the id and ticket number do not move",
    )
    # **Card #0069, and it is the CLI half of the dialog's new picker.** The default
    # stays `claude` because that is what `Item.owner` already did; his standing rule is
    # *"if in doubt, assign to claude."*
    ap.add_argument(
        "--owner", default="", help="owner for --create; defaults to the board's defaultOwner"
    )
    # **Card #0028. Both cards in one call, always.** The relationship is stored once and
    # the other direction is derived, so there is no call shape that writes half of one.
    ap.add_argument(
        "--link",
        nargs=3,
        metavar=("ID", "KIND", "OTHER"),
        help=f"relate two cards; KIND is one of {', '.join(sorted(LINK_INVERSE))}",
    )
    ap.add_argument(
        "--unlink",
        nargs=3,
        metavar=("ID", "KIND", "OTHER"),
        help="remove a relationship between two cards",
    )
    ap.add_argument(
        "--set-parent",
        nargs=2,
        metavar=("CHILD", "PARENT"),
        help="put one card under another; refuses a cycle",
    )
    ap.add_argument("--clear-parent", metavar="CHILD", help="move a card back to the top level")
    ap.add_argument(
        "--create", nargs=2, metavar=("ID", "SUBJECT"), help="add one card; needs --state"
    )
    # Lane and priority values are board-specific under schema 4. The running service
    # validates them against the board's embedded policy; argparse cannot know them.
    ap.add_argument("--state", help="the lane ID for --create")
    ap.add_argument(
        "--priority",
        default=None,
        help="priority for --create; defaults to the board policy",
    )
    detail = ap.add_mutually_exclusive_group()
    detail.add_argument("--detail", default="", help="description for --create")
    detail.add_argument(
        "--detail-file", type=pathlib.Path, help="read the description from a file instead"
    )
    args = ap.parse_args()

    try:
        _validate_activity_arguments(args)
        _validate_inspection_arguments(args)
        offline_result = _run_offline_operation(args)
    except BoardError as exc:
        ap.error(str(exc))
    if offline_result is not None:
        print(f"  {offline_result}")
        return

    if _handle_shutdown_operation(args):
        return

    if _handle_remote_mutation(args, ap):
        return

    board = load(args.board)
    if _handle_inspection_report(board, args, ap):
        return
    if _activity_requested(args):
        try:
            bounds = _activity_bounds(args)
            _report_activity(board, *bounds, as_json=bool(args.json))
        except BoardError as exc:
            ap.error(str(exc))
        return
    _report(board, args)


# **ONE TABLE, so a write flag cannot be added in one place and forgotten in another.**
#
# This replaced a literal `if args.move or args.comment or args.create:` in `main` plus
# a matching if-chain here. **`--assign` was added to argparse and to the chain and NOT
# to the condition on 2026-08-19**, so it fell through to the REPORT path: it printed
# the whole board and exited 0. A write that silently became a read, and reported
# success.
#
# These are the argparse destination names that mutate through the service. Keeping
# one immutable tuple makes the dispatch boundary explicit and type-checkable.
MUTATIONS = (
    "create",
    "move",
    "assign",
    "comment",
    "set_project",
    "set_priority",
    "set_detail",
    "set_subject",
    "link",
    "unlink",
    "set_parent",
    "clear_parent",
)


@dataclass(frozen=True)
class ItemSummary:
    """The default coordination fields needed to identify one related card."""

    item_id: str
    ticket: int
    subject: str
    state: str
    priority: str
    owner: str

    @classmethod
    def from_item(cls, item: Item) -> Self:
        """Build one summary from a validated board item."""
        return cls(
            item_id=item.id,
            ticket=item.ticket,
            subject=item.subject,
            state=item.state,
            priority=item.priority,
            owner=item.owner,
        )

    @property
    def label(self) -> str:
        """Return the human ticket label without requiring the source item."""
        return f"#{self.ticket:04d}"

    def to_json(self) -> JsonObject:
        """Return the focused CLI JSON shape for one card reference."""
        return {
            "id": self.item_id,
            "ticket": self.ticket,
            "subject": self.subject,
            "state": self.state,
            "priority": self.priority,
            "owner": self.owner,
        }

    def describe(self) -> str:
        """Return one compact relationship endpoint for human output."""
        return (
            f"{self.label} {self.item_id} [{self.state}, {self.priority}, owner {self.owner}] "
            f"{self.subject}"
        )


@dataclass(frozen=True)
class ItemRelationship:
    """One directional relationship as the inspected card sees it."""

    kind: str
    item: ItemSummary

    def to_json(self) -> JsonObject:
        """Return a relationship and its resolved opposite endpoint."""
        return {"kind": self.kind, "item": cast("JsonValue", self.item.to_json())}


@dataclass(frozen=True)
class ItemInspection:
    """One focused report with detail and comments omitted unless requested."""

    item: ItemSummary
    comment_count: int
    parent: ItemSummary | None
    children: tuple[ItemSummary, ...]
    relationships: tuple[ItemRelationship, ...]
    detail: str | None = None
    comments: tuple[Comment, ...] | None = None

    def to_json(self) -> JsonObject:
        """Return a composable focused report without implicit prose exposure."""
        out = self.item.to_json()
        out["commentCount"] = self.comment_count
        out["parent"] = (
            cast("JsonValue", self.parent.to_json()) if self.parent is not None else None
        )
        out["children"] = [cast("JsonValue", child.to_json()) for child in self.children]
        out["relationships"] = [
            cast("JsonValue", relationship.to_json()) for relationship in self.relationships
        ]
        if self.detail is not None:
            out["detail"] = self.detail
        if self.comments is not None:
            out["comments"] = [cast("JsonValue", comment.to_json()) for comment in self.comments]
        return out


def _next_item_json(inspection: ItemInspection) -> JsonObject:
    """Return compact triage JSON without expanding a large child subtree."""
    out = inspection.item.to_json()
    out["commentCount"] = inspection.comment_count
    out["parent"] = (
        cast("JsonValue", inspection.parent.to_json()) if inspection.parent is not None else None
    )
    out["childCount"] = len(inspection.children)
    out["relationships"] = [
        cast("JsonValue", relationship.to_json()) for relationship in inspection.relationships
    ]
    if inspection.detail is not None:
        out["detail"] = inspection.detail
    if inspection.comments is not None:
        out["comments"] = [cast("JsonValue", comment.to_json()) for comment in inspection.comments]
    return out


def inspect_item(board: Board, ref: str, *, include_prose: bool = False) -> ItemInspection:
    """Resolve one card plus its parent, children, and directional relationships."""
    item = board.find(ref)
    parent = ItemSummary.from_item(board.find(item.parent)) if item.parent else None
    children = tuple(
        ItemSummary.from_item(child)
        for child in sorted(
            (candidate for candidate in board.items if candidate.parent == item.id),
            key=lambda candidate: (candidate.ticket, candidate.id),
        )
    )
    relationships = tuple(
        sorted(
            (
                ItemRelationship(kind, ItemSummary.from_item(board.find(other_id)))
                for kind, other_id in board.links_for(item.id)
            ),
            key=lambda relationship: (
                relationship.kind,
                relationship.item.ticket,
                relationship.item.item_id,
            ),
        )
    )
    return ItemInspection(
        item=ItemSummary.from_item(item),
        comment_count=len(item.comments),
        parent=parent,
        children=children,
        relationships=relationships,
        detail=item.detail if include_prose else None,
        comments=tuple(item.comments) if include_prose else None,
    )


def inspect_next_items(
    board: Board,
    lanes: tuple[str, ...],
    limit: int,
    *,
    include_prose: bool = False,
) -> tuple[ItemInspection, ...]:
    """Return the next cards across selected lanes using the board's total order."""
    if limit < 1:
        raise BoardError("--next must be a positive integer")
    unknown = tuple(lane for lane in lanes if lane not in board.policy.states)
    if unknown:
        raise BoardError(
            "--lanes contains unknown lane(s): "
            f"{', '.join(unknown)}; want one or more of {', '.join(board.policy.states)}"
        )

    selected = frozenset(lanes)
    items = sorted(
        (item for item in board.items if item.state in selected),
        key=lambda item: item_order_key(item, board.policy),
    )[:limit]
    return tuple(inspect_item(board, item.id, include_prose=include_prose) for item in items)


def _item_matches_search(
    item: Item,
    needle: str,
    ticket_query: int | None,
    *,
    include_prose: bool,
) -> bool:
    """Match only visible fields unless the caller explicitly opts into prose."""
    if ticket_query is not None:
        return item.ticket == ticket_query
    if needle in item.id.casefold() or needle in item.subject.casefold():
        return True
    if not include_prose:
        return False
    if needle in item.detail.casefold():
        return True
    return any(needle in comment.text.casefold() for comment in item.comments)


def inspect_search_items(
    board: Board,
    query: str,
    lanes: tuple[str, ...] | None = None,
    *,
    include_prose: bool = False,
) -> tuple[ItemInspection, ...]:
    """Find cards by concept without requiring a broad board export."""
    normalized_query = query.strip()
    if not normalized_query:
        raise BoardError("--search requires non-whitespace text")

    selected_lanes = lanes or tuple(board.policy.states)
    unknown = tuple(lane for lane in selected_lanes if lane not in board.policy.states)
    if unknown:
        raise BoardError(
            "--lanes contains unknown lane(s): "
            f"{', '.join(unknown)}; want one or more of {', '.join(board.policy.states)}"
        )

    needle = normalized_query.casefold()
    ticket_text = needle.removeprefix("#")
    ticket_query = int(ticket_text) if ticket_text.isdecimal() else None
    selected = frozenset(selected_lanes)
    items = sorted(
        (
            item
            for item in board.items
            if item.state in selected
            and _item_matches_search(
                item,
                needle,
                ticket_query,
                include_prose=include_prose,
            )
        ),
        key=lambda item: item_order_key(item, board.policy),
    )
    return tuple(inspect_item(board, item.id, include_prose=include_prose) for item in items)


def _detail_text(args: argparse.Namespace) -> str:
    """A new card's description, from `--detail` or from `--detail-file`.

    **The file form exists because the SHELL eats punctuation and the board keeps the
    damage.** A detail passed inline through bash on 2026-08-18 lost the apostrophe in
    `I'd` to a literal `%27`, and it reached the board that way -- a quoting artifact
    is indistinguishable from something Terry typed once it is in the record.

    **A file has no quoting layer to survive**, which is why it is offered rather than
    left as the caller's problem.
    """
    if args.detail_file:
        return args.detail_file.read_text(encoding="utf-8").rstrip("\n")
    return str(args.detail)


_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


def _parse_activity_bound(raw: str, option: str) -> datetime.datetime:
    """Parse one strict, offset-bearing RFC 3339 CLI boundary."""
    if _RFC3339.fullmatch(raw) is None:
        raise BoardError(f"{option} requires an RFC 3339 timestamp with a UTC offset")
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BoardError(f"{option} has an invalid timestamp: {raw!r}") from exc
    if parsed.utcoffset() is None:
        raise BoardError(f"{option} requires an RFC 3339 timestamp with a UTC offset")
    return parsed


def _activity_bounds(
    args: argparse.Namespace,
) -> tuple[datetime.datetime, datetime.datetime | None]:
    """Resolve the selected CLI activity window, inclusive at both boundaries."""
    since = cast("str | None", args.activity_since)
    if since is not None:
        return _parse_activity_bound(since, "--activity-since"), None
    between = cast("list[str] | None", args.activity_between)
    if between is None:
        raise BoardError("activity query has no time bounds")
    start = _parse_activity_bound(between[0], "--activity-between START")
    end = _parse_activity_bound(between[1], "--activity-between END")
    if end < start:
        raise BoardError("--activity-between END must not be earlier than START")
    return start, end


def _event_instant(raw: str, where: str) -> datetime.datetime:
    """Parse a stored event timestamp and refuse to hide malformed audit data."""
    instant = parse_stamp(raw)
    if instant == _BEGINNING_OF_TIME:
        raise BoardError(f"{where} has an unreadable timestamp {raw!r}")
    return instant


def activity_events(
    board: Board,
    start: datetime.datetime,
    end: datetime.datetime | None,
) -> list[ActivityEvent]:
    """Return sanitized events in one inclusive window, oldest first.

    History and comments are separate persisted arrays, so events sharing a one-second
    timestamp have no recoverable cross-array causal order. Ticket, source sequence,
    and kind provide a deterministic tie-break without pretending otherwise.
    """
    events: list[ActivityEvent] = []
    for item in board.items:
        for index, change in enumerate(item.history):
            instant = _event_instant(change.at, f"{item.id} history[{index}]")
            if instant < start or (end is not None and instant > end):
                continue
            if change.kind == "lane":
                kind = "created" if change.frm is None else "moved"
                event = ActivityEvent(
                    ticket=item.ticket,
                    item_id=item.id,
                    kind=kind,
                    at=change.at,
                    instant=instant,
                    by=change.by,
                    lane_from=change.frm,
                    lane_to=change.to,
                    sequence=index,
                )
            elif change.kind == "owner":
                event = ActivityEvent(
                    ticket=item.ticket,
                    item_id=item.id,
                    kind="assigned",
                    at=change.at,
                    instant=instant,
                    by=change.by,
                    owner_from=change.owner_frm,
                    owner_to=change.owner_to,
                    sequence=index,
                )
            else:
                event = ActivityEvent(
                    ticket=item.ticket,
                    item_id=item.id,
                    kind="prioritized",
                    at=change.at,
                    instant=instant,
                    by=change.by,
                    priority_from=change.priority_frm,
                    priority_to=change.priority_to,
                    sequence=index,
                )
            events.append(event)
        for index, comment in enumerate(item.comments):
            instant = _event_instant(comment.at, f"{item.id} comments[{index}]")
            if instant < start or (end is not None and instant > end):
                continue
            events.append(
                ActivityEvent(
                    ticket=item.ticket,
                    item_id=item.id,
                    kind="commented",
                    at=comment.at,
                    instant=instant,
                    by=comment.by,
                    comment_chars=len(comment.text),
                    sequence=len(item.history) + index,
                )
            )
    return sorted(
        events, key=lambda event: (event.instant, event.ticket, event.sequence, event.kind)
    )


def _report_activity(
    board: Board,
    start: datetime.datetime,
    end: datetime.datetime | None,
    *,
    as_json: bool,
) -> None:
    """Print one sanitized activity query in JSON or compact human form."""
    drift = board.verify()
    if drift:
        raise BoardError(f"activity query refused {len(drift)} audit-trail problem(s): {drift[0]}")
    events = activity_events(board, start, end)
    if as_json:
        payload: list[JsonValue] = [cast("JsonValue", event.to_json()) for event in events]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if not events:
        print("  No activity in the requested window.")
        return
    for event in events:
        print(f"  {event.describe()}")


def _print_report_section(label: str, lines: tuple[str, ...]) -> None:
    """Print one indented report section with an explicit empty value."""
    print(f"  {label}:")
    if not lines:
        print("    none")
        return
    for line in lines:
        print(f"    {line}")


def _comment_report_lines(comments: tuple[Comment, ...]) -> tuple[str, ...]:
    """Render explicit comment prose with its author and timestamp."""
    lines: list[str] = []
    for comment in comments:
        lines.append(f"{comment.at}  {comment.by}")
        lines.extend(f"  {line}" for line in comment.text.splitlines())
    return tuple(lines)


def _report_item(
    board: Board,
    ref: str,
    *,
    as_json: bool,
    include_prose: bool,
) -> None:
    """Print one focused card report with optional private prose."""
    drift = board.verify()
    if drift:
        raise BoardError(
            f"focused inspection refused {len(drift)} audit-trail problem(s): {drift[0]}"
        )
    inspection = inspect_item(board, ref, include_prose=include_prose)
    if as_json:
        print(json.dumps(inspection.to_json(), indent=2, ensure_ascii=False))
        return

    _print_item_inspection(inspection, include_prose=include_prose)


def _print_item_inspection(
    inspection: ItemInspection,
    *,
    include_prose: bool,
    expand_children: bool = True,
) -> None:
    """Print one already-validated card inspection in the human format."""
    item = inspection.item
    print(f"{item.label} {item.item_id}")
    print(f"  subject: {item.subject}")
    print(f"  state: {item.state}")
    print(f"  priority: {item.priority}")
    print(f"  owner: {item.owner}")
    print(f"  comments: {inspection.comment_count}")
    parent = inspection.parent.describe() if inspection.parent is not None else "none"
    print(f"  parent: {parent}")

    if expand_children:
        _print_report_section(
            "children",
            tuple(child.describe() for child in inspection.children),
        )
    else:
        print(f"  children: {len(inspection.children)}")
    _print_report_section(
        "relationships",
        tuple(
            f"{relationship.kind}: {relationship.item.describe()}"
            for relationship in inspection.relationships
        ),
    )

    if not include_prose:
        return
    _print_report_section(
        "detail",
        tuple(inspection.detail.splitlines()) if inspection.detail else (),
    )
    _print_report_section("comment text", _comment_report_lines(inspection.comments or ()))


def _report_next_items(
    board: Board,
    lanes: list[str],
    limit: int,
    *,
    as_json: bool,
    include_prose: bool,
) -> None:
    """Print prioritized cards from selected lanes without exporting the whole board."""
    drift = board.verify()
    if drift:
        raise BoardError(
            f"prioritized inspection refused {len(drift)} audit-trail problem(s): {drift[0]}"
        )
    selected_lanes = tuple(dict.fromkeys(lanes))
    inspections = inspect_next_items(
        board,
        selected_lanes,
        limit,
        include_prose=include_prose,
    )
    if as_json:
        payload: JsonObject = {
            "lanes": list(selected_lanes),
            "limit": limit,
            "order": ["policyPriority", "ticket"],
            "items": [cast("JsonValue", _next_item_json(inspection)) for inspection in inspections],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if not inspections:
        print(f"  No cards in selected lanes: {', '.join(selected_lanes)}")
        return

    print(
        f"Next {len(inspections)} card(s) from {', '.join(selected_lanes)} "
        "(policy priority, then ticket):"
    )
    for inspection in inspections:
        print()
        _print_item_inspection(
            inspection,
            include_prose=include_prose,
            expand_children=False,
        )


def _report_search_items(
    board: Board,
    query: str,
    lanes: list[str] | None,
    *,
    as_json: bool,
    include_prose: bool,
) -> None:
    """Print deterministic focused matches without exporting the whole board."""
    drift = board.verify()
    if drift:
        raise BoardError(f"ticket search refused {len(drift)} audit-trail problem(s): {drift[0]}")
    selected_lanes = tuple(dict.fromkeys(lanes or board.policy.states))
    inspections = inspect_search_items(
        board,
        query,
        selected_lanes,
        include_prose=include_prose,
    )
    fields: list[JsonValue] = ["id", "ticket", "subject"]
    if include_prose:
        fields.extend(("detail", "comments"))
    if as_json:
        payload: JsonObject = {
            "query": query,
            "lanes": list(selected_lanes),
            "searchedFields": fields,
            "order": ["policyPriority", "ticket"],
            "items": [cast("JsonValue", _next_item_json(inspection)) for inspection in inspections],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if not inspections:
        print(f"  No cards matched {query!r} in {', '.join(selected_lanes)}")
        return

    print(
        f"Search matched {len(inspections)} card(s) for {query!r} in "
        f"{', '.join(selected_lanes)} (policy priority, then ticket):"
    )
    for inspection in inspections:
        print()
        _print_item_inspection(
            inspection,
            include_prose=include_prose,
            expand_children=False,
        )


def _report(board: Board, args: argparse.Namespace) -> None:
    """Print the board, and exit non-zero if anything fails its own audit."""
    if args.json:
        print(json.dumps(board.to_json(), indent=2, ensure_ascii=False))
        return

    bad_edges = check_edges()
    if bad_edges:
        print(f"  PERMISSION TABLE IS INCONSISTENT, {len(bad_edges)} problem(s):")
        for problem in bad_edges:
            print(f"      {problem}")

    drift = board.verify()
    if drift:
        print(f"  {len(drift)} item(s) FAIL THEIR OWN AUDIT TRAIL:")
        for problem in drift:
            print(f"      {problem}")
    elif args.verify:
        print("  Every item's history replays cleanly and matches its state.")

    # **Card #0047.** `--verify` is the integrity command, so the sort's own checks
    # belong here rather than on every mutation, where they would double the output.
    sort_problems = report_sort_health(board) if args.verify else []

    print(f"\n{board.project}  ({len(board.items)} items, port {board.port})")
    for lane in board.lanes():
        if not lane.items:
            continue
        print(f"\n  {lane.label}  [{lane.owner_label}]  {len(lane.items)}")
        for item in lane.items:
            note = f"  ({len(item.comments)} comment(s))" if item.comments else ""
            print(f"    {item.priority}  {item.subject}{note}")

    if bad_edges or drift or sort_problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
