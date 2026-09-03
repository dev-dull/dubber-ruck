#!/usr/bin/env bash
# Symlink dubber-ruck into ~/bin (and, once it exists, the skill into ~/.claude/skills).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$HOME/bin"
chmod +x "$here/dubber_ruck.py"
ln -sfn "$here/dubber_ruck.py" "$HOME/bin/dubber-ruck"
echo "linked ~/bin/dubber-ruck -> $here/dubber_ruck.py"

if [ -d "$here/skills/dubber-ruck" ]; then
  mkdir -p "$HOME/.claude/skills"
  ln -sfn "$here/skills/dubber-ruck" "$HOME/.claude/skills/dubber-ruck"
  echo "linked ~/.claude/skills/dubber-ruck -> $here/skills/dubber-ruck"
fi

if [ ! -f "$HOME/.config/dubber-ruck/config" ]; then
  mkdir -p "$HOME/.config/dubber-ruck"
  cp "$here/config.example" "$HOME/.config/dubber-ruck/config"
  echo "created ~/.config/dubber-ruck/config from config.example: set DUBBER_RUCK_URL (and DUBBER_RUCK_MODEL) there"
fi

case ":$PATH:" in
  *":$HOME/bin:"*) ;;
  *) echo "note: ~/bin is not on PATH" ;;
esac

cat <<'EOF'

Optional: enforce the commit and plan checkpoints with hooks by merging this into
~/.claude/settings.json (paths as installed here):
EOF
cat <<EOF
{"hooks": {"PreToolUse": [
  {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 $here/hooks/hook.py commit", "if": "Bash(git *)", "timeout": 600, "statusMessage": "dubber ruck is reviewing the diff"}]},
  {"matcher": "ExitPlanMode", "hooks": [{"type": "command", "command": "python3 $here/hooks/hook.py plan", "timeout": 900, "statusMessage": "dubber ruck is checking the plan"}]}
]}}
EOF
