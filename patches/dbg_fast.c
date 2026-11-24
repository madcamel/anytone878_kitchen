/* Debug channel v2 ("fast"): part of the baseline, built by patches/mkpatch_debug.py.
 *
 * Stock: task_p3_usb_pc_link wakes only on the 10 ms OS flag and serves one 64-byte USB packet per
 * wake, so the debug channel tops out at 100 requests/s; and usb_pc_link_step ignores the link while
 * the radio transmits (rf_tx_active), so debug commands stop during TX.
 *
 * dbg_usb_irq   replaces the USB_LP_CAN1_RX0 vector: stock handler, then, if an OUT packet is
 *               pending, set IRQ 25 (TIM1_UP/TIM10, unused) pending. It calls NO uC/OS function:
 *               USB runs at priority 0x10, above BASEPRI 0x20 used by OS critical sections, so the
 *               OS isn't protected against it (calling OSFlagPost/OSIntExit here locked the radio).
 * dbg_wake_irq  IRQ 25 handler at priority 0x30 (masked by critical sections, so kernel-safe):
 *               OSIntEnter, post the p3 flag (0x10), OSIntExit, so p3 runs as soon as the
 *               interrupts return instead of at the next 10 ms tick.
 * dbg_link_step replaces task_p3's call to usb_pc_link_step: an exact debug command (D0/D1/D2,
 *               same format rules as dbg_hook) is served directly, also while transmitting, with
 *               the stock re-arm and power-save keep-awake; everything else goes to the stock
 *               function unchanged (CPS, firmware update, NAND dump).
 * No RAM of its own. Integer-only. */
#include <stdint.h>

extern volatile uint16_t usb_rx_pending_len;
extern volatile uint8_t  usb_rx_done;
extern volatile uint8_t  usb_rx_timeout;
extern volatile uint8_t  usb_dev_state;
extern volatile uint8_t  powersave_active;
extern volatile uint16_t powersave_hold;
extern volatile uint16_t pc_rx_len;
extern volatile uint8_t  pc_buf[];
extern uint8_t usb_cdc_handle[];
extern void *volatile os_flags_usb_dmr;
extern volatile uint8_t  OSRunning;

void USB_LP_CAN1_RX0_IRQHandler(void);
void OSIntEnter(void);
void OSIntExit(void);
uint16_t OSFlagPost(void *grp, uint16_t flags, uint8_t opt, uint8_t *err);
void usb_pc_link_step(void);
void usb_cdc_rx_arm(void *handle);
void DBG_HOOK(void);                        /* dbg_hook (mkpatch_debug.py), handles D0/D1/D2 + reply */

#define OS_FLAG_SET 1u
#define P3_FLAG     0x10u
#define WAKE_IRQ    25u                       /* TIM1_UP / TIM10: never enabled by the firmware */
#define WAKE_PRIO   0x30u                     /* >= BASEPRI 0x20: masked by OS critical sections */
#define NVIC_ISER0  (*(volatile uint32_t *)0xE000E100)
#define NVIC_ISPR0  (*(volatile uint32_t *)0xE000E200)
#define NVIC_IPR(n) (*(volatile uint8_t *)(0xE000E400 + (n)))

void dbg_usb_irq(void)                        /* priority 0x10: no uC/OS calls allowed here */
{
    USB_LP_CAN1_RX0_IRQHandler();
    if (usb_rx_pending_len) {
        NVIC_IPR(WAKE_IRQ) = WAKE_PRIO;
        NVIC_ISER0 = 1u << WAKE_IRQ;
        NVIC_ISPR0 = 1u << WAKE_IRQ;
    }
}

void dbg_wake_irq(void)                       /* priority 0x30: kernel-aware */
{
    if (OSRunning != 1 || !os_flags_usb_dmr)
        return;
    OSIntEnter();
    uint8_t err;
    OSFlagPost(os_flags_usb_dmr, P3_FLAG, OS_FLAG_SET, &err);
    OSIntExit();
}

static int is_dbg(uint32_t n)
{
    if (n < 8 || pc_buf[1] != 'd')
        return 0;
    uint32_t c = pc_buf[0], k = pc_buf[2];
    if (c == 0xD0)
        return n == 8 && k != 0;
    if (c == 0xD1)
        return k != 0 && n == 8 + k;
    if (c == 0xD2)
        return n == 24;
    return 0;
}

void dbg_link_step(void)
{
    uint32_t n = usb_rx_pending_len;
    if (usb_dev_state == 3 && n && is_dbg(n)) {
        powersave_hold = powersave_active ? 0 : 500;     /* as usb_pc_link_step: PC traffic keeps the radio awake */
        if (usb_rx_done == 1) {
            usb_rx_timeout = 0x32;
            usb_cdc_rx_arm(usb_cdc_handle);
        }
        pc_rx_len = (uint16_t)n;
        usb_rx_pending_len = 0;
        DBG_HOOK();
        return;
    }
    usb_pc_link_step();
}
