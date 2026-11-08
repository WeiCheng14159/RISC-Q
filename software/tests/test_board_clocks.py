"""RF clock plans: the PYNQ driver's ZCU216 table (riscq.board.pynq_driver.CLOCK_PLANS) and its Vivado
twin (vivado-scripts/riscvsoc-bd/inc/clock-plan.tcl) agree; each board's bundled opentics files decode to
RISC-Q's rates (PL_CLK = dspClk, a refclk the tile PLL multiplies to 16x / 4x dspClk, a SYSREF that divides
every clock) and program every LMK/LMX found; MTS uses the plan's reference tile and tile mask and, with
no measured targets, stops at the free sync.
pynq / xrfclk / xrfdc are board-only, so they are faked in sys.modules for the import."""

from __future__ import annotations

import importlib
import json
import re
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONFIGS = REPO / "software" / "configs"
TCL_PLAN = REPO / "vivado-scripts" / "riscvsoc-bd" / "inc" / "clock-plan.tcl"
TCL_RFDC_4X2 = REPO / "vivado-scripts" / "riscvsoc-bd" / "inc" / "rfdc-rfsoc4x2.tcl"


@pytest.fixture
def drv(monkeypatch):
    writes = []
    xc = types.SimpleNamespace(lmk_devices=[], lmx_devices=[])

    def find():
        xc.lmk_devices.append({"spi_device": "lmk0"})
        xc.lmx_devices.extend([{"spi_device": "lmx0"}, {"spi_device": "lmx1"}])

    xc._find_devices = find
    xc._write_LMK_regs = lambda regs, dev: writes.append((dev["spi_device"], list(regs)))
    xc._write_LMX_regs = lambda regs, dev: writes.append((dev["spi_device"], list(regs)))
    xrfclk = types.ModuleType("xrfclk")
    xrfclk.xrfclk = xc
    for name, mod in {"pynq": types.ModuleType("pynq"), "xrfclk": xrfclk,
                      "xrfdc": types.ModuleType("xrfdc")}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "riscq.board.pynq_driver", raising=False)
    mod = importlib.import_module("riscq.board.pynq_driver")
    mod._test_writes = writes
    return mod


def _tcl_plans() -> dict:
    """{board: {plan: {key: value}}} from clock-plan.tcl's nested CLOCK_PLANS dict."""
    body = re.search(r"set CLOCK_PLANS \{(.*?)\n\}", TCL_PLAN.read_text(), re.S).group(1)
    plans = {}
    for board, inner in re.findall(r"(\w+)\s+\{((?:\s*\w+\s+\{[^}]*\})+)\s*\}", body):
        plans[board] = {name: dict(zip(f.split()[::2], f.split()[1::2]))
                        for name, f in re.findall(r"(\w+)\s+\{([^}]*)\}", inner)}
    return plans


def test_python_and_tcl_plans_agree(drv):
    tcl = _tcl_plans()
    assert set(tcl) == set(drv.CLOCK_PLANS)
    for board, plans in drv.CLOCK_PLANS.items():
        assert set(tcl[board]) == {p["plan"] for p in plans.values()}
        for freq, plan in plans.items():
            t = tcl[board][plan["plan"]]
            assert float(t["dsp"]) == freq
            assert int(t["tile"]) == plan["ref_tile"]
            assert plan["mts_tiles"] >> plan["ref_tile"] & 1             # the ref tile is in the sync


def test_rfsoc4x2_blocks_match_rfdc_wiring(drv):
    """The driver's (tile, block) lists are the converters rfdc-rfsoc4x2.tcl wires, in channel order:
    DAC s{t}0 -> (t, 0); dual-tile ADC m{t}0 / m{t}2 -> blocks 0 / 1."""
    text = TCL_RFDC_4X2.read_text()
    dacs = re.search(r"set _dac_map \{(.*?)\}\n", text).group(1)
    adcs = re.search(r"set _adc_map \{(.*?)\}\n", text).group(1)
    dac = [(int(a[1]), int(a[2]) // 2) for a in re.findall(r"\{(s\d\d) ", dacs)]
    adc = [(int(a[1]), int(a[2]) // 2) for a in re.findall(r"\{(m\d\d) ", adcs)]
    plan = drv.clock_plan(500e6, "RFSoC4x2")
    assert dac == list(plan["dac_blocks"]) and adc == list(plan["adc_blocks"])
    cfg = json.loads((CONFIGS / "rfsoc4x2-4q.json").read_text())
    assert (cfg["dac_num"], cfg["adc_num"]) == (len(dac), len(adc))


def test_board_from_pynq_env(drv, monkeypatch):
    monkeypatch.setenv("BOARD", "RFSoC4x2")
    assert drv._env_board() == "RFSoC4x2"
    monkeypatch.setenv("BOARD", "ZCU111")                               # no plans: fall back
    assert drv._env_board() == "ZCU216"


_CHDIV = [2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 72, 96, 128, 192, 256, 384, 512, 768]


def _lmx_out_mhz(regs: dict, fin: float) -> dict:
    """RFoutA/B of an LMX2594 register image (SNAS696 register map), None where powered down."""
    fpd = fin * (1 + ((regs[9] >> 12) & 1)) * ((regs[10] >> 7) & 0x1F) / (((regs[11] >> 4) & 0xFF) * (regs[12] & 0xFFF))
    n = ((regs[34] & 7) << 16) | regs[36]
    num, den = (regs[42] << 16) | regs[43], (regs[38] << 16) | regs[39]
    fvco = fpd * (n + num / den)
    assert 7500 <= fvco <= 15000
    chdiv = fvco / _CHDIV[(regs[75] >> 6) & 0x1F]
    out_a = None if (regs[44] >> 6) & 1 else (chdiv if ((regs[45] >> 11) & 3) == 0 else fvco)
    out_b = None if regs[44] >> 7 & 1 else (chdiv if (regs[46] & 3) == 0 else fvco)
    return {"vco": fvco, "a": out_a, "b": out_b}


def _lmk_mhz(regs: dict) -> dict:
    """VCO, CLKout0..12 and SYSREF of an LMK04828 register image (SNAS605): 160 MHz VCXO into PLL2."""
    r = lambda a: regs[a] & 0xFF
    pll2_r = ((r(0x160) & 0xF) << 8) | r(0x161)
    pll2_p = (r(0x162) >> 5) & 7 or 8
    pll2_n = ((r(0x166) & 3) << 16) | (r(0x167) << 8) | r(0x168)
    vco = 160.0 / pll2_r * pll2_p * pll2_n
    div = lambda o: (r(0x100 + 8 * (o // 2)) & 0x1F) or 32
    sysref_div = ((r(0x13A) & 0x1F) << 8) | r(0x13B)
    return {"vco": vco, "sysref": vco / sysref_div, **{o: vco / div(o) for o in range(0, 14, 2)}}


@pytest.mark.parametrize("board", ["ZCU216", "RFSoC4x2"])
def test_native_plan_files_meet_riscq_rates(drv, board):
    plan = drv.clock_plan(500e6, board)
    d = Path(plan["clock_dir"])
    lmk = {v >> 8: v & 0xFF for v in drv.read_tics(d / plan["lmk_file"])}
    lmx = {(v >> 16) & 0x7F: v & 0xFFFF for v in drv.read_tics(d / plan["lmx_file"])}
    k = _lmk_mhz(lmk)
    assert k["vco"] == pytest.approx(3000.0)
    assert k[8] == pytest.approx(500.0)                  # PL_CLK = dspClk
    assert k[0] == pytest.approx(250.0)                  # the LMX2594s' reference
    for f in (500.0, 8000.0, 2000.0):                    # SYSREF divides dspClk and both sample clocks
        assert (f / k["sysref"]) == pytest.approx(round(f / k["sysref"]))
    x = _lmx_out_mhz(lmx, k[0])
    refclk = float(_tcl_plans()[board]["native"]["refclk_mhz"])
    assert x["b"] == pytest.approx(refclk)               # RFoutB: the RFDC reference on both boards
    assert 16 * 500.0 / refclk == 16 and 4 * 500.0 / refclk == 4   # integer tile-PLL multipliers
    assert x["a"] is None if board == "ZCU216" else x["a"] == pytest.approx(refclk)


def test_configs_have_plans(drv):
    for name, board in (("zcu216-14q.json", "ZCU216"), ("zcu216-14q-hw23.json", "ZCU216"),
                        ("zcu216-2q.json", "ZCU216"), ("rfsoc4x2-4q.json", "RFSoC4x2")):
        drv.clock_plan(float(json.loads((CONFIGS / name).read_text())["dsp_freq_hz"]), board)
    with pytest.raises(ValueError, match="no RF clock plan"):
        drv.clock_plan(300e6)
    with pytest.raises(ValueError, match="no RF clock plans for board"):
        drv.clock_plan(500e6, "ZCU111")


@pytest.mark.parametrize("board", ["ZCU216", "RFSoC4x2"])
def test_program_clock_files_writes_every_device(drv, board):
    plan = drv.clock_plan(500e6, board)
    obj = drv.PynqDriver.__new__(drv.PynqDriver)
    obj.cfg = plan
    obj.program_clock_files(plan["lmk_file"], plan["lmx_file"])
    lmk = drv.read_tics(Path(plan["clock_dir"]) / plan["lmk_file"])
    lmx = drv.read_tics(Path(plan["clock_dir"]) / plan["lmx_file"])
    assert drv._test_writes == [("lmk0", lmk), ("lmx0", lmx), ("lmx1", lmx)]   # LMK before the LMXs
    assert len(lmx) == 113 and (lmx[-1] >> 16) == 0                              # R112 .. R0


class _MtsCfg:
    def __init__(self, lat):
        self.Latency = lat
        self.RefTile = self.Tiles = self.SysRef_Enable = self.Target_Latency = None


def _mts_driver(drv, plan, dac_lat, adc_lat):
    obj = drv.PynqDriver.__new__(drv.PynqDriver)
    obj.cfg, obj.ref_tile = plan, plan["ref_tile"]
    obj.rfdc = types.SimpleNamespace(mts_dac_config=_MtsCfg(dac_lat), mts_adc_config=_MtsCfg(adc_lat),
                                     calls=[])
    obj.rfdc.mts_dac = lambda: obj.rfdc.calls.append(("dac", obj.rfdc.mts_dac_config.Target_Latency))
    obj.rfdc.mts_adc = lambda: obj.rfdc.calls.append(("adc", obj.rfdc.mts_adc_config.Target_Latency))
    return obj


def test_mts_free_sync_only_without_targets(drv):
    obj = _mts_driver(drv, drv.clock_plan(500e6), [224] * 4, [64] * 4)
    assert obj.mts() == 0
    assert obj.rfdc.mts_dac_config.RefTile == 1 == obj.rfdc.mts_adc_config.RefTile
    assert obj.rfdc.mts_dac_config.Tiles == 0b1111
    assert obj.rfdc.calls == [("dac", -1), ("adc", -1)]                  # one free pass, nothing pinned
    assert obj.mts_latencies == {"dac": [224] * 4, "adc": [64] * 4}
    assert _mts_driver(drv, drv.clock_plan(500e6), [224, 224, 225, 224], [64] * 4).mts() == 1


def test_mts_rfsoc4x2_tiles(drv):
    obj = _mts_driver(drv, drv.clock_plan(500e6, "RFSoC4x2"), [224] * 4, [64] * 4)
    obj.mts()
    assert obj.rfdc.mts_dac_config.RefTile == 2 == obj.rfdc.mts_adc_config.RefTile
    assert obj.rfdc.mts_dac_config.Tiles == 0b0101 == obj.rfdc.mts_adc_config.Tiles


def test_mts_pins_board_json_targets(drv):
    plan = {**drv.clock_plan(500e6), "mts": {"daclatency": 240, "adclatency": 72}}   # board.json "mts"
    obj = _mts_driver(drv, plan, [224] * 4, [64] * 4)
    obj.mts()
    assert obj.rfdc.calls == [("dac", -1), ("adc", -1), ("dac", 240), ("adc", 72)]
