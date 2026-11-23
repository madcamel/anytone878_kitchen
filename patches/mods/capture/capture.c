/* capture: squelch-triggered RX-audio recorder (development tool for APRS RX; either/or with aprs_rx).
 * TIM6 mode 4 stores ADC>>4 into a ring in MOD_RAM; the squelch opening triggers; after POST
 * samples (cut to TAIL once the squelch closes) the ring freezes (state 2) for the host
 * (tools/aprscap.py), which re-arms by writing state = 0. Mode 3 keeps the stock zero-cross handler. */
#include "fw.h"

struct cap {
    uint8_t  state;     /* 0 recording, 1 triggered, 2 frozen */
    uint8_t  pad;
    uint16_t wr;        /* next ring index */
    uint16_t left;      /* samples still to record after the trigger */
    uint16_t trig;      /* ring index at the trigger */
    uint32_t seq;       /* completed captures */
    uint32_t ticks;     /* ISR calls in mode 4 */
    uint8_t  buf[];
};
#define C   ((volatile struct cap *)MOD_RAM)
#define N   ((uint32_t)(MOD_RAM_END - MOD_RAM) - 16)
#define PRE  1920u      /* ~200 ms kept before the trigger */
#define TAIL 480u       /* ~50 ms after the squelch closes */

void cap_tim6(uint32_t mode)
{
    if (mode == 3) {
        tim6_audio_zero_cross_period();
        return;
    }
    if (mode != 4)
        return;
    C->ticks++;
    uint32_t st = C->state;
    if (st == 2)
        return;
    uint32_t wr = C->wr;
    C->buf[wr] = (uint8_t)(adc_pc0_rx_audio >> 4);
    if (++wr >= N)
        wr = 0;
    C->wr = (uint16_t)wr;
    uint32_t sq = ana_sq_open;
    if (st == 0) {
        if (sq) {
            C->state = 1;
            C->trig = (uint16_t)wr;
            C->left = (uint16_t)(N - PRE);
        }
        return;
    }
    uint32_t left = C->left;
    if (!sq && left > TAIL)
        left = TAIL;
    C->left = (uint16_t)--left;
    if (left == 0) {
        C->state = 2;
        C->seq++;
    }
}
