"""Exercise the browser's actual ordering helpers across wall-clock boundaries."""

import json
import pathlib
import shutil
import subprocess
from typing import Any, cast

import pytest

from localswim import api_endpoint

MINUTE = 1_792_065_600_000
NODE_DRIVER = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const start = input.source.indexOf('  const boardNames =');
const end = input.source.indexOf('  const pinnedHeading =', start);
if (start < 0 || end < 0) throw Error('Production ordering helpers not found.');
const context = vm.createContext({categories: [['blocked'], ['needsHuman'], ['review']]});
vm.runInContext(input.source.slice(start, end) + `
  this.orders = (entries, times) => times.map(now => {
    Date.now = () => now;
    return entries.slice().sort((a, b) => compareBoards(a, b, Date.now()))
      .map(entry => entry.key);
  });`, context);
process.stdout.write(JSON.stringify(context.orders(input.entries, input.times)));
"""


def entry(key: str, offset: int | None, count: int, project: str | None = None) -> dict[str, Any]:
    """Create catalog data without writing any board metadata."""
    return {
        "key": key,
        "project": key if project is None else project,
        "lastCardEditMs": None if offset is None else MINUTE + offset,
        "counts": {"blocked": count, "needsHuman": 0, "review": 0},
    }


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        (
            [entry("older-attention", 5_000, 8), entry("newer-empty", 50_000, 0)],
            ["newer-empty", "older-attention"],
        ),
        (
            [entry("older", 59_000, 20), entry("newer", 61_000, 0)],
            ["newer", "older"],
        ),
        (
            [entry("older-millisecond", 5_001, 20), entry("newer-millisecond", 5_999, 0)],
            ["newer-millisecond", "older-millisecond"],
        ),
        (
            [
                entry("blocked", 5_000, 8),
                entry("combined", 5_000, 0)
                | {"counts": {"blocked": 0, "needsHuman": 5, "review": 4}},
            ],
            ["combined", "blocked"],
        ),
        (
            [
                entry("b", 5_000, 2, "ALPHA"),
                entry("a", 5_000, 2, "alpha"),
                entry("c", 5_000, 2, "Beta"),
            ],
            ["a", "b", "c"],
        ),
        (
            [entry("unknown", None, 50), entry("known", 1_000, 0)],
            ["known", "unknown"],
        ),
        (
            [
                entry("future-close", 86_400_000, 20),
                entry("future-later", 86_400_001, 0),
                entry("known", 0, 50),
            ],
            ["future-later", "future-close", "known"],
        ),
    ],
    ids=[
        "same-minute",
        "different-minute",
        "same-second",
        "combined-attention",
        "names-and-keys",
        "unknown",
        "future",
    ],
)
def test_unchanged_catalog_order_is_independent_of_wall_clock(
    entries: list[dict[str, Any]], expected: list[str]
) -> None:
    """Exact recorded timestamps retain their order as elapsed ages change."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node.js executable is unavailable.")
    source = (
        pathlib.Path(api_endpoint.__file__).with_name("workspace.js").read_text(encoding="utf-8")
    )
    times = [MINUTE + offset for offset in range(-86_400_000, 86_400_000, 13_711)]
    times.extend([MINUTE + 70_000, MINUTE + 115_000])
    result = subprocess.run(
        [node, "-e", NODE_DRIVER],
        input=json.dumps({"source": source, "entries": entries, "times": times}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )
    orders = cast("list[list[str]]", json.loads(result.stdout))
    assert len(orders) == len(times)
    assert all(order == expected for order in orders)
