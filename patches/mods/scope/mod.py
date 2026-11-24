"""scope: on-radio spectrum waterfall of the current band; PF key function 55 opens it, P2/P1 zoom.
Assign "Predicting" to a PF/P key in the radio's key menu. Knob = cursor, P2 = zoom in (finest level:
tune VFO there), P1 = zoom out / exit, Exit = exit, PTT = exit and transmit."""
import struct

from unicorn.arm_const import UC_ARM_REG_SP

NAME = "scope"
VERSION = 1
RAM = 7552                        # struct state (7544 B: 64x160 4-bit waterfall, 2 KB task stack)
SOURCES = ["scope.c"]
HOOKS = [
    dict(site=0x080124E2, orig="fef712fd", asm="bl {scope_key}",
         why="task_p10_keys_ui -> ui_key_handler: scope owns the keys while open; installs key_fn_table[55]"),
    dict(site=0x080124CE, orig="18f0d9fa", asm="bl {scope_ptt}",
         why="task_p10_keys_ui -> ptt_press: hand the RF back (scan stopped, radio restored) before TX"),
    dict(site=0x080427E4, orig="fff7b8fb", asm="bl {scope_render}",
         why="task_p12_display -> ui_render: draw the scope screen (0x22) under lcd_bus_sem"),
    # Key function 55 "Predicting" (the scope's entry) is hidden by stock in every key picker and
    # scrubbed from the settings at load; these four byte patches make it selectable and keep it.
    dict(site=0x08050882, orig="3728", asm="cmp r0, #0xff",
         why="keyfn_menu_to_id: `if (v > 0x36) v++` skips ID 55 -> never"),
    dict(site=0x0804C0A4, orig="3728", asm="cmp r0, #0xff",
         why="keyfn_id_to_menu: inverse of the skip -> never"),
    dict(site=0x0804C0B0, orig="521e", asm="subs r2, r2, #0",
         why="keyfn_id_to_menu: list_count - 1 for the hidden entry -> keep the entry"),
    dict(site=0x080267E0, orig="fff7d8fe", asm="nop\nnop",
         why="settings_sanitize -> keyfn_predicting_fixup: clears a long key when the short is 55, resets long 55 to Off"),
]

# struct state offsets (pinned by _Static_assert in scope.c)
O_STATE, O_DIRTY, O_STANDBY, O_CURSOR, O_LEVEL, O_ACQ, O_DIV, O_NROWS = 4, 5, 6, 7, 8, 9, 10, 11
O_GEN, O_START, O_SPAN, O_LO, O_HI, O_TUNE, O_CUR = 16, 20, 24, 28, 32, 36, 56
O_SPEED = 7544
ST_IDLE, ST_RUN, ST_STOP, ST_HALT, ST_HALTED = 0, 1, 2, 3, 4
SCOPE_ID = 0x22
SIGNAL = 45_000_000               # 450.000 MHz, 10 Hz units


CAL_VHF = [400 + 90 * i for i in range(39)]          # fake tracking-filter calibration tables
CAL_220 = [3000 + 50 * i for i in range(6)]
CAL_UHF = [600 + 280 * i for i in range(13)]


def track_pwm(f):
    """stock rf_frontend_tune_from_tables maths with edge clamps (reference for the mod's C)"""
    ip = lambda t, i, fr: (t[i] + ((((t[i + 1] - t[i]) & 0xFFFF) * fr) & 0xFFFF) // 10) & 0xFFFF
    if f < 31000000:
        if f <= 17400000:
            g = max(f, 13600000) - 13600000; i = g // 100000
            return ip(CAL_VHF, i, g // 10000 % 10) if i < 38 else CAL_VHF[38]
        g = max(f, 22000000) - 22000000; i = g // 100000
        return ip(CAL_220, i, g // 10000 % 10) if i < 5 else CAL_220[5]
    g = max(f, 40000000) - 40000000; i = g // 1000000
    return ip(CAL_UHF, i, g // 100000 % 10) if i < 12 else CAL_UHF[12]


def bcd(f):
    s = "%08d" % f
    return bytes(int(s[i:i + 2], 16) for i in range(0, 8, 2))


def test(e, m, fw):
    S, sy = m["ram"], m["syms"]
    RS = fw["radio_settings"]
    REC = 0x20010000                  # VFO record (main side)
    MEM = 0x20010100                  # a memory channel record
    e.write(S, bytes(m["ram_len"]))
    e.write(MEM, bcd(44612500) + bytes(60))
    for name, t in (("rf_tune_cal_vhf", CAL_VHF), ("rf_tune_cal_220", CAL_220), ("rf_tune_cal_uhf", CAL_UHF)):
        e.write(fw[name], struct.pack("<%dH" % len(t), *t))
    e.write(fw["key_fn_table"], bytes(4 * 112))
    e.write(fw["OSTCBPrioTbl"], bytes(4 * 64))
    e.w32(fw["cur_chan_rec"], REC)
    e.write(REC, bcd(14710500) + bytes(60))
    e.w8(fw["screen_id"], 0); e.w8(fw["rf_tx_active"], 0)
    e.w8(RS + 0x2C, 0); e.w8(RS + 0x15, 1); e.w8(RS + 0x2D, 1); e.w8(RS + 0x0B, 1)

    log = {k: [] for k in ("ukh", "render", "ptt", "wake", "post", "text", "reload", "apply", "cmd",
                           "task", "dly", "vhf", "uhf", "sem", "pwm", "vm")}
    chip = dict(word=0, wrong=0, regs={0x30: 0x4026, 0x5A: 0x0CDD})

    def rd(e):
        r = e.arg(0)
        if r in (0x29, 0x2A):
            f_khz = chip["word"] / 16.0
            div = 6 if f_khz < 200000 else 4 if f_khz < 300000 else 2
            rb = int(round(f_khz * div / 6 * 4424.6)) + (5_000_000 if chip["wrong"] else 0)
            return rb >> 16 if r == 0x29 else rb & 0xFFFF
        if r == 0x1B:
            f10 = chip["word"] * 100 // 16
            return (60 if abs(f10 - SIGNAL) < 1250 else 30) << 8
        return chip["regs"].get(r, 0)

    def wr(e):
        r, v = e.arg(0), e.arg(1)
        chip["regs"][r] = v
        if r == 0x29: chip["word"] = (v << 16) | (chip["word"] & 0xFFFF)
        if r == 0x2A: chip["word"] = (chip["word"] & ~0xFFFF) | v

    def cstr(a):
        b = e.read(a, 32)
        return b[:b.index(0)].decode()

    def task(e):
        log["task"].append((e.arg(0), e.arg(2), e.arg(3)))
        e.w32(fw["OSTCBPrioTbl"] + 4 * e.arg(3), 1)

    def dly(e):
        log["dly"].append(e.arg(0))
        if e.r8(S + O_STATE) == ST_HALT:          # p10 waiting: the scan task acknowledges the halt
            e.w8(S + O_STATE, ST_HALTED)

    vm_works = [True]

    def vfo_mr(e):                                # stock V/M toggle: main side to VFO, record = VFO record
        log["vm"].append(1)
        if vm_works[0] and e.r8(fw["screen_id"]) in (0, 9):
            e.w8(RS + 0x15, 1 - e.r8(RS + 0x15))
            e.w32(fw["cur_chan_rec"], REC if e.r8(RS + 0x15) == 1 else MEM)

    def f2b(e):
        e.write(e.arg(1), bcd(e.arg(0)))

    for n, fn in (("at1846_read_reg", rd), ("at1846_write_reg", wr),
                  ("ui_key_handler", lambda e: log["ukh"].append(1)), ("ui_render", lambda e: log["render"].append(1)),
                  ("ptt_press", lambda e: log["ptt"].append(e.r8(S + O_STATE))), ("ui_activity_wake", lambda e: log["wake"].append(1)),
                  ("app_event_post", lambda e: log["post"].append(e.arg(0))),
                  ("lcd_text_centered", lambda e: None),
                  ("lcd_set_window", lambda e: None), ("lcd_write_cmd", lambda e: None),
                  ("sides_reload_freqs", lambda e: log["reload"].append(1)), ("chan_apply", lambda e: log["apply"].append(1)),
                  ("rf_cmd_post", lambda e: log["cmd"].append(e.arg(0))), ("OSTaskCreate", task), ("OSTimeDly", dly),
                  ("OSSemPend", lambda e: log["sem"].append(("P", e.arg(0)))), ("OSSemPost", lambda e: log["sem"].append(("V", e.arg(0)))),
                  ("at1846_vhf_frontend_set", lambda e: log["vhf"].append(e.arg(0))),
                  ("at1846_uhf_frontend_set", lambda e: log["uhf"].append(e.arg(0))),
                  ("freq_10hz_to_bcd", f2b), ("key_pf_vfo_mr", vfo_mr),
                  ("frontend_tune_pwm_write", lambda e: log["pwm"].append((chip["word"], e.arg(0))))):
        e.stub(fw[n], fn)
    # lcd_text_centered(x0, x1, y, h, str, flag): str is the 5th argument, on the stack
    e.stubs[fw["lcd_text_centered"] & ~1] = lambda e: log["text"].append(cstr(e.r32(e.u.reg_read(UC_ARM_REG_SP))))

    call = lambda name: e.call(sy[name])
    st = lambda: e.r8(S + O_STATE)

    # 0. key pickers: ID 55 selectable for short and long keys, round trip consistent
    e.w8(fw["aprs_board_present"], 1)
    for k, n in ((1, 57), (6, 53)):                      # PF1 short (no skips), PF1 long (4 momentary skipped)
        ids = [e.call(fw["keyfn_menu_to_id"], v, k) for v in range(n)]
        assert 55 in ids and ids[-1] == 56 and len(set(ids)) == n, (k, ids[-4:])
        e.w32(fw["list_count"], 57)
        assert e.call(fw["keyfn_id_to_menu"], 55, k) == ids.index(55) and e.r32(fw["list_count"]) == n, (k, e.r32(fw["list_count"]))

    # 1. closed: everything passes through, and table[55] gets installed
    e.w8(fw["key_code"], 0x0D)
    call("scope_key"); call("scope_render")
    assert log["ukh"] == [1] and log["render"] == [1], log
    assert e.r32(fw["key_fn_table"] + 4 * 55) & ~1 == sy["scope_open"] & ~1, "key_fn_table[55] not installed"

    # 2. band selection from the main side's frequency
    for f, lo, hi, div in ((14710500, 13100000, 17800000, 6), (22000000, 20000000, 28600000, 4),
                           (44600000, 39300000, 57200000, 2)):
        e.w32(fw["rx_freq_a"], f); e.w8(fw["screen_id"], 0); e.w8(S + O_STATE, ST_IDLE)
        call("scope_open")
        assert e.r8(fw["screen_id"]) == SCOPE_ID and st() == ST_RUN
        assert (e.r32(S + O_LO), e.r32(S + O_HI), e.r8(S + O_DIV)) == (lo, hi, div), hex(f)
        cur = e.r8(S + O_CURSOR)
        assert abs(lo + (hi - lo) * (2 * cur + 1) // 320 - f) <= (hi - lo) // 160, "cursor not on the current frequency"
    assert len(log["task"]) == 1 and log["task"][0][2] == 15, "scan task created once at prio 15"
    assert log["task"][0][1] == S + 5496 + 511 * 4, "task stack top"

    # speed keys: default x2 (80 measurements per row); Up faster, Down slower, clamped 0..3
    assert e.r8(S + O_SPEED) == 1
    for k, want in ((0x0F, 2), (0x0F, 3), (0x0F, 3), (0x10, 2), (0x10, 1), (0x10, 0), (0x10, 0)):
        e.w8(fw["key_code"], k); call("scope_key"); assert e.r8(S + O_SPEED) == want, (k, e.r8(S + O_SPEED))

    # 3. one full-resolution coarse sweep (UHF): radio taken over, a row lands in the waterfall
    for _ in range(160):
        call("scope_scan_step")
    assert e.r8(RS + 0x2D) == 0 and e.r8(RS + 0x0B) == 0, "dual watch / power save not paused"
    assert chip["regs"][0x49] == 0x3FFF and chip["regs"][0x05] == 0x8763
    assert chip["regs"][0x5A] == 0x08DD, "RSSI averaging not set to 2 while scanning"
    assert log["uhf"][-1] == 1 and log["vhf"][-1] == 0, "UHF front end not selected"
    assert e.r8(S + O_NROWS) == 1 and 0x40000000 in log["post"]
    vals = e.read(S + O_CUR, 160)
    assert all(v == 30 or v == 60 for v in vals), vals
    # tracking filter set before each synthesizer write, to the stock value for that frequency
    assert len(log["pwm"]) >= 160
    for (prev_word, v), (w, _) in zip(log["pwm"], log["pwm"][1:] + [(chip["word"], 0)]):
        assert v == track_pwm(w * 100 // 16), (w, v, track_pwm(w * 100 // 16))
    pend = [x for x in log["sem"] if x[0] == "P"]; post = [x for x in log["sem"] if x[0] == "V"]
    assert len(pend) == len(post), "bus semaphore unbalanced"

    # speed x4: a row is 40 measurements, each filling 4 columns
    e.w8(fw["key_code"], 0x0F); call("scope_key"); e.w8(fw["key_code"], 0x0F); call("scope_key")
    assert e.r8(S + O_SPEED) == 2
    n0 = len(log["pwm"])
    for _ in range(40):
        call("scope_scan_step")
    vals = e.read(S + O_CUR, 160)
    assert len(log["pwm"]) - n0 == 40 and e.r8(S + O_NROWS) == 1, (len(log["pwm"]) - n0, e.r8(S + O_NROWS))
    assert all(vals[i] == vals[i - i % 4] and vals[i] for i in range(160)), vals
    for _ in range(2):
        e.w8(fw["key_code"], 0x10); call("scope_key")
    assert e.r8(S + O_SPEED) == 0

    # 4. render: header shows the span, no stock render, lcd_bus_sem balanced
    log["text"].clear(); log["render"].clear(); log["sem"].clear()
    e.w32(fw["disp_dirty_bits"], 0x80000000)
    call("scope_render")
    assert log["render"] == [] and log["text"][0] == "393.000-572.000 MHz", log["text"]
    assert log["sem"] == [("P", e.r32(fw["lcd_bus_sem"])), ("V", e.r32(fw["lcd_bus_sem"]))]

    # 5. knob + zoom to the finest level around 450.000, then the carrier shows up directly
    e.w8(S + O_CURSOR, 0)
    for _ in range(3):
        e.w8(fw["key_code"], 0x11); call("scope_key")
    assert e.r8(S + O_CURSOR) == 3 and log["wake"]
    e.w8(fw["key_code"], 0x12); call("scope_key")
    assert e.r8(S + O_CURSOR) == 2
    target = (SIGNAL - 39300000) * 160 // 17900000
    e.w8(S + O_CURSOR, target)
    spans = []
    for _ in range(2):
        e.w8(fw["key_code"], 0x14); call("scope_key"); spans.append(e.r32(S + O_SPAN))
    assert spans == [17900000 // 8, 17900000 // 64], spans
    start = e.r32(S + O_START)
    assert start <= SIGNAL < start + spans[-1]
    for _ in range(160):
        call("scope_scan_step")
    vals = e.read(S + O_CUR, 160)
    hot = [c for c, v in enumerate(vals) if v == 60]
    sigcol = (SIGNAL - start) * 160 // spans[-1]
    assert hot and all(abs(c - sigcol) <= 1 for c in hot), (hot, sigcol)
    # a step with a wrong synthesizer readback is dropped
    e.write(S + O_CUR, bytes(160)); chip["wrong"] = 1
    call("scope_scan_step"); chip["wrong"] = 0
    assert e.read(S + O_CUR, 1) == b"\0", "disturbed step not rejected"

    # 6. P1 zooms back out, then exits from the full band, restoring the radio synchronously (p10)
    for want in (17900000 // 8, 17900000):
        e.w8(fw["key_code"], 0x13); call("scope_key"); assert e.r32(S + O_SPAN) == want
    log["reload"].clear(); log["apply"].clear()
    e.w8(fw["key_code"], 0x13); call("scope_key")
    assert st() == ST_IDLE and e.r8(fw["screen_id"]) == 0
    assert log["reload"] and log["apply"] and log["cmd"][-1] == 1 and not log["vm"]
    assert e.r8(RS + 0x2D) == 1 and e.r8(RS + 0x0B) == 1, "settings not restored"
    assert chip["regs"][0x5A] == 0x0CDD, "RSSI averaging not restored"
    assert e.read(REC, 4) == bcd(14710500), "VFO changed without a tune"

    def finest_and_tune(f_open):
        e.w32(fw["rx_freq_a"], f_open); e.w8(fw["screen_id"], 0)
        call("scope_open")
        e.w8(S + O_CURSOR, target)
        for _ in range(3):
            e.w8(fw["key_code"], 0x14); call("scope_key")
        assert st() == ST_IDLE and e.r8(fw["screen_id"]) == 0
        return e.r32(S + O_SPAN), e.r32(S + O_START)

    # 7. VFO mode: finest level + P2 tunes the VFO record to the cursor (12.5 kHz), mode untouched
    span, start = finest_and_tune(44600000)
    tuned = e.read(REC, 4)
    assert not log["vm"] and tuned != bcd(14710500) and e.r8(RS + 0x15) == 1
    f = int(tuned.hex()); assert f % 1250 == 0 and start <= f <= start + span, f

    # 7b. channel mode, exit without tune: mode and memory channel untouched
    e.w8(RS + 0x15, 0); e.w32(fw["cur_chan_rec"], MEM); e.w8(fw["screen_id"], 0)
    e.w32(fw["rx_freq_a"], 44612500)
    call("scope_open")
    e.w8(fw["key_code"], 0x0E); call("scope_key")
    assert st() == ST_IDLE and e.r8(RS + 0x15) == 0 and not log["vm"] and e.read(MEM, 4) == bcd(44612500)
    # 7c. channel mode + tune: stock V/M switches to VFO, the tune lands in the VFO record
    e.write(REC, bcd(14710500))
    finest_and_tune(44612500)
    assert log["vm"] == [1] and e.r8(RS + 0x15) == 1 and e.r32(fw["cur_chan_rec"]) == REC
    assert e.read(MEM, 4) == bcd(44612500) and e.read(REC, 4) != bcd(14710500)
    # 7d. channel mode + tune but V/M refuses: tune dropped, memory channel never written
    e.w8(RS + 0x15, 0); e.w32(fw["cur_chan_rec"], MEM); vm_works[0] = False; log["vm"].clear()
    finest_and_tune(44612500)
    assert log["vm"] == [1] and e.r8(RS + 0x15) == 0 and e.read(MEM, 4) == bcd(44612500)
    vm_works[0] = True; e.w8(RS + 0x15, 1); e.w32(fw["cur_chan_rec"], REC)

    # 8. PTT while open: scan halted and the radio restored before stock ptt_press runs
    e.w8(fw["screen_id"], 0); call("scope_open")
    call("scope_ptt")
    assert log["ptt"] == [ST_IDLE] and e.r8(fw["screen_id"]) == 0, log["ptt"]

    # 8b. a task-side exit (STOP) left pending with the scope screen up: the next key restores
    e.w8(fw["screen_id"], 0); call("scope_open"); log["reload"].clear()
    e.w8(S + O_STATE, ST_STOP); e.w8(S + O_STANDBY, 1)
    e.w8(fw["key_code"], 0x11); call("scope_key")
    assert st() == ST_IDLE and log["reload"] and e.r8(fw["screen_id"]) == 0

    # 9. stock takes the screen (e.g. call alert): scope stops without forcing standby, task restores
    e.w8(fw["screen_id"], 0); call("scope_open"); log["render"].clear()
    e.w8(fw["screen_id"], 7)
    call("scope_render")
    assert st() == ST_STOP and e.r8(S + O_STANDBY) == 0 and log["render"] == [1]
    call("scope_service")
    assert st() == ST_IDLE and e.r8(fw["screen_id"]) == 7
    return "Predicting selectable (short/long), passthrough, band select, speed keys, sweep + takeover + tracking filter, render, knob/zoom/carrier, readback reject, exit/restore, tune (VFO / channel->VFO / V/M refused), PTT, stuck exit, screen loss"
