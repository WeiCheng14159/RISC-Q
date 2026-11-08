"""RF clock plans: the PYNQ driver's table (riscq.board.pynq_driver.CLOCK_PLANS) and its Vivado twin
(vivado-scripts/riscvsoc-bd/inc/clock-plan.tcl) agree; the bundled SPEAR TICS files program every
LMK/LMX found; MTS uses the plan's reference tile and, with no measured targets, stops at the free sync.
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
    body = re.search(r"set CLOCK_PLANS \{(.*?)\n\}", TCL_PLAN.read_text(), re.S).group(1)
    plans = {}
    for name, fields in re.findall(r"(\w+)\s+\{([^}]*)\}", body):
        kv = fields.split()
        plans[name] = dict(zip(kv[::2], kv[1::2]))
    return plans


def test_python_and_tcl_plans_agree(drv):
    tcl = _tcl_plans()
    assert set(tcl) == {p["plan"] for p in drv.CLOCK_PLANS.values()}
    for freq, plan in drv.CLOCK_PLANS.items():
        t = tcl[plan["plan"]]
        assert float(t["dsp"]) == freq
        assert int(t["tile"]) == plan["ref_tile"]


def test_spear_plan_files_match_refclk(drv):
    """The LMX export behind the spear plan outputs the refclk the Tcl plan compiles into the RFDC."""
    plan = drv.clock_plan(300e6)
    refclk = float(_tcl_plans()["spear"]["refclk_mhz"])
    regs = {(v >> 16) & 0x7F: v & 0xFFFF for v in drv.read_tics(drv.CLOCK_DIR / plan["lmx_file"])}
    fin = 250.0                                         # LMK04828_10.0_250.0_* feeds the LMXs 250 MHz
    fpd = fin * (1 + ((regs[9] >> 12) & 1)) * ((regs[10] >> 7) & 0x1F) / (((regs[11] >> 4) & 0xFF) * (regs[12] & 0xFFF))
    n = ((regs[34] & 7) << 16) | regs[36]
    num, den = (regs[42] << 16) | regs[43], (regs[38] << 16) | regs[39]
    chdiv = [2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 72, 96, 128, 192, 256, 384, 512, 768][(regs[75] >> 6) & 0x1F]
    assert fpd * (n + num / den) / chdiv == pytest.approx(refclk)
    assert "_250.0_300.0_10.0" in plan["lmk_file"]       # 250 to the LMXs, PL_CLK 300 = dsp, SYSREF 10


def test_configs_have_plans(drv):
    for name in ("zcu216-14q.json", "zcu216-14q-hw23.json", "zcu216-14q-spear.json"):
        drv.clock_plan(float(json.loads((CONFIGS / name).read_text())["dsp_freq_hz"]))
    with pytest.raises(ValueError, match="no RF clock plan"):
        drv.clock_plan(250e6)


def test_program_clock_files_writes_every_device(drv):
    plan = drv.clock_plan(300e6)
    obj = drv.PynqDriver.__new__(drv.PynqDriver)
    obj.program_clock_files(plan["lmk_file"], plan["lmx_file"])
    lmk = drv.read_tics(drv.CLOCK_DIR / plan["lmk_file"])
    lmx = drv.read_tics(drv.CLOCK_DIR / plan["lmx_file"])
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
    obj = _mts_driver(drv, drv.clock_plan(300e6), [100] * 4, [60] * 4)
    assert obj.mts() == 0
    assert obj.rfdc.mts_dac_config.RefTile == 1 == obj.rfdc.mts_adc_config.RefTile
    assert obj.rfdc.calls == [("dac", -1), ("adc", -1)]                  # one free pass, nothing pinned
    assert obj.mts_latencies == {"dac": [100] * 4, "adc": [60] * 4}
    assert _mts_driver(drv, drv.clock_plan(300e6), [100, 100, 101, 100], [60] * 4).mts() == 1


def test_mts_pins_plan_targets(drv):
    obj = _mts_driver(drv, drv.clock_plan(500e6), [224] * 4, [64] * 4)
    obj.mts()
    assert obj.rfdc.mts_dac_config.RefTile == 2
    assert obj.rfdc.calls == [("dac", -1), ("adc", -1), ("dac", 240), ("adc", 72)]
