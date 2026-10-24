"""Typed constants shared by the behavioral tests and fixtures."""

from localswim import board_state
from localswim.write_protocol import WriteToken

# Synthetic boards use a known identity unless a test explicitly replaces it.
GENERATION = "11111111-1111-4111-8111-111111111111"


def token(revision: int, generation: str = GENERATION) -> WriteToken:
    """Build an originating fixture token without reading or rebasing state."""
    return WriteToken(generation, revision)


USERS = (
    board_state.User("terry", "Terry", board_state.HUMAN, "#2266aa"),
    board_state.User("bot", "Bot", board_state.BOT, "#aa6622"),
)
