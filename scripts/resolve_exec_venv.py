#!/usr/bin/env python3
"""Print the interpreter the Polymarket order executor will re-exec into.

One implementation, used by deploy-runtime.sh's venv check AND by the tests.
The deploy had its own copy of this extraction and the test helper had another;
they agreed on the day they were written, which is the only day two copies of
one computation reliably agree (Reviewer, PR #59).

The point is that nothing here RECONSTRUCTS the path. It executes the executor's
own prologue — everything above its first real import — and reports what that
prologue resolved. A checker that recomputes what the checked thing computes can
be right while the thing it checks is wrong, which is how deploy-runtime.sh came
to print "order-executor venv ok" for a venv the executor never consults.

The caller owns the ENVIRONMENT and the WORKING DIRECTORY, because those are
exactly what the resolution depends on: deploy-runtime.sh invokes this with the
runtime checkout as cwd and every SPORTS_PICKS_* variable cleared, which is how a
cron job resolves it.

Usage: resolve_exec_venv.py <path-to-polymarket_us_sdk_bet.py>
"""
from __future__ import annotations

import os
import sys

# Everything the prologue needs runs before this marker; stopping here avoids
# importing httpx and the SDK just to read a path.
PROLOGUE_END = "import argparse"


def resolve(executor: str) -> str:
    text = open(executor, encoding="utf-8").read()
    if PROLOGUE_END not in text:
        raise SystemExit(f"{executor}: no {PROLOGUE_END!r} marker — refusing to guess")
    # The sentinel stops the prologue re-execing this process into the very
    # interpreter it is being asked to name.
    os.environ["_SP_VENV_REEXEC"] = "1"
    namespace: dict[str, object] = {}
    exec(compile(text.split(PROLOGUE_END, 1)[0], executor, "exec"), namespace)  # noqa: S102
    if "_SP_VENV" not in namespace:
        raise SystemExit(f"{executor}: prologue defines no _SP_VENV")
    return str(namespace["_SP_VENV"])


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: resolve_exec_venv.py <path-to-polymarket_us_sdk_bet.py>",
              file=sys.stderr)
        return 2
    print(resolve(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
