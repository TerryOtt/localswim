"""Fail closed for stale legacy watcher launchers; service notifications replace them."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

RETIREMENT_MESSAGE = (
    "Legacy watcher retired; use shared localswim-cli BOARD notifications "
    "status, target THREAD_UUID, and enable. Legacy runtime files are preserved."
)


def main(argv: Sequence[str] | None = None) -> int:
    """Refuse every legacy invocation before parsing inputs or touching runtime state."""
    del argv
    print(RETIREMENT_MESSAGE, file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
