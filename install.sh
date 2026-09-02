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

case ":$PATH:" in
  *":$HOME/bin:"*) ;;
  *) echo "note: ~/bin is not on PATH" ;;
esac
