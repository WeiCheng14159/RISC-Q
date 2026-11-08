# ---- RF clock plan: what the ZCU216's CLK104 delivers, and which RFDC tile takes it -------------------
#
# The converter sample rates are fixed multiples of the DSP clock (16 DAC / 4 ADC samples per dspClk
# cycle — SocSpec.batchSize / adcBatch, never parameterised), so choosing DSP_FREQ chooses the rates.
# What varies with it is the clock tree that has to deliver them:
#
#   qubic  (dsp 500 MHz) — the RISC-Q reference: DAC 8 GS/s, ADC 2 GS/s; tile 2 (DAC 230 / ADC 226)
#          takes a 500 MHz reference, runs its PLL and distributes the sample clock. Needs the
#          qcal-riscq LMK04828_500.25 xrfclk file.
#   spear  (dsp 300 MHz) — SPEAR's ZCU216 clocking, so its proven assets apply unchanged: LMK04828
#          10 MHz in -> 250 MHz to the LMXs, PL_CLK 300 MHz, PL/RF SYSREF 10 MHz
#          (LMK04828_10.0_250.0_300.0_10.0), LMX2594 250 -> 300 MHz (LMX2594_250.0_300.0), into
#          tile 1 (DAC 229 / ADC 225) — the tiles SPEAR's shells distribute from and MTS-reference.
#          DAC 4.8 GS/s, ADC 1.2 GS/s.
#
# RISCQ_CLOCK_PLAN overrides the choice; DSP_FREQ must still match the plan.

set CLOCK_PLANS {
  qubic {dsp 500000000 refclk_mhz 500.0 tile 2}
  spear {dsp 300000000 refclk_mhz 300.0 tile 1}
}

if {[info exists ::env(RISCQ_CLOCK_PLAN)]} {
  set CLOCK_PLAN $::env(RISCQ_CLOCK_PLAN)
} else {
  set CLOCK_PLAN ""
  dict for {_name _p} $CLOCK_PLANS {
    if {[dict get $_p dsp] == $DSP_FREQ} { set CLOCK_PLAN $_name }
  }
  if {$CLOCK_PLAN eq ""} {
    error "no RF clock plan for dsp ${DSP_FREQ} Hz (known: [dict keys $CLOCK_PLANS]) — add one to clock-plan.tcl"
  }
}
if {![dict exists $CLOCK_PLANS $CLOCK_PLAN]} { error "unknown RISCQ_CLOCK_PLAN $CLOCK_PLAN" }
set _p [dict get $CLOCK_PLANS $CLOCK_PLAN]
if {[dict get $_p dsp] != $DSP_FREQ} {
  error "clock plan $CLOCK_PLAN wants dsp [dict get $_p dsp] Hz, build has $DSP_FREQ Hz"
}
set RF_REFCLK_MHZ [dict get $_p refclk_mhz]
set RF_CLK_TILE   [dict get $_p tile]
set DSP_MHZ       [expr {$DSP_FREQ / 1e6}]
set DAC_FS_MHZ    [expr {16 * $DSP_MHZ}]
set ADC_FS_MHZ    [expr {4 * $DSP_MHZ}]
puts "\[clock-plan\] $CLOCK_PLAN: dsp ${DSP_MHZ} MHz, DAC ${DAC_FS_MHZ} / ADC ${ADC_FS_MHZ} MS/s, refclk ${RF_REFCLK_MHZ} MHz into tile $RF_CLK_TILE"
