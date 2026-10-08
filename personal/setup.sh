#!/usr/bin/env bash
set -euo pipefail

# Restores this machine's Claude Code model setup from the repo: the router
# config, the installed hooks and routed agents, and the settings.json keys
# that select the main model, advisor and effort and register the hooks.
# Safe to re-run; settings.json is backed up before it is changed.
#
#   personal/setup.sh           keep an existing ~/.claude/model-router.json
#   personal/setup.sh --force   replace it with personal/model-router.json

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAUDE_DIR="$HOME/.claude"

mkdir -p "$CLAUDE_DIR"

# The installer generates routed variants from the user config, so the config
# has to be in place first.
if [ "${1:-}" = "--force" ] || [ ! -f "$CLAUDE_DIR/model-router.json" ]; then
    cp "$SCRIPT_DIR/model-router.json" "$CLAUDE_DIR/model-router.json"
    echo "Wrote $CLAUDE_DIR/model-router.json"
else
    echo "Kept existing $CLAUDE_DIR/model-router.json (--force to replace)"
fi

bash "$REPO_ROOT/plugins/claude-model-router-hook/install.sh" >/dev/null
echo "Installed hooks, routed agents and schema into $CLAUDE_DIR"

python3 - "$CLAUDE_DIR" <<'EOF'
import json
import pathlib
import shutil
import sys
import time

claude_dir = pathlib.Path(sys.argv[1])
path = claude_dir / "settings.json"
settings = json.loads(path.read_text()) if path.exists() else {}
before = json.dumps(settings, sort_keys=True)

settings["model"] = "claude-opus-5-5"
settings["advisorModel"] = "claude-fable-5-1"
# Opus 5.5 ignores a top-level effortLevel in user settings, so it needs its
# own modelSettings entry. The top-level key still covers older models and is
# what the router reads as the session effort.
settings["effortLevel"] = "high"
settings.setdefault("modelSettings", {}).setdefault("claude-opus-5-5", {})[
    "effortLevel"
] = "high"

hooks_dir = claude_dir / "hooks"
wanted = (
    ("SessionStart", None, "session_init.py", 5),
    ("UserPromptSubmit", None, "user_prompt_submit.py", 10),
    ("PreToolUse", "Agent|Task", "pre_tool_use.py", 10),
    ("PostToolUse", "Agent|Task", "post_tool_use.py", 5),
)
hooks = settings.setdefault("hooks", {})
for event, matcher, script, timeout in wanted:
    command = f'python3 "{hooks_dir / script}"'
    groups = hooks.setdefault(event, [])
    if any(
        h.get("command") == command for g in groups for h in g.get("hooks", [])
    ):
        continue
    group = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
    if matcher:
        group = {"matcher": matcher, **group}
    groups.append(group)

if json.dumps(settings, sort_keys=True) == before:
    print(f"{path} already up to date")
else:
    if path.exists():
        backup = path.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"Backed up {path} to {backup}")
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"Updated {path}")
EOF

echo "Restart Claude Code to load the hooks."
