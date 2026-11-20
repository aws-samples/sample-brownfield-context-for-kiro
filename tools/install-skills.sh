#!/usr/bin/env sh
# Fallback install: copy the three brownfield-context skills into ~/.kiro/skills/.
# Use when importing the Kiro Power is not possible (older Kiro CLI, blocked Power import).
# Usage: sh tools/install-skills.sh [dist/brownfield-context]
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
PACKAGE="${1:-$HERE/../dist/brownfield-context}"
SRC="$PACKAGE/skills"
if [ ! -d "$SRC" ]; then
  echo "Skills not found under '$SRC'. Run: python3 tools/build_power.py (or unzip brownfield-context-1.4.3.zip)." >&2
  exit 1
fi
DEST="$HOME/.kiro/skills"
mkdir -p "$DEST"
for skill in "$SRC"/*/; do
  name="$(basename "$skill")"
  rm -rf "${DEST:?}/$name"
  cp -R "$skill" "$DEST/$name"
  echo "installed $name -> $DEST/$name"
done
CLI="$DEST/brownfield-context-setup/scripts/bcp_cli.py"
echo
echo "Run the tool directly with Python 3.12+:"
echo "  python3 \"$CLI\" --help"
echo "  python3 \"$CLI\" generate --bre <bre-folder> --target <repo> --review-dir <review-folder>"
