#!/usr/bin/env python3
"""Build a firmware image = baseline (band-unlock r1 + debug channel) + any set of mods.

    .venv/bin/python -I patches/modbuild.py MOD [MOD...]     -> out/D878UV_V4.00_base+MOD+.../
    .venv/bin/python -I patches/modbuild.py --list

Baseline: out/D878UV_V4.00_bandunlock_r1_dbg1 (patches/mkpatch_bandunlock.py uv, then
patches/mkpatch_debug.py); those two stay in place in dead library code and are always present.

Mods live in patches/mods/<name>/: mod.py (NAME, VERSION, RAM bytes, SOURCES, HOOKS, test()) and C
sources. Each mod is compiled on its own (clang, Thumb-2, soft-float, no writable data) and linked by
patches/elflink.py into the MOD AREA; firmware names resolve from annotations/ (patches/mods/fw.h
declares them), the mod's RAM slice is MOD_RAM..MOD_RAM_END.

Mod area (flash after the stock image, never touched by the app; see OVERVIEW "Image"):
  0x080E7600-0x080E77FF  0xFF (rest of the image's last page)
  0x080E7800             header: "D878MODS" u16 format=1 u16 count u32 area_end, then count x 32 B
                         {char name[12]; u16 version; u16 0; u32 code; u32 code_len; u32 ram; u32 ram_len}
  0x080E7C00..           mod code, 16-byte aligned, in command-line order
  ..0x080F57FF           0xFF padding (overwrites stale data from older firmware: deterministic flash)
Mod RAM: 0x20015720-0x20017FFF (above the initial SP), slices 8-byte aligned in command-line order.

Independence rules (enforced): a hook's bytes must match the baseline image AND the stock image (no
overlap with baseline patches) and may not overlap another selected mod's hook; mods share nothing
else. Mods that hook the same site are either/or (e.g. capture vs aprs_rx). Hook replacement code is
assembled at the site with keystone; `{symbol}` names refer to the mod's own symbols and must produce
exactly len(orig) bytes.
"""
import hashlib
import importlib.util
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from keystone import KS_ARCH_ARM, KS_MODE_THUMB, Ks

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
import elflink  # noqa: E402
import hcfg  # noqa: E402
import modemu  # noqa: E402

BASE = hcfg.BASE
STOCK = hcfg.fw_path("uv")
STOCK_SHA = hcfg.MODELS["uv"]["sha256"]["CDD"]
BASELINE = hcfg.OUT / "D878UV_V4.00_bandunlock_r1_dbg1/D878UV_V4.00_bandunlock_r1_dbg1.bin"
BASELINE_SHA = "257c2af64a8d4ae9dfbae8580fefd3c5566f59f407bc9859376084df69893376"
STEM = "D878UV_V4.00_20250725"
MODS = HERE / "mods"

IMG_END = 0x080E7600
AREA = 0x080E7800
AREA_END = 0x080F5800            # firmware's own SPI->flash copy area starts here (FUN_0807CE86)
CODE0 = 0x080E7C00
MAGIC = b"D878MODS"
FORMAT = 1
MAX_MODS = (CODE0 - AREA - 16) // 32
RAM0, RAM_END = 0x20015720, 0x20018000
CFLAGS = ["--target=thumbv7em-none-eabi", "-mcpu=cortex-m4", "-mthumb", "-mfloat-abi=soft", "-Os",
          "-ffreestanding", "-fno-builtin", "-fomit-frame-pointer", "-nostdlib", "-fno-common", "-fno-pic",
          "-fno-unwind-tables", "-fno-asynchronous-unwind-tables", "-fno-exceptions",
          "-Wall", "-Wextra", "-Werror", "-std=c11"]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def load_mod(name):
    d = MODS / name
    spec = importlib.util.spec_from_file_location("mod_" + name, d / "mod.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.NAME == name and len(name.encode()) <= 11, "bad NAME in %s" % d
    m.DIR = d
    return m


def firmware_symbols():
    """name -> (addr, is_function) from annotations/."""
    out = {}
    for line in (hcfg.ROOT / "annotations/names.txt").read_text().splitlines():
        f = line.split()
        if len(f) == 2:
            out[f[1]] = (int(f[0], 16), True)
    for line in (hcfg.ROOT / "annotations/globals.txt").read_text().splitlines():
        f = line.split("#")[0].split()
        if len(f) >= 2:
            out[f[1]] = (int(f[0], 16), False)
    return out


def compile_mod(m, tmp):
    objs = []
    for src in m.SOURCES:
        o = Path(tmp) / (m.NAME + "_" + Path(src).stem + ".o")
        subprocess.run(["clang", *CFLAGS, "-I", str(MODS), "-c", str(m.DIR / src), "-o", str(o)], check=True)
        objs.append(o)
    if len(objs) != 1:
        raise SystemExit("%s: one source file per mod for now (elflink links a single object)" % m.NAME)
    return objs[0].read_bytes()


def main():
    args = sys.argv[1:]
    if args == ["--list"]:
        for d in sorted(MODS.iterdir()):
            if (d / "mod.py").exists():
                m = load_mod(d.name)
                print("%-12s v%-3d RAM %5d  %s" % (m.NAME, m.VERSION, m.RAM, (m.__doc__ or "").strip().splitlines()[0]))
        return
    if not args or any(a.startswith("-") for a in args) or len(set(args)) != len(args):
        sys.exit(__doc__)
    if len(args) > MAX_MODS:
        sys.exit("at most %d mods" % MAX_MODS)

    stock = STOCK.read_bytes()
    assert sha(stock) == STOCK_SHA, "stock image mismatch"
    base = BASELINE.read_bytes()
    assert sha(base) == BASELINE_SHA, "baseline mismatch (rebuild mkpatch_bandunlock.py uv + mkpatch_debug.py)"
    assert len(base) == IMG_END - BASE
    fw = firmware_symbols()
    mods = [load_mod(a) for a in args]

    img = bytearray(base) + b"\xFF" * (AREA_END - IMG_END)
    o = lambda a: a - BASE
    code, ram = CODE0, RAM0
    layout, claimed = [], {}
    ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)
    with tempfile.TemporaryDirectory(prefix="modbuild_", dir=hcfg.OUT) as tmp:
        for m in mods:
            ram = (ram + 7) & ~7
            m_ram, m_ram_end = ram, ram + m.RAM
            if m_ram_end > RAM_END:
                sys.exit("%s: RAM %d doesn't fit (pool ends %#x, slice would end %#x)" % (m.NAME, m.RAM, RAM_END, m_ram_end))
            ext = dict(fw)
            ext.update(MOD_RAM=(m_ram, False), MOD_RAM_END=(m_ram_end, False))
            try:
                blob, syms = elflink.link(compile_mod(m, tmp), code, ext)
            except elflink.LinkError as e:
                sys.exit("%s: link: %s" % (m.NAME, e))
            if code + len(blob) > AREA_END:
                sys.exit("%s: code doesn't fit in the mod area" % m.NAME)
            img[o(code):o(code) + len(blob)] = blob
            for h in m.HOOKS:
                site, orig = h["site"], bytes.fromhex(h["orig"])
                rng = range(site, site + len(orig))
                assert img[o(site):o(site) + len(orig)] == orig, "%s: hook %#x: image bytes differ from orig" % (m.NAME, site)
                assert stock[o(site):o(site) + len(orig)] == orig, "%s: hook %#x overlaps a baseline patch" % (m.NAME, site)
                for a in rng:
                    if a in claimed:
                        sys.exit("%s: hook %#x overlaps %s's hook (mods hooking the same site are either/or)" % (m.NAME, site, claimed[a]))
                    claimed[a] = m.NAME
                rep, _ = ks.asm(h["asm"].format(**{k: "%#x" % (v & ~1) for k, v in syms.items()}), site)
                rep = bytes(rep)
                assert len(rep) == len(orig), "%s: hook %#x: replacement %d bytes, orig %d" % (m.NAME, site, len(rep), len(orig))
                img[o(site):o(site) + len(rep)] = rep
            layout.append(dict(name=m.NAME, version=m.VERSION, code=code, code_len=len(blob),
                               ram=m_ram, ram_len=m.RAM, syms=syms, mod=m))
            print("%-12s code %#x +%-5d RAM %#x +%-5d hooks %s" % (m.NAME, code, len(blob), m_ram, m.RAM,
                  " ".join("%#x" % h["site"] for h in m.HOOKS)))
            code = (code + len(blob) + 15) & ~15
            ram = m_ram_end

    hdr = MAGIC + struct.pack("<HHI", FORMAT, len(layout), AREA_END)
    for L in layout:
        hdr += L["name"].encode().ljust(12, b"\0") + struct.pack("<HHIIII", L["version"], 0, L["code"], L["code_len"], L["ram"], L["ram_len"])
    assert AREA + len(hdr) <= CODE0
    img[o(AREA):o(AREA) + len(hdr)] = hdr

    fwaddr = {k: v[0] for k, v in fw.items()}
    for L in layout:
        e = modemu.Emu(img)
        r = L["mod"].test(e, L, fwaddr)
        print("%-12s emulation OK: %s" % (L["name"], r))

    out = hcfg.OUT / ("D878UV_V4.00_base+" + "+".join(args))
    out.mkdir(parents=True, exist_ok=True)
    binname = out.name + ".bin"
    (out / binname).write_bytes(img)
    (out / (STEM + ".CDD")).write_bytes(img)
    cdi = bytearray(STOCK.with_suffix(".CDI").read_bytes())
    spi = bytearray(STOCK.with_suffix(".spi").read_bytes())
    assert struct.unpack_from("<II", cdi, 256) == (BASE, len(stock)) and struct.unpack_from("<I", spi, 6)[0] == len(stock)
    struct.pack_into("<I", cdi, 260, len(img))
    struct.pack_into("<I", spi, 6, len(img))
    (out / (STEM + ".CDI")).write_bytes(cdi)
    (out / (STEM + ".spi")).write_bytes(spi)
    with open(out / "mods.map", "w") as f:
        for L in layout:
            f.write("# %s v%d code %#x+%d ram %#x+%d\n" % (L["name"], L["version"], L["code"], L["code_len"], L["ram"], L["ram_len"]))
            for k, v in sorted(L["syms"].items(), key=lambda kv: kv[1]):
                f.write("%08x %s\n" % (v, k))
    print("wrote %s  sha256 %s  (%d bytes, image extended by %d)" % (out / binname, sha(img), len(img), len(img) - len(stock)))


if __name__ == "__main__":
    main()
