/* aprs_rx: analog APRS receive without the APRS daughterboard.
 *
 * The stock firmware has the whole APRS RX stack (parser, filters, inbox, popup, auto-ack) but expects
 * an external board to demodulate and send "+APRS:" + raw AX.25 over UART4. This mod demodulates
 * AFSK1200 on the MCU from the RX audio on PC0 and hands frames to that stack:
 *
 *   TIM6 mode 5 (9600 Hz)  aprs_tim6: DC block -> 1200/2200 Hz correlators over one bit -> 4 slicers
 *                          (space gains 0.5/0.7/1/1.4) -> DPLL -> NRZI -> HDLC -> CRC -> pending buffer
 *   rf_cmd_apply_lo        aprs_sigdec: after the stock decoder selection, start mode 5 if TIM6 is
 *                          idle and the channel is analog with "APRS Receive" on (rec +0x21 bit 5);
 *                          runs on every retune/TX end, so dual watch works too
 *   p14 housekeeping       aprs_p14: move a pending frame into aprs_rx_frame in the board's layout
 *                          ("+APRS:~" body FCS "~") under the stock acceptance conditions, then run
 *                          the stock parser
 *   parser FCS check       aprs_fcs: frames from this mod are already CRC-checked (the stock check
 *                          stops at the first '~' and would reject frames with 0x7E in the info)
 *   bt_board_probe         starts with aprs_board_present = 1: APRS RX menus visible
 *
 * Model and test oracle: tools/afsk1200.py. Interrupt code is integer-only. */
#include "fw.h"

#define FS      9600u
#define SPB     8u                          /* samples per bit */
#define NSL     4u                          /* slicers */
#define MAXF    191u                        /* body + FCS: what the stock UART path can deliver (<200 B incl. "+APRS:~" and "~") */
#define MINF    18u                         /* 2 addresses + ctl + pid + FCS */
#define STEP    ((uint32_t)(4294967296ull * 1200u / FS))
#define DUP_TICKS (2u * FS)                 /* same FCS within 2 s = same frame from another slicer */
#define RATE_PERIOD (120000000u / FS - 1u)  /* TIM6 clock 120 MHz (measured) */

struct slicer {
    uint32_t phase;
    int16_t  len;                           /* bytes collected, -1 = hunting for a flag */
    uint16_t crc;
    uint8_t  last_d, last_bit, reg, ones, nbits, byte;
    uint8_t  buf[MAXF];
};

struct state {
    uint32_t ticks;
    int32_t  dc;                            /* Q8 running mean of the 8-bit sample */
    int16_t  win[SPB];
    uint8_t  wi;
    volatile uint8_t ours;                  /* aprs_rx_frame currently holds a frame from this mod */
    volatile uint16_t plen;                 /* pending frame length (body + FCS), 0 = free */
    uint16_t last_fcs;
    uint32_t last_ticks;
    uint32_t frames, dropped;               /* statistics for the host (radio.py peek) */
    uint8_t  pbuf[MAXF];
    struct slicer sl[NSL];
};
#define S ((struct state *)MOD_RAM)
#define MAGIC 0x31785261u                   /* "aRx1" in the last word of the slice: state initialised */
#define MG (*(volatile uint32_t *)(MOD_RAM_END - 4))
_Static_assert(sizeof(struct state) + 4 <= MOD_RAM_SIZE, "struct state doesn't fit the RAM slice");

/* round(127 * cos/sin(2 pi f k / 9600)), k = 0..7 */
static const int8_t MC[SPB] = {127, 90, 0, -90, -127, -90, 0, 90};
static const int8_t MS[SPB] = {0, 90, 127, 90, 0, -90, -127, -90};
static const int8_t SC[SPB] = {127, 17, -123, -49, 110, 77, -90, -101};
static const int8_t SS[SPB] = {0, 126, 33, -117, -63, 101, 90, -77};
/* slicer d = mark * NUM > space * DEN  (space gains 0.5, 0.7, 1.0, 1.4: chosen on recorded bursts) */
static const uint8_t NUM[NSL] = {2, 10, 1, 5};
static const uint8_t DEN[NSL] = {1, 7, 1, 7};

static uint16_t crc_byte(uint16_t c, uint8_t b)
{
    c ^= b;
    for (int k = 0; k < 8; k++)
        c = (c & 1) ? (uint16_t)((c >> 1) ^ 0x8408) : (uint16_t)(c >> 1);
    return c;
}

static void frame_done(struct slicer *s)
{
    uint32_t n = (uint32_t)s->len;
    if (n < MINF || s->crc != 0xF0B8)        /* X.25 residue: CRC over body + FCS */
        return;
    uint16_t fcs = (uint16_t)(s->buf[n - 2] | (s->buf[n - 1] << 8));
    if (fcs == S->last_fcs && S->ticks - S->last_ticks < DUP_TICKS)
        return;
    if (S->plen) {
        S->dropped++;
        return;
    }
    for (uint32_t i = 0; i < n; i++)
        S->pbuf[i] = s->buf[i];
    S->last_fcs = fcs;
    S->last_ticks = S->ticks;
    S->frames++;
    S->plen = (uint16_t)n;
}

static void hdlc_bit(struct slicer *s, uint32_t b)
{
    s->reg = (uint8_t)((s->reg << 1) | b);
    if (s->reg == 0x7E) {
        if (s->len >= 0 && s->nbits == 7)
            frame_done(s);
        s->len = 0; s->nbits = 0; s->byte = 0; s->ones = 0; s->crc = 0xFFFF;
        return;
    }
    if (s->len < 0)
        return;
    if (b) {
        if (++s->ones > 6) {                 /* abort / idle */
            s->len = -1;
            return;
        }
    } else {
        if (s->ones == 5) {                  /* stuffed zero */
            s->ones = 0;
            return;
        }
        s->ones = 0;
    }
    s->byte |= (uint8_t)(b << s->nbits);
    if (++s->nbits == 8) {
        if (s->len >= (int16_t)MAXF) {
            s->len = -1;
            return;
        }
        s->buf[s->len++] = s->byte;
        s->crc = crc_byte(s->crc, s->byte);
        s->byte = 0;
        s->nbits = 0;
    }
}

static void demod_sample(void)
{
    struct state *st = S;
    int32_t x = (int32_t)(adc_pc0_rx_audio >> 4) << 8;
    st->dc += (x - st->dc) >> 6;
    st->win[st->wi] = (int16_t)((x - st->dc) >> 4);
    st->wi = (uint8_t)((st->wi + 1) & (SPB - 1));
    st->ticks++;
    int32_t mc = 0, ms = 0, sc = 0, ss = 0;
    for (uint32_t k = 0; k < SPB; k++) {
        int32_t v = st->win[(st->wi + k) & (SPB - 1)];
        mc += v * MC[k]; ms += v * MS[k];
        sc += v * SC[k]; ss += v * SS[k];
    }
    int64_t m = (int64_t)mc * mc + (int64_t)ms * ms;
    int64_t sp = (int64_t)sc * sc + (int64_t)ss * ss;
    for (uint32_t i = 0; i < NSL; i++) {
        struct slicer *s = &st->sl[i];
        uint8_t d = m * NUM[i] > sp * DEN[i];
        if (d != s->last_d) {                /* transition: pull the bit clock toward mid-bit */
            s->last_d = d;
            s->phase = (uint32_t)(int32_t)(((int64_t)(int32_t)s->phase * 2576980378ll) >> 32);   /* x0.6 */
        }
        uint32_t old = s->phase;
        s->phase = old + STEP;
        if (old < 0x80000000u && s->phase >= 0x80000000u) {
            hdlc_bit(s, d == s->last_bit);
            s->last_bit = d;
        }
    }
}

void aprs_tim6(uint32_t mode)
{
    if (mode == 3) {
        tim6_audio_zero_cross_period();
        return;
    }
    if (mode == 5)
        demod_sample();
}

static void demod_reset(void)
{
    struct state *st = S;
    st->dc = 128 << 8;
    st->wi = 0;
    for (uint32_t k = 0; k < SPB; k++)
        st->win[k] = 0;
    for (uint32_t i = 0; i < NSL; i++) {
        struct slicer *s = &st->sl[i];
        s->phase = 0; s->len = -1; s->reg = 0; s->ones = 0; s->nbits = 0; s->byte = 0;
        s->last_d = 0; s->last_bit = 0;
    }
}

static int aprs_channel(void)
{
    return (rf_chan_rec[0x21] & 0x20) && !chan_is_digital();
}

void aprs_sigdec(void)
{
    sigdec_select_for_channel();
    if (MG != MAGIC) {                      /* first use after boot: the slice holds garbage */
        S->plen = 0; S->ours = 0; S->frames = 0; S->dropped = 0; S->last_fcs = 0; S->ticks = 0;
        MG = MAGIC;
    }
    if (tim6_audio_mode == 0 && aprs_channel()) {
        demod_reset();
        tim6_audio_mode = 5;
        tim6_set_rate(RATE_PERIOD, 0);
        TIM_Cmd(TIM6, 1);
    }
}

void aprs_p14(void)
{
    struct state *st = S;
    if (MG != MAGIC) {                      /* nothing of ours can be pending before aprs_sigdec ran */
        aprs_rx_frame_process();
        return;
    }
    uint32_t n = st->plen;
    if (n && !aprs_rx_frame_ready) {
        if (aprs_channel() && aprs_tx_holdoff == 0 && ana_rx_audio_open) {
            static const char hdr[7] = "+APRS:~";
            for (uint32_t i = 0; i < 7; i++)
                aprs_rx_frame[i] = (uint8_t)hdr[i];
            for (uint32_t i = 0; i < n; i++)
                aprs_rx_frame[7 + i] = st->pbuf[i];
            aprs_rx_frame[7 + n] = '~';
            aprs_rx_frame[8 + n] = 0;
            aprs_rx_frame_len = (uint8_t)(n + 4);      /* = index of the last info byte (board layout) */
            aprs_rx_in_frame = 0;
            st->ours = 1;
            aprs_rx_frame_ready = 1;
        }
        st->plen = 0;
    }
    aprs_rx_frame_process();
    if (!aprs_rx_frame_ready)
        st->ours = 0;
}

int aprs_fcs(volatile uint8_t *start, uint32_t offset)
{
    if (MG == MAGIC && S->ours)
        return 1;
    return ax25_fcs_check(start, offset);
}
