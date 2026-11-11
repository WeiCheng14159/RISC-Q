/* pulser_delay.h — test_pmt.py `pulser_delay` (ARTIQ 8) on RISC-Q: the PMT monitor.
 * The Sampler (vacuum gauge) has no counterpart on the zcu216-ion build: its calls keep their ARTIQ 8
 * timeline cost (ion_unsupported) and read 0.0, as the reference model's Sampler does. The ARTIQ loop
 * runs until the scheduler pauses it; here it runs `npts` iterations (the reference's pause_after + 1). */
#ifndef PULSER_DELAY_H
#define PULSER_DELAY_H
#define X_SAMPLER_SET_GAIN 1
#define X_SAMPLER_SAMPLE   2
/* count_pmt_oneshot() */
static void experiment_run(volatile int32_t *P, volatile int32_t *span, int32_t npts, int32_t reps,
                           volatile int32_t *pmt) {
    (void)span; (void)reps;
    core_reset();
    delay_mu(P[P_D500US]);
    for (int32_t ii = 0; ii < 8; ii++) ion_unsupported(X_SAMPLER_SET_GAIN, P[P_X_SET_GAIN]);  /* set_gain_mu(ii, 0) */
    delay_mu(P[P_D1MS]);
    for (int32_t i = 0; i < npts; i++) {          /* while True: … if check_pause(): return */
        pmt[i] = pmt_number(P, P[P_GATE100]);     /* record_data(self.pmt_number(100)) */
        delay_mu(P[P_D10MS]);
        ion_unsupported(X_SAMPLER_SAMPLE, P[P_X_SAMPLE]);   /* sampler0.sample(smp); record_VACdata(smp[0]) */
        delay_mu(P[P_D10MS]);
    }
}
#endif
