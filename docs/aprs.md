# APRS in the V4.00 firmware

## Summary

The stock firmware has a complete **analog APRS receive stack**: AX.25 parsing, FCS check, source
filter, Mic-E, compressed and uncompressed positions, PHG and course/speed, messages with an
automatic ack, a NAND inbox with A/B-banked ring storage, distance and bearing to your GPS fix, and
the receive popup and inbox menus. It does **no demodulation**. The AFSK1200 demodulation and HDLC
deframing happen on an external module (the "APRS board", whose version shows as `Aprs Bord Ver`).
That module sends each frame to the MCU over UART4 as `+APRS:` followed by the raw AX.25 bytes. A
D878UV without that module never receives a frame, and the firmware hides the receive menus.

APRS **transmit** is generated on the MCU (`sig_seq` states 14–16: TIM output-compare + DMA PWM
AFSK) and needs no module.

So making APRS RX work needs two things:
1. An AFSK1200 demodulator on the MCU. Its input is the RX audio already sampled on PC0.
2. A shim that hands each decoded frame to the existing parser and makes the firmware believe an
   APRS board is fitted. The firmware then provides every screen and the storage.

## Stock data path (module → UI)

```
UART4_IRQHandler                       byte -> bt_rxbuf[bt_rx_len]
 └ bt_uart4_rx_byte      0x0807521A    drops bytes while bt_rx_hold != 0
   └ bt_rx_line_byte     0x080751AA    a line starts with '+', 'O' or 'E'; len capped at 200
     ├ at len 6: aprs_rx_check_prefix 0x080756A2   "+APRS:" -> aprs_rx_in_frame = 1
     ├ else    : bt_rx_line_at_byte   0x08075194   (BT AT responses)
     └ aprs_rx_frame_byte   0x080756D2
         state: wait 0x03 -> wait 0xF0 -> wait '~' with len > 9
         copy bt_rxbuf -> aprs_rx_frame (0x2000C10F, 300 B), aprs_rx_frame_len = len-4
         aprs_rx_frame_ready = 1  only if
           rf_chan_rec[0x21] bit 5 (channel "APRS Receive" on) && channel analog
           && aprs_tx_holdoff == 0 && ana_rx_audio_open (squelch + CTCSS/DCS open)

task_p14_housekeeping_loop 0x08029DAC  (every 2 ticks = 10 ms)
 └ aprs_rx_frame_process   0x08078620  (no-op unless aprs_rx_frame_ready)
     find '~' in the first 10 bytes = frame start
     ax25_fcs_check 0x0807699C: scan for 03 F0 then the closing '~'; CRC-16/X.25
         (crc16_x25 0x0806E000, table 0x080B2A88, FCS low byte first)
         failure is ignored when aprs_pass_all (menu "APRSPassAll") is set
     dest/src callsigns (>>1) + SSIDs -> aprs_dst_call / aprs_src_call
     aprs_src_filter_match 0x08076A26  (Info Filter: 32-entry list, when aprs_filter_on)
     digipeater path -> aprs_digi_path_split (4 × 9 chars)
     needs ctl 0x03, pid 0xF0; data-type whitelist ! = @ / ; ) ` ' 0x1C 0x1D : T > _ $ space }
     '}' third-party (aprs_3rdparty_hdr_scan), Mic-E (aprs_parse_mice: lat from dest call),
     compressed (aprs_parse_compressed: base91), PHG/course (aprs_parse_phg_ext),
     ':' message -> aprs_msg_is_for_me (own call 0x2000A48D, SSID 0x2000A493)
     aprs_calc_dist_bearing (uses GPS fix)
     aprs_rx_kind 0: aprs_info_build_record -> aprs_info_store_append
     aprs_rx_kind 2: aprs_sms_build_record  -> aprs_sms_store_append
     message to me: aprs_msg_send_ack -> rf_cmd_post(0x16) (AFSK TX)
     aprs_rx_show 0x08076CA4: screen_id 0x0C popup (also from menus 0x0D–0x0F)
     bt_rx_reset
```

**Stock limitation:** the stream ends a frame at the first `~` after `03 F0`. A `0x7E` byte in
the info field truncates the frame, and the FCS check then fails. A software decoder that writes
`aprs_rx_frame` directly avoids this, because it doesn't need the `~` delimiter.

### Inbox storage (SPI NAND, logical addresses)
| Area | Bank A / B | Unit |
|---|---|---|
| Info index, records 0–255 | 0x2BC0000 / 0x2BE0000 | 32 B per entry (`aprs_info_index_read16` reads the length; ≤ 0x208 = used) |
| Info index, records 256–511 | 0x2C00000 / 0x2C20000 | 32 B |
| Info records 0–255 | 0x2C40000 / 0x2C60000 | 256 B (`aprs_info_record_write`) |
| Info records 256–511 | 0x2C80000 / 0x2CA0000 | 256 B |

There are 512 records in two halves of 256. When a half fills, the firmware erases the idle bank
and swaps the bank flags (0x20000FDE–FE1). `aprs_info_store_scan` (boot) binary-searches the index
for the fill level. If `aprs_board_present` is 0, it sets the count to 0. The SMS inbox has its own
counters (0x20000FE4–FEA).

## Board detection and UI gating

`main` → `bt_board_probe` 0x0807455A: power on the module (PE15), UART4 at **115200**, send
`AT+VER?` up to 5 times. Any reply sets `bt_module_ok`. A version reply that starts with `APRS`
sets `aprs_board_present` and stores the `Vxxx` version for "Aprs Bord Ver". No reply turns PE15
off. `main` then calls `bt_probe` (binary-protocol module, UART4 at 9600).

When `aprs_board_present == 0`:
- APRS top menu (`menu_L2_show` + remap 0x080461E8): **Ana APRS Info** is hidden.
- Channel settings (`menu_L3_show`, remaps 0x0804C0A4 / 0x080507F2 around field 0x33): **APRS
  Receive** (Off / On / On(Mute)) is hidden. This is channel record +0x21 bit 5.
- `key_aprs_info_open` 0x08014D7C (key/PF function) only sets an error code.
- `aprs_info_store_scan` reports an empty inbox.

Every Bluetooth path that reads `aprs_board_present` (`bt_power_apply`, `aprs_board_bt_switch`)
first checks `bt_module_ok == 1`. On a radio with no module, forcing `aprs_board_present = 1`
therefore only unhides the APRS UI.

## Audio available to the MCU

- ADC1 runs a continuous scan of ch10 (PC0 RX audio), ch11 (PC1 battery) and ch12 (PC2), DMA
  circular into 0x20001048. `adc_pc0_rx_audio` always holds a fresh 12-bit sample. The scan rate
  is far above audio rate.
- TIM6 (timer clock assumed 120 MHz, rate = clock / ((psc + 1)(period + 1)),
  `tim6_set_rate(period, psc)`) drives the software decoders through `tim6_audio_mode`: 1 tone
  sampling, 2 9.02 kHz 8-bit capture (period 0x33F1), 3 zero-crossing periods. Mode 0 is idle.
  `tim6_init` (boot) enables the update IRQ and NVIC, then stops the timer.
- **TIM6 lifecycle:** `rf_cmd_apply_lo` (every RX re-entry: channel/VFO change, after TX) calls
  `sigdec_select_for_channel`, which starts the decoder the channel asks for (rec +0x1A high nibble)
  or stops TIM6 and sets mode 0. TX (`rf_cmd_apply_hi`, `rf_tx_aprs_afsk`) stops it as well and sets
  `rf_tx_active` (0x200001B1); the ISR does nothing while that is set. So anything that uses TIM6
  must be restarted after every retune and TX, and it can't run on channels with 2-tone, 5-tone or
  DTMF decoding.

## Recording real bursts (capture firmware)

Test packets only come from other stations, on their schedule, so the recorder runs unattended
and every burst it catches becomes a reusable test vector.

- `patches/mkpatch_capture.py` → `out/D878UV_V4.00_bandunlock_r1_dbg1_cap1/` (on top of the
  debug image). In `TIM6_IRQHandler`, the mode-3 branch now calls `cap_isr` (0x080078EC, in the dead
  FLASH option-byte functions). `cap_isr` tail-calls the stock zero-cross handler for mode 3 and
  records in mode 4. Mode 4 stores ADC>>4 at 9.6 kHz into a ring in free RAM (CAP block 0x20015740,
  10416 samples ≈ 1.09 s). The squelch opening (`ana_sq_open`) triggers a capture: it keeps about
  200 ms from before the trigger, then records until the ring is full or 50 ms after the squelch
  closes, and freezes.
- `scripts/aprscapctl start|stop|status|tail` runs `tools/aprscap.py`. That script arms the
  capture over the debug channel (poke mode 4, call `tim6_set_rate` and `TIM_Cmd`), re-arms it
  after each retune or TX, and saves each burst to `out/aprscap/YYYYMMDD/cap_*.wav` with a `.json`
  sidecar (RX frequency, pre-trigger length, truncation, measured sample rate, decoded frames). It
  holds the radio port while it runs.
- `tools/afsk1200.py FILE.wav…` is the reference decoder (pure Python, integer-friendly; the model
  for the MCU version). `--selftest` modulates frames at several rates, noise levels and tone tilts.
- Operating notes: listen on the APRS frequency (A/B dual watch off, so the radio doesn't keep
  retuning), set the squelch so noise alone doesn't open it, and use a channel without 2-tone,
  5-tone or DTMF decoding.

### What the captures show
- TIM6 clock is 120 MHz: period 12499 gives a measured 9600 Hz.
- PC0 audio decodes as-is with `afsk1200.py`; the first burst on 144.390 was a digipeated Mic-E packet.
- Level: DC bias ≈ 86/255 of the 8-bit range (≈ 1.1 V), swing up to ≈ 172 (≈ 2.2 V). A strong
  packet clipped ~4.5 % of samples at 0 (asymmetric clipping adds 2400 Hz, next to the 2200 Hz
  space tone). Still unknown: whether the volume knob scales PC0, and how often clipping costs a
  decode.
- Dual watch (`radio_settings[0x2D]`, toggled by `key_sub_ch_toggle`, the PF function "Sub Ch
  Hide") retunes every ~120 ms and resets TIM6 each time: the recorder (and a future demod) needs it
  off. Calling `key_sub_ch_toggle` over the debug channel from the standby screen works and saves
  the setting.
- The volume knob doesn't change PC0 (same bias and level in every capture): PC0 is before the
  volume control, so its clipping is fixed by the hardware.
- Dual watch with APRS RX: `dualwatch_signal_check` (p14 loop, 10 ms) locks the radio on a side
  after 2 consecutive hits of RSSI ≥ threshold (0x20009F3A VHF / 0x20009F24 UHF) plus the AT1846S
  reg 0x1C bit 0. The dwell is ~120 ms per side, so up to ~140–160 ms of a packet's preamble can be
  lost. Local stations send ~240–550 ms of flags, so packets survive if the demod restarts on every
  switch to the APRS channel (hook `sigdec_select_for_channel`). A host-armed recorder is too slow
  for that, so record with dual watch off.

## Plan: MCU APRS receive

1. **Capture test (hardware feasibility):** collect bursts with the recorder above. Check the
   audio level and DC bias, how much de-emphasis tilts the tones, and whether `afsk1200.py`
   decodes them. Keep good and marginal WAVs as fixed test vectors.
2. **Demodulator in the TIM6 ISR:** 1200/2200 Hz correlators or Goertzel filters, a DPLL bit
   clock, NRZI decoding, HDLC flag detection and bit unstuffing, then an FCS check. The ISR hands
   complete frames to a small ring buffer. At about 60–100 cycles per sample, that is under 1 % of
   the 120 MHz CPU.
3. **Shim:** from p14 (or the ISR) write `'~' + frame + FCS + '~'` into `aprs_rx_frame`, then set
   `aprs_rx_frame_ready` using the same conditions as `aprs_rx_frame_byte`. Patch
   `bt_board_probe` so `aprs_board_present` ends up 1. The stock parser, inbox, popup, ack and
   menus then work unchanged.
4. **Coexistence:** run the AFSK mode only on channels with APRS Receive set and while TIM6 is
   idle (mode 0). 2-tone, 5-tone and DTMF decoding take TIM6 when a channel enables them.

## Open questions
- What "On(Mute)" changes (likely mutes the speaker while a frame is received).
- Which modules answer `+APRS:` (D878UVII Plus BT/APRS board?), and whether the binary-protocol
  module (`bt_probe`, 9600 baud) is a different part.
