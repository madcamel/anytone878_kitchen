# Transmit audio over the USB data link

With the programming cable in the accessory jack there's no mic/speaker audio connection, so
`tools/txaudio.py` sends audio from the PC through the USB data link instead:

```sh
tools/txaudio.py --tts "N0CALL test" --yes     # speak text (espeak or espeak-ng)
tools/txaudio.py message.wav --yes             # any format sox reads
tools/txaudio.py message.wav --dry             # stream without keying: checks the chain, no RF
tools/txaudio.py message.wav --yes --expect 147.105 --level 0.7
```

It keys the transmitter like a PTT press, plays the audio, and unkeys (also on errors or Ctrl-C).
It transmits on the radio's current channel/VFO with its power and CTCSS/DCS settings, so check
those first; `--expect MHz` refuses (and unkeys) if the TX frequency is different. `--level`
scales the audio relative to the radio's own APRS tone level; `--max` (default 60 s) caps the clip
length. The radio's TOT still applies as a backstop.

Requirements: the debug channel v2 firmware (any `patches/modbuild.py` image, or
`patches/mkpatch_debug.py`): v1 can't keep up and goes silent while transmitting. `sox` on the PC;
`espeak`/`espeak-ng` for `--tts`. The radio must be idle on the standby screen.

## How it works

- **DAC:** the radio's beeps and APRS AFSK come from a PWM output on PB9 (TIM4 channel 4, 76.8 kHz,
  duty 0..1000 of 1562 for the stock tones), which is mixed into the transmit audio. The tool uses
  the stock helpers to set it up (`gpio_pb9_af_pwm_out`, `afsk_pwm_tim4_ch4_start`).
- **Sample clock:** TIM1 (unused by the firmware) runs at 8 kHz and requests DMA1 channel 5, which
  copies the next 16-bit sample from a 2048-sample ring in free RAM (0x20016000) into TIM4's duty
  register.
- **Streaming:** the PC converts the audio to 8 kHz (300–3000 Hz band-pass), maps it to duty
  values around 500, keeps the ring filled with 56-byte debug pokes (~16 KB/s of the ~100 KB/s
  link) and follows the DMA position by reading its counter.
- **Order:** PTT first (key code 0x18 to the UI task), wait for `rf_tx_active`, then set up the
  audio: the stock key-up path reconfigures audio timers. Afterwards TIM1, TIM4, PB9 and DMA are
  put back and PTT released (key code 0x19).

## Things learned the hard way

- **Don't pace audio with TIM6.** The firmware stops TIM6 on every retune (dual watch switches
  several times a second) and the `aprs_rx` mod re-arms it at 9600 Hz, so the sample rate jumped
  between ~2.5 and ~8.5 kHz. TIM1 → DMA1 ch5 is untouched by the firmware.
- **A low buzz on the receiver** is usually the channel's CTCSS tone (sub-audible, but audible on a
  receiver without a sub-tone filter), not the audio path.
