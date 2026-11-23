#!/bin/sh
# Ship the analyzed Ghidra project without making the other person run the bootstrap.
#   scripts/project_pack.sh export [out.gzf]     work/ghidra -> packed program (default out/d878_<date>.gzf)
#   scripts/project_pack.sh import <file.gzf> [project-dir]   -> work/ghidra (refuses to overwrite)
# A .gzf CONTAINS THE FULL FIRMWARE IMAGE: never commit it or post it publicly; hand it only to
# people who have their own copy of the firmware. The text annotations remain the merge-able source
# of truth; after importing, `scripts/selftest.sh` confirms the project matches them.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
G=${GHIDRA_INSTALL_DIR:-$HOME/opt/ghidra_12.1.4_PUBLIC}
P=$ROOT/work/ghidra
case "$1" in
export)
	OUT=$(realpath -m "${2:-$ROOT/out/d878_$(date +%Y%m%d).gzf}")
	mkdir -p "$(dirname "$OUT")"
	was_up=; "$ROOT/scripts/ghidractl" status | grep -q running && was_up=1
	[ -n "$was_up" ] && "$ROOT/scripts/ghidractl" stop
	"$G/support/analyzeHeadless" "$P" d878 -process D878UV_V4.00.bin -noanalysis -readOnly \
		-scriptPath "$ROOT/ghidra_scripts" -postScript ExportGzf.java "$OUT" 2>&1 | grep "ExportGzf.java>\|ERROR" || true
	[ -n "$was_up" ] && "$ROOT/scripts/ghidractl" start
	ls -l "$OUT" ;;
import)
	[ -f "$2" ] || { echo "usage: $0 import <file.gzf> [project-dir]"; exit 2; }
	P=$(realpath -m "${3:-$P}")
	[ -e "$P/d878.gpr" ] && { echo "$P already has a project; move it away first"; exit 1; }
	mkdir -p "$P/.import"
	# Ghidra names the program after the file; ghidra_serve.py opens /D878UV_V4.00.bin.
	cp "$2" "$P/.import/D878UV_V4.00.bin.gzf"
	"$G/support/analyzeHeadless" "$P" d878 -import "$P/.import/D878UV_V4.00.bin.gzf" -noanalysis 2>&1 | grep -E "Import succeeded|ERROR" || true
	rm -rf "$P/.import"
	echo "imported into $P; start with scripts/ghidractl start, then run scripts/selftest.sh" ;;
*) echo "usage: $0 export [out.gzf] | import <file.gzf>"; exit 2 ;;
esac
