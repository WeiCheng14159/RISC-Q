# RF Data Converter for the RFSoC4x2 (xczu48dr): configuration + wiring.
#
# The board bonds out DAC 228 / 230 (slice 0 of DAC tiles 0 / 2) and both converters of the dual-converter
# ADC tiles 224 / 226; only tile 2 has a reference-clock input (the LMX2594 500 MHz, clock-plan.tcl). The
# SoC's converter channels map onto them as
#     DAC0 -> s00 (DAC 228)    DAC1 -> s20 (DAC 230)
#     ADC0 -> m00 (ADC 224 ch0) ADC1 -> m02 (224 ch1) ADC2 -> m20 (226 ch0) ADC3 -> m22 (226 ch1)
# at RISC-Q's rates (16 DAC / 4 ADC samples per dspClk cycle: DAC 8 GS/s, ADC 2 GS/s at 500 MHz).
#
# Clocking. DAC229 / DAC231 top out at 7 GS/s, so the tile-2 PLL output cannot be distributed to tile 0
# through tile 1 as on the ZCU216. Instead tile 2 distributes its 500 MHz REFERENCE (Clock_Dist 1), and
# every used tile runs its own PLL; MTS (tiles 0 and 2) aligns them. Tile 1 is the relay the distribution
# needs (no tile hopping): one slice enabled, its own PLL at a legal rate (DAC 4 GS/s, ADC 2 GS/s), its
# AXIS ports tied off. Tile 3 is unused and disabled. Vivado validates each set_property batch as a whole,
# so the tiles are configured in dependency order: tile 2, its clock, relay tile 1, then tile 0.
# Validated against the IP (usp_rf_data_converter 2.6, Vivado 2023.1).

if {$RF_CLK_TILE != 2} { error "rfdc-rfsoc4x2.tcl: the 4x2 reference enters tile 2, clock plan says $RF_CLK_TILE" }
set _ref $RF_CLK_TILE
set _fs_dac [expr {double($DAC_FS_MHZ)}]
set _fs_adc [expr {double($ADC_FS_MHZ)}]
set _relay_dac_fs 4000.0

proc rfdc4x2_set {cell props} {
  if {[catch {set_property -dict $props $cell} e]} { error "rfdc-rfsoc4x2: $e" }
}

# One tile's converters: DAC slice 0; ADC both converters (slices 0..3 of the dual tile) or, on the relay,
# one converter (slices 0/1).
proc rfdc4x2_tile {t dac_fs adc_fs relay} {
  set P [list CONFIG.DAC${t}_Enable 1 CONFIG.DAC${t}_Sampling_Rate [format %.3f [expr {$dac_fs / 1000}]] \
              CONFIG.ADC${t}_Enable 1 CONFIG.ADC${t}_Sampling_Rate [format %.3f [expr {$adc_fs / 1000}]]]
  lappend P CONFIG.DAC_Slice${t}0_Enable true CONFIG.DAC_Interpolation_Mode${t}0 1 \
            CONFIG.DAC_Mixer_Type${t}0 1 CONFIG.DAC_Coarse_Mixer_Freq${t}0 3 CONFIG.DAC_Mode${t}0 3 \
            CONFIG.DAC_Nyquist${t}0 0
  foreach s [expr {$relay ? {0 1} : {0 1 2 3}}] {
    lappend P CONFIG.ADC_Slice${t}${s}_Enable true CONFIG.ADC_Decimation_Mode${t}${s} 1 \
              CONFIG.ADC_Mixer_Type${t}${s} 1 CONFIG.ADC_Coarse_Mixer_Freq${t}${s} 3 \
              CONFIG.ADC_Data_Width${t}${s} 4 CONFIG.ADC_Dither${t}${s} false
  }
  return $P
}

# A tile's clock: own PLL from the 500 MHz reference; the ref tile distributes it, the others take it.
proc rfdc4x2_clock {t ref refclk} {
  set P [list CONFIG.DAC${t}_PLL_Enable true CONFIG.DAC${t}_Refclk_Freq [format %.3f $refclk] \
              CONFIG.ADC${t}_PLL_Enable true CONFIG.ADC${t}_Refclk_Freq [format %.3f $refclk]]
  if {$t == $ref} {
    lappend P CONFIG.DAC${t}_Clock_Dist 1 CONFIG.ADC${t}_Clock_Dist 1
  } else {
    lappend P CONFIG.DAC${t}_Clock_Source [expr {4 + $ref}] CONFIG.ADC${t}_Clock_Source $ref
  }
  return $P
}

rfdc4x2_set $RFDC_TARGET [rfdc4x2_tile 2 $_fs_dac $_fs_adc 0]
rfdc4x2_set $RFDC_TARGET [rfdc4x2_clock 2 $_ref $RF_REFCLK_MHZ]
rfdc4x2_set $RFDC_TARGET [dict merge [rfdc4x2_tile 1 $_relay_dac_fs $_fs_adc 1] [rfdc4x2_clock 1 $_ref $RF_REFCLK_MHZ]]
rfdc4x2_set $RFDC_TARGET [dict merge [rfdc4x2_tile 0 $_fs_dac $_fs_adc 0] [rfdc4x2_clock 0 $_ref $RF_REFCLK_MHZ]]
set _P {}
foreach t {0 1 2} { lappend _P CONFIG.ADC${t}_Outclk_Freq [format %.3f [expr {$_fs_adc / 16}]] }
foreach t {0 2} { lappend _P CONFIG.DAC${t}_Multi_Tile_Sync true CONFIG.ADC${t}_Multi_Tile_Sync true }
rfdc4x2_set $RFDC_TARGET $_P

# The configuration must have landed as asked (the IP silently keeps defaults for some invalid values).
foreach {k want} [list DAC0_Sampling_Rate [format %.3f [expr {$_fs_dac / 1000}]] DAC2_Sampling_Rate [format %.3f [expr {$_fs_dac / 1000}]] \
                       ADC0_Sampling_Rate [format %.3f [expr {$_fs_adc / 1000}]] ADC2_Sampling_Rate [format %.3f [expr {$_fs_adc / 1000}]] \
                       DAC0_Fabric_Freq [format %.3f $DSP_MHZ] DAC2_Fabric_Freq [format %.3f $DSP_MHZ] \
                       ADC0_Fabric_Freq [format %.3f $DSP_MHZ] ADC2_Fabric_Freq [format %.3f $DSP_MHZ] \
                       DAC2_Clock_Dist 1 ADC2_Clock_Dist 1 DAC0_Clock_Source 6 ADC0_Clock_Source 2] {
  set got [get_property CONFIG.$k $RFDC_TARGET]
  if {$got != $want} { error "rfdc-rfsoc4x2: CONFIG.$k is $got, want $want" }
}
puts "\[rfdc-rfsoc4x2\] DAC 228/230 @ [expr {$_fs_dac / 1000}] GS/s, ADC 224/226 x2 @ [expr {$_fs_adc / 1000}] GS/s, ref tile $_ref (relay tile 1)"

# ---- wiring ----
set _dac_map {0 {s00 vout00} 1 {s20 vout20}}
set _adc_map {0 {m00 vin0_01} 1 {m02 vin0_23} 2 {m20 vin2_01} 3 {m22 vin2_23}}
foreach {i e} $_dac_map {
  lassign $e axis pin
  if {![llength [get_bd_intf_pins -quiet $TOP/DAC${i}_AXIS]]} { error "rfdc-rfsoc4x2: the top has no DAC${i}_AXIS (config dac_num must be 2)" }
  connect_bd_intf_net [get_bd_intf_pins $TOP/DAC${i}_AXIS] [get_bd_intf_pins rf_data_converter/${axis}_axis]
}
foreach {i e} $_adc_map {
  lassign $e axis pin
  if {![llength [get_bd_intf_pins -quiet $TOP/ADC${i}_AXIS]]} { error "rfdc-rfsoc4x2: the top has no ADC${i}_AXIS (config adc_num must be 4)" }
  connect_bd_intf_net [get_bd_intf_pins $TOP/ADC${i}_AXIS] [get_bd_intf_pins rf_data_converter/${axis}_axis]
}
if {[llength [get_bd_intf_pins -quiet $TOP/DAC2_AXIS]] || [llength [get_bd_intf_pins -quiet $TOP/ADC4_AXIS]]} {
  error "rfdc-rfsoc4x2: the top has more converter channels than the 4x2 bonds out (dac_num 2, adc_num 4)"
}
# analog pins, the relay tile's included (dedicated RF pins: no LOC needed)
foreach pin {vout00 vout10 vout20} {
  create_bd_intf_port -mode Master -vlnv xilinx.com:interface:diff_analog_io_rtl:1.0 $pin
  connect_bd_intf_net [get_bd_intf_pins rf_data_converter/$pin] [get_bd_intf_ports $pin]
}
foreach pin {vin0_01 vin0_23 vin1_01 vin2_01 vin2_23} {
  create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:diff_analog_io_rtl:1.0 $pin
  connect_bd_intf_net [get_bd_intf_pins rf_data_converter/$pin] [get_bd_intf_ports $pin]
}

# sysref + the reference clock into tile 2
create_bd_intf_port -mode Slave -vlnv xilinx.com:display_usp_rf_data_converter:diff_pins_rtl:1.0 sysref_in
create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:diff_clock_rtl:1.0 dac_clk
create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:diff_clock_rtl:1.0 adc_clk
connect_bd_intf_net [get_bd_intf_ports sysref_in] [get_bd_intf_pins rf_data_converter/sysref_in]
connect_bd_intf_net [get_bd_intf_ports dac_clk]   [get_bd_intf_pins rf_data_converter/dac${_ref}_clk]
connect_bd_intf_net [get_bd_intf_ports adc_clk]   [get_bd_intf_pins rf_data_converter/adc${_ref}_clk]

# AXIS clocks/resets: the used tiles and the relay ADC on dspClk (all 500 MHz fabric); the relay DAC runs
# 250 MHz (4 GS/s, 16 samples), so it clocks itself from its own tile output clock. Its s10 / the relay
# m10 carry no data: s10 is tied idle (Vivado ties an unconnected AXIS slave's inputs to 0), m10 dangles.
connect_bd_net $DSP_CLK [get_bd_pins rf_data_converter/s0_axis_aclk] [get_bd_pins rf_data_converter/s2_axis_aclk] \
  [get_bd_pins rf_data_converter/m0_axis_aclk] [get_bd_pins rf_data_converter/m1_axis_aclk] \
  [get_bd_pins rf_data_converter/m2_axis_aclk]
connect_bd_net [get_bd_pins dsp_rst/peripheral_aresetn] \
  [get_bd_pins rf_data_converter/s0_axis_aresetn] [get_bd_pins rf_data_converter/s2_axis_aresetn] \
  [get_bd_pins rf_data_converter/m0_axis_aresetn] [get_bd_pins rf_data_converter/m1_axis_aresetn] \
  [get_bd_pins rf_data_converter/m2_axis_aresetn]
connect_bd_net [get_bd_pins rf_data_converter/clk_dac1] [get_bd_pins rf_data_converter/s1_axis_aclk]
set _one [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 relay_rstn]
connect_bd_net [get_bd_pins $_one/dout] [get_bd_pins rf_data_converter/s1_axis_aresetn]

# s_axi control port in the PS pl_clk0 domain; user SYSREF from the PL_SYSREF pin
connect_bd_net [get_bd_pins zynq_ps/pl_clk0]    [get_bd_pins rf_data_converter/s_axi_aclk]
connect_bd_net [get_bd_pins zynq_ps/pl_resetn0] [get_bd_pins rf_data_converter/s_axi_aresetn]
connect_bd_net $USER_SYSREF \
  [get_bd_pins rf_data_converter/user_sysref_adc] [get_bd_pins rf_data_converter/user_sysref_dac]
