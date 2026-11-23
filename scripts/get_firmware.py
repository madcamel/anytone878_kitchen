#!/usr/bin/env python3
"""Copy the stock firmware files the harness expects into firmware/, verifying sha256.

    python3 scripts/get_firmware.py <vendor .zip or extracted folder> [...]

The firmware is not distributed with this repo; get the official D878UV / D878UVII V4.00
(2025-07-25) packages from the AnyTone distributor (e.g. BridgeCom support pages). Any file
named like <stem>.CDD/.CDI/.spi inside the given zips or folders is checked against
tools/hcfg.py MODELS and copied if it matches. Files with other hashes are reported and skipped.
"""
import hashlib
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import hcfg  # noqa: E402

WANT = {}
for model, m in hcfg.MODELS.items():
    for ext, h in m["sha256"].items():
        WANT[h] = (model, "%s.%s" % (m["stem"], ext))


def candidates(src):
    p = Path(src)
    if p.is_dir():
        for f in p.rglob("*"):
            if f.is_file() and f.suffix.lower() in (".cdd", ".cdi", ".spi"):
                yield str(f), f.read_bytes()
    elif zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if Path(n).suffix.lower() in (".cdd", ".cdi", ".spi"):
                    yield "%s:%s" % (p.name, n), z.read(n)
    else:
        raise SystemExit("%s: not a folder or zip" % src)


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    hcfg.FW_DIR.mkdir(parents=True, exist_ok=True)
    got = set()
    for src in sys.argv[1:]:
        for name, data in candidates(src):
            h = hashlib.sha256(data).hexdigest()
            if h in WANT:
                model, fn = WANT[h]
                (hcfg.FW_DIR / fn).write_bytes(data)
                got.add(fn)
                print("ok   %-28s <- %s" % (fn, name))
            elif "V4.00" in name:
                print("skip %s (unknown sha256 %s...)" % (name, h[:12]))
    for model, m in hcfg.MODELS.items():
        missing = [e for e in m["sha256"] if "%s.%s" % (m["stem"], e) not in got
                   and not (hcfg.FW_DIR / ("%s.%s" % (m["stem"], e))).exists()]
        print("%-5s %s" % (model, "complete" if not missing else "missing " + ", ".join(missing)))


if __name__ == "__main__":
    main()
