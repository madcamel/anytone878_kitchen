#!/usr/bin/env python3
"""Build a band-unlocked D878UV V4.00 firmware: region 1 opened up, calibration lookups clamped.

Needs keystone-engine, capstone, unicorn:
    scripts/setup_venv.sh
    .venv/bin/python -I patches/mkpatch_bandunlock.py [uv|uvii]

Patch points are located by signature (patches/bandunlock_sigs.py); the D878UV addresses below are
the reference — the finder must reproduce them exactly, and other models must match their shape
(same registers/bases/steps in the same order) before anything is written.

What it does (see OVERVIEW.md "RF path, limits, calibration"):
 1. Region REGION rows of the six RX and six TX band tables get the WIDE limits below.
 2. All 14 frequency-indexed calibration lookups compute an unsigned (f - BASE)/STEP index with no
    range guard; outside the stock band that index wraps or runs off the table. Each is redirected
    (b.w) to a stub that clamps (f - BASE) into [0, SPAN] — i.e. frequencies outside a table get the
    nearest stock-calibrated edge value — then branches back. No LR use (one host is a leaf).
 3. Stubs live in dead library code (FLASH_EraseAll*Pages, tail of FLASH_BootConfig) — image length
    is unchanged, so the stock .CDI/.spi still describe it.
Every stub is executed under unicorn with test frequencies before the image is written.
"""
import hashlib
import shutil
import struct
import sys
from pathlib import Path

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs
from keystone import KS_ARCH_ARM, KS_MODE_THUMB, Ks
from unicorn import UC_ARCH_ARM, UC_MODE_THUMB, Uc
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_CPSR
import unicorn.arm_const as A

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import bandunlock_sigs as SIG  # noqa: E402
import hcfg  # noqa: E402

ROOT = hcfg.ROOT
MODELS = {
    m: dict(stock=hcfg.fw_path(m), sha256=hcfg.MODELS[m]["sha256"]["CDD"],
            out=hcfg.OUT / ("%s_V4.00_bandunlock_r1" % name), stem=hcfg.MODELS[m]["stem"],
            binname="%s_V4.00_bandunlock_r1.bin" % name)
    for m, name in (("uv", "D878UV"), ("uvii", "D878UVII"))
}
REF_STOCK = MODELS["uv"]["stock"]
BASE = 0x08004000

REGION = 1
MHZ = 100_000                       # firmware frequency unit is 10 Hz
# Band classification elsewhere is by the BCD MHz byte: <200 VHF, 200-309 "220", >=310 UHF;
# at1846_set_freq splits VHF/UHF at 300. Keep the three ranges inside those buckets.
# Limits = measured AT1846S PLL lock range (docs/patch.md "Measured synthesizer limits"): one VCO
# 779-1155 MHz, /6 /4 /2. Margin: VCO >= 785 (floor 779) and <= 1145 (cal code pins at 511 above).
WIDE = {"vhf": (13_100_000, 17_800_000),    # 131.000 - 178.000 MHz (locks 129.84 - 178.17; /6->/4 switch at 178.18)
        "m220": (20_000_000, 28_600_000),   # 200.000 - 286.000 MHz (locks 194.75 - 288.73; <200 is the VHF bucket)
        "uhf": (39_300_000, 57_200_000)}    # 393.000 - 572.000 MHz (locks 389.48 - ~577.6)
TABLES = {  # (lo, hi) table addresses, 18 x u32 each
    "rx": {"uhf": (0x080A0C04, 0x080A0C4C), "vhf": (0x080A0D24, 0x080A0D6C), "m220": (0x080A0E44, 0x080A0E8C)},
    "tx": {"uhf": (0x080A0C94, 0x080A0CDC), "vhf": (0x080A0DB4, 0x080A0DFC), "m220": (0x080A0ED4, 0x080A0F1C)},
}
STOCK_ROW1 = {"vhf": (13_600_000, 17_400_000), "m220": (22_000_000, 22_500_000), "uhf": (40_000_000, 48_000_000)}

# Table-lookup sites: `ldr Rb,=BASE` at site-2, `subs Rd,Rs,Rb` at site. SPAN = stock band width.
TABLE_SITES = [  # site, Rd, Rs, Rb, base MHz, span MHz, host
    (0x0805FB16, "r0", "r0", "r1", 400, 120, "rf_squelch_cal_apply UHF"),
    (0x0805FB28, "r0", "r0", "r1", 136, 38, "rf_squelch_cal_apply VHF"),
    (0x0805FDBA, "r6", "r0", "r1", 400, 120, "rf_frontend_tune UHF"),
    (0x0805FEC8, "r6", "r0", "r1", 136, 38, "rf_frontend_tune VHF"),
    (0x0805FF2E, "r6", "r0", "r1", 220, 5, "rf_frontend_tune 220"),
    (0x0806AEBE, "r1", "r2", "r3", 136, 38, "tx_power_cal_lookup VHF (leaf fn)"),
    (0x0806AFCC, "r1", "r2", "r3", 400, 120, "tx_power_cal_lookup UHF (leaf fn)"),
    (0x0806B150, "r5", "r0", "r1", 136, 38, "tx_power_apply VHF"),
    (0x0806B2DA, "r5", "r0", "r1", 400, 120, "tx_power_apply UHF"),
]
# DMR calibration segments: `sub.w r9,r0,r1` at site (r1 = base already). Clamp keeps each
# segment within its stock-calibrated span (155 seg ends at 174; 174 seg serves 220-225).
DMR_SITES = [  # site, base MHz, span MHz
    (0x0808326A, 460, 60), (0x080832E0, 400, 60), (0x080833EE, 174, 51),
    (0x0808346A, 155, 19), (0x080834DC, 136, 19),
]
HOLES = [(0x08007538, 0x08007628), (0x08007B8C, 0x08007CA0)]  # verified unreferenced dead code
HOLE_HOST_HEADS = [(0x08007B28, 0x08007B8C)]  # dead FLASH_BootConfig head may reference its own tail

ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)
cs = Cs(CS_ARCH_ARM, CS_MODE_THUMB)


def asm(src, addr):
    enc, _ = ks.asm(src, addr)
    return bytes(enc)


def movc(reg, val):
    return f"movw {reg}, #{val & 0xffff}; movt {reg}, #{val >> 16}"


def table_stub(rd, rs, rb, base, span, back):
    t = next(r for r in ("r2", "r3", "r7", "r4") if r not in (rd, rb))
    return (f"{movc(rb, base)}; subs {rd}, {rs}, {rb}; bcs Lok; movs {rd}, #0; Lok: push {{{t}}}; "
            f"{movc(t, span)}; cmp {rd}, {t}; it hi; movhi {rd}, {t}; pop {{{t}}}; b.w {back:#x}")


def dmr_stub(span, back):
    return (f"subs.w r9, r0, r1; bcs Lok; mov.w r9, #0; Lok: push {{r2}}; {movc('r2', span)}; "
            f"cmp r9, r2; it hi; movhi r9, r2; pop {{r2}}; b.w {back:#x}")


def emulate(img, entry, regs, back):
    """Run a stub from entry until it branches to back; return final registers."""
    uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    uc.mem_map(0x08000000, 0x00100000)
    uc.mem_write(BASE, bytes(img))
    uc.mem_map(0x20000000, 0x00020000)
    uc.reg_write(UC_ARM_REG_SP, 0x2001F000)
    for r, v in regs.items():
        uc.reg_write(getattr(A, f"UC_ARM_REG_{r.upper()}"), v)
    uc.emu_start(entry | 1, back, count=64)
    pc = uc.reg_read(UC_ARM_REG_PC)
    assert pc == back, f"stub @{entry:#x} ended at {pc:#x}, expected {back:#x}"
    assert uc.reg_read(UC_ARM_REG_SP) == 0x2001F000, "stack unbalanced"
    return {r: uc.reg_read(getattr(A, f"UC_ARM_REG_{r.upper()}")) for r in
            ("r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r9")}


def locate(stock, ref):
    """Return (tables, table_sites, dmr_sites) for this image, shape-checked against the reference."""
    tables = SIG.find_tables(stock, ref, TABLES)
    t, d = SIG.find_sites(stock)
    shape = lambda L: [(x.get("rd"), x.get("rs"), x.get("rb"), x["base"]) for x in L]
    ref_t = [(rd, rs, rb, b) for _, rd, rs, rb, b, _, _ in TABLE_SITES]
    ref_d = [(None, None, None, b) for _, b, _ in DMR_SITES]
    assert shape(t) == ref_t, f"table-site shape differs: {shape(t)}"
    assert shape(d) == ref_d, f"dmr-site shape differs: {shape(d)}"
    tsites = [(x["site"],) + r[1:] for x, r in zip(t, TABLE_SITES)]
    dsites = [(x["site"],) + r[1:] for x, r in zip(d, DMR_SITES)]
    # Library region (holes) must be byte-identical to the reference, and unreferenced here.
    for lo, hi in HOLES + HOLE_HOST_HEADS:
        assert stock[lo - BASE:hi - BASE] == ref[lo - BASE:hi - BASE], f"hole {lo:#x} differs from reference"
    bad = SIG.unsafe_refs(stock, HOLES, exclude=HOLE_HOST_HEADS)
    assert not bad, f"references into holes: {bad}"
    mids = [s for s, *_ in tsites] + [s + 2 for s, *_ in dsites]
    bad = SIG.unsafe_refs(stock, [(m, m + 2) for m in mids])
    assert not bad, f"branches/literals into the middle of a patch site: {bad}"
    return tables, tsites, dsites


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "uv"
    M = MODELS[model]
    STOCK, OUT = M["stock"], M["out"]
    stock = STOCK.read_bytes()
    sha = hashlib.sha256(stock).hexdigest()
    print(f"model {model}: {STOCK.name} sha256 {sha}")
    if M["sha256"] and sha != M["sha256"]:
        sys.exit(f"unexpected stock image hash: {STOCK}")
    ref = REF_STOCK.read_bytes()
    tables, tsites, dsites = locate(stock, ref)
    if model == "uv":
        assert tables == TABLES and [s[0] for s in tsites] == [s[0] for s in TABLE_SITES] \
            and [s[0] for s in dsites] == [s[0] for s in DMR_SITES], "finder no longer reproduces reference"
    img = bytearray(stock)
    off = lambda a: a - BASE
    u32 = lambda a: struct.unpack_from("<I", img, off(a))[0]

    # 1. Region tables.
    for kind in ("rx", "tx"):
        for band, (lo_tab, hi_tab) in tables[kind].items():
            for tab, idx, val in ((lo_tab, 0, WIDE[band][0]), (hi_tab, 1, WIDE[band][1])):
                a = tab + 4 * REGION
                assert u32(a) == STOCK_ROW1[band][idx], f"{kind} {band} table {tab:#x}: unexpected {u32(a)}"
                struct.pack_into("<I", img, off(a), val)

    # 2. Stubs + site redirects.
    holes = [[lo, hi] for lo, hi in HOLES]
    placed = []

    def place(code_fn, need_back):
        for h in holes:
            addr = (h[0] + 3) & ~3
            code = code_fn(addr)
            if addr + len(code) <= h[1]:
                img[off(addr):off(addr) + len(code)] = code
                h[0] = addr + len(code)
                return addr, code
        sys.exit("out of hole space")

    for site, rd, rs, rb, bmhz, smhz, host in tsites:
        patch_at, back = site - 2, site + 2
        orig = cs.disasm(bytes(img[off(patch_at):off(patch_at) + 4]), patch_at)
        o = [f"{i.mnemonic} {i.op_str}" for i in orig]
        assert o[0].startswith(f"ldr {rb}, [pc") and o[1] == f"subs {rd}, {rs}, {rb}", (hex(site), o)
        stub, code = place(lambda a: asm(table_stub(rd, rs, rb, bmhz * MHZ, smhz * MHZ, back), a), back)
        img[off(patch_at):off(patch_at) + 4] = asm(f"b.w {stub:#x}", patch_at)
        placed.append(("table", site, stub, len(code), host, rd, rs, rb, bmhz, smhz, patch_at, back))

    for site, bmhz, smhz in dsites:
        back = site + 4
        o = [f"{i.mnemonic} {i.op_str}" for i in cs.disasm(bytes(img[off(site):off(site) + 4]), site)]
        assert o in (["sub.w r9, r0, r1"], ["sub.w sb, r0, r1"]), (hex(site), o)
        stub, code = place(lambda a: asm(dmr_stub(smhz * MHZ, back), a), back)
        img[off(site):off(site) + 4] = asm(f"b.w {stub:#x}", site)
        placed.append(("dmr", site, stub, len(code), f"dmr_freq_cal_send seg {bmhz}", None, "r0", "r1", bmhz, smhz, site, back))

    # 3. Verify every stub by emulation: below band, at edges, inside, above.
    print("emulated clamps (f MHz -> f-BASE clamped, in MHz):")
    for kind, site, stub, n, host, rd, rs, rb, bmhz, smhz, patch_at, back in placed:
        tests = [bmhz - 20, bmhz, bmhz + smhz / 2, bmhz + smhz, bmhz + smhz + 30]
        res = []
        for f in tests:
            fv = int(f * MHZ)
            if kind == "table":
                regs = {r: 0xA5A50000 + i for i, r in enumerate(("r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7"))}
                regs[rs] = fv
                out = emulate(img, patch_at, regs, back)  # start at the site's b.w itself
                got = out[rd]
                assert out[rb] == bmhz * MHZ, "base reg not restored"
                keep = {r for r in regs if r not in (rd, rb)}
                assert all(out[r] == regs[r] for r in keep if r != rs or rs == rd), f"clobbered regs at {site:#x}"
            else:
                regs = {"r0": fv, "r1": bmhz * MHZ, "r2": 0x1234, "r9": 0xDEAD}
                out = emulate(img, site, regs, back)
                got = out["r9"]
                assert out["r2"] == 0x1234, "r2 clobbered"
            want = min(max(fv - bmhz * MHZ, 0), smhz * MHZ)
            assert got == want, f"{host} f={f}: got {got} want {want}"
            res.append(f"{f:g}->{got / MHZ:g}")
        print(f"  {host:<36} @{site:#010x} stub {stub:#010x} ({n:3d} B): " + ", ".join(res))

    # 4. Report and write.
    diffs, i = [], 0
    while i < len(img):
        if img[i] != stock[i]:
            j = i
            while j < len(img) and img[j] != stock[j]:
                j += 1
            diffs.append((i + BASE, j - i))
            i = j
        else:
            i += 1
    print(f"{len(diffs)} changed ranges, {sum(n for _, n in diffs)} bytes; holes left:",
          [f"{h[0]:#x}-{h[1]:#x} ({h[1] - h[0]} B)" for h in holes])
    assert len(img) == len(stock)

    OUT.mkdir(parents=True, exist_ok=True)
    stem = M["stem"]
    (OUT / f"{stem}.CDD").write_bytes(img)
    (OUT / M["binname"]).write_bytes(img)
    for ext in ("CDI", "spi"):
        shutil.copy(STOCK.with_suffix(f".{ext}"), OUT / f"{stem}.{ext}")
    print("sha256", hashlib.sha256(img).hexdigest())
    print("wrote", OUT)


if __name__ == "__main__":
    main()
