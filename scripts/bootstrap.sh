#!/bin/sh
# Build the analyzed Ghidra project from the stock firmware + the shared annotations.
#   scripts/bootstrap.sh [project-dir]        (default work/ghidra; refuses to overwrite)
# Phase 1 (auto-analysis on): raw import at 0x08004000 (ARM Cortex, Thumb) -> CortexSetup (memory
#   map, vectors) -> analysis -> RemoveBogusVectorSyms -> CreateCodePtrFunctions -> ApplyFunctions
#   + ApplyNames (create every known function; analysis then completes their code/bodies).
# Phase 2 (-noanalysis, nothing re-analyses afterwards): ApplyFunctions again (now exactly
#   annotations/functions.txt) -> ApplyNames -> ApplyComments -> SetupGlobals (RAM image, constant
#   literal-pool pointers, globals.txt). Takes a few minutes.
# BOOTSTRAP_NO_EXACT=1 skips the phase-2 ApplyFunctions: use it to produce a new canonical
#   functions.txt (bootstrap, check, then tools/names_snapshot.py against that project).
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
G=${GHIDRA_INSTALL_DIR:-$HOME/opt/ghidra_12.1.4_PUBLIC}
PROJ=$(mkdir -p "${1:-$ROOT/work/ghidra}" && cd "${1:-$ROOT/work/ghidra}" && pwd)
PY=$ROOT/.venv/bin/python
STEM=D878UV_V4.00_20250725
A=$ROOT/annotations
S=$ROOT/ghidra_scripts
[ -x "$G/support/analyzeHeadless" ] || { echo "set GHIDRA_INSTALL_DIR to a Ghidra 12.x install"; exit 1; }
[ -x "$PY" ] || { echo "run scripts/setup_venv.sh first"; exit 1; }
[ -e "$PROJ/d878.gpr" ] && { echo "$PROJ/d878.gpr exists; remove it or pass another dir"; exit 1; }
"$PY" -c "import sys; sys.path.insert(0, '$ROOT/tools'); import hcfg; hcfg.load_fw('uv')"
"$PY" "$ROOT/tools/raminit.py" 2>/dev/null
# The program is named D878UV_V4.00.bin in the project (what ghidra_serve.py opens).
cp "$ROOT/firmware/$STEM.CDD" "$PROJ/D878UV_V4.00.bin"
LOG=$PROJ/bootstrap.log
echo "phase 1: import + analysis"
"$G/support/analyzeHeadless" "$PROJ" d878 -import "$PROJ/D878UV_V4.00.bin" \
	-loader BinaryLoader -loader-baseAddr 0x08004000 -processor ARM:LE:32:Cortex -cspec default \
	-scriptPath "$S" -preScript CortexSetup.java \
	-postScript RemoveBogusVectorSyms.java -postScript CreateCodePtrFunctions.java \
	-postScript ApplyFunctions.java "$A/functions.txt" -postScript ApplyNames.java "$A/names.txt" \
	-max-cpu 8 > "$LOG" 2>&1 || { tail -30 "$LOG"; exit 1; }
echo "phase 2: annotations"
FUN=""
[ -n "$BOOTSTRAP_NO_EXACT" ] || FUN="-postScript ApplyFunctions.java $A/functions.txt"
"$G/support/analyzeHeadless" "$PROJ" d878 -process D878UV_V4.00.bin -noanalysis -scriptPath "$S" \
	$FUN -postScript ApplyNames.java "$A/names.txt" -postScript ApplyComments.java "$A/comments.txt" \
	-postScript SetupGlobals.java "$ROOT/out/sram_init.bin" "$A/globals.txt" >> "$LOG" 2>&1 || { tail -30 "$LOG"; exit 1; }
grep -E "\.java> (vectors|functions|names|comments|globals|literal|candidates|removed)|ERROR .*Script|Import succeeded" "$LOG" | sed 's/ (GhidraScript)//; s/^INFO  //'
echo "project ready: $PROJ  (start the API with scripts/ghidractl start)"
