# D878UV / D878UVII V4.00 band-unlock patch (region 1)

| Model | Output folder | Raw image |
|---|---|---|
| AT-D878UV | `out/D878UV_V4.00_bandunlock_r1/` | `D878UV_V4.00_bandunlock_r1.bin` |
| AT-D878UVII (Plus) | `out/D878UVII_V4.00_bandunlock_r1/` | `D878UVII_V4.00_bandunlock_r1.bin` |

Build: `.venv/bin/python patches/mkpatch_bandunlock.py uv|uvii`. The firmware is model-locked, so flash each model only with its own image. The rest of this document uses D878UV addresses; the UVII port is described at the end.

Patched firmware (D878UV): `out/D878UV_V4.00_bandunlock_r1/`
- `D878UV_V4.00_bandunlock_r1.bin` — the patched image (raw, loads at 0x08004000)
- `D878UV_V4.00_20250725.CDD` / `.CDI` / `.spi` — same image as a drop-in set for the AnyTone updater (CDI/spi are stock: the length did not change)

| | SHA-256 |
|---|---|
| stock `D878UV_V4.00_20250725.CDD` | `aae150dc3ff7f64c5c1f3a64abc7d91a59438635d7cb946e7aec68011dad4b55` |
| patched | `885864f8ca9e109cf2c5802b2ee503d0b4c5d89f2eb3174ab1c8b3535b8512d2` |

Rebuild: `.venv/bin/python patches/mkpatch_bandunlock.py` (`scripts/setup_venv.sh` creates `.venv`). The script refuses to run on anything but the stock image, asserts every original byte it replaces, and emulates every stub before writing.

**Untested on hardware.** Select region 1 in the secret menu after flashing.

## Result

Region 1, both the RX and the TX plan:

| Band | Stock | Patched |
|---|---|---|
| VHF | 136.000 – 174.000 | **50.000 – 199.995** |
| "220" | 220.000 – 225.000 | **200.000 – 299.995** |
| UHF | 400.000 – 480.000 | **310.000 – 599.995** |

The bands are bounded the way they are because the firmware sorts frequencies into bands in two ways. It uses the first BCD byte of the frequency (<0x20 → VHF, 0x20–0x30 → "220", ≥0x31 → UHF ≥310 MHz). `at1846_set_freq` also splits VHF/UHF at 300 MHz. Keeping each range inside its bucket means every band-dependent branch still takes the right path. The documented AT1846S ranges are 134–174 / 200–260 / 400–520 MHz. Anything beyond that is allowed but may simply not lock or receive.

TX is unlocked over the same ranges on purpose: transmitting only where you're licensed is your call. Outside 136–174 / 400–480 the PA and harmonic filters are out of their design range.

## How the limits work (what was found)

1. **The tuning code has no limits.** `at1846_set_freq` (0x0805F504) writes f(kHz)×16 to AT1846 registers 0x29/0x2A for any frequency.
2. **Limits are clamps against a per-region band plan.** `freq_clamp_to_region` (0x080166F6) and five sibling validators (0x08016D9A, 0x0801E9CE, 0x0801FF44, 0x08022D8E, 0x08025780/0x08026668) all read the same tables. Each table is 18 × u32 in 10 Hz units, indexed by the region byte at RAM 0x2000A33C+3 (`identity_u8` is just `bx lr`).

   | | VHF lo / hi | 220 lo / hi | UHF lo / hi |
   |---|---|---|---|
   | RX tables | 0x080A0D24 / 0x080A0D6C | 0x080A0E44 / 0x080A0E8C | 0x080A0C04 / 0x080A0C4C |
   | TX tables | 0x080A0DB4 / 0x080A0DFC | 0x080A0ED4 / 0x080A0F1C | 0x080A0C94 / 0x080A0CDC |

   Region names (pointer array 0x080AAE28) describe the TX plan; region 1 is "400-480 136-174".
   A flag at 0x20000D9B swaps in hard-coded 136–174 / 220–225 limits. Only factory test mode sets it (0x0806A9FA, entered via power-on checks in `main`), so normal use always goes through the tables.
3. **Why a table edit alone is not enough.** Fourteen per-frequency calibration lookups compute an index like this:

   ```
   ldr  rB, =BASE          ; e.g. 136.000 MHz
   subs rD, rS, rB         ; f - BASE   (unsigned, no guard)
   ldr  rX, =STEP
   udiv rD, rD, rX
   uxtb rI, rD             ; index & 0xff → table[index]
   ```
   Below BASE the subtraction wraps, so the index becomes garbage. Above the stock band edge the index runs past the end of the table. Either way the code reads non-calibration bytes:

   | Lookup | Feeds | Out-of-band effect without the fix |
   |---|---|---|
   | `rf_squelch_cal_apply` 0x0805FAAA | AT1846 reg 0x49 squelch thresholds | squelch stuck open / never opens |
   | `rf_frontend_tune_from_tables` 0x0805FD5E | TIM CC1 PWM = RX tracking-filter tune voltage | below 136 its "guard" falls through to the **174 MHz** value |
   | `tx_power_cal_lookup` 0x0806AE3C / `tx_power_apply` 0x0806B0CC | DAC ch1 = PA power control | unpredictable PA drive |
   | `dmr_freq_cal_send` 0x0808311C | per-frequency calibration sent to the SCT3258 | huge interpolation index → garbage cal |

   They were found by scanning every function that loads a frequency-sized constant for `ldr base; sub(s) ; … udiv`. Two independent scans found exactly these 14 sites.

## The patch

### 1. Tables (12 words)
Entry `[1]` of each of the 12 tables above is overwritten with the patched values. The script asserts the stock values (136/174, 220/225, 400/480) first.

### 2. Calibration clamps (14 sites)
Each site's `ldr rB,=BASE` + `subs` pair (4 bytes), or its 4-byte `sub.w r9,r0,r1` in the DMR function, becomes a `b.w` to a stub. The stub computes `clamp(f − BASE, 0, SPAN)`, where SPAN is the stock-calibrated width, and branches back with `b.w`. **LR is never used**, because `tx_power_cal_lookup` is a leaf function that doesn't save it. Registers other than the ones the original instructions wrote are preserved, and the scratch register is pushed and popped. Flags are clobbered, which is harmless: the original `subs` clobbered them too, and the DMR path calls `__aeabi_ui2d` right after.

Example, VHF squelch lookup:
```
stock                                   patched
0805fb26  bd49      ldr  r1,[pc,#0x2f4] 0805fb26  a7f719bd  b.w  #0x800755c
0805fb28  401a      subs r0,r0,r1
0805fb2a  bd49      ldr  r1,[pc,#0x2f4] 0805fb2a  bd49      ldr  r1,[pc,#0x2f4]   (unchanged)

stub 0x0800755c:
  movw r1,#0x8500 ; movt r1,#0xcf    ; r1 = 136.000 MHz (as stock left it)
  subs r0,r0,r1                      ; f - base
  bhs  +2 ; movs r0,#0               ; below band → 0
  push {r2}
  movw r2,#0xfbc0 ; movt r2,#0x39    ; 38.000 MHz span
  cmp  r0,r2 ; it hi ; movhi r0,r2   ; above band → span
  pop  {r2}
  b.w  #0x805fb2a                    ; back to the STEP load / udiv
```

| Site | Lookup | BASE | SPAN (MHz) | Stub |
|---|---|---|---|---|
| 0x0805FB16 | squelch UHF | 400 | 120 | 0x08007538 |
| 0x0805FB28 | squelch VHF | 136 | 38 | 0x0800755C |
| 0x0805FDBA | front-end UHF | 400 | 120 | 0x08007580 |
| 0x0805FEC8 | front-end VHF | 136 | 38 | 0x080075A4 |
| 0x0805FF2E | front-end 220 | 220 | 5 | 0x080075C8 |
| 0x0806AEBE | TX power VHF (leaf) | 136 | 38 | 0x080075EC |
| 0x0806AFCC | TX power UHF (leaf) | 400 | 120 | 0x08007B8C |
| 0x0806B150 | TX power apply VHF | 136 | 38 | 0x08007BB0 |
| 0x0806B2DA | TX power apply UHF | 400 | 120 | 0x08007BD4 |
| 0x0808326A | DMR cal seg 460 | 460 | 60 | 0x08007BF8 |
| 0x080832E0 | DMR cal seg 400 | 400 | 60 | 0x08007C18 |
| 0x080833EE | DMR cal seg 174 (serves 220–225) | 174 | 51 | 0x08007C38 |
| 0x0808346A | DMR cal seg 155 (155–174) | 155 | 19 | 0x08007C58 |
| 0x080834DC | DMR cal seg 136 | 136 | 19 | 0x08007C78 |

The effect is that a frequency outside a calibration table gets the **nearest calibrated band edge's** value: 118 MHz uses the 136 MHz squelch, filter-tune, power and DMR calibration.

### 3. Where the stubs live
There is no padding in the image, so the stubs overwrite unreachable ST library code:
- **0x08007538–0x08007627**: `FLASH_EraseAllPages`, `FLASH_EraseAllBank1Pages`, `FLASH_EraseAllBank2Pages`. No references at all, and no data word points there.
- **0x08007B8C–0x08007C9F**: the tail of `FLASH_BootConfig`, which is never called. **0x08007B78–0x08007B8B was deliberately left alone**: it is a literal pool shared with the live `FLASH_ProgramHalfWord`, which the firmware uses to rewrite internal flash at 0x080F5800+. The trailing pool at 0x08007CA0 is untouched too.

Before patching, every halfword in both ranges was checked with Ghidra's xrefs: nothing outside the dead functions references them. Each patched site was also checked to confirm no branch lands mid-pair.

## Verification done
- Stock hash check, and assertions on every replaced instruction (decoded with capstone) and every table word.
- **Every stub executed in unicorn**, starting at the patched site's own `b.w`, at five frequencies each: 20 MHz below base, base, mid-band, top edge, 30 MHz above. Each run checked the clamped result, the return address, a balanced stack, the base register value, and that the other registers were preserved.
- Image length unchanged (0xE3600), so the stock `.CDI` (load 0x08004000, length 0xE3600) still matches. 566 bytes differ.
- The application has no self-checksum; only `SystemInit` references 0x08004000, to set VTOR. The `.CDI`/`.spi` contain no checksum. Whether the bootloader verifies anything is unknown, because it isn't in the update file.

## Not verified / caveats
- **Hardware behaviour.** It hasn't been flashed. Whether the AT1846S synthesizer locks below its documented 134 MHz, or above 520 MHz, can only be found by trying.
- **AM is not added.** Airband will be heard as FM: an AM carrier with audio comes out as distorted or no audio. This patch only makes tuning there possible. Nothing in the firmware or the RDA1846 guide shows an AM mode.
- **The UI may still refuse some entries.** The VFO keypad entry or channel editing may have their own digit rules, for example for a leading 0 at 50 MHz. The PC programming software also enforces its own ranges, so out-of-band channels may have to be entered on the radio.
- **Calibration outside the bands is the edge value, not real calibration.** Expect reduced sensitivity, imperfect squelch, and off-nominal TX power far from the stock bands.
- **Recovery** if the radio misbehaves: reflash the stock `.CDD` from `firmware/` the same way (`tools/radio.py flash firmware/D878UV_V4.00_20250725.CDD --yes` in PTT+PF3 update mode). The bootloader sits in its own protected 16 KB below 0x08004000.

## D878UVII port

The UVII V4.00 (2025-07-25) image is the same codebase: 81 % of 16-byte chunks are shared and it's 64 bytes longer. Nothing was re-reversed. `patches/bandunlock_sigs.py` locates every patch point by signature, and the builder refuses to write unless:
- **The finder reproduces the reference.** On the D878UV image it must find exactly the 14 known sites and 12 tables, and the D878UV rebuild must stay byte-identical (`885864f8…`).
- **The UVII sites have the same shape**: the same 9 + 5 sites, in the same order, with identical registers and base/step constants. In the UVII they've moved by +0x0C to +0x38.
- **The tables are found as one block.** Tables with unique contents give a single consistent offset (+0x38), and every table, including the RX/TX 220 tables that look identical, must carry the reference contents at that offset. The region-name list is identical.
- **The holes are unchanged and unreferenced.** The ST library region is byte-identical at the same addresses in both images. A capstone sweep that decodes at every halfword finds no branch, literal load or data word pointing into the holes, and no branch landing mid-site. On the D878UV image this sweep agrees with Ghidra's xrefs.
- **Emulation passes.** Every stub is emulated exactly as for the D878UV.

| | UVII SHA-256 |
|---|---|
| stock `D878UVII_V4.00_20250725.CDD` | `8f840cfcfc1bb9acab65429ced211481eeb80079725ec46cf225afce071afeeb` |
| patched | `76d5b9c87d4621339064730cfe86b03c89e06c9441889644dd537fd7660fcb9a` |

UVII addresses: table sites 0x0805FB22, 0x0805FB34, 0x0805FDC6, 0x0805FED4, 0x0805FF3A, 0x0806AECA, 0x0806AFD8, 0x0806B15C, 0x0806B2E6. DMR sites 0x080832A2, 0x08083318, 0x08083426, 0x080834A2, 0x08083514. RX tables UHF 0x080A0C3C/0x080A0C84, VHF 0x080A0D5C/0x080A0DA4, 220 0x080A0E7C/0x080A0EC4. TX tables UHF 0x080A0CCC/0x080A0D14, VHF 0x080A0DEC/0x080A0E34, 220 0x080A0F0C/0x080A0F54. The stubs sit at the same addresses as on the D878UV. 565 bytes changed, and the length is unchanged (0xE3640, matching the stock `.CDI`).

What the signature port does **not** prove: that the UVII has no *additional* band checks the D878UV lacks. Its extra 64 bytes are unexplained. The same hardware caveats apply.

## Suggested first test
1. Flash, set region 1 in the secret menu, then confirm stock VHF/UHF channels still receive and transmit normally into a dummy load.
2. VFO to a strong local airband frequency (ATIS/tower), wide bandwidth, squelch open: listen for the carrier and distorted audio.
3. Step 136 → 130 → 125 → 118 and note where reception falls off. That's the synthesizer/front-end limit.
