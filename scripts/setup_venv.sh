#!/bin/sh
# Create .venv with everything the harness needs, including pyghidra from your Ghidra install.
#   GHIDRA_INSTALL_DIR=/path/to/ghidra_12.1.4_PUBLIC scripts/setup_venv.sh
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
G=${GHIDRA_INSTALL_DIR:-$HOME/opt/ghidra_12.1.4_PUBLIC}
[ -d "$G/Ghidra" ] || { echo "set GHIDRA_INSTALL_DIR to a Ghidra 12.x install (not found: $G)"; exit 1; }
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -q --upgrade pip
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
W=$(ls "$G"/Ghidra/Features/PyGhidra/pypkg/dist/pyghidra-*.whl 2>/dev/null | head -1)
if [ -n "$W" ]; then "$ROOT/.venv/bin/pip" install -q "$W"; else "$ROOT/.venv/bin/pip" install -q pyghidra; fi
echo "ok: $ROOT/.venv ($("$ROOT/.venv/bin/python" -c 'import pyghidra,capstone,keystone,unicorn,serial;print("pyghidra",pyghidra.__version__)'))"
