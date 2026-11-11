# ZCU216 PMOD0 / PMOD1 (bank 88, VCCO 1.8 V; the header side is level-shifted — the master XDC's
# PMOD*_LS nets) for the timed-DIO (TTL) bank: PMOD0 0..7 = bank output lines 0..7 (TTL out: AOM
# switches, shutters, triggers), PMOD1 0..7 = bank input lines 8..15 (TTL in: 8 = PMT counter).
# Pins from the ZCU216 master XDC (10 MAR 2020). Added by inc/bd-finalize.tcl on DIO builds only.

set_property -dict {PACKAGE_PIN G15 IOSTANDARD LVCMOS18} [get_ports {pmod0[0]}]
set_property -dict {PACKAGE_PIN G16 IOSTANDARD LVCMOS18} [get_ports {pmod0[1]}]
set_property -dict {PACKAGE_PIN H14 IOSTANDARD LVCMOS18} [get_ports {pmod0[2]}]
set_property -dict {PACKAGE_PIN H15 IOSTANDARD LVCMOS18} [get_ports {pmod0[3]}]
set_property -dict {PACKAGE_PIN G13 IOSTANDARD LVCMOS18} [get_ports {pmod0[4]}]
set_property -dict {PACKAGE_PIN H13 IOSTANDARD LVCMOS18} [get_ports {pmod0[5]}]
set_property -dict {PACKAGE_PIN J13 IOSTANDARD LVCMOS18} [get_ports {pmod0[6]}]
set_property -dict {PACKAGE_PIN J14 IOSTANDARD LVCMOS18} [get_ports {pmod0[7]}]
set_property -dict {PACKAGE_PIN L17 IOSTANDARD LVCMOS18} [get_ports {pmod1[0]}]
set_property -dict {PACKAGE_PIN M17 IOSTANDARD LVCMOS18} [get_ports {pmod1[1]}]
set_property -dict {PACKAGE_PIN M14 IOSTANDARD LVCMOS18} [get_ports {pmod1[2]}]
set_property -dict {PACKAGE_PIN N14 IOSTANDARD LVCMOS18} [get_ports {pmod1[3]}]
set_property -dict {PACKAGE_PIN M15 IOSTANDARD LVCMOS18} [get_ports {pmod1[4]}]
set_property -dict {PACKAGE_PIN N15 IOSTANDARD LVCMOS18} [get_ports {pmod1[5]}]
set_property -dict {PACKAGE_PIN M16 IOSTANDARD LVCMOS18} [get_ports {pmod1[6]}]
set_property -dict {PACKAGE_PIN N16 IOSTANDARD LVCMOS18} [get_ports {pmod1[7]}]

# outputs come straight from the bank's output register: pack it into the IOB (fixed pin delay, so every
# TTL line has the same clock-to-pin time); inputs land on the TimedDio synchronizer (ASYNC_REG).
set_property IOB TRUE [get_ports {pmod0[*]}]
# asynchronous TTL I/O: no setup/hold relationship to any clock
set_false_path -to [get_ports {pmod0[*]}]
set_false_path -from [get_ports {pmod1[*]}]
