# D878UV V4.00 — PC / programming-cable link (USB CDC)

Full map of the host↔radio serial protocol as implemented in firmware. Addresses are the
D878UV V4.00 image. This is the protocol the official CPS, firmware updater, and `qdmr`/`dmrconfig`
drive. Use it to read/write the radio over the cable (§7: `tools/radio.py`).

## 1. Transport

- **USB CDC-ACM** (virtual COM). Enumerates as a serial port; on Linux it's `/dev/ttyACM0`.
  Driver: `task_p3_usb_pc_link` (prio 3) wakes on OS flag 0x10, runs `usb_pc_link_step`
  0x0808112A. `usb_cdc_rx_arm` 0x08098404 arms a receive into RX buffer `g_2000D9A8`;
  `usb_cdc_tx` 0x080983E6 sends. Baud is ignored (USB).
- A received frame is copied to the command buffer **`g_2000D5A8`** (byte 0 = command,
  length in `g_20001090`), then `pc_cmd_dispatch` 0x0807FC2E runs. The reply is built back
  in `g_2000D5A8`; reply length is `g_200010CA`; it's sent by `FUN_0807CFB2(len)`.
- While a PC session is active the radio shows screen 0x23/0x24/0x25 ("PC MODE/READ/WRITE");
  `g_2000090C==0x16` also blocks the link. **ACK byte is `0x06`**; many commands end their
  payload with a trailing 0x06 that the parser checks.

## 2. Two-stage dispatcher (`pc_cmd_dispatch` 0x0807FC2E)

Stage 1 matches the **command byte** `g_2000D5A8[0]` (+ length + any magic string), and on a
match sets an **internal code** `g_20001097` and calls `pc_cmd_accept` 0x0807FC26 (which just
sets the valid flag `g_20001092=1`). Stage 2 is `switch(g_20001097)` and does the work.
The internal code is written **only** inside this function — there is no other (hidden)
entry point that injects a command. "Hidden" commands here means undocumented command bytes,
all of which are listed below.

**Session gate (top of dispatch):** when no session is open (`g_20001096==0`), only these
command bytes are accepted: `P` PROGRAM, `F` FALTORY, `r` recording, `k` kind*, `U` UPDATE.
Everything else requires an open session. `g_20001096`: 0 idle, 1 read/CPS session,
2 factory/test write, 4 record/APRS.

## 3. Command table

Magic-string constant pool at 0x080DDB3C:
`PROGRAM\0 FALTORY\0 UPDATE\0 QX\x06 END\0 D878UV\0 A-BORD\0 V200\0 kindrecord\0 kaprs\0 V101\0 NEWEROM\0 recording\0`.

| Cmd byte | len / payload | internal code | Stage-2 action |
|---|---|---|---|
| `P` 0x50 | 7 = `"PROGRAM"` | 1 | **Open read/CPS session** (`g_20001096=1`); reset addr caches; show PC screen |
| `U` 0x55 | 6 = `"UPDATE"` | 2 | Enter firmware-update session (only when screen 0x14 boot mode) |
| `F` 0x46 | 7 = `"FALTORY"` (sic) | 0xA | **Factory/test write mode** (`g_20001096=2`, `FUN_0806A9FA`) |
| `N` 0x4E | 7 = `"NEWEROM"` | 0x11 | (no-op in switch; handshake for new-EEPROM/erase flow) |
| `E` 0x45 | 3 = `"END"` | 0xB | **End session**: flush, redraw, reboot into normal mode (infinite-loop wait for reset) |
| `Q` 0x51 | 3 = `"QX"`+0x06 | 6 | (no-op stage-2 → just ACK) query/handshake |
| `R` 0x52 | 1 | 0xC | **READ**: see §7. Read 16 B from SPI flash at a 32-bit address |
| `W` 0x57 0x18 | 0x18 | 0xD | **WRITE**: write 16 B to SPI flash (codeplug, addr ≥ 0x800000) |
| `H` 0x48 | 0x18 | 0x28 | **Factory-block write (SPI flash, not internal flash)** via `pc_factory_block_write` 0x08028270: addr 0x2FA0000..0x2FC0000, 2 KB-page RMW into whichever of the A/B 128 KB banks (0x2FA0000 / 0x2FC0000) is inactive, then flips banks. Writing 0x2FA0120 also copies 5 bytes to `g_20000DE1` and `FUN_08027BD0` sets `g_20000DE0=1` iff they equal `"1846N"`: an **RF-chip variant tag** (AT1846N vs AT1846S) that `at1846_init_regs`, `at1846_load_mode_profile` and `at1846_profile_analog` branch on (also writes AT1846 reg 0x0F=0x8C24). It is not the region byte (that's `g_2000A33C[3]`) |
| `Y` 0x59 | 3 +0x06 | 0x16 | PTT control: `g_2000D5A8[1]`==1 → `ptt_press`, else `ptt_release` |
| `A` 0x41 | 0xE +0x06 | 0x18 | Write 12 B to `g_20009FB2` (DTMF/alias buf B) then `FUN_080278CC` |
| `B` 0x42 | 0xE +0x06 | 0x17 | Write 12 B to `g_20009FA6` (buf A) |
| `G` 0x47 | 3 +0x06 | 0x14 | `FUN_0807EAD2` |
| `J` 0x4A | 0xE +0x06 | 0x19 | Write 12 B to `g_20009FBE` (buf C) |
| `K` 0x4B | 0xE +0x06 | 0x1A | Write 12 B to `g_20009FCA` (buf D) |
| `M` 0x4D | 0xA | 0x27 | Screen/record-ident (`FUN_0807F7BE`/`FUN_0807F928` in record mode) |
| 0x01 | 0x28 | 3 | `pc_write_codeplug_block` 0x0807D14A (36-byte block + checksum, screen 0x13) |
| 0x02 | 1 | 0xE | Reply model string `D878UV` / `A-BORD` (0x080DDB5A / 0x080DDB61), region at `g_2000A33C[3]` |
| 0x06 | 1 | 0x1F | (no-op) |
| 0x18 | 1 | 8 | Close/commit write session; `FUN_0807AE48` flush, redraw |
| `D` 0x44 | 5 +0x06 | 0x13 | `FUN_0807D4BA` |
| 0x71 `q` | 3 +0x06 | 0x15 | `FUN_0807E484` |
| 0x82 | 3 +0x06 | 0x22 | **Read 0x30-byte block** from `g_200049B8` (reply type 0x43) |
| 0xA4 | — (session 2) | 0x24 | Write 0x27/0x28-byte sub-blocks to `g_2000A1xx/A2xx` by `g_2000D5A8[1]` selector (0–8…): factory cal tables |
| `Z` 0x5A | 8 +0x06 | 0x2B | `FUN_0807CFC6` |
| `h` 0x68 | 2 +0x06 | 0x2C | Reply 16 B from `g_2000B5CA` (reply type 0x48) |
| `k` 0x6B | 10 = `"kindrecord"` | 0x29 | `FUN_08071CDA`; reply type 0x72 |
| `k` 0x6B | 5 = `"kaprs"` | 0x2D | **Open record/APRS session** (`g_20001096=4`) |
| `r` 0x72 | 9 = `"recording"` | 0x2A | `FUN_0807FB00` (start recording session) |
| `S` 0x53 | 6 (session 2) | 0x23 | `FUN_0807F186` |
| `T` 0x54 | 5 +0x06 (session 2) | 0x12 | `FUN_0807DA86` |
| `V` 0x56 | 6 | 0x26 | record cmd (`FUN_0807F628`/`FUN_0807F6FA`) |
| `X` 0x58 | 6 | 0x25 | record cmd (`FUN_0807F556`/`FUN_0807F5B6`) |

(Record/APRS-mode variants of codes 0x25/0x26/0x27 branch on `g_20001096==4`.)

## 4. The READ command (code 0xC, `R`) — most useful

Request layout in `g_2000D5A8`: `'R'(0x52)` + 4-byte **big-endian** address (`pc_be32`
0x0807F53E) + 1-byte length (`g_2000D5A8[5]`, up to 16). Handler:
- `g_200010A8 = addr`. The data source is **SPI flash**, read by `spiflash_read_span`
  0x0807B0B6 (page-split at 2 KB via `FUN_0807AEA4`). An A/B-bank select happens for the
  `DAT_08080A8C` region (`pc_factory_select_bank` 0x0807F52E).
- Reply: `'W'(0x57)` + echoed 4-byte addr + len + `len` data bytes + 1-byte checksum (8-bit **sum**, not XOR,
  of bytes 1..n) + trailing `0x06`. Reply length `g_200010CA = len + 8`.
- **No address bound on reads** — any 32-bit address is accepted, so the whole SPI flash is
  dumpable. (The write path `W`/code 0xD requires addr ≥ 0x800000; `H`/code 0x28 writes the SPI-flash
  factory block 0x2FA0000..0x2FC0000 only.)

## 5. Typical CPS read session

1. Host → `PROGRAM` (0x50 …). Radio enters read mode, shows "PC MODE", replies with an ACK
   and identity.
2. Host → `0x02` (len 1): radio replies model (`D878UV`) + region/hardware bytes.
3. Host → repeated `R addr(4 BE) len(1)` ; radio replies `W addr len data csum 06`.
4. Host → `END`: radio reboots to normal operation.

Write session is the same with `PROGRAM`→code 1, then codeplug via code 0xD (`W`) /
code 3 (0x01) blocks, closed by code 8 (0x18) then `END`.

## 6. Key globals & helpers

| Symbol | Meaning |
|---|---|
| `g_2000D5A8` | command / reply buffer (also the live RX buffer `g_2000D9A8` is separate; copied in) |
| `g_20001090` | received payload length (u16) |
| `g_20001092` | command-valid flag (set by `pc_cmd_accept`) |
| `g_20001096` | session mode: 0 idle / 1 read / 2 factory / 4 record |
| `g_20001097` | internal command code (stage-2 selector) |
| `g_200010A8` | current flash address |
| `g_200010CA` | reply length |
| `g_2000109C` | running checksum accumulator |
| `pc_cmd_dispatch` 0x0807FC2E | the whole parser |
| `pc_be32` 0x0807F53E | 4 bytes → big-endian u32 (address decode) |
| `spiflash_read_span` 0x0807B0B6 | read N bytes from SPI flash to RAM (used by READ) |
| `pc_factory_block_write` 0x08028270 | A/B-banked 2 KB-page RMW of the SPI-flash factory block 0x2FA0000–0x2FC0000 (cmd `H`) |
| `pc_write_codeplug_block` 0x0807D14A | 36-byte checksummed codeplug block write |

## 7. Using it from Linux (`tools/radio.py`)

Your user needs access to the serial device (on most distros: membership in `dialout`). The radio
enumerates as GD32 VID 28e9:018a (`/dev/ttyACM0` unless `D878_PORT` says otherwise).

    tools/radio.py info                       # PROGRAM handshake + identity, then END (radio reboots)
    tools/radio.py dump out/nand.bin          # whole SPI NAND (~15 min at ~150 KB/s), resumable
    tools/radio.py dump out/nand.bin --skip-contacts   # skip the DMR ID database (~half the time)
    tools/radio.py read 0x2fa0000 0x100       # hexdump a range

Raw protocol, if you need it: `PROGRAM` → `QX 06`; `R` + BE32 addr + len → `W addr len data sum8 06`;
`END` reboots. Reads are side-effect-free. **Never** send `W`, `H`, `0x01`, `0xA4` or `Y` casually:
the first four write flash, and `Y` keys the transmitter. Only one program may hold the port;
`radio.py` takes a lock (`out/.radio.lock`).

## 8. Verified on hardware (tools/radio.py)

- Handshake: `PROGRAM` → `QX 06`. `0x02` → `I D878UV 00 00 V101 00 00 06` (V101 is a constant in the app's
  reply; the update-mode identity comes from the bootloader and may differ — the `.spi` says V100).
- `R` accepts **len 1..255** (reply buffer is 1 KB): 255-byte reads give ~150 KB/s vs ~26 KB/s at 16.
- The SPI flash is a **1 Gbit SPI NAND** (cmd 0x13 page read to cache, 0x0F/0xC0 status with ECC bits,
  0x3B read; 2 KB pages, 16-bit row) behind a bad-block remap (`FUN_080284A6`). Addresses ≥ 0x8000000 are
  ignored on read *and* write (writes silently dropped).
- **`R` is not a raw read.** For every 128 KB block it runs the codeplug A/B-bank select
  (`pc_factory_select_bank` → `FUN_08021236`) and returns block X+0x20000 when block X isn't marked
  valid. A stock-protocol "dump" is therefore the CPS's logical view (e.g. dump@0x20000 = NAND 0x40000,
  so the GBK16 font that the icon pack puts at NAND 0x20000 shows up at dump 0x0). Raw NAND needs the
  debug patch (`call spiflash_read_span` / `FUN_0807AEA4` into a RAM buffer, then peek).
- Resources: NAND matches icon pack `D878_ICON_V1.23` (42 segments, `.CDI` = 278-byte records
  name[256] + nand addr + len + pad). Its `lcdfront_check.bin` at NAND 0x3E0000 (`55 56 .. 5E`) is the
  signature V4.00 checks at boot (`FUN_08021162`); if missing, the app forces firmware-receive mode.

## 9. Firmware update path

- PTT+PF3 at power-on = **bootloader** update mode (red LED blinks). The app also has a receive mode
  (`FUN_080811DA` key check → `FUN_0808118C`, screen 0x13, before the OS starts), but its block writer
  sends 0x0800xxxx addresses to the NAND driver, which drops anything ≥ 0x8000000, so it can't be what
  installs firmware. The bootloader programs internal flash itself, so a broken app stays recoverable.
- Wire protocol (reald/anytone-flash-tools, matches the app's handler): `UPDATE` → 06; `02` → identity
  (model at [1:7], hw at [9:13], must equal `.spi` [14:20]/[22:26]); then per 32 bytes
  `01 | addr LE32 | data[32] | sum16 LE of addr+data | 06` → 06 (0x15 = checksum error); `18` → 06.
- PTT+PF1 at power-on = app "Initialize radio?" (factory init: settings/codeplug must be reloaded).
  Vendor docs require it after version changes; it should not be needed for a same-version patch.
