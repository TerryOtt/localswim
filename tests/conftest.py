"""Shared, isolated board fixtures."""

from typing import TYPE_CHECKING

import pytest

from localswim import api_endpoint, board_state
from tests.support import USERS

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def configured_users(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore the module-level cast before and after every test."""
    board_state.install_users(USERS, "terry", "bot", "bot", "pytest")
    monkeypatch.setattr(api_endpoint, "_autopush_enabled", False)
    yield
    board_state.install_users(USERS, "terry", "bot", "bot", "pytest cleanup")


@pytest.fixture
def board() -> board_state.Board:
    """An empty, valid board using the standard two test actors."""
    return board_state.Board(
        generation="11111111-1111-4111-8111-111111111111",
        project="Test",
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
