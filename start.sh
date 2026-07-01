#!/bin/sh
set -eu

FONT_SRC="news_bot/assets/fonts/SourceHanSerifSC-VF.otf"
USER_FONT_DIR="$HOME/.local/share/fonts"
SYSTEM_FONT_DIR="/usr/local/share/fonts/source-han-serif"

if [ -f "$FONT_SRC" ]; then
  mkdir -p "$USER_FONT_DIR"
  cp "$FONT_SRC" "$USER_FONT_DIR/SourceHanSerifSC-VF.otf"
  chmod 644 "$USER_FONT_DIR/SourceHanSerifSC-VF.otf" || true
  if mkdir -p "$SYSTEM_FONT_DIR" 2>/dev/null; then
    cp "$FONT_SRC" "$SYSTEM_FONT_DIR/SourceHanSerifSC-VF.otf"
    chmod 644 "$SYSTEM_FONT_DIR/SourceHanSerifSC-VF.otf" || true
  fi
  if command -v fc-cache >/dev/null 2>&1; then
    fc-cache -f -v || true
  fi
  echo "[fonts] Installed SourceHanSerifSC-VF.otf into $USER_FONT_DIR"
  if [ -f "$SYSTEM_FONT_DIR/SourceHanSerifSC-VF.otf" ]; then
    echo "[fonts] Installed SourceHanSerifSC-VF.otf into $SYSTEM_FONT_DIR"
  fi
  if command -v fc-match >/dev/null 2>&1; then
    echo "[fonts] fc-match Source Han Serif SC VF: $(fc-match 'Source Han Serif SC VF' || true)"
    echo "[fonts] fc-match Source Han Serif SC: $(fc-match 'Source Han Serif SC' || true)"
    echo "[fonts] fc-match SourceHanSerifSC: $(fc-match 'SourceHanSerifSC' || true)"
  else
    echo "[fonts] fc-match not found, skipping font match diagnostics"
  fi
  if command -v fc-scan >/dev/null 2>&1; then
    echo "[fonts] fc-scan bundled font:"
    fc-scan --format='family=%{family}\nfullname=%{fullname}\npostscriptname=%{postscriptname}\nfile=%{file}\n' "$USER_FONT_DIR/SourceHanSerifSC-VF.otf" || true
  fi
else
  echo "[fonts] SourceHanSerifSC-VF.otf not found at $FONT_SRC"
fi

exec gunicorn app:app --bind "0.0.0.0:${PORT:-8080}" --timeout 600
