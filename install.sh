#!/bin/sh
set -e

INSTALL_DIR="${CCPLUGIN_INSTALL_DIR:-$HOME/.local/bin}"
INSTALL_PATH="$INSTALL_DIR/ccplugin"
RAW_URL="https://raw.githubusercontent.com/pavanpaik/ccplugin/main/ccplugin.py"

mkdir -p "$INSTALL_DIR"
curl -fsSL "$RAW_URL" -o "$INSTALL_PATH"
chmod +x "$INSTALL_PATH"

echo "Installed ccplugin to $INSTALL_PATH"

# Warn if install dir is not in PATH
case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *) echo "Note: add $INSTALL_DIR to your PATH (e.g. export PATH=\"\$HOME/.local/bin:\$PATH\")" ;;
esac
