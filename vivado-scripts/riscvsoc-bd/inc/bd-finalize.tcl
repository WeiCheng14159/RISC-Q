# ---- Validate, generate the HDL wrapper, add constraints, set the top ------------------------------
validate_bd_design
set WR_BUILD [llength [get_bd_ports -quiet wrRefClkP]]   ;# White Rabbit ports present? (query pre-close)

make_wrapper -files [get_files $BD_NAME.bd] -top -import -force
generate_target all [get_files $BD_NAME.bd]
close_bd_design $BD_NAME

set_property top ${BD_NAME}_wrapper [current_fileset]

set _pins $SCRIPT_DIR/constraints-[string tolower $BOARD].xdc
if {![file exists $_pins]} { error "no pin constraints for $BOARD: $_pins" }
add_files -fileset constrs_1 -norecurse $_pins
# Board clocks, periods from the RF clock plan: dspClk = the LMK04828 PL_CLK, dac_clk / adc_clk = the LMX
# reference into tile RF_CLK_TILE, hostClk = the ZCU216's 100 MHz oscillator (the 4x2 uses the PS pl_clk0,
# which the PS IP constrains itself).
set _xdc $BUILD_DIR/clocks-[string tolower $BOARD].xdc
set _fh [open $_xdc w]
set _dsp [expr {$BOARD eq "ZCU216" ? "dspClk_clk_p" : "dspClk"}]
puts $_fh "create_clock -period [format %.3f [expr {1e9 / $DSP_FREQ}]] -name $_dsp \[get_ports {$_dsp}\]"
if {$BOARD eq "ZCU216"} {
  puts $_fh "create_clock -period [format %.3f [expr {1e9 / $HOST_FREQ}]] -name hostClk_clk_p \[get_ports {hostClk_clk_p}\]"
}
puts $_fh "create_clock -period [format %.3f [expr {1e3 / $RF_REFCLK_MHZ}]] -name dac_clk_clk_p \[get_ports {dac_clk_clk_p}\]"
puts $_fh "create_clock -period [format %.3f [expr {1e3 / $RF_REFCLK_MHZ}]] -name adc_clk_clk_p \[get_ports {adc_clk_clk_p}\]"
puts $_fh "set_clock_groups -asynchronous -group {$_dsp}"
if {$BOARD eq "ZCU216"} { puts $_fh "set_clock_groups -asynchronous -group {hostClk_clk_p}" }
close $_fh
add_files -fileset constrs_1 -norecurse $_xdc
# White Rabbit builds (the BD grew wr* ports in bd-build.tcl): GTY placement + link clocks
if {$WR_BUILD && [file exists $SCRIPT_DIR/constraints-wr.xdc]} {
  add_files -fileset constrs_1 -norecurse $SCRIPT_DIR/constraints-wr.xdc
}
update_compile_order -fileset sources_1
