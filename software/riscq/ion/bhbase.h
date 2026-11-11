/* bhbase.h — test_pmt.py `_BHbase` methods shared by every experiment class (ARTIQ 8 -> RISC-Q). */
#ifndef BHBASE_H
#define BHBASE_H
/* pmt_number(itime): ttl0_counter.gate_rising(itime*ms); return fetch_count() — `gate_mu` is
 * seconds_to_mu(itime*ms), computed host-side (itime <= 0 -> 10, as the ARTIQ method does) */
static int32_t pmt_number(volatile int32_t *P, int32_t gate_mu) {
    (void)P;
    pmt_gate(PMT, gate_mu);
    return pmt_fetch(PMT);
}
#endif
