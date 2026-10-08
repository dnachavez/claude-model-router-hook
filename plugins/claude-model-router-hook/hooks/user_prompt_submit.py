#!/usr/bin/env python3
"""UserPromptSubmit entrypoint: warn/autoswitch routing (FR-8..FR-11).

Thin wiring only; all logic lives in the router package. Exit 0 = allow,
exit 2 = warn or autoswitch notice (stderr, prompt blocked for resend).
A planning prompt in "delegate" mode exits 0 with additionalContext instead.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from router import config, hookio, ladder, policy, taxonomy  # noqa: E402

V1_HINT = (
    "Model router: a v1 config was detected and migrated in memory. "
    "Update model-router.json to the v2 schema (add \"version\": 2). "
    "This hint is shown once."
)

PLANNING_SPAWN_PREFIX = "Create an implementation plan for"


def _planning_delegate_context(decision, cfg):
    """Instructions that hand a planning prompt to a sub-agent on the target tier.

    The PreToolUse hook routes the spawn by classifying its prompt, so the
    fixed opening phrase is what lands it on the planning target. When that
    hook will not rewrite the spawn (Fable gate off, enforcement not "on"),
    the model has to be pinned on the call instead.
    """
    pin = (
        cfg.get("subagent_enforcement") != "on"
        or (decision.model == "fable" and not cfg.get("allow_fable_autoswitch"))
    )
    spawn = (
        f'2. Spawn an Agent whose prompt starts with "{PLANNING_SPAWN_PREFIX}" '
        f"and carries that context, so it routes to {decision.model}"
    )
    if pin:
        spawn += f'. Pass model: "{decision.model}" explicitly on that call'
    spawn += ". The sub-agent only reads and plans; it does not edit files."
    return "\n".join((
        f"Model router: this is a planning request. Produce the plan on "
        f"{decision.model} through a sub-agent rather than in this session:",
        "1. Gather the context the plan needs (relevant files, constraints, "
        "existing patterns) in this session.",
        spawn,
        "3. Review the returned plan, present it to the user, and wait for "
        "their approval before executing any of it.",
    ))


def _v1_config_detected(global_path, cwd=None):
    """True when the global or nearest project config file is v1-shaped (FR-31)."""
    paths = []
    if global_path is not None:
        paths.append(global_path)
    search_root = Path(cwd) if cwd else Path.cwd()
    for parent in [search_root, *search_root.parents]:
        project_path = parent / ".claude" / "model-router.json"
        if project_path.exists():
            paths.append(project_path)
            break
    for path in paths:
        raw = config._read_json(path)
        if raw and config.detect_version(raw) == 1:
            return True
    return False


@hookio.fail_open
def main():
    if hookio.is_child():
        sys.exit(0)

    event = hookio.read_event()
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        sys.exit(0)
    if hookio.bypassed(prompt):
        sys.exit(0)

    current_model, current_effort = hookio.current_model_effort(event)
    if ladder.detect_tier(current_model) is None:
        sys.exit(0)  # unknown/unset session model: fail-open (v1 parity)

    global_path = config.global_config_path()
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
    cfg = config.load_config(global_path=global_path, cwd=cwd)

    # One-time v1 upgrade hint (FR-32, AC-8.3): marker in CLAUDE_PLUGIN_DATA;
    # user config files are never written.
    hint = None
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
    if _v1_config_detected(global_path, cwd) and config.v1_hint_due(data_dir):
        hint = V1_HINT

    klass, score = taxonomy.classify(prompt, cfg, data_dir)
    if klass is None:
        if hint:
            print(json.dumps({"systemMessage": hint}))
        sys.exit(0)  # abstain

    decision = policy.main_prompt_decision(
        klass, current_model, current_effort, cfg, score, prompt
    )
    if decision is None:
        if hint:
            print(json.dumps({"systemMessage": hint}))
        sys.exit(0)  # match

    # Apply capability gates and effort floors on the main-prompt path too, so
    # handoff/multi-agent gating (configured implementation target) and
    # data-handling/debugging floors
    # fire here and not only on the subagent path (F1). Gating is monotonic and
    # idempotent, so an already-gated decision passes through unchanged.
    decision = policy.apply_gates(prompt, decision, cfg)

    if decision.klass == "planning" and cfg.get("planning_delivery") == "delegate":
        hookio.log(
            f"DELEGATE->{decision.model}",
            prompt,
            model=current_model,
            effort=current_effort,
            klass=decision.klass,
            target_effort=decision.effort,
        )
        hookio.emit_prompt_context(
            _planning_delegate_context(decision, cfg), system_message=hint
        )
        sys.exit(0)

    _, suffix = ladder.split_suffix(current_model)  # suffix preserved (FR-6)
    if decision.model != ladder.detect_tier(current_model):
        # Tier change: the suffix belongs to the old tier (e.g. a [1m] context
        # variant); reattaching it to a new alias yields invalid strings like
        # "haiku[1m]". Only carry the suffix when the tier is unchanged (F2).
        suffix = ""
    suggestion = decision.model + suffix

    # Autoswitch: write the default for new sessions (FR-9, FR-10); a fable
    # decision with the gate off behaves as warn (FR-11); a settings write
    # failure degrades to warn.
    autoswitch = (
        cfg.get("apply_mode") == "autoswitch"
        and decision.effort != "max"
        and not (
            decision.model == "fable" and not cfg.get("allow_fable_autoswitch")
        )
    )
    if autoswitch and hookio.write_settings(suggestion, decision.effort):
        effort_part = f" (effort {decision.effort})" if decision.effort else ""
        message = (
            f"Router set default to {suggestion}{effort_part} for new sessions. "
            f"Run /model {suggestion} to apply now, then resend. (~ to skip)"
        )
        if hookio.settings_masked():
            message += (
                " Note: a higher-precedence model setting (ANTHROPIC_MODEL or "
                "project settings) masks this default."
            )
        if hint:
            message += "\n" + hint
        hookio.log(
            f"AUTOSWITCH->{suggestion}",
            prompt,
            model=current_model,
            effort=current_effort,
            klass=decision.klass,
            target_effort=decision.effort,
        )
        print(message, file=sys.stderr)
        sys.exit(2)

    # Warn mode: suggest /model and /effort, block for resend.
    parts = [f"/model {suggestion}"]
    if decision.effort is not None:
        parts.append(f"/effort {decision.effort}")
    hookio.log(
        f"SUGGEST->{suggestion}",
        prompt,
        model=current_model,
        effort=current_effort,
        klass=decision.klass,
        target_effort=decision.effort,
    )
    warn_line = f"Run {' and '.join(parts)}, then resend  (~ prefix to skip)"
    if decision.effort == "max" and cfg.get("apply_mode") == "autoswitch":
        warn_line += "\nMax effort is session-only, so the router did not persist xhigh instead."
    if hint:
        warn_line += "\n" + hint
    print(warn_line, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
