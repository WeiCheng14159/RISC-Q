# Building and deploying with Vivado 2023.1

The maintained flow targets **Vivado 2026.1** (see the top-level [README](../README.md)). This page
records how to set up a fresh Linux machine and build the ZCU216 image with **Vivado 2023.1**
instead, and what had to change for it. Nothing here needs root.

Status: the full flow (RTL → block design → synth → impl → bitstream → XSA) runs on 2023.1, but
the 14-qubit build **does not close timing** on `dspClk` — see [Results](#results-on-20231).

## 1. Host toolchain (user space)

```bash
# submodules (ext/SpinalHDL, ext/rvls, ext/NaxSoftware)
git submodule update --init

# mill 1.1.0 launcher (the version is pinned in .mill-version; the launcher fetches it + a JDK)
mkdir -p ~/.local/bin
curl -fsSL -o ~/.local/bin/mill \
  https://repo1.maven.org/maven2/com/lihaoyi/mill-dist/1.1.0/mill-dist-1.1.0-mill.sh
chmod +x ~/.local/bin/mill

# riscv LLVM toolchain: a stock prebuilt LLVM + three wrappers under the names riscq.build expects
mkdir -p ~/.local/opt && cd ~/.local/opt
curl -fsSL -o llvm.tar.xz \
  https://github.com/llvm/llvm-project/releases/download/llvmorg-19.1.7/LLVM-19.1.7-Linux-X64.tar.xz
tar xf llvm.tar.xz && rm llvm.tar.xz
L=~/.local/opt/LLVM-19.1.7-Linux-X64/bin
printf '#!/bin/sh\nexec %s/clang --target=riscv64-unknown-elf -fuse-ld=lld "$@"\n' "$L" > ~/.local/bin/riscv64-unknown-elf-clang
printf '#!/bin/sh\nexec %s/llvm-objcopy "$@"\n' "$L" > ~/.local/bin/riscv64-unknown-elf-objcopy
printf '#!/bin/sh\nexec %s/llvm-nm "$@"\n'      "$L" > ~/.local/bin/riscv64-unknown-elf-nm
chmod +x ~/.local/bin/riscv64-unknown-elf-{clang,objcopy,nm}
```

`-march=rv32i[_zmmul] -mabi=ilp32` from `riscq.build` switches the riscv64 target to riscv32, and
`-fuse-ld=lld` links without a GNU riscv binutils install.

## 2. Python environment

Ubuntu 24.04's system Python is externally managed (PEP 668), so use a venv:

```bash
python3 -m venv ~/.venvs/riscq
~/.venvs/riscq/bin/pip install -e 'software[cal]'
# host-side extras the tests / co-sim / notebooks import but pyproject.toml does not list
~/.venvs/riscq/bin/pip install pyyaml matplotlib 'cocotb>=1.8,<2' qutip jupyter ipykernel pytest
~/.venvs/riscq/bin/python -m ipykernel install --user --name riscq --display-name "Python (riscq)"
```

**cocotb must be 1.x**: `riscq.sim.server` imports `cocotb.runner`, which cocotb 2.0 moved out to
`cocotb_tools.runner`. With cocotb 2.x the co-sim dies at start-up with
`ModuleNotFoundError: No module named 'cocotb.runner'`.

Check: `cd software && PYTHONPATH=. ~/.venvs/riscq/bin/python -m pytest tests/ -q` →
378 passed, 106 skipped.

**Running the co-sim notebooks** needs two things beyond `PATH`:

```bash
. ~/.venvs/riscq/bin/activate                  # cocotb picks the simulator's embedded Python from $VIRTUAL_ENV
export PYTHONPATH=$PWD/software                # the embedded interpreter does not see the editable install
jupyter nbconvert --to notebook --execute --ExecutePreprocessor.kernel_name=riscq \
  --output-dir build/examples examples/amplitude_sweep.ipynb
```

Without `VIRTUAL_ENV` the Verilated simulator falls back to the system Python; without
`PYTHONPATH` it fails with `Failed to import module riscq.sim.bench: No module named 'riscq'`.
Both show up only in the co-sim log tail, as `cosim process died`. The first run Verilates the
SoC (a couple of minutes), cached under `software/build/sim-2q/`.

## 3. Build the XSA with Vivado 2023.1

```bash
RISCQ_VIVADO_BIN=/opt/Xilinx/Vivado/2023.1/bin ./vivado-scripts/riscvsoc-bd/build-riscvsoc-bd.sh
```

All the IP the block design instantiates exists at the pinned versions in 2023.1:
`zynq_ultra_ps_e:3.5`, `usp_rf_data_converter:2.6`, `smartconnect:1.0`, `proc_sys_reset:5.0`.
Every run property, strategy and place/route directive the flow sets is also present
(`Flow_PerfOptimized_high`, `Performance_NetDelay_high`, `ExtraNetDelay_high`, `AggressiveExplore`,
the `STEPS.*` directive / `TCL.PRE` / `POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED` properties).

### What changed for 2023.1

| Failure on 2023.1 | Fix |
|---|---|
| `ERROR: [Common 17-54] The object 'run' does not have a property 'STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING'` | 2023.1 only has the older boolean `STEPS.SYNTH_DESIGN.ARGS.RETIMING`. `inc/run.tcl` now sets retiming through `riscq_retiming_on`, which uses `GLOBAL_RETIMING on` when the run has it and `RETIMING true` otherwise, so the same script still runs on 2026.1. |

### Results on 2023.1

`zcu216-14q-hw23.json`, default knobs (`ExtraNetDelay_high` place, `AggressiveExplore` route, IP
retiming), 2026-10-24, about 4 h on a 16-core / 30 GB machine (peak 19 GB):

| Clock | WNS | TNS | Failing endpoints |
|---|---|---|---|
| `dspClk` (500 MHz) | **−0.782 ns** | −5004 ns | 35,075 / 1,305,932 |
| `hostClk` (100 MHz) | +1.757 ns | 0 | 0 |
| `clk_pl_0` | +1.601 ns | 0 | 0 |

Hold is met on every clock (WHS +0.009 ns). The worst paths are in **every core's multiplier**
(`MulPlugin`: `Execute_RS1/RS2_DATA` → `mul_ppHH`, 8–9 levels of CARRY8/LUT, ~60–68 % routing),
the path [RISCV.md](riscv/RISCV.md) already names as the core's fmax binder (~508 MHz OOC on
2026.1). At −0.782 ns the image is good for roughly 360 MHz — **do not run it at 500 MHz**.

With the multiplier pinned to DSPs (`MulPlugin`, `use_dsp` on the four partial products) the next
500 MHz build reached about −0.32 ns after routing, now limited by the demod carrier's fabric-built
`freqReg × batchTime` multiply. That one is pinned too (`CarrierBatchGenerator`), but its 500 MHz
build was stopped while the 300 MHz SPEAR clock plan was brought up (results below); the native
500 MHz plan now replaces that plan, so closing 500 MHz is open work again.

### RF clock plan: RISC-Q's native rates, clock files from opentics

The converters take a fixed 16 DAC / 4 ADC samples per `dspClk` cycle, so `dsp_freq_hz` in the
config picks the sample rates. RISC-Q's rate is 500 MHz: DAC 8 GS/s, ADC 2 GS/s. The plan exists
twice and the copies must agree:
[`inc/clock-plan.tcl`](../vivado-scripts/riscvsoc-bd/inc/clock-plan.tcl) for the build and
`riscq.board.pynq_driver.CLOCK_PLANS` for the board; `tests/test_board_clocks.py` checks that they
do, and that each board's clock files decode to these rates.

| Board | Plan | Reference into | MTS tiles | Clock files (`software/riscq/board/clocks/<board>/`) |
|---|---|---|---|---|
| ZCU216 (CLK104) | `native`, dsp 500 MHz | tile 1 (DAC 229 / ADC 225), 500 MHz, LMX2594 RFoutB | all 4 | `LMK04828_10.0_250.0_500.0_10.0` + `LMX2594_250.0_500.0` |
| RFSoC4x2 | `native`, dsp 500 MHz | tile 2 (DAC 230 / ADC 226), 500 MHz, LMX2594 RFoutA+B | 0 and 2 (the bonded-out tiles) | same names, the 4x2's own register images |

The clock chain, identical in frequencies on both boards:

| Device | Setting |
|---|---|
| LMK04828 | 10 MHz CLKin, 160 MHz VCXO, PLL2 R 4 / P 3 / N 25, VCO 3000 MHz. CLKout8 = **500 MHz PL_CLK** (÷6; E10/E9 on the ZCU216 = `dspClk`). CLKout0/2/4/6/10/12 = 250 MHz (÷12) to the LMX2594s. SYSREF **10 MHz** (÷300) to the PL and the RFDC. |
| LMX2594 (both) | 250 MHz in, f_PD 25 MHz (R 10), integer N 320, VCO 8000 MHz (VCO1), CHDIV 16 → **500 MHz**. |
| RFDC ref tile | PLL on, 500 MHz → 8 GS/s DAC (×16) and 2 GS/s ADC (×4), distributed (`Clock_Dist` 2) to the other tiles. |
| hostClk | the board's fixed 100 MHz oscillator, independent of the RF clocks. |

10 MHz SYSREF divides 500 MHz, 8 GHz and 2 GHz, so it is a valid MTS SYSREF for every clock above.

**How the files were made.** They are generated with
opentics (`ticspro-container/opentics`, the open-source TICS Pro-compatible generator in the
ticspro-container repo, `recommended` policy) from each board's own TICS Pro projects. The
board-specific settings come from those projects: which outputs are powered, their formats, and
the SYSREF routing. Regenerate in place, with ticspro-container cloned next to RISC-Q:

```bash
opentics batch software/riscq/board/clocks/ZCU216/opentics.toml --force
opentics batch software/riscq/board/clocks/RFSoC4x2/opentics.toml --force
```

- The LMK04828 files are byte-identical to the 500 MHz TICS Pro exports in the boards'
  `my_config/` folders.
- The LMX2594 files differ from those exports in three registers: R17, R20 and R78 (the VCO
  calibration start: VCO1, CAPCTRL 95, DACISET 272). opentics re-estimates those for the 8 GHz VCO.
  The TICS Pro exports still carried the 9.6 GHz values from the project they were derived from.
- The RFSoC4x2 LMK04828 file starts from the 4x2's 125 MHz project, not the 250 MHz one: its PL_CLK
  (CLKout8) and PL_SYSREF (SDCLKout9) are HSDS8. The 4x2 receives both single-ended (LVCMOS18), and
  Xilinx's RFSoC-MTS RFSoC4x2 design, the only one here that uses those pins, drives them HSDS8.
- opentics warns that `OSCin_FREQ` is 1 while the 160 MHz VCXO is in range code 2. It is 1 in
  every Xilinx/TI CLK104 and RFSoC4x2 project, which lock on hardware, so the files keep it.

`PynqDriver.program_clock_files` writes the files with raw TICS writes through `xrfclk`'s SPI
helpers, LMK first. Nothing has to be installed into the board's `xrfclk` package. `board.json`
`"board": "RFSoC4x2"` selects the 4x2 plan; the default is `ZCU216`. The Vivado flow here builds
both boards: `RISCQ_BOARD=RFSoC4x2` selects the xczu48dr port (RFSoC4x2 section below).

MTS targets are unpinned (free sync, latencies reported in `info()`). Pin them in `board.json`
`"mts"` once the 500 MHz build has been measured.

```bash
RISCQ_CONFIG=software/configs/zcu216-14q-hw23.json RISCQ_PROJ_NAME=zcu216-14q-hw23 \
  RISCQ_VIVADO_BIN=/opt/Xilinx/Vivado/2023.1/bin ./vivado-scripts/riscvsoc-bd/build-riscvsoc-bd.sh
```

### RFSoC4x2 port (`RISCQ_BOARD=RFSoC4x2`)

The same SoC, block-design flow and clock plan, on the RFSoC4x2's xczu48dr. Differences from the ZCU216,
all selected by `RISCQ_BOARD` (details in [vivado-scripts/riscvsoc-bd/README.md](../vivado-scripts/riscvsoc-bd/README.md)):

- **Converters.** The board bonds out DAC 228 / 230 and both channels of the dual-converter ADC tiles
  224 / 226, so the config has `dac_num 2`, `adc_num 4`. `software/configs/rfsoc4x2-4q.json`: 4 qubits,
  gates summed on DAC0 (228), readouts on DAC1 (230), demod on ADC0 (224 ch0).
- **RF clocking.** Only tile 2 has a reference input. DAC229 tops out at 7 GS/s, so the ZCU216 scheme
  (distribute the ref tile's PLL output) is impossible at 8 GS/s. Tile 2 distributes the 500 MHz
  reference instead, through relay tile 1, and tiles 0 and 2 each run their own PLL; MTS syncs tiles 0
  and 2 (`inc/rfdc-rfsoc4x2.tcl`, validated against the IP).
- **PL clocks.** PL_CLK (AN11) and PL_SYSREF (AP18) are single-ended LVCMOS18, as in Xilinx's
  RFSoC-MTS 4x2 design. There is no PL oscillator, so `hostClk` is the PS `pl_clk0`, 99.999 MHz from
  IOPLL (the IP is packaged at that exact rate).
- **Driver.** `board.json` `"board": "RFSoC4x2"`, or PYNQ's `$BOARD`, selects the 4x2 clock plan:
  clock files `clocks/RFSoC4x2`, ref tile 2, MTS tiles 0 and 2, Nyquist zones only on the bonded-out
  converters.

```bash
RISCQ_BOARD=RFSoC4x2 RISCQ_CONFIG=software/configs/rfsoc4x2-4q.json RISCQ_PROJ_NAME=rfsoc4x2-4q-native \
  RISCQ_VIVADO_BIN=/opt/Xilinx/Vivado/2023.1/bin ./vivado-scripts/riscvsoc-bd/build-riscvsoc-bd.sh
```

First build (2026-11-08, Vivado 2023.1, no floorplan): **all timing constraints met**. `dspClk` 500 MHz
WNS +0.012 ns, WHS +0.008 ns; `clk_pl_0` +2.392 ns; relay `RFDAC1_CLK` +2.812 ns. Utilization: 15.7 %
LUT, 14.6 % FF, 28 BRAM, 4 URAM. **Not yet run on hardware:** rfsoc4x2-0 and rfsoc4x2-1 were unreachable
over ssh. The bundle (`build/deploy/riscq-bits/rfsoc4x2-4q`, with `board.json`) is staged.

### Expected differences from the results of record

The timing numbers in [vivado-scripts/riscvsoc-bd/README.md](../vivado-scripts/riscvsoc-bd/README.md)
were measured on 2026.1. Older placer/router releases give different results, so the 14-qubit
build may not close at 0.000 ns WNS; `close-incremental.sh` is the first lever if it misses by a
small margin.

## 4. Deploy to a ZCU216

### PYNQ 2.7 boards (e.g. zcu216-0)

zcu216-0 runs the PYNQ 2.7 image (Python 3.8.2, 128 MB CMA). Four things differ from what the
software was written against (five, counting the RFDC driver version):

| Issue | What to do |
|---|---|
| **CMA is 128 MB.** The default 14-qubit build needs 14 × 16 MB = 224 MB for the host window, and `hostwin_bits` is baked into the RTL. | Build `software/configs/zcu216-14q-hw23.json` (`hostwin_bits: 23`, 8 MB per core = 112 MB): `RISCQ_CONFIG=software/configs/zcu216-14q-hw23.json RISCQ_PROJ_NAME=zcu216-14q-hw23 ...build-riscvsoc-bd.sh` |
| **PYNQ 2.7 cannot load an `.xsa`**; `pynq.Overlay` wants a `.bit` with a same-named `.hwh`. | `PynqDriver` now falls back to `top.bit` + `top.hwh` beside `top.xsa` on PYNQ < 3. Put all three (and `params.json`) in the bundle directory. |
| **Python 3.8**, while `pyproject.toml` says `>=3.10`. Every board-side module (`riscq`, `map`, `spec`, `build`, `run`, `driver`, `driver.remote`, `board`, `board.server`, `board.pynq_driver`) imports fine on 3.8. | Run from source instead of installing the wheel: `PYTHONPATH=<repo>/software:pyro5-*.whl:serpent-*.whl`, in a login shell (`bash -l`) so PYNQ's profile sets `BOARD=ZCU216`, which `xrfclk` needs at import. |
| **PYNQ 2.7's `xrfdc` binds only `usp_rf_data_converter` 2.3/2.4**; Vivado 2023.1 emits 2.6, so the RFDC loads as `DefaultIP` (no `mts_dac_config`, no tiles) and `load()` fails in MTS. | `PynqDriver` opens the overlay with `ignore_version=True`, as SPEAR's `rfsoc_overlay.py` does on this board. |
| **No 500 MHz clock files in the stock `xrfclk`.** It has only LMK04828 245.76 MHz (plus LMK04832/LMK04208 122.88 MHz and a few LMX2594 files); qcal-riscq's old `set_ref_clks(lmk_freq=500.25)` needed a file installed there. | The `native` plan programs its own bundled opentics files (above). |

SPEAR's own zcu216-0 notes (`hw_design/ZCU216/CLAUDE.md` in SPEAR-riscv) flag two board
issues: the ADC-side reference clock delivering no usable clock (2026-09-01: ADC tile PLLs
unlocked, a 48-sample ring on every ADC channel), and `mts_dac()` raising "Alignment correction
delay 31 required exceeds maximum (31)". Neither showed up under the 300 MHz SPEAR plan; see the
bring-up results below. Recheck both under the native plan. Keep checking `info()["rf_status"]` before trusting data.

### zcu216-0 14-qubit native build (2026-11-09, `zcu216-14q-lp6`)

**Timing.** 14 cores at 500 MHz on Vivado 2023.1, closed in four steps:

| Step | `dspClk` WNS | TNS | Failing endpoints |
|---|---|---|---|
| `zcu216-14q-hw23`, flow as-is | −0.208 ns | −79 ns | 2045 |
| + `adc_pipe` 5, phys_opt `AggressiveExplore` (`RISCQ_PHYSOPT_DIRECTIVE`) | −0.066 ns | −32 ns | 1404 |
| `zcu216-14q-lp6`: + `link_pipe` 6 | −0.046 ns | −9.5 ns | 609 |
| + incremental TimingClosure (`close-incremental.sh`) | −0.024 ns | −0.54 ns | 103 |
| + a second incremental pass seeded from the first (`RISCQ_INCR_SEED`), with post-route `AggressiveFanoutOpt AlternateReplication` (`RISCQ_INCR_POSTROUTE`) | **0.000 ns** | 0 | 0 |

Final result: "All user specified timing constraints are met". WHS is +0.003 ns, `hostClk` +2.446 ns,
`clk_pl_0` +2.526 ns. The bitstream is written from `routed_incr2.dcp`, and the XSA carries the same
bitstream.

The misses were spread thinly:
- the ADC 4 pipe (RF-ADC tile 1 to cores 7–13, every 0-logic hop of the 3-stage pipe);
- the core-to-datapath links;
- core-internal paths (execute ALU, PC / fetch clock enables, jump broadcast).

On 2026.1 the same design reached −0.015 ns in one pass.

Notes on the board results:

- **Pulse-train warm-up.** The first run after a different program can keep the extra 9 batches for
  several iterations: the GShare predictor is not cleared by reset, so it carries the previous program's
  history. Co-sim (power-up predictor) shows them on the first iteration only. After one run of the same
  program, board and co-sim agree exactly. An earlier note here put the extra 9 batches down to the longer
  links; that was wrong.
- **DAC Nyquist zone and low tones.** The low 250 MHz loopback level (peak 1224, SNR 28 dB) comes from
  DAC 0's Nyquist zone 2 (mix-mode, the `dac_nyquist` default), which suppresses low frequencies.
  Measured on DAC 0:

  | zone | 250 MHz on-rms / SNR | 3.25 GHz on-rms / SNR |
  |---|---|---|
  | 2 (default) | 817 / 28.5 dB | 2927 / 39.1 dB |
  | 1 | 5185 / 44.3 dB | 3404 / 40.4 dB |

  Zone 1 recovers 16 dB at 250 MHz. An ideal mix-mode model predicts about 26 dB, so the DAC's zone-2
  response is not fully characterized. Keep zone 2 for drives above fs/2 (qubits at 4–8 GHz); set
  `board.json` `dac_nyquist` to zone 1 for converters driving below about 2 GHz.
- **Noise floor.** The noise floor is ~32 codes rms on the 14q build vs ~24 on the 2-core build: `robs`
  sums the mapped ADCs, and the 14q build maps two (ADC 0 for cores 0–6, ADC 4 for cores 7–13), ADC 4
  uncabled. Expect a factor of about √2.

**On the board** (after a fresh boot; this test needs 112 MB of CMA, so restart the server rather than
kill it, and reboot if `CmaFree` stays low after a crash):

| Check | Measured |
|---|---|
| RFDC | all 8 tiles running, tile-1 PLLs locked, MTS 0 (DAC 224 / ADC 64) |
| `dspClk` | **499.994 MHz** (31 samples over 61 s, residual 1.6 ms) |
| Host window, core multiply | exact |
| 14 cores, pulse trains | all done; **identical to the batch with co-sim of the same config** (all 14 cores, reps 1–2 and core 0 alone): first interval +9 (predictor warm-up), then dur + gap + 224, the same loop body as the 2-core and 300 MHz builds |
| Loopback 250 MHz / 3.25 GHz | 250.00 MHz (SNR 28 dB) / alias 750.00 MHz (SNR 40 dB); burst 717–719 ns (expected 720); starts 187 ns after the gate (+7 ns; the two extra ADC pipe stages account for 4) |

### zcu216-0 native-plan results (2026-11-08, `zcu216-2q`)

The opentics clock files were checked on hardware before the 14-qubit native build finished, with a 2-core
probe: `software/configs/zcu216-2q.json`, the same RFDC and clock plan (500 MHz, tile 1, 16 DAC + 16 ADC).
It met timing at 500 MHz first pass (Vivado 2023.1: `dspClk` WNS +0.067 ns, WHS +0.011 ns; `hostClk`
+5.040 ns; `clk_pl_0` +2.738 ns). The board was rebooted first. The new files were then programmed by hand
(no hang, no errors), and finally through `PynqDriver` when the server loaded the bundle.

The CLK104's SPI path is write-only: LMK04828 and LMX2594 readback returns 0 for every register, ID
registers included. Lock is shown by what depends on it instead:

| Check | Expected | Measured |
|---|---|---|
| RFDC tiles (`rf_status`) | all 8 running; tile-1 PLLs locked from the 500 MHz LMX2594 reference | every DAC/ADC tile `TileState` 15, `PLLLockStatus` 2; tile 1 `PLLState` 1 (`ClockSource` 1), the others on its distributed clock |
| MTS free sync | all tiles of a kind agree | `mts_result` 0; DAC 224 ×4, ADC 64 ×4 (the latencies qcal-riscq measured for its 500 MHz build) |
| `dspClk` = LMK04828 CLKout8 | 500 MHz | **500.058 MHz**: 26 `now()` samples over 62 s, unwrapped, least-squares slope (RPC latency 25–200 ms today, so single 4 s intervals scatter ±1.7 %) |
| Host window, core multiply | exact | exact |
| Loopback DAC 0 → ADC 0, 250 MHz | 250 MHz | 250.00 MHz, SNR 30 dB |
| Loopback, 3.25 GHz | alias of 3.25 GHz at ADC 2 GS/s = 750 MHz, which needs DAC 8 GS/s and ADC 2 GS/s exactly | **750.00 MHz**, SNR 42 dB |
| Burst width | 720 ns (half-maximum width of the 800 ns pulse with 10 % cosine edges) | 718 ns, starting ≈ 180 ns after the gate |
| 2 cores, pulse trains | first interval long by 9 batches (predictor warm-up), then constant | `[241, 232, 232, …]`; directly after a different kernel, a few more 241s (the predictor is not cleared by reset) |

### zcu216-0 bring-up results (2026-11-07, `zcu216-14q-spear`)

> **Historical: 300 MHz SPEAR clock plan.** The `spear` plan, its config and SPEAR's clock files
> have since been removed for the native plan above. Its results stand as a check of the 2023.1
> build, the RFDC on tile 1 and the software stack; the bundle, its build and its launcher were deleted
> on 2026-11-08 once the native plan was confirmed on the board (results above).

Build: Vivado 2023.1, timing met on every clock (`dspClk` 300 MHz WNS +0.261 ns, WHS +0.009 ns;
`hostClk` +2.279 ns; `clk_pl_0` +2.899 ns), no incremental pass needed. Loaded with
`deploy/start-board-server.sh --bundle zcu216-14q-spear`; tested from the host over `RemoteDriver`.

| Check | Expected | Measured |
|---|---|---|
| RFDC tiles (`rf_status`) | all 8 tiles running, tile-1 PLLs locked | DAC and ADC tiles 0–3 `TileState` 15; DAC/ADC tile 1 PLL locked (`PLLLockStatus` 2, `ClockSource` 1); other tiles on the distributed clock |
| MTS free sync | all tiles of a kind agree | `mts_result` 0; DAC latency 224 ×4, ADC 60 ×4 |
| `dspClk` (core spins writing `now()`, host samples 4 s apart) | 300 MHz | 299.98 MHz (±0.05 % RPC jitter) |
| Host window (DMA into PS DDR4) | window array = RAM array | equal |
| Core multiply (`with_mul`) | int32 products | bit-exact over 32 values |
| 14 cores at once, pulse trains | every core done; spacing = `dur + gap + loop body` | all 14 done; loop body 233 batches on the first iteration(s), then 224 — `[241, 232, 232, …]` for gap 8, identical to the `sim-2q` co-sim |
| Loopback DAC 0 (`vout00`) → ADC 0 (`vin00`), `robs`, 250 MHz tone | alias 250 MHz, 600 ns FWHM (667 ns pulse with 10 % cosine edges) | 249.98 MHz, 598 ns, SNR 42 dB, peak code 6100 |
| Same at 3.25 GHz (2nd DAC Nyquist zone) | alias 4.8 − 3.25 = 1.55 GHz → 350 MHz | 350.02 MHz, 598 ns, SNR 45 dB, peak code 8488 |
| ADC noise floor, TX off | no 48-sample ring | autocorrelation peak ≤ 0.11 (SPEAR saw 0.81 at lag 48) |

The tone appears ≈ 200 ns after the gate fires (DAC → cable → ADC → `robs`, at the 50 % point).
The largest spurs are the RF-ADC interleaving images at fs/4 ± f_in (−38 dBc for 250 MHz). The
first loop iteration's extra 9 batches come from the core warming up (the branch predictor is not
cleared by reset), not from the pulse hardware. The co-sim reproduces it to the batch. MTS targets
are still unpinned: pin `{"daclatency": 224 + k, "adclatency": 60 + k}` in `board.json` once a
deterministic latency across reloads is needed.

### Generic flow

Unchanged from [software/README.md](../software/README.md#board-install-offline): build offline
wheels, `scp` them to the board, `pip install --no-index` there, check `CmaFree` exceeds
`cores × 16 MB`, start `riscq-board-server --port 9091`, then from the host
`upload_bundle(drv, name, xsa=..., params_json=...)` and `drv.board.load(name)`, and check that
`mts_result == 0`.
