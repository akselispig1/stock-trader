"""Claude reading the bot's own failures and rewriting the bot's own source.

The mechanism - incident log, sandbox, guards, git commit - lives in
bot/selfmod.py. This is only the part that produces a candidate patch.

The split matters: everything that decides whether a patch is SAFE is in the
other file and in tests/, and neither is self-modifiable. This file can be
rewritten by the bot freely, because nothing here can approve its own work.
"""
from __future__ import annotations

import json
import os

import anthropic

from . import selfmod
from .config import Config
from .costs import price_usage

ENGINEER_SYSTEM = """You are fixing a defect in an automated trading system by \
editing its own source code. You wrote none of this from scratch; you are \
repairing running software that manages real positions.

You are given a recurring operational failure, real examples of it, and the full \
current source of one file. Return the COMPLETE corrected file.

WHAT MAKES A GOOD FIX
- Address the ROOT CAUSE shown in the examples, not the symptom. If orders keep \
being rejected for a malformed value, fix whatever produces that value.
- Change as little as possible. A minimal diff can be reviewed; a rewrite cannot.
- Preserve every behaviour not implicated in the defect - especially all risk \
checks, limits, guards and error handling.
- Match the file's existing style, naming and comment density.
- If this is NOT a code defect - a network outage, a wrong API key, market \
conditions, or a risk check correctly doing its job - say so and return the file \
COMPLETELY UNCHANGED. That is a valid and frequently correct answer.

HARD CONSTRAINTS
- Never weaken, remove or bypass a risk limit, allocation cap, cash reserve, \
order limit, stop-loss, or the independent auditor.
- Never add code that reads credentials, calls new network hosts, spawns \
processes, or writes outside docs/data.
- Never weaken or work around a test.
- Never add a third-party dependency.

Your output is written to a throwaway copy of the repository and the full \
invariant suite is run against it there. It reaches the running system only if \
every test passes. A plausible-looking fix that breaks an invariant is worse \
than no fix at all, because it wastes the one chance to fix this properly."""

PATCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["diagnosis", "is_code_defect", "changed", "new_source", "confidence"],
    "properties": {
        "diagnosis": {"type": "string",
                      "description": "Root cause, one or two sentences."},
        "is_code_defect": {"type": "boolean",
                           "description": "False for outages, bad keys, or a guard "
                                          "correctly rejecting something."},
        "changed": {"type": "boolean",
                    "description": "False when returning the file unmodified."},
        "new_source": {"type": "string",
                       "description": "The COMPLETE file. Never a fragment or a diff."},
        "confidence": {"type": "number", "description": "0-1 that this fixes it."},
    },
}


class Engineer:
    """Diagnoses a recurring failure and rewrites the file responsible."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self.last_cost = 0.0

    def due(self, incidents: list[dict]) -> tuple[bool, str]:
        """Has any single failure recurred often enough to be a defect?"""
        hit = selfmod.recurring(incidents, self.cfg.selfmod_min_incidents)
        if not hit:
            return False, (f"nothing has recurred {self.cfg.selfmod_min_incidents}+ "
                           f"times ({len(incidents)} incidents logged)")
        kind, rows = hit
        return True, f"'{kind}' has occurred {len(rows)} times"

    def attempt(self, rel_path: str, kind: str, examples: list[dict]) -> dict:
        """Propose a fix, prove it in the sandbox, and apply it only if it passes."""
        self.last_cost = 0.0
        out: dict = {"at": selfmod._now(), "kind": kind, "path": rel_path,
                     "applied": False}

        if selfmod.is_protected(rel_path):
            out["error"] = f"{rel_path} is protected from self-modification"
            selfmod._log_change(out)
            return out

        target = os.path.join(selfmod.REPO_ROOT, rel_path)
        try:
            with open(target) as f:
                source = f.read()
        except OSError as e:
            out["error"] = f"cannot read {rel_path}: {e}"
            selfmod._log_change(out)
            return out
        if len(source) > selfmod.MAX_FILE_CHARS:
            out["error"] = f"{rel_path} too large to patch safely ({len(source)} chars)"
            selfmod._log_change(out)
            return out

        user = "\n".join([
            f"RECURRING FAILURE: {kind} ({len(examples)} occurrences)",
            "",
            "RECENT EXAMPLES:",
            *[f"  [{e.get('t', '')[:19]}] {e.get('detail', '')[:400]}"
              for e in examples[-8:]],
            "",
            f"FILE TO FIX: {rel_path}",
            "```python",
            source,
            "```",
            "",
            "Diagnose the root cause and return the complete corrected file. "
            "If this is not a code defect, say so and return it unchanged.",
        ])

        try:
            resp = self.client.messages.create(
                model=self.cfg.model,
                max_tokens=32000,
                thinking={"type": "adaptive"},
                system=ENGINEER_SYSTEM,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "high",
                               "format": {"type": "json_schema", "schema": PATCH_SCHEMA}},
            )
            self.last_cost = price_usage(self.cfg.model, resp.usage)
            text = next((b.text for b in resp.content if b.type == "text"), "{}")
            patch = json.loads(text)
        except (anthropic.APIError, json.JSONDecodeError, ValueError) as e:
            out["error"] = f"could not produce a patch: {e}"
            out["cost"] = self.last_cost
            selfmod._log_change(out)
            return out

        out.update({
            "diagnosis": patch.get("diagnosis", ""),
            "is_code_defect": bool(patch.get("is_code_defect")),
            "confidence": float(patch.get("confidence") or 0),
            "cost": self.last_cost,
        })

        new_source = patch.get("new_source") or ""
        if not patch.get("changed") or not patch.get("is_code_defect"):
            out["outcome"] = "no code change proposed"
            selfmod._log_change(out)
            return out
        if new_source.strip() == source.strip():
            out["outcome"] = "proposed source identical to current"
            selfmod._log_change(out)
            return out

        ok, detail = selfmod.sandbox_check(rel_path, new_source)
        out["tests_passed"] = ok
        out["test_output"] = detail[-1200:]
        if not ok:
            # The common and healthy outcome: the bot tried, the invariants
            # held, nothing shipped.
            out["outcome"] = "rejected by the invariant tests"
            selfmod._log_change(out)
            return out

        res = selfmod.apply_patch(rel_path, new_source,
                                  patch.get("diagnosis", ""), kind)
        out.update(res)
        out["incident_kind"] = kind          # so this failure is not re-patched
        out["outcome"] = "applied" if res.get("applied") else "failed to apply"
        selfmod._log_change(out)
        if res.get("applied"):
            selfmod.request_restart(f"self-fix applied to {rel_path}: {kind}")
        return out
