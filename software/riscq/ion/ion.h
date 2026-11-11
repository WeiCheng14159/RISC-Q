/* ion.h — the RISC-Q runtime of the trapped-ion port (riscq.ion): ARTIQ 8's timeline semantics for an
 * Urukul / TTL / EdgeCounter experiment, on one RISC-Q core of the zcu216-ion build. Included after
 * riscq.h (compile_kernel(..., include=[("ion.h", ...)])).
 *
 * Time. `ion_cursor` is ARTIQ's now_mu in ns (int64), advanced with ARTIQ's own arithmetic — delays
 * arrive as ARTIQ machine units computed host-side exactly as the ARTIQ 8 compiler does, the AD9910
 * set() aligns down to the 8 ns grid (`at_mu(now_mu() & ~7)`), every driver call costs its ARTIQ 8
 * duration (ION_*_NS, measured by riscq/ion/artiq_ref.py from the ARTIQ 8 drivers themselves), and
 * break_realtime puts the cursor at real time + 125000 mu, aligned to the 8 ns coarse grid. Only at
 * the hardware boundary is the ns cursor quantized to the 2 ns batch (`ion_hw`: floor).
 *
 * Beams (Urukul channels -> fine-frequency RF channels RF_CH0..7). The envelope RAM of each beam is
 * uniform full-scale; slot ON has dur 0xFFFF, which a fine channel HOLDS (holdMaxDur) — a continuous
 * tone that stays on, across kernels, until the next fire: the Urukul RF switch. sw.on() fires ON at the
 * cursor, sw.off() fires OFF (amp 0, dur 1). A DDS frequency / amplitude or attenuator change while on
 * refires ON at the change (seamless: the carrier is absolute-time referenced). The DDS frequency change keeps the
 * output phase continuous (the AD9910's PHASE_MODE_CONTINUOUS): ph += top16((f_old − f_new)·16·t).
 * Amplitude = DDS ASF amplitude × attenuator: (acode · attq + 2^14) >> 15, attq = round(2^15·10^(−dB/20)).
 *
 * TTL bank (RF_CH9): output lines 0..7 = PMOD0 (ARTIQ ttl4..7 on lines 4..7), input lines 8..15 =
 * PMOD1, every input a gated counter (EdgeCounter): its gate is output bit `line`. Same-time writes to
 * different lines are coalesced into one entry (one DIO queue serves all 16 lines).
 *
 * Queues. Every RF channel and the TTL bank schedule through 4-deep timed queues; a push into a full
 * queue would be DROPPED by the hardware, so every push first waits until the oldest pending entry has
 * started (ARTIQ's CPU likewise stalls on a full RTIO FIFO). A push whose time is closer than RQ_LEAD
 * to real time lands late — counted in ion_late (ARTIQ would raise RTIOUnderflow).
 *
 * Log. Every operation appends {op | beam << 8, t_ns (low 32), a, b} to the host-window log array, the
 * record riscq/ion/compare.py checks against the ARTIQ 8 reference. */
#ifndef ION_H
#define ION_H
#include <stdint.h>

#define ION_NBEAM     8
#define ION_HOLD      65535u          /* dur all-ones: hold until the next fire (holdMaxDur) */
#define ION_DEPTH     4               /* TimedQueue depth */
#define ION_SLOT_ON   0
#define ION_SLOT_OFF  1
#define ION_SLACK_NS  125000          /* ARTIQ Core.break_realtime / reset margin */
#define ION_TTL       RF_CH9
#define ION_SINK      RQ_SINK_TTL
#define ION_CH(b)     (RF_CH0 + (uint32_t)(b) * (RF_CH1 - RF_CH0))

/* ARTIQ 8 driver costs (ns), from the reference run (riscq/ion/artiq_ref.py) */
#define ION_9910_IOU_NS   1248        /* AD9910.set: IO_UPDATE at aligned start + 1248 */
#define ION_9910_END_NS   1256        /*             returns at aligned start + 1256 */
#define ION_ATT_NS        1600        /* CPLD.set_all_att_mu: latched + returns at start + 1600 */
#define ION_9912_IOU_NS   1376        /* AD9912.set: no alignment; IO_UPDATE at start + 1376 */
#define ION_9912_END_NS   1386        /*             returns at start + 1386 (10 ns pulse) */

/* photon emulation (board self-test): emitted on output line 0, looped back into input line 8 */
#define ION_EMU_OUT       0
#define ION_EMU_FIRST     500u        /* batches after the gate opens */
#define ION_EMU_SPACING   1000u       /* 2 us between photons */

enum { ION_OP_SW = 1, ION_OP_DDS = 2, ION_OP_ATT = 3, ION_OP_GATE = 4, ION_OP_COUNT = 5,
       ION_OP_BREAK = 6, ION_OP_TTL = 7, ION_OP_RESET = 8, ION_OP_MARK = 9, ION_OP_X = 10 };

struct ion_q { uint32_t t[ION_DEPTH]; uint32_t n, head; };

static int64_t  ion_cursor;
static struct ion_q ion_fq[ION_NBEAM], ion_pq[ION_NBEAM], ion_dq;
static uint32_t ion_on[ION_NBEAM];
static int32_t  ion_f[ION_NBEAM], ion_a[ION_NBEAM], ion_att[ION_NBEAM], ion_ph[ION_NBEAM], ion_hwf[ION_NBEAM];
static int32_t  ion_pow[ION_NBEAM];                  /* the DDS phase offset word (16-bit turns) */
static uint32_t ion_dpend, ion_dpend_t, ion_dpend_m, ion_dpend_v, ion_last_dt, ion_tight;
static uint32_t ion_late, ion_emu_trunc;
static volatile int32_t *ion_log;
static uint32_t ion_log_n, ion_log_cap;
static volatile int32_t *ion_emu;
static uint32_t ion_emu_i, ion_emu_n;

static inline uint32_t ion_hw(int64_t ns) { return (uint32_t)(ns >> 1); }
static inline int32_t  ion_dt(uint32_t a, uint32_t b) { return (int32_t)(a - b); }

static void ion_logw(uint32_t op, uint32_t b, int64_t t, int32_t a, int32_t c) {
    if (ion_log_n + 4 > ion_log_cap) return;
    ion_log[ion_log_n + 0] = (int32_t)(op | (b << 8));
    ion_log[ion_log_n + 1] = (int32_t)(uint32_t)t;
    ion_log[ion_log_n + 2] = a;
    ion_log[ion_log_n + 3] = c;
    ion_log_n += 4;
}

/* real time, extended into the cursor's ns epoch */
static int64_t ion_now_ns(void) {
    return ion_cursor + 2 * (int64_t)ion_dt(now(), ion_hw(ion_cursor));
}

static void ion_q_drain(struct ion_q *q) {
    uint32_t nw = now();
    while (q->n && ion_dt(nw, q->t[q->head]) >= 0) { q->head = (q->head + 1) & (ION_DEPTH - 1); q->n--; }
}

static void ion_q_push(struct ion_q *q, uint32_t t) {
    ion_q_drain(q);
    while (q->n == ION_DEPTH) {                      /* full: wait for the oldest entry to start */
        wait_until(q->t[q->head]);
        ion_q_drain(q);
    }
    if (ion_dt(t, now()) < (int32_t)RQ_LEAD) ion_late++;
    q->t[(q->head + q->n) & (ION_DEPTH - 1)] = t;
    q->n++;
}

static inline int32_t ion_amp(uint32_t b) { return (ion_a[b] * ion_att[b] + (1 << 14)) >> 15; }

static void ion_fire(uint32_t b, uint32_t t, int on) {
    uint32_t ch = ION_CH(b);
    ion_q_push(&ion_fq[b], t);
    set_start(ch, t);
    if (on) {
        if (ion_hwf[b] != ion_f[b]) {                /* the new frequency, queued against startTime t */
            ion_q_push(&ion_pq[b], t);
            set_freq(ch, ion_f[b]);
            ion_hwf[b] = ion_f[b];
        }
        set_amp(ch, ION_SLOT_ON, ion_amp(b) << 16);
        set_phase(ch, ION_SLOT_ON, ((ion_ph[b] + ion_pow[b]) & 0xFFFF) << 16);
        fire(ch, ION_SLOT_ON);
    } else {
        fire(ch, ION_SLOT_OFF);
    }
}

static void ion_retile(uint32_t b, uint32_t t) {     /* refire a lit beam at t (new params) */
    if (ion_on[b]) ion_fire(b, t, 1);
}

/* ── the TTL bank (coalesced: one entry per batch) ── */
static void ion_dio_flush(void) {
    if (!ion_dpend) return;
    uint32_t t = ion_dpend_t;
    if (ion_last_dt && ion_dt(t, ion_last_dt) < 2) ion_tight++;   /* closer than the 2-batch hold */
    ion_q_push(&ion_dq, t);
    dio_slot(ION_TTL, 0, ion_dpend_m, ion_dpend_v, 2);
    set_start(ION_TTL, t);
    fire(ION_TTL, 0);
    ion_last_dt = t;
    ion_dpend = 0;
}

static void ion_dio(uint32_t t, uint32_t mask, uint32_t value) {
    if (ion_dpend && ion_dpend_t == t) {
        ion_dpend_m |= mask;
        ion_dpend_v = (ion_dpend_v & ~mask) | (value & mask);
        return;
    }
    ion_dio_flush();
    ion_dpend = 1; ion_dpend_t = t; ion_dpend_m = mask; ion_dpend_v = value & mask;
}

/* ── the ARTIQ API ── */
static void ion_init(volatile int32_t *log, int32_t log_words, volatile int32_t *emu, int32_t emu_n) {
    for (uint32_t b = 0; b < ION_NBEAM; b++) {
        set_dur(ION_CH(b), ION_SLOT_ON, ION_HOLD << 16);
        set_dur(ION_CH(b), ION_SLOT_OFF, 1u << 16);
        ion_att[b] = 1 << 15;                       /* 0 dB until set_att */
        ion_hwf[b] = 0x7FFFFFFF;                    /* nothing programmed yet */
    }
    ion_log = log; ion_log_cap = (uint32_t)log_words; ion_log_n = 0;
    ion_emu = emu; ion_emu_n = emu_n > 0 ? (uint32_t)emu_n : 0; ion_emu_i = 0;
    set_dc_offset(ION_TTL, ion_emu_n ? (int32_t)((1u << (8 + ION_EMU_OUT)) << 16) : 0);
    ion_cursor = 2 * (int64_t)now();
}

static void delay_mu(int32_t d) { ion_cursor += d; }

static void at_mu_rel(int32_t d) { ion_cursor += d; }  /* raw cursor move (no service): at_mu helpers */

static void core_break_realtime(void) {
    ion_dio_flush();
    int64_t m = ion_now_ns() + ION_SLACK_NS;
    m = (m + 7) & ~(int64_t)7;                        /* the RTIO counter is coarse (8 ns) */
    ion_logw(ION_OP_BREAK, m > ion_cursor, ion_cursor, (int32_t)(uint32_t)m, 0);
    if (m > ion_cursor) ion_cursor = m;
}

static void core_reset(void) {
    ion_dio_flush();
    int64_t m = (ion_now_ns() + ION_SLACK_NS + 7) & ~(int64_t)7;
    ion_logw(ION_OP_RESET, 0, ion_cursor, (int32_t)(uint32_t)m, 0);
    ion_cursor = m;
}

static void sw_set(uint32_t b, int on) {             /* urukulN_chM.sw.on() / .off() */
    uint32_t t = ion_hw(ion_cursor);
    if (on && !ion_on[b]) {
        ion_on[b] = 1;
        ion_fire(b, t, 1);
    } else if (!on && ion_on[b]) {
        ion_fire(b, t, 0);
        ion_on[b] = 0;
    }
    ion_logw(ION_OP_SW, b, ion_cursor, on, 0);
}
static void sw_on(uint32_t b)  { sw_set(b, 1); }
static void sw_off(uint32_t b) { sw_set(b, 0); }

static void ion_dds(uint32_t b, int32_t fcode, int32_t acode, int32_t pow16, int align, int32_t iou, int32_t end) {
    int64_t a = align ? (ion_cursor & ~(int64_t)7) : ion_cursor;   /* AD9910: at_mu(now_mu() & ~7) */
    int64_t te = a + iou;
    ion_cursor = a + end;
    uint32_t t = ion_hw(te);
    if (fcode != ion_f[b]) {                          /* phase-continuous frequency switch */
        uint32_t d = (uint32_t)(ion_f[b] - fcode) * (t << 4);
        ion_ph[b] = (ion_ph[b] + (int32_t)(d >> 16)) & 0xFFFF;
        ion_f[b] = fcode;
    }
    ion_a[b] = acode;
    ion_pow[b] = pow16 & 0xFFFF;
    ion_retile(b, t);
    ion_logw(ION_OP_DDS, b, te, fcode, acode);
}
/* urukul1 (AD9910) set(f, phase, amplitude): fcode = round(FTW / 8), acode from the ASF, POW 16-bit */
static void dds_set(uint32_t b, int32_t fcode, int32_t acode) { ion_dds(b, fcode, acode, 0, 1, ION_9910_IOU_NS, ION_9910_END_NS); }
static void dds_set_ph(uint32_t b, int32_t fcode, int32_t acode, int32_t pow16) { ion_dds(b, fcode, acode, pow16, 1, ION_9910_IOU_NS, ION_9910_END_NS); }
/* urukul0 (AD9912) set(f, phase): fcode = round(FTW48 / 2^19), no ASF (full scale), POW 14-bit << 2 */
static void dds9912_set(uint32_t b, int32_t fcode, int32_t acode, int32_t pow16) { ion_dds(b, fcode, acode, pow16, 0, ION_9912_IOU_NS, ION_9912_END_NS); }

static void att_set(uint32_t b, int32_t attq) {      /* urukulN_chM.set_att(dB) */
    int64_t te = ion_cursor + ION_ATT_NS;
    ion_cursor = te;
    uint32_t t = ion_hw(te);
    ion_att[b] = attq;
    ion_retile(b, t);
    ion_logw(ION_OP_ATT, b, te, attq, 0);
}

static void ttl_set(uint32_t line, int on) {         /* ttlN.on() / .off() */
    uint32_t t = ion_hw(ion_cursor);
    ion_dio(t, 1u << line, on ? (1u << line) : 0);
    ion_logw(ION_OP_TTL, line, ion_cursor, on, 0);
}

/* ttlN_counter.gate_rising_mu(d): the gate [cursor, cursor + d) on input `line` */
static void pmt_gate(uint32_t line, int32_t d) {
    int64_t c0 = ion_cursor;
    uint32_t t0 = ion_hw(c0);
    ion_dio(t0, 1u << line, 1u << line);
    ion_cursor += d;
    uint32_t t1 = ion_hw(ion_cursor);
    if (ion_emu_n && line == 8 + ION_EMU_OUT) {      /* emulated photons, emitted inside the gate */
        uint32_t k = ion_emu_i < ion_emu_n ? (uint32_t)ion_emu[ion_emu_i] : 0;
        ion_emu_i++;
        uint32_t tp = t0 + ION_EMU_FIRST;
        for (uint32_t i = 0; i < k; i++, tp += ION_EMU_SPACING) {
            if (ion_dt(t1, tp + 2) <= 0) { ion_emu_trunc++; break; }
            ion_dio(tp, 1u << ION_EMU_OUT, 1u << ION_EMU_OUT);
            ion_dio(tp + 2, 1u << ION_EMU_OUT, 0);
        }
    }
    ion_dio(t1, 1u << line, 0);
    ion_logw(ION_OP_GATE, line, c0, (int32_t)(uint32_t)ion_cursor, 0);
}

/* ttlN_counter.fetch_count(): halts until the gate's count posts */
static int32_t pmt_fetch(uint32_t line) {
    ion_dio_flush();
    int32_t n;
    for (;;) {
        uint32_t w = pop_event(ION_SINK);
        if ((w >> 16) & (1u << line)) { n = (int32_t)(w & 0xFFFF); break; }
    }
    ion_logw(ION_OP_COUNT, line, ion_cursor, n, 0);
    return n;
}

/* a call on a device the zcu216-ion build has no counterpart for (Sampler, Zotino): logged, and the
 * cursor advanced by the call's ARTIQ 8 timeline cost, so everything after it stays on ARTIQ's timeline */
static void ion_unsupported(uint32_t id, int32_t cost_ns) {
    ion_logw(ION_OP_X, id, ion_cursor, cost_ns, 0);
    ion_cursor += cost_ns;
}

/* end of a kernel: flush the TTL bank, wait out every scheduled entry, report the run health */
static void ion_finish(volatile int32_t *stat) {
    ion_dio_flush();
    wait_until(ion_hw(ion_cursor) + 4);              /* lit beams keep holding after the kernel */
    stat[0] = (int32_t)ion_log_n;
    stat[1] = (int32_t)ion_late;
    stat[2] = (int32_t)ion_tight;
    stat[3] = (int32_t)ion_emu_trunc;
    stat[4] = (int32_t)ion_emu_i;
}
#endif
