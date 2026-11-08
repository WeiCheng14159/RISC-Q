# ZCU216 physical + timing constraints for the PulseTableSoc block-design wrapper.
# Pin assignments are taken from the RISC-Q reference (utils/constraints-zcu216.xdc), trimmed to the
# interfaces this SoC actually exposes (no GTY / LED / clk125). The external converter clocks
# (dac_clk / adc_clk / sysref) come from the board clock chip. The three async clock groups keep the
# dsp domain, the 100 MHz host domain, and the PS clock independent for timing.

# dsp clock: CLK104 PL_CLK (LVDS), 500 or 300 MHz by RF clock plan
set_property -dict {PACKAGE_PIN E10 IOSTANDARD LVDS_25} [get_ports {dspClk_clk_p}]
set_property -dict {PACKAGE_PIN E9  IOSTANDARD LVDS_25} [get_ports {dspClk_clk_n}]

# user sysref (LVDS)
set_property -dict {PACKAGE_PIN E11 IOSTANDARD LVDS_25} [get_ports {user_sysref_clk_p}]
set_property -dict {PACKAGE_PIN D11 IOSTANDARD LVDS_25} [get_ports {user_sysref_clk_n}]

# 100 MHz host clock (LVDS)
set_property -dict {PACKAGE_PIN G12 IOSTANDARD LVDS_25} [get_ports {hostClk_clk_p}]
set_property -dict {PACKAGE_PIN G11 IOSTANDARD LVDS_25} [get_ports {hostClk_clk_n}]

# RF Data Converter sample clocks (from the board clock chip)

# Clocks (dspClk / dac_clk / adc_clk periods follow the RF clock plan) are written per build by
# inc/bd-finalize.tcl into <build>/clocks-zcu216.xdc.
