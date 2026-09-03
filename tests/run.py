"""Zero-dependency test runner: `python -m tests.run`.

pytest would be nicer, but this has to run inside the self-modification sandbox
on whatever laptop the bot lives on, with only the bot's own requirements
installed and possibly no network. So it discovers `test_*` functions in
`tests/test_*.py` and runs them.

Exit code 0 means every invariant held. That exit code is the gate a
self-written patch has to pass, so it must be trustworthy: an error importing a
module counts as failure, never as "no tests ran".
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import traceback


def main() -> int:
    here = pathlib.Path(__file__).parent
    modules = sorted(p.stem for p in here.glob("test_*.py"))
    if not modules:
        print("FAIL: no test modules found - refusing to report success")
        return 1

    passed, failures = 0, []
    for name in modules:
        try:
            mod = importlib.import_module(f"tests.{name}")
        except Exception:
            # An import error is a failure, not an absence of tests.
            failures.append((f"{name} (import)", traceback.format_exc()))
            continue
        for attr in sorted(dir(mod)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(mod, attr)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
            except Exception:
                failures.append((f"{name}.{attr}", traceback.format_exc()))

    for name, tb in failures:
        print(f"\n{'=' * 70}\nFAIL {name}\n{'=' * 70}\n{tb}")

    total = passed + len(failures)
    print(f"\n{passed}/{total} passed" + (f", {len(failures)} FAILED" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
