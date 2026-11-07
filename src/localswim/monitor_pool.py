"""Fail closed for stale legacy pool launchers, including former stop requests."""

from .monitor import main

if __name__ == "__main__":
    raise SystemExit(main())
