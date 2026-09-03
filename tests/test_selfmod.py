"""Tests for the self-modification guards.

These matter more than ordinary tests. The bot rewrites its own source, and the
only reason that is safe is that a candidate patch must pass the invariant suite
in a sandbox, and that it cannot edit the suite. Both of those properties are
asserted here.

Note the circularity, and that it is fine: these tests live in `tests/`, which is
protected, so the bot cannot weaken them to widen its own permissions. A patch
that tried would be rejected before it ran.
"""
from __future__ import annotations

import os

from bot import selfmod

# Set by selfmod.run_tests when this suite is running INSIDE a sandbox. Tests
# that themselves start a sandbox must not run there: each nested run would
# start more sandboxes, each running the whole suite again, and the outermost
# call would never return. The outer run has no such flag and exercises them all.
IN_SANDBOX = bool(os.environ.get("SELFMOD_SANDBOX"))


def test_the_referee_cannot_be_edited_by_the_thing_it_judges():
    """The single most important property in the whole system."""
    for p in ("tests/", "tests/run.py", "tests/test_invariants.py",
              "tests/test_selfmod.py", "bot/selfmod.py", "bot/playbook.py",
              ".git/config"):
        assert selfmod.is_protected(p), f"{p} must never be self-modifiable"


def test_ordinary_bot_code_is_freely_self_modifiable():
    """The guard list is deliberately small; everything else is fair game."""
    for p in ("bot/engine.py", "bot/brain.py", "bot/config.py", "bot/sizing.py",
              "bot/regime.py", "bot/engineer.py", "bot/serve.py", "docs/app.js"):
        assert not selfmod.is_protected(p), f"{p} should be self-modifiable"


def test_path_tricks_do_not_escape_the_guard():
    for p in ("./tests/run.py", "tests//run.py", "tests\\run.py", "tests/sub/x.py"):
        assert selfmod.is_protected(p), f"guard escaped via {p!r}"


def test_apply_refuses_a_protected_path():
    r = selfmod.apply_patch("tests/run.py", "# wiped", "because", "kind")
    assert r["applied"] is False and "protected" in r["error"]
    # And the file must be untouched.
    with open(os.path.join(selfmod.REPO_ROOT, "tests/run.py")) as f:
        assert "# wiped" not in f.read()


def test_sandbox_refuses_a_protected_path():
    ok, why = selfmod.sandbox_check("bot/selfmod.py", "raise SystemExit")
    assert ok is False and "protected" in why


def test_sandbox_rejects_code_that_does_not_compile():
    ok, why = selfmod.sandbox_check("bot/sizing.py", "def broken( :\n  pass")
    assert ok is False and "syntax error" in why.lower()


def test_sandbox_rejects_a_patch_that_breaks_an_invariant():
    """The property that makes 'auto-apply' safe: weaken a cap, get rejected."""
    if IN_SANDBOX:
        return  # would start another sandbox; see IN_SANDBOX above
    path = os.path.join(selfmod.REPO_ROOT, "bot/sizing.py")
    with open(path) as f:
        source = f.read()
    # Make the volatility sizer WIDEN caps instead of only tightening them,
    # which test_volatility_sizing_never_widens_a_cap must catch.
    sabotaged = source.replace(
        "    return min(base_cap_pct, base_cap_pct * scale_for(vol_pct))",
        "    return base_cap_pct * 5.0",
    )
    assert sabotaged != source, "test fixture is stale - the target line moved"
    ok, why = selfmod.sandbox_check("bot/sizing.py", sabotaged)
    assert ok is False, "a patch that widens a risk cap must be rejected"
    assert "never_widens" in why or "FAIL" in why


def test_sandbox_accepts_a_harmless_change():
    """A real improvement must still get through, or the gate is useless."""
    if IN_SANDBOX:
        return  # would start another sandbox; see IN_SANDBOX above
    path = os.path.join(selfmod.REPO_ROOT, "bot/sizing.py")
    with open(path) as f:
        source = f.read()
    ok, why = selfmod.sandbox_check("bot/sizing.py", source + "\n\n# a harmless comment\n")
    assert ok is True, f"benign change wrongly rejected: {why[-600:]}"


def test_the_sandbox_leaves_the_real_repo_untouched():
    if IN_SANDBOX:
        return  # would start another sandbox; see IN_SANDBOX above
    path = os.path.join(selfmod.REPO_ROOT, "bot/sizing.py")
    with open(path) as f:
        before = f.read()
    selfmod.sandbox_check("bot/sizing.py", "# destroyed\n")
    with open(path) as f:
        assert f.read() == before, "sandbox wrote through to the real file"


def test_a_fixed_failure_is_not_patched_again():
    """Without this the bot re-patches the same incident forever."""
    incidents = [{"kind": "boom", "detail": "x"} for _ in range(9)]
    assert selfmod.recurring(incidents, 5) is not None
    assert selfmod.recurring(incidents, 20) is None, "must respect the threshold"


def test_restart_flag_round_trips():
    selfmod.clear_restart()
    assert selfmod.restart_requested() is None
    selfmod.request_restart("because")
    got = selfmod.restart_requested()
    assert got and got["reason"] == "because"
    selfmod.clear_restart()
    assert selfmod.restart_requested() is None
