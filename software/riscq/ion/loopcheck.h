/* loopcheck.h — board self-check of a beam through the DAC 0 -> ADC 0 loopback cable: the 729
 * (u1c1, traced) at span[0] with the attenuation P[P_ATT], on for P[P_ON] ns, then off. The trace
 * (robs) holds the last 1024 batches of the traced fire window. */
#ifndef LOOPCHECK_H
#define LOOPCHECK_H
static void experiment_run(volatile int32_t *P, volatile int32_t *span, int32_t npts, int32_t reps,
                           volatile int32_t *pmt) {
    (void)npts; (void)reps; (void)pmt;
    core_break_realtime();
    att_set(U1C1, P[P_ATT]);
    dds_set(U1C1, span[0], P[P_A_FULL]);
    delay_mu(P[P_D10US]);
    sw_on(U1C1);
    delay_mu(P[P_ON]);
    sw_off(U1C1);
    delay_mu(P[P_D10US]);
}
#endif
