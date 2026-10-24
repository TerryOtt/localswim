"""Distinct dependency reachability and triage score ordering."""

from localswim import board_state

# ruff: noqa: SLF001 -- direct coverage of pure reachability seam


def test_distinct_reachability_deduplicates_diamonds_and_cycles() -> None:
    graph: dict[str, set[str]] = {
        "root": {"left", "right"},
        "left": {"leaf"},
        "right": {"leaf"},
        "leaf": set(),
        "cycle_a": {"cycle_b", "root"},
        "cycle_b": {"cycle_a"},
        "isolated": set(),
    }
    assert board_state._blocking_counts(graph) == {
        "root": 3,
        "left": 1,
        "right": 1,
        "leaf": 0,
        "cycle_a": 5,
        "cycle_b": 5,
        "isolated": 0,
    }


def test_reachability_handles_self_loops_and_empty_graphs() -> None:
    assert board_state._blocking_counts({}) == {}
    assert board_state._blocking_counts({"self": {"self"}}) == {"self": 0}


def test_reachability_handles_chains_beyond_recursion_limit() -> None:
    size = 2000
    graph = {str(index): {str(index + 1)} for index in range(size - 1)}
    graph[str(size - 1)] = set()
    counts = board_state._blocking_counts(graph)
    assert counts["0"] == size - 1
    assert counts[str(size - 1)] == 0
    assert counts["1000"] == size - 1001


def test_formula_uses_audited_direct_and_indirect_priority_inheritance(
    board: board_state.Board,
) -> None:
    for name, priority in [("root", "P1"), ("child", "P3"), ("leaf", "P5")]:
        board.create(name, name, "backlog", "bot", priority=priority)
    board.link("child", "blocks", "leaf", "bot")
    board.link("root", "blocks", "child", "terry")
    order = board_state.blocking_order(board)
    assert [board.find(name).priority for name in ("root", "child", "leaf")] == ["P1"] * 3
    assert order.blocked_counts == {"root": 2, "child": 1, "leaf": 0}
    assert order.triage_scores == {"root": 198, "child": 199, "leaf": 200}
    assert board.find("child").history[-1].by == "terry"
    assert board.find("leaf").history[-1].by == "terry"
    assert board.verify() == []


def test_owner_formula_p1_blocking_five_is_195(board: board_state.Board) -> None:
    board.create("root", "Root", "backlog", "bot", priority="P1")
    for index in range(5):
        board.create(f"leaf{index}", "Leaf", "backlog", "bot", priority="P3")
        board.link("root", "blocks", f"leaf{index}", "bot")
    order = board_state.blocking_order(board)
    assert order.blocked_counts["root"] == 5
    assert order.triage_scores["root"] == 195


def test_completed_stops_paths_but_archived_unfinished_and_cross_lane_cards_count(
    board: board_state.Board,
) -> None:
    for name in ("root", "done", "hidden", "leaf"):
        board.create(name, name, "ready_for_work", "bot", priority="P1")
    for lane, actor in [
        ("in_progress", "bot"),
        ("ready_for_review", "bot"),
        ("completed", "terry"),
    ]:
        board.move("done", lane, actor)
    board.move("root", "in_progress", "bot")
    board.set_archived("hidden", archived=True, by="bot")
    for source, target in [("root", "done"), ("done", "leaf"), ("root", "hidden")]:
        board.link(source, "blocks", target, "bot")
    order = board_state.blocking_order(board)
    assert order.depths == {"root": 2, "done": 1, "hidden": 0, "leaf": 0}
    assert order.blocked_counts == {"root": 1, "done": 0, "hidden": 0, "leaf": 0}
    assert order.triage_scores == {"root": 199, "done": 200, "hidden": 200, "leaf": 200}
    assert board_state.inspect_next_items(board, ("in_progress",), 1)[0].triage_score == 199
    assert board_state.inspect_search_items(board, "root")[0].blocked_card_count == 1


def test_nonblocking_relationships_and_parent_hierarchy_do_not_count(
    board: board_state.Board,
) -> None:
    for name in ("parent", "child"):
        board.create(name, name, "backlog", "bot", priority="P3")
    board.set_parent("child", "parent", "bot")
    board.link("parent", "references", "child", "bot")
    board.link("parent", "relates_to", "child", "bot")
    assert board_state.blocking_order(board).blocked_counts == {"parent": 0, "child": 0}


def test_numeric_priority_gaps_and_named_policy_ordinals(board: board_state.Board) -> None:
    raw = board.to_json()
    policy = raw["policy"]
    assert isinstance(policy, dict)
    policy["priorities"] = [{"id": "P10", "label": "Ten"}, {"id": "P2", "label": "Two"}]
    policy["defaultPriority"] = "P10"
    numeric = board_state.Board.from_json(raw, "numeric gaps")
    numeric.create("ten", "Ten", "backlog", "bot", priority="P10")
    numeric.create("two", "Two", "backlog", "bot", priority="P2")
    assert board_state.blocking_order(numeric).triage_scores == {"ten": 1100, "two": 300}
    assert [item.id for item in numeric.lanes()[0].items] == ["two", "ten"]
    policy["priorities"] = [
        {"id": "urgent", "label": "Urgent"},
        {"id": "routine", "label": "Routine"},
    ]
    policy["defaultPriority"] = "routine"
    named = board_state.Board.from_json(raw, "named ordinals")
    named.create("routine", "Routine", "backlog", "bot", priority="routine")
    named.create("urgent", "Urgent", "backlog", "bot", priority="urgent")
    assert board_state.blocking_order(named).triage_scores == {"routine": 200, "urgent": 100}


def test_scores_can_cross_priority_bands_and_be_negative_with_stable_ties(
    board: board_state.Board,
) -> None:
    for name, priority in [("ticket_first", "P0"), ("wide", "P1"), ("peer", "P0")]:
        board.create(name, name, "backlog", "bot", priority=priority)
    for index in range(201):
        board.create(f"leaf{index}", "Leaf", "backlog", "bot", priority="P1")
    board.links = [board_state.Link("wide", "blocks", f"leaf{index}") for index in range(201)]
    order = board_state.blocking_order(board)
    assert order.triage_scores["wide"] == -1
    assert [item.id for item in board.lanes()[0].items][:3] == ["wide", "ticket_first", "peer"]
    board.links = board.links[:100]
    order = board_state.blocking_order(board)
    assert order.triage_scores["wide"] == order.triage_scores["ticket_first"] == 100
    assert [item.id for item in board.lanes()[0].items][:3] == ["ticket_first", "peer", "wide"]
    assert [
        item.item.item_id for item in board_state.inspect_next_items(board, ("backlog",), 3)
    ] == ["ticket_first", "peer", "wide"]
    assert board_state.total_order_problems(board) == []
