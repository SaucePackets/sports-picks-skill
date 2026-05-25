#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/SaucePackets/sports-picks-skill.git"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

if ! command -v git >/dev/null 2>&1; then
  echo "git is required" >&2
  exit 1
fi

git clone --depth 1 "$REPO_URL" "$TMP_DIR/repo" >/dev/null

mkdir -p "$HERMES_HOME/skills/sports"
cp -R "$TMP_DIR/repo/skills/"* "$HERMES_HOME/skills/sports/"

mkdir -p "$HERMES_HOME/sports-picks/.picks"
for f in PROCESS.md REFLECTIONS.md; do
  if [ -f "$TMP_DIR/repo/.picks/$f" ] && [ ! -f "$HERMES_HOME/sports-picks/.picks/$f" ]; then
    cp "$TMP_DIR/repo/.picks/$f" "$HERMES_HOME/sports-picks/.picks/$f"
  fi
done

echo "Installed sports-picks skill bundle into $HERMES_HOME/skills/sports"
echo "Runtime ledger root: $HERMES_HOME/sports-picks/.picks"
echo "Start a fresh Hermes session, then use: Use sports-picks. Show the runtime checklist."
