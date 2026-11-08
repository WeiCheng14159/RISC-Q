# ===========================================================================================
# Assemble the block design: board clock inputs + Zynq PS + user top IP + proc_sys_resets + RF Data
# Converter + AXI SmartConnect. Cell handles ($ZYNQ_PS / $TOP / $RFDC) and clock pins ($DSP_CLK / $HOST_CLK / $USER_SYSREF) are published at
# script scope for bd-finalize.tcl. Mirrors the RISC-Q utils/*.tcl, merged and proc-free.
# ===========================================================================================

create_bd_design -dir $BUILD_DIR/bd $BD_NAME

# ---- Zynq UltraScale+ PS: supplies pl_clk0 / pl_resetn0 / M_AXI_HPM0_LPD ----
set ZYNQ_PS [create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e:3.5 zynq_ps]
apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e -config {apply_board_preset "1"} \
  [get_bd_cells zynq_ps]
connect_bd_net [get_bd_pins zynq_ps/pl_clk0] [get_bd_pins zynq_ps/maxihpm0_lpd_aclk]

# ---- Board clock inputs -> $DSP_CLK / $HOST_CLK / $USER_SYSREF (BD pins the rest of the design uses) ----
if {$BOARD eq "ZCU216"} {
  # CLK104 PL_CLK + PL SYSREF and the 100 MHz board oscillator, three LVDS pairs into ClockInterface
  # (IBUFGDS + BUFG for dspClk).
  set CLKIFC [create_bd_cell -type module -reference ClockInterface clkifc]
  foreach c {dspClk hostClk user_sysref} {
    create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:diff_clock_rtl:1.0 $c
    connect_bd_intf_net [get_bd_intf_ports $c] [get_bd_intf_pins $CLKIFC/${c}_diff]
  }
  set_property -dict [list CONFIG.FREQ_HZ $DSP_FREQ]  [get_bd_intf_ports dspClk]
  set_property -dict [list CONFIG.FREQ_HZ $HOST_FREQ] [get_bd_intf_ports hostClk]
  set DSP_CLK     [get_bd_pins $CLKIFC/dspClk]
  set HOST_CLK    [get_bd_pins $CLKIFC/hostClk]
  set_property -dict [list CONFIG.FREQ_HZ $DSP_FREQ]  $DSP_CLK
  set_property -dict [list CONFIG.FREQ_HZ $HOST_FREQ] $HOST_CLK
  set USER_SYSREF [get_bd_pins $CLKIFC/user_sysref]
} elseif {$BOARD eq "RFSoC4x2"} {
  # The 4x2 brings LMK04828 PL_CLK (CLKout8) and PL_SYSREF (SDCLKout9) in single-ended (LVCMOS18,
  # constraints-rfsoc4x2.xdc) and has no free-running PL oscillator: dspClk = PL_CLK through a BUFG,
  # hostClk = the PS pl_clk0 (100 MHz), which also clocks the AXI control path.
  set_property -dict {CONFIG.PSU__CRL_APB__PL0_REF_CTRL__SRCSEL IOPLL CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ 100} $ZYNQ_PS
  set _pl0 [get_property CONFIG.FREQ_HZ [get_bd_pins zynq_ps/pl_clk0]]
  if {$_pl0 != $HOST_FREQ} { error "RFSoC4x2: PS pl_clk0 is $_pl0 Hz but the IP was packaged for hostClk $HOST_FREQ Hz (set RISCQ_HOST_FREQ=$_pl0)" }
  create_bd_port -dir I -type clk -freq_hz $DSP_FREQ dspClk
  set _bufg [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf:2.2 dsp_bufg]
  set_property CONFIG.C_BUF_TYPE BUFG $_bufg
  connect_bd_net [get_bd_ports dspClk] [get_bd_pins $_bufg/BUFG_I]
  create_bd_port -dir I user_sysref
  set DSP_CLK     [get_bd_pins $_bufg/BUFG_O]
  set HOST_CLK    [get_bd_pins zynq_ps/pl_clk0]
  set USER_SYSREF [get_bd_ports user_sysref]
}

# ---- User top IP ----
set TOP [create_bd_cell -type ip -vlnv user.org:user:${TOP_MODULE}:1.0 top]
connect_bd_net $HOST_CLK [get_bd_pins $TOP/hostClk]
connect_bd_net $DSP_CLK  [get_bd_pins $TOP/dspClk]

# ---- proc_sys_reset for each clock domain ----
set PS_RST  [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 ps_rst]
set DSP_RST [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 dsp_rst]
connect_bd_net [get_bd_pins zynq_ps/pl_resetn0] [get_bd_pins ps_rst/ext_reset_in] [get_bd_pins dsp_rst/ext_reset_in]
connect_bd_net [get_bd_pins dsp_rst/peripheral_reset] [get_bd_pins $TOP/dspRst]
connect_bd_net [get_bd_pins ps_rst/peripheral_reset]  [get_bd_pins $TOP/hostRst]
connect_bd_net $HOST_CLK [get_bd_pins ps_rst/slowest_sync_clk]
connect_bd_net $DSP_CLK  [get_bd_pins dsp_rst/slowest_sync_clk]

# ---- RF Data Converter: ZCU216 16 DAC + 16 ADC (config verbatim from the RISC-Q reference); RFSoC4x2 the
# 2 DAC + 4 ADC the board bonds out ----
set RFDC [create_bd_cell -type ip -vlnv xilinx.com:ip:usp_rf_data_converter:2.6 rf_data_converter]
set RFDC_TARGET [get_bd_cells rf_data_converter]
if {$BOARD eq "ZCU216"} {
  source $INC/rfdc-config.tcl
  source $INC/rfdc-connect.tcl
} else {
  source $INC/rfdc-rfsoc4x2.tcl
}

# ---- AXI SmartConnect: PS HPM0_LPD -> { top S_AXIS, rfdc s_axi } ----
set AXI_CONNECT [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 smartconnect]
set_property CONFIG.NUM_SI 1 $AXI_CONNECT
set_property CONFIG.NUM_MI 2 $AXI_CONNECT
set_property CONFIG.NUM_CLKS {2} $AXI_CONNECT
set_property CONFIG.HAS_ARESETN {0} $AXI_CONNECT

connect_bd_intf_net [get_bd_intf_pins $AXI_CONNECT/M00_AXI]      [get_bd_intf_pins $TOP/S_AXIS]
connect_bd_intf_net [get_bd_intf_pins $AXI_CONNECT/M01_AXI]      [get_bd_intf_pins rf_data_converter/s_axi]
connect_bd_intf_net [get_bd_intf_pins zynq_ps/M_AXI_HPM0_LPD]    [get_bd_intf_pins $AXI_CONNECT/S00_AXI]
connect_bd_net      [get_bd_pins zynq_ps/pl_clk0]                [get_bd_pins $AXI_CONNECT/aclk]
connect_bd_net      $HOST_CLK                [get_bd_pins $AXI_CONNECT/aclk1]

assign_bd_address -offset 0x80000000 -range 0x10000000 \
  -target_address_space [get_bd_addr_spaces zynq_ps/Data] [get_bd_addr_segs $TOP/S_AXIS/reg0] -force
assign_bd_address

# ---- Host window: per-core results -> PS DDR4 over S_AXI_HP0_FPD (docs/soc/HostWindow.md) ----
# HP0 owns a DDRC port of its own (HP1/HP2 share one); the width is pinned to 32 so IP Integrator
# inserts no width converter, and the port is a DIRECT connection, not through the SmartConnect.
set_property -dict [list CONFIG.PSU__USE__S_AXI_GP2 {1} CONFIG.PSU__SAXIGP2__DATA_WIDTH {32}] $ZYNQ_PS
connect_bd_intf_net [get_bd_intf_pins $TOP/M_AXI_HOST] [get_bd_intf_pins zynq_ps/S_AXI_HP0_FPD]
connect_bd_net      $HOST_CLK      [get_bd_pins zynq_ps/saxihp0_fpd_aclk]
assign_bd_address -target_address_space [get_bd_addr_spaces $TOP/M_AXI_HOST] \
  [get_bd_addr_segs zynq_ps/SAXIGP2/HP0_DDR_LOW] -force
assign_bd_address -target_address_space [get_bd_addr_spaces $TOP/M_AXI_HOST] \
  [get_bd_addr_segs zynq_ps/SAXIGP2/HP0_DDR_HIGH] -force

# ---- White Rabbit GTY pins (with_white_rabbit builds only — the IP then has wr* pins): the GT
# refclk + serial lanes go straight out as BD ports; constraints-wr.xdc places them (SFP0/X0Y4).
# wrMarker (the digital marker pin) stays unconnected — the marker rides a spare DAC instead. ----
if {[llength [get_bd_pins -quiet $TOP/wrRefClkP]]} {
  foreach p {wrRefClkP wrRefClkN wrRxP wrRxN} {
    create_bd_port -dir I $p
    connect_bd_net [get_bd_ports $p] [get_bd_pins $TOP/$p]
  }
  foreach p {wrTxP wrTxN} {
    create_bd_port -dir O $p
    connect_bd_net [get_bd_ports $p] [get_bd_pins $TOP/$p]
  }
}
