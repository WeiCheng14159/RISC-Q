# RFSoC4x2 physical constraints for the PulseTableSoc block-design wrapper.
# The LMK04828 PL_CLK (CLKout8, 500 MHz) and PL_SYSREF (SDCLKout9, 10 MHz) reach the PL single-ended on
# HD-bank pins, as in Xilinx's RFSoC-MTS RFSoC4x2 design (boards/RFSoC4x2/build_mts/mts.xdc); the clock
# files drive both outputs HSDS8 (software/riscq/board/clocks/RFSoC4x2). hostClk is the PS pl_clk0 and the
# converter pins are dedicated RF pins, so nothing else needs a LOC. Clock periods: inc/bd-finalize.tcl
# writes <build>/clocks-rfsoc4x2.xdc from the RF clock plan.

set_property -dict {PACKAGE_PIN AN11 IOSTANDARD LVCMOS18} [get_ports {dspClk}]
set_property -dict {PACKAGE_PIN AP18 IOSTANDARD LVCMOS18} [get_ports {user_sysref}]

# PL_CLK is not on a pin whose CMT column holds every load: let the BUFG reach the other columns (as the
# Xilinx design does for its BUFG_PL_CLK).
set_property CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN [get_nets -of_objects [get_pins -hierarchical -filter {NAME =~ "*dsp_bufg*/BUFG_O*"}]]
