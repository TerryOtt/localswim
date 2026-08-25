"""Command-oriented localswim board client."""

from __future__ import annotations

import io
import pathlib
import sys
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click

from localswim import board_state

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class CliContext:
    """One board selected at the root of a CLI command tree."""

    board: pathlib.Path


def _configure_streams() -> None:
    """Make every result representable regardless of the inherited locale."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="strict")


def _board_call[T](operation: Callable[[], T]) -> T:
    """Translate a domain refusal into Click's stable user-facing error boundary."""
    try:
        return operation()
    except board_state.BoardError as exc:
        raise click.ClickException(str(exc)) from exc


def _load(context: CliContext) -> board_state.Board:
    """Load the selected board through the validated model boundary."""
    return _board_call(lambda: board_state.load(context.board))


def _quote(value: str) -> str:
    """Quote one opaque card reference for an HTTP path segment."""
    return urllib.parse.quote(value, safe="")


def _mutate(context: CliContext, route: str, body: dict[str, object]) -> None:
    """Apply one mutation through the live service and print its result."""
    result = _board_call(
        lambda: board_state.apply_remote_mutation(context.board.resolve(), route, body)
    )
    click.echo(f"  {result}")


def _detail_text(detail: str, detail_file: pathlib.Path | None) -> str:
    """Resolve an inline description or read one without a shell quoting layer."""
    if detail_file is None:
        return detail
    try:
        return detail_file.read_text(encoding="utf-8").rstrip("\n")
    except (OSError, UnicodeError) as exc:
        raise click.FileError(str(detail_file), hint=str(exc)) from exc


def _positive_count(_context: click.Context, _parameter: click.Parameter, value: int) -> int:
    """Require a positive report limit while retaining a direct error message."""
    if value < 1:
        raise click.BadParameter("must be a positive integer")
    return value


@click.group(no_args_is_help=True)
@click.argument(
    "board",
    type=click.Path(path_type=pathlib.Path, dir_okay=False, resolve_path=True),
)
@click.pass_context
def cli(context: click.Context, board: pathlib.Path) -> None:
    """Inspect or update BOARD through explicit commands."""
    context.obj = CliContext(board)


@cli.group("board", no_args_is_help=True)
def board_commands() -> None:
    """Inspect, initialize, verify, or administer the board itself."""


@board_commands.command("show")
@click.option("--json", "as_json", is_flag=True, help="Emit the complete board as JSON.")
@click.pass_obj
def board_show(context: CliContext, *, as_json: bool) -> None:
    """Show the board summary or its complete JSON document."""
    board_state.report_board(_load(context), as_json=as_json, verify=False)


@board_commands.command("verify")
@click.pass_obj
def board_verify(context: CliContext) -> None:
    """Replay every history and refuse state or permission-table drift."""
    board_state.report_board(_load(context), as_json=False, verify=True)


@board_commands.command("shutdown")
@click.pass_obj
def board_shutdown(context: CliContext) -> None:
    """Flush autopush and stop the live board service gracefully."""
    result = _board_call(lambda: board_state.shutdown_service(context.board.resolve()))
    click.echo(f"  {result}")


@board_commands.command("set-project")
@click.argument("name")
@click.pass_obj
def board_set_project(context: CliContext, name: str) -> None:
    """Rename the board's project field through the live service."""
    _mutate(context, board_state.API_PREFIX + "/board/project", {"project": name})


@board_commands.command("init")
@click.argument(
    "description",
    type=click.Path(exists=True, path_type=pathlib.Path, dir_okay=False, readable=True),
)
@click.argument(
    "permissions",
    type=click.Path(exists=True, path_type=pathlib.Path, dir_okay=False, readable=True),
)
@click.pass_obj
def board_init(
    context: CliContext,
    description: pathlib.Path,
    permissions: pathlib.Path,
) -> None:
    """Initialize BOARD from name-based description and permission JSON."""
    result = _board_call(
        lambda: board_state.initialize_board(context.board, description, permissions)
    )
    click.echo(f"  {result}")


@board_commands.command("embed-policy")
@click.argument(
    "rules",
    type=click.Path(exists=True, path_type=pathlib.Path, dir_okay=False, readable=True),
)
@click.pass_obj
def board_embed_policy(context: CliContext, rules: pathlib.Path) -> None:
    """Upgrade a schema-3 board by embedding one resolved rules file."""
    result = _board_call(lambda: board_state.embed_policy(context.board, rules))
    click.echo(f"  {result}")


@cli.group("lane", no_args_is_help=True)
def lane_commands() -> None:
    """Perform explicit offline lane migrations."""


@lane_commands.command("migrate")
@click.argument("old")
@click.argument("new")
@click.pass_obj
def lane_migrate(context: CliContext, old: str, new: str) -> None:
    """Migrate a schema-2 lane ID and embed the current policy."""
    result = _board_call(lambda: board_state.migrate_lane(context.board, old, new))
    click.echo(f"  {result}")


@lane_commands.command("rename-label")
@click.argument("lane_id")
@click.argument("label")
@click.pass_obj
def lane_rename_label(context: CliContext, lane_id: str, label: str) -> None:
    """Rename a display label without changing its stable lane ID."""
    result = _board_call(lambda: board_state.rename_lane_label(context.board, lane_id, label))
    click.echo(f"  {result}")


@lane_commands.command("migrate-id")
@click.argument("old")
@click.argument("new")
@click.pass_obj
def lane_migrate_id(context: CliContext, old: str, new: str) -> None:
    """Migrate one lane ID atomically across policy, cards, and history."""
    result = _board_call(lambda: board_state.migrate_lane_id(context.board, old, new))
    click.echo(f"  {result}")


@cli.group("activity", no_args_is_help=True)
def activity_commands() -> None:
    """Inspect sanitized board events without exposing card prose."""


@activity_commands.command("since")
@click.argument("timestamp")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON event array.")
@click.pass_obj
def activity_since(context: CliContext, timestamp: str, *, as_json: bool) -> None:
    """List events at or after one RFC 3339 timestamp."""
    start = _board_call(
        lambda: board_state.parse_activity_bound(timestamp, "activity since TIMESTAMP")
    )
    _board_call(lambda: board_state.report_activity(_load(context), start, None, as_json=as_json))


@activity_commands.command("between")
@click.argument("start")
@click.argument("end")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON event array.")
@click.pass_obj
def activity_between(context: CliContext, start: str, end: str, *, as_json: bool) -> None:
    """List events in one inclusive RFC 3339 time window."""
    start_at = _board_call(
        lambda: board_state.parse_activity_bound(start, "activity between START")
    )
    end_at = _board_call(lambda: board_state.parse_activity_bound(end, "activity between END"))
    if end_at < start_at:
        raise click.UsageError("activity between END must not be earlier than START")
    _board_call(
        lambda: board_state.report_activity(_load(context), start_at, end_at, as_json=as_json)
    )


@cli.group("card", no_args_is_help=True)
def card_commands() -> None:
    """Inspect or mutate individual cards."""


@card_commands.command("show")
@click.argument("reference")
@click.option("--json", "as_json", is_flag=True, help="Emit structured JSON.")
@click.option(
    "--include-comments",
    is_flag=True,
    help="Include detail and comment text in this focused result.",
)
@click.pass_obj
def card_show(
    context: CliContext,
    reference: str,
    *,
    as_json: bool,
    include_comments: bool,
) -> None:
    """Show one card by stable ID or ticket number, including relationships."""
    _board_call(
        lambda: board_state.report_item(
            _load(context),
            reference,
            as_json=as_json,
            include_comments=include_comments,
        )
    )


@card_commands.command("search")
@click.argument("query")
@click.option("--lane", "lanes", multiple=True, help="Limit results to this lane; repeatable.")
@click.option("--json", "as_json", is_flag=True, help="Emit structured JSON.")
@click.option(
    "--include-comments",
    is_flag=True,
    help="Search and return detail and comment text too.",
)
@click.pass_obj
def card_search(
    context: CliContext,
    query: str,
    lanes: tuple[str, ...],
    *,
    as_json: bool,
    include_comments: bool,
) -> None:
    """Find cards by ID, ticket, subject, and optionally private prose."""
    selected_lanes = list(lanes) if lanes else None
    _board_call(
        lambda: board_state.report_search_items(
            _load(context),
            query,
            selected_lanes,
            as_json=as_json,
            include_comments=include_comments,
        )
    )


@card_commands.command("next")
@click.argument("count", type=int, callback=_positive_count)
@click.option(
    "--lane",
    "lanes",
    multiple=True,
    required=True,
    help="Include this lane; repeat for each eligible lane.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit structured JSON.")
@click.option(
    "--include-comments",
    is_flag=True,
    help="Include detail and comment text in focused results.",
)
@click.pass_obj
def card_next(
    context: CliContext,
    count: int,
    lanes: tuple[str, ...],
    *,
    as_json: bool,
    include_comments: bool,
) -> None:
    """Show prioritized cards from one or more explicit lanes."""
    _board_call(
        lambda: board_state.report_next_items(
            _load(context),
            list(lanes),
            count,
            as_json=as_json,
            include_comments=include_comments,
        )
    )


@card_commands.command("create")
@click.argument("card_id")
@click.argument("subject")
@click.option("--state", required=True, help="Initial board-specific lane ID.")
@click.option("--priority", default=None, help="Priority; defaults to board policy.")
@click.option("--owner", default="", help="Owner; defaults to the board's default owner.")
@click.option("--detail", default="", help="Inline card description.")
@click.option(
    "--detail-file",
    type=click.Path(exists=True, path_type=pathlib.Path, dir_okay=False, readable=True),
    help="Read the description from a UTF-8 file.",
)
@click.pass_obj
def card_create(  # noqa: PLR0913
    context: CliContext,
    card_id: str,
    subject: str,
    *,
    state: str,
    priority: str | None,
    owner: str,
    detail: str,
    detail_file: pathlib.Path | None,
) -> None:
    """Create one card through the live service."""
    if detail and detail_file is not None:
        raise click.UsageError("--detail and --detail-file are mutually exclusive")
    _mutate(
        context,
        board_state.API_PREFIX + "/cards",
        {
            "id": card_id,
            "subject": subject,
            "state": state,
            "priority": priority,
            "detail": _detail_text(detail, detail_file),
            "owner": owner or board_state.DEFAULT_OWNER,
        },
    )


@card_commands.command("move")
@click.argument("reference")
@click.argument("state")
@click.pass_obj
def card_move(context: CliContext, reference: str, state: str) -> None:
    """Move one card to a board-specific lane."""
    _mutate(
        context,
        f"{board_state.API_PREFIX}/cards/{_quote(reference)}/move",
        {"to": state},
    )


@card_commands.command("comment")
@click.argument("reference")
@click.argument("text")
@click.pass_obj
def card_comment(context: CliContext, reference: str, text: str) -> None:
    """Append one comment to a card."""
    _mutate(
        context,
        f"{board_state.API_PREFIX}/cards/{_quote(reference)}/comment",
        {"text": text},
    )


@card_commands.command("assign")
@click.argument("reference")
@click.argument("owner")
@click.pass_obj
def card_assign(context: CliContext, reference: str, owner: str) -> None:
    """Assign one card to a configured board user."""
    _mutate(
        context,
        f"{board_state.API_PREFIX}/cards/{_quote(reference)}/assign",
        {"owner": owner},
    )


@card_commands.command("set-priority")
@click.argument("reference")
@click.argument("priority")
@click.pass_obj
def card_set_priority(context: CliContext, reference: str, priority: str) -> None:
    """Change one card's board-specific priority."""
    _mutate(
        context,
        f"{board_state.API_PREFIX}/cards/{_quote(reference)}/priority",
        {"priority": priority},
    )


@card_commands.command("set-detail")
@click.argument("reference")
@click.option("--detail", default="", help="Inline replacement description.")
@click.option(
    "--detail-file",
    type=click.Path(exists=True, path_type=pathlib.Path, dir_okay=False, readable=True),
    help="Read the replacement description from a UTF-8 file.",
)
@click.pass_obj
def card_set_detail(
    context: CliContext,
    reference: str,
    detail: str,
    detail_file: pathlib.Path | None,
) -> None:
    """Replace a card description, or clear it when no text option is given."""
    if detail and detail_file is not None:
        raise click.UsageError("--detail and --detail-file are mutually exclusive")
    _mutate(
        context,
        f"{board_state.API_PREFIX}/cards/{_quote(reference)}/detail",
        {"detail": _detail_text(detail, detail_file)},
    )


@card_commands.command("set-subject")
@click.argument("reference")
@click.argument("text")
@click.pass_obj
def card_set_subject(context: CliContext, reference: str, text: str) -> None:
    """Rename one card without changing its stable ID or ticket."""
    _mutate(
        context,
        f"{board_state.API_PREFIX}/cards/{_quote(reference)}/subject",
        {"subject": text},
    )


@card_commands.command("set-parent")
@click.argument("child")
@click.argument("parent")
@click.pass_obj
def card_set_parent(context: CliContext, child: str, parent: str) -> None:
    """Place one card under another while refusing cycles."""
    _mutate(
        context,
        f"{board_state.API_PREFIX}/cards/{_quote(child)}/parent",
        {"parent": parent},
    )


@card_commands.command("clear-parent")
@click.argument("child")
@click.pass_obj
def card_clear_parent(context: CliContext, child: str) -> None:
    """Move one child card back to the top level."""
    _mutate(
        context,
        f"{board_state.API_PREFIX}/cards/{_quote(child)}/parent",
        {"parent": None},
    )


def _relationship(
    context: CliContext,
    reference: str,
    kind: str,
    other: str,
    *,
    remove: bool,
) -> None:
    """Create or remove one symmetric relationship through its source card."""
    _mutate(
        context,
        f"{board_state.API_PREFIX}/cards/{_quote(reference)}/link",
        {"kind": kind, "other": other, "remove": remove},
    )


@card_commands.command("link")
@click.argument("reference")
@click.argument("kind", type=click.Choice(sorted(board_state.LINK_INVERSE)))
@click.argument("other")
@click.pass_obj
def card_link(context: CliContext, reference: str, kind: str, other: str) -> None:
    """Relate two cards using one configured relationship kind."""
    _relationship(context, reference, kind, other, remove=False)


@card_commands.command("unlink")
@click.argument("reference")
@click.argument("kind", type=click.Choice(sorted(board_state.LINK_INVERSE)))
@click.argument("other")
@click.pass_obj
def card_unlink(context: CliContext, reference: str, kind: str, other: str) -> None:
    """Remove one relationship between two cards."""
    _relationship(context, reference, kind, other, remove=True)


@cli.group("comments", no_args_is_help=True)
def comment_commands() -> None:
    """Inspect comments across cards."""


@comment_commands.command("newest")
@click.argument("count", type=int, callback=_positive_count)
@click.option("--json", "as_json", is_flag=True, help="Emit structured JSON.")
@click.pass_obj
def comments_newest(context: CliContext, count: int, *, as_json: bool) -> None:
    """Show the newest board comments with their card identities."""
    _board_call(lambda: board_state.report_newest_comments(_load(context), count, as_json=as_json))


def main() -> None:
    """Run the installed Click command tree."""
    _configure_streams()
    cli.main(prog_name="localswim-cli")


if __name__ == "__main__":
    main()
