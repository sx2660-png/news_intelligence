#!/bin/sh
set -eu

FONT_SRC="news_bot/assets/fonts/SourceHanSerifSC-VF.otf"
USER_FONT_DIR="$HOME/.local/share/fonts"

if [ -f "$FONT_SRC" ]; then
  mkdir -p "$USER_FONT_DIR"
  cp "$FONT_SRC" "$USER_FONT_DIR/"
  if command -v fc-cache >/dev/null 2>&1; then
    fc-cache -f -v "$USER_FONT_DIR" >/dev/null 2>&1 || true
  fi
  echo "[fonts] Installed SourceHanSerifSC-VF.otf into $USER_FONT_DIR"
else
  echo "[fonts] SourceHanSerifSC-VF.otf not found at $FONT_SRC"
fi

exec gunicorn app:app --bind "0.0.0.0:${PORT:-8080}" --timeout 600
