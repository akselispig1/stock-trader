"""The bot rewrites its own source code, learning from its own failures.

HOW IT WORKS

1. Every operational failure is recorded: a crashed cycle, an order the risk
   checks rejected, a parse failure, an auditor veto. Patterns accumulate in
   docs/data/incidents.jsonl.
2. When one pattern recurs often enough to be a real defect rather than bad
   luck, Claude is given the incident, the offending source file, and asked for
   a fixed version of that file.
3. The candidate is written into a THROWAWAY COPY of the repository and the
   full invariant suite runs against it there.
4. Only if every test passes is it written to the real repo, git-committed with
   its reasoning, and the process restarted into the new code by the service
   manager.

THE BOUNDARY, AND WHY IT IS WHERE IT IS

The test suite is what makes this safe. `tests/test_invariants.py` asserts the
properties that matter - allocation caps capping, the cash reserve reserving,
shorting off, the capital cap holding - so a patch that weakens a risk limit
fails and is discarded automatically. That is a stronger guarantee than a list
of forbidden files, because it checks BEHAVIOUR rather than guessing which lines
were dangerous.

Which is exactly why a small set of paths is not self-modifiable: the parts that
would let the bot edit its own referee. `tests/` (it could make any patch pass),
this file (it could delete these guards), and `bot/playbook.py` (the rule
validator). Everything else - the engine, the prompts, sizing, regime,
scorecard, the dashboard - it may freely rewrite.

That is not a hedge against the model being malicious. It is that a model
debugging a failing test at 3am, with no human watching, has an obvious and
tempting shortcut available, and the only robust answer is to make the shortcut
impossible rather than to ask it not to take one.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import anthropic

from .config import Config
from .costs import price_usage

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "docs", "data")
INCIDENTS_PATH = os.path.join(DATA_DIR, "incidents.jsonl")
CHANGES_PATH = os.path.join(DATA_DIR, "self_changes.jsonl")
RESTART_FLAG = os.path.join(DATA_DIR, ".restart_requested")

# Paths the bot may never rewrite: the referee, and the guards on the referee.
# Anything else in the repository is fair game provided the tests still pass.
PROTECTED = ("tests/", "bot/selfmod.py", "bot/playbook.py", ".git/")

MAX_INCIDENTS = 400
MAX_FILE_CHARS = 60000   # a file larger than this will not fit a useful prompt


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_protected(rel_path: str) -> bool:
    """Is this path off-limits to self-modification?

    Note the deliberate absence of `lstrip("./")`. lstrip takes a SET of
    characters, not a prefix, so it turns ".git/config" into "git/config" -
    which then matches nothing, leaving the git directory writable. Strip the
    "./" prefix explicitly instead.
    """
    p = rel_path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    return any(p == g or p.startswith(g) for g in PROTECTED)


# --------------------------------------------------------------------------
# Incident log - the raw material the bot learns from
# --------------------------------------------------------------------------

def record_incident(kind: str, detail: str, context: dict | None = None) -> None:
    """Log an operational failure. Cheap and always-on; analysed later."""
    os.makedirs(DATA_DIR, exist_ok=True)
    row = {"t": _now(), "kind": kind, "detail": str(detail)[:600],
           "context": context or {}}
    rows = load_incidents()
    rows.append(row)
    rows = rows[-MAX_INCIDENTS:]
    with open(INCIDENTS_PATH, "w") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\n")


def load_incidents() -> list[dict]:
    try:
        with open(INCIDENTS_PATH) as f:
            return [json.loads(l) for l in f.read().splitlines() if l.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def recurring(incidents: list[dict], min_count: int) -> tuple[str, list[dict]] | None:
    """The most frequent unresolved failure kind, if it recurs enough to be real.

    One crash is an accident; the same crash eight times is a defect. Anything
    already fixed is excluded so the bot does not keep re-patching history.
    """
    fixed = {c.get("incident_kind") for c in load_changes() if c.get("applied")}
    counts: dict[str, list[dict]] = {}
    for i in incidents:
        k = i.get("kind", "")
        if k and k not in fixed:
            counts.setdefault(k, []).append(i)
    if not counts:
        return None
    kind, rows = max(counts.items(), key=lambda kv: len(kv[1]))
    return (kind, rows) if len(rows) >= min_count else None


def load_changes() -> list[dict]:
    try:
        with open(CHANGES_PATH) as f:
            return [json.loads(l) for l in f.read().splitlines() if l.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _log_change(row: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CHANGES_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")


# --------------------------------------------------------------------------
# Sandbox - where a candidate patch is proved or discarded
# --------------------------------------------------------------------------

def run_tests(root: str, timeout: int = 300) -> tuple[bool, str]:
    """Run the invariant suite inside `root`. True only on a clean exit 0."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "tests.run"],
            cwd=root, capture_output=True, text=True, timeout=timeout,
            # No network and no inherited keys: a test run must never be able to
            # place a trade or call an API, whatever the patch did to the code.
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
                 "PYTHONPATH": root, "NO_NETWORK": "1",
                 # Without this the suite recurses without bound: the sandbox
                 # runs every test, tests/test_selfmod.py calls sandbox_check,
                 # which starts another sandbox running every test. Each level
                 # multiplies, so it never returns. The inner run skips exactly
                 # those tests; the outer run still exercises them fully.
                 "SELFMOD_SANDBOX": "1"},
        )
        return r.returncode == 0, (r.stdout + r.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return False, f"tests timed out after {timeout}s (probable infinite loop)"
    except Exception as e:
        return False, f"could not run tests: {e}"


def sandbox_check(rel_path: str, new_source: str) -> tuple[bool, str]:
    """Apply a candidate to a disposable copy of the repo and test it there.

    Nothing touches the working tree until this returns True, so a patch that
    does not even compile can never take the running bot down.
    """
    if is_protected(rel_path):
        return False, f"{rel_path} is protected and cannot be self-modified"

    tmp = tempfile.mkdtemp(prefix="selfmod-")
    try:
        for name in ("bot", "tests"):
            shutil.copytree(os.path.join(REPO_ROOT, name), os.path.join(tmp, name))
        target = os.path.join(tmp, rel_path)
        if not os.path.exists(target):
            return False, f"{rel_path} does not exist"
        with open(target, "w") as f:
            f.write(new_source)

        # Cheapest check first: does it even parse?
        r = subprocess.run([sys.executable, "-m", "py_compile", target],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"syntax error:\n{r.stderr[-1500:]}"

        return run_tests(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Applying - only ever after the sandbox said yes
# --------------------------------------------------------------------------

def _git(*args: str) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def apply_patch(rel_path: str, new_source: str, reason: str, kind: str) -> dict:
    """Write a proven candidate into the repo and commit it.

    Committed rather than merely written: an auto-applied change the owner
    cannot see, diff, or revert would be exactly the black box this project
    exists to avoid.
    """
    if is_protected(rel_path):
        return {"applied": False, "error": f"{rel_path} is protected"}

    target = os.path.join(REPO_ROOT, rel_path)
    before = ""
    if os.path.exists(target):
        with open(target) as f:
            before = f.read()
    with open(target, "w") as f:
        f.write(new_source)

    code, out = _git("add", rel_path)
    if code == 0:
        code, out = _git(
            "commit", "-m",
            f"self-fix: {kind}\n\n{reason}\n\n"
            f"Written and validated by the bot itself: the full invariant suite "
            f"passed against this change in a sandbox before it was applied.\n"
            f"Revert with: git revert HEAD",
        )
    if code != 0:
        # Leave the file in place - it passed the tests - but say the commit
        # failed, because an uncommitted auto-change is the hard one to undo.
        return {"applied": True, "committed": False, "error": out[:300],
                "bytes_before": len(before), "bytes_after": len(new_source)}

    _, sha = _git("rev-parse", "--short", "HEAD")
    return {"applied": True, "committed": True, "commit": sha,
            "bytes_before": len(before), "bytes_after": len(new_source)}


def request_restart(reason: str) -> None:
    """Ask the runner to restart so the new code actually takes effect.

    A flag rather than an exec: the service manager (systemd, Task Scheduler)
    already knows how to restart cleanly, and reusing it means a self-update
    follows exactly the same path as any other restart.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RESTART_FLAG, "w") as f:
        f.write(json.dumps({"t": _now(), "reason": reason}))


def restart_requested() -> dict | None:
    try:
        with open(RESTART_FLAG) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clear_restart() -> None:
    try:
        os.remove(RESTART_FLAG)
    except FileNotFoundError:
        pass
