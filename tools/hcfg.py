"""Shared paths and constants for the D878UV firmware harness.

Every tool imports this instead of hard-coding locations. Overrides via environment:
  D878_ROOT      repo root (default: parent of tools/)
  D878_FW_DIR    directory holding the stock firmware files (default: <root>/firmware)
  D878_API       base URL of scripts/ghidra_serve.py's API (default: http://127.0.0.1:8081/)
  D878_PORT      radio serial port (default: /dev/ttyACM0)
  GHIDRA_INSTALL_DIR  Ghidra 12.x install (used by the shell scripts / service)
"""
import os
from pathlib import Path

ROOT = Path(os.environ.get("D878_ROOT", Path(__file__).resolve().parent.parent))
FW_DIR = Path(os.environ.get("D878_FW_DIR", ROOT / "firmware"))
OUT = ROOT / "out"
WORK = ROOT / "work"
CACHE = WORK / "cache"              # decompile cache (tools/dump.py), menu map (tools/menumap.py)
ANNOT = ROOT / "annotations"        # names.txt / globals.txt: the shared RE knowledge
API = os.environ.get("D878_API", "http://127.0.0.1:8081/")
PORT = os.environ.get("D878_PORT", "/dev/ttyACM0")

BASE = 0x08004000                   # app load address (16 KB bootloader below, not in the update file)
CODE_END = 0x080A0000               # code below, strings/menus/tables above

# Stock images the harness is built against. The Ghidra project, all addresses in annotations/,
# and every patch target this exact file; tools refuse anything else.
MODELS = {
    "uv": dict(stem="D878UV_V4.00_20250725",
               sha256={"CDD": "aae150dc3ff7f64c5c1f3a64abc7d91a59438635d7cb946e7aec68011dad4b55",
                       "CDI": "8af519a75446992e19f3cdca5f44df03a9e2837654571816238753e99fc2c11a",
                       "spi": "13926fbc6a254af09515719f4d88b3ec47f01c901545589fd0367728cba4118f"}),
    "uvii": dict(stem="D878UVII_V4.00_20250725",
                 sha256={"CDD": "8f840cfcfc1bb9acab65429ced211481eeb80079725ec46cf225afce071afeeb",
                         "CDI": "1c3656463d24ed165e0617d77c97479f462b443b2ff30e28b41df3a1f4679abe",
                         "spi": "0f815683ad6594f2ebc1b7c655ee9a94cafe1c2ac07c92bf5af5e6687e79aaac"}),
}


def fw_path(model="uv", ext="CDD"):
    return FW_DIR / ("%s.%s" % (MODELS[model]["stem"], ext))


def load_fw(model="uv"):
    """Stock .CDD bytes, hash-checked. Run scripts/get_firmware.py first."""
    import hashlib
    p = fw_path(model)
    if not p.exists():
        raise SystemExit("missing %s: run scripts/get_firmware.py <vendor zip or dir>" % p)
    b = p.read_bytes()
    if hashlib.sha256(b).hexdigest() != MODELS[model]["sha256"]["CDD"]:
        raise SystemExit("%s is not the expected stock image (sha256 mismatch)" % p)
    return b
