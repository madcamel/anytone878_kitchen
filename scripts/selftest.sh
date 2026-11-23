#!/bin/sh
# Harness self-test. Offline checks always run; API checks run if scripts/ghidractl is up.
# Exit status 0 = all passed.
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=$ROOT/.venv/bin/python
fail=0
ok()  { echo "  ok    $1"; }
bad() { echo "  FAIL  $1"; fail=1; }
check() { desc=$1; shift; if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi; }
sha() { sha256sum "$1" 2>/dev/null | cut -c1-64; }

echo "environment"
check ".venv present" test -x "$PY"
check "python deps import" "$PY" -c "import capstone, keystone, unicorn, serial"
check "firmware/ D878UV stock image" "$PY" -c "import sys; sys.path.insert(0,'$ROOT/tools'); import hcfg; hcfg.load_fw('uv')"
check "firmware/ D878UVII stock image" "$PY" -c "import sys; sys.path.insert(0,'$ROOT/tools'); import hcfg; hcfg.load_fw('uvii')"
check "all python compiles" "$PY" -m py_compile "$ROOT"/tools/*.py "$ROOT"/patches/*.py "$ROOT"/scripts/*.py

echo "patch builds (each builder emulates its stubs before writing)"
build() {  # name, expected sha256, output, command...
	n=$1; want=$2; f=$3; shift 3
	if "$@" >/dev/null 2>&1 && [ "$(sha "$f")" = "$want" ]; then ok "$n"; else bad "$n (got $(sha "$f" | cut -c1-12))"; fi
}
build "band-unlock r1 D878UV" 885864f8ca9e109cf2c5802b2ee503d0b4c5d89f2eb3174ab1c8b3535b8512d2 \
	"$ROOT/out/D878UV_V4.00_bandunlock_r1/D878UV_V4.00_bandunlock_r1.bin" "$PY" "$ROOT/patches/mkpatch_bandunlock.py" uv
build "band-unlock r1 D878UVII" 76d5b9c87d4621339064730cfe86b03c89e06c9441889644dd537fd7660fcb9a \
	"$ROOT/out/D878UVII_V4.00_bandunlock_r1/D878UVII_V4.00_bandunlock_r1.bin" "$PY" "$ROOT/patches/mkpatch_bandunlock.py" uvii
build "debug channel (on r1) D878UV" 257c2af64a8d4ae9dfbae8580fefd3c5566f59f407bc9859376084df69893376 \
	"$ROOT/out/D878UV_V4.00_bandunlock_r1_dbg1/D878UV_V4.00_bandunlock_r1_dbg1.bin" "$PY" "$ROOT/patches/mkpatch_debug.py"

echo "offline tools"
check "raminit: chanset option table -> Band width desc" sh -c \
	"'$PY' '$ROOT/tools/raminit.py' 200006f0 1 2>/dev/null | grep -q '200006f0 \[ 0\] 080a9680'"
check "radio.py --help" "$PY" "$ROOT/tools/radio.py" --help

echo "harness API (scripts/ghidractl start)"
if curl -s -m3 "${D878_API:-http://127.0.0.1:8081/}program_info" >/dev/null 2>&1; then
	check "decompile shows named globals" sh -c "'$PY' '$ROOT/tools/decr.py' 0x08044cf2 | grep -q 'list_type != 0x11'"
	check "project names match annotations/names.txt" "$PY" - <<PYEOF
import sys; sys.path.insert(0, "$ROOT/tools")
import gh, hcfg
want = dict(l.split() for l in (hcfg.ANNOT / "names.txt").read_text().splitlines() if l.strip())
have = {f["address"]: f["name"] for f in gh.functions()}
miss = [a for a, n in want.items() if have.get(a) != n]
print(len(miss), "differ", miss[:5]); sys.exit(1 if miss else 0)
PYEOF
	check "function boundaries match annotations/functions.txt" "$PY" - <<PYEOF
import sys; sys.path.insert(0, "$ROOT/tools")
import gh, hcfg
want = {}
for l in (hcfg.ANNOT / "functions.txt").read_text().splitlines():
    if l.strip() and not l.startswith("#"):
        a, r = l.split(); want[a] = r
have, off = {}, 0
while True:
    d = gh.get("function_bodies", offset=off, limit=1000)
    for f in d["items"]:
        have[f["address"]] = ",".join("%s-%s" % tuple(x) for x in f["ranges"])
    if not d["has_more"]: break
    off += 1000
bad = [a for a in set(want) | set(have) if want.get(a) != have.get(a)]
print(len(bad), "differ", sorted(bad)[:5]); sys.exit(1 if bad else 0)
PYEOF
	check "xrefs to screen_id" sh -c "'$PY' '$ROOT/tools/ramref.py' 0x20000951 | grep -q ui_key_handler"
	check "callsweep finds at1846_write_reg(0x30,...)" sh -c "'$PY' '$ROOT/tools/callsweep.py' 0x0807A6FC | grep -q 'r0=0x30'"
else
	echo "  skip  (service not running)"
fi
[ $fail = 0 ] && echo "ALL PASSED" || echo "SOME CHECKS FAILED"
exit $fail
