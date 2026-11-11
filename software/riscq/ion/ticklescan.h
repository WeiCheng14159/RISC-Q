/* ticklescan.h — test_pmt.py `Ticklescan` (ARTIQ 8) on RISC-Q, method for method.
 * U0C0 = urukul0_ch0 (trap RF), U0C1 = urukul0_ch1 (tickle, AD9912), U1C2 = urukul1_ch2 (397). */
#ifndef TICKLESCAN_H
#define TICKLESCAN_H
/* Ticklescan.run() */
static void experiment_run(volatile int32_t *P, volatile int32_t *span, int32_t npts, int32_t reps,
                           volatile int32_t *pmt) {
    (void)reps;
    core_reset();
    delay_mu(P[P_D200US]);
    delay_mu(P[P_D1MS]);
    att_set(U0C0, P[P_ATT_9_5]);
    att_set(U0C1, P[P_ATT_0]);
    dds_set(U1C2, P[P_F_397], P[P_A_FULL]);        /* (90 + respoint/2 - 2.5) MHz, amplitude=1.0 */
    delay_mu(P[P_D1MS]);
    for (int32_t p = 0; p < npts; p++) {           /* for freq in self.ticklerange */
        delay_mu(P[P_D10US]);
        dds9912_set(U0C1, span[p], P[P_A_FULL], 0);   /* urukul0_ch1.set(freq * Hz) */
        delay_mu(P[P_D100US]);
        sw_on(U0C1);
        delay_mu(P[P_D10US]);
        pmt[p] = pmt_number(P, P[P_GATE10]);        /* record_data(self.pmt_number(10)) */
        delay_mu(P[P_D250US]);
        sw_off(U0C1);
        delay_mu(P[P_D1MS]);
    }
    dds_set(U1C2, P[P_F_100MHZ], P[P_A_FULL]);
    delay_mu(P[P_D1MS]);
}
#endif
