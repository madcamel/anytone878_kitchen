"""Hardware test for the scope mod (radio running a +scope image, debug channel, never transmits):
snapshot ~580 items of radio state (settings, channel records, frequencies, key table, AT1846S config
registers, tracking filter, GPIO outputs, task states, PC-mode/power-save/squelch flags), run a scope
session (open, scan, knob, zoom in, zoom out, exit by P1) while checking the OS keeps ticking, snapshot
again and print every difference. Dual watch churns a few items on its own (reg 0x17/0x1F/0x2C, TIM3 CCR1,
GPIOD LED bit, task 12 state): run with no scope session in between to see that baseline.

    .venv/bin/python -I patches/mods/scope/hwtest.py [out/<image>/mods.map]"""
import sys, time, struct
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
from radio import Radio

MAP = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "out/D878UV_V4.00_base+aprs_rx+scope/mods.map")
M = {l.split()[1]: int(l.split()[0], 16) for l in open(MAP) if not l.startswith("#")}
r = Radio()
S = [int(l.split("ram ")[1].split("+")[0], 16) for l in open(MAP) if l.startswith("# scope")][0]
call = lambda a, *x: r.call(a | 1, *x)[0]
rr = lambda g: call(0x0807a73a, g) & 0xFFFF
u32 = lambda a: struct.unpack("<I", r.peek(a, 4))[0]
STATUS_REGS = {0x0D, 0x0F, 0x13, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x20, 0x21, 0x22, 0x23, 0x29, 0x2A, 0x3B, 0x7A}


def snap():
    while r.peek(0x20000CB8, 1)[0] != r.peek(0x20008024, 1)[0]:   # wait until RF is on the main side
        time.sleep(0.05)
    d = {}
    rs = r.peek(0x20007FF8, 256)
    for i, b in enumerate(rs):
        d["settings[%#04x]" % i] = b
    for n, a in (("chan_rec_a", 0x200049B8), ("chan_rec_b", 0x20004A38)):
        b = r.peek(a, 128)
        for i in range(0, 128, 4):
            d["%s+%#04x" % (n, i)] = b[i:i + 4].hex()
    for n, a in (("rx_freq_a", 0x200003C4), ("rx_freq_b", 0x200003C8), ("tx_freq_a", 0x200003B8),
                 ("tx_freq_10hz", 0x200003B4), ("rx_freq_10hz", 0x200003C0), ("cur_chan_rec", 0x200003DC),
                 ("other_chan_rec", 0x200003E0)):
        d[n] = u32(a)
    for n, a in (("screen_id", 0x20000951), ("pc_mode_active", 0x200010D4), ("powersave_active", 0x20000CB5),
                 ("ana_sq_open", 0x2000003E), ("rf_tx_active", 0x200001B1), ("rf_side", 0x20000CB8)):
        d[n] = r.peek(a, 1)[0]
    kt = r.peek(0x200001B4, 112 * 4)
    for i in range(112):
        if i != 55:
            d["key_fn_table[%d]" % i] = kt[4 * i:4 * i + 4].hex()
    for g in range(0x7F):
        if g not in STATUS_REGS:
            d["at1846[%#04x]" % g] = rr(g)
    d["TIM3_CCR1 (track filter)"] = u32(0x40000434)
    for p, base in (("A", 0x40010800), ("B", 0x40010C00), ("C", 0x40011000), ("D", 0x40011400), ("E", 0x40011800)):
        o = u32(base + 0x0C)
        if p == "E": o &= ~0x7F80          # LCD/keypad data bus
        if p == "B": o &= ~0x3C00          # LCD control
        d["GPIO%s_ODR" % p] = "%04x" % o
    for prio in list(range(16)) + [30, 62, 63]:
        t = u32(0x20013930 + 4 * prio)
        if t not in (0, 1):
            d["task%d.stat" % prio] = r.peek(t + 0x30, 1)[0] & ~0x01 if prio in (0,) else r.peek(t + 0x30, 1)[0]
    return d


def os_ok():
    a, c = u32(0x20001328), u32(0x200012E4)
    time.sleep(0.4)
    return u32(0x20001328) - a > 40 and u32(0x200012E4) - c > 40 and r.peek(0x200010D4, 1)[0] == 0


def wait_idle():
    for _ in range(40):
        if r.peek(S + 4, 1)[0] == 0:
            return True
        time.sleep(0.1)
    return False


assert r.peek(0x20000951, 1)[0] == 0 and not r.peek(0x200001B1, 1)[0]
assert os_ok(), "OS not healthy before the test"
call(0x08062994); time.sleep(1.0)             # same wake a key press does, then let it settle
before = snap()
call(0x08062994); time.sleep(0.2)
call(M["scope_open"])
time.sleep(6)
assert os_ok(), "OS frozen while scope open"
for k in (0x11, 0x11, 0x12, 0x14):
    r.key(k); time.sleep(0.3)
time.sleep(5)
for k in (0x13, 0x13):
    r.key(k); time.sleep(0.4)
assert wait_idle(), "scope did not return to idle"
time.sleep(1.5)
assert os_ok(), "OS frozen after exit"
call(0x08062994); time.sleep(1.0)
after = snap()
diff = [(k, before[k], after.get(k)) for k in before if before[k] != after.get(k)]
print("%d items compared, %d differ" % (len(before), len(diff)))
for k, a, b in diff:
    print("  %-28s %s -> %s" % (k, a, b))
