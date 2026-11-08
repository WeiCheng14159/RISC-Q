# ---- Validate, generate the HDL wrapper, add constraints, set the top ------------------------------
validate_bd_design
set WR_BUILD [llength [get_bd_ports -quiet wrRefClkP]]   ;# White Rabbit ports present? (query pre-close)

make_wrapper -files [get_files $BD_NAME.bd] -top -import -force
generate_target all [get_files $BD_NAME.bd]
close_bd_design $BD_NAME

set_property top ${BD_NAME}_wrapper [current_fileset]

if {[file exists $SCRIPT_DIR/constraints-zcu216.xdc]} {
  add_files -fileset constrs_1 -norecurse $SCRIPT_DIR/constraints-zcu216.xdc
  # Board clocks, periods from the RF clock plan: dspClk = CLK104 PL_CLK, dac_clk / adc_clk = the LMX
  # reference into tile RF_CLK_TILE, hostClk = the 100 MHz board oscillator.
  set _xdc $BUILD_DIR/clocks-zcu216.xdc
  set _fh [open $_xdc w]
  puts $_fh "create_clock -period [format %.3f [expr {1e9 / $DSP_FREQ}]] -name dspClk_clk_p \[get_ports {dspClk_clk_p}\]"
  puts $_fh "create_clock -period [format %.3f [expr {1e9 / $HOST_FREQ}]] -name hostClk_clk_p \[get_ports {hostClk_clk_p}\]"
  puts $_fh "create_clock -period [format %.3f [expr {1e3 / $RF_REFCLK_MHZ}]] -name dac_clk_clk_p \[get_ports {dac_clk_clk_p}\]"
  puts $_fh "create_clock -period [format %.3f [expr {1e3 / $RF_REFCLK_MHZ}]] -name adc_clk_clk_p \[get_ports {adc_clk_clk_p}\]"
  puts $_fh "set_clock_groups -asynchronous -group {dspClk_clk_p}"
  puts $_fh "set_clock_groups -asynchronous -group {hostClk_clk_p}"
  close $_fh
  add_files -fileset constrs_1 -norecurse $_xdc
}
# White Rabbit builds (the BD grew wr* ports in bd-build.tcl): GTY placement + link clocks
if {$WR_BUILD && [file exists $SCRIPT_DIR/constraints-wr.xdc]} {
  add_files -fileset constrs_1 -norecurse $SCRIPT_DIR/constraints-wr.xdc
}
update_compile_order -fileset sources_1
