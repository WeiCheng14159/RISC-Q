# Building and deploying with Vivado 2023.1

The maintained flow targets **Vivado 2026.1** (see the top-level [README](../README.md)). This page
records how to set up a fresh Linux machine and build the ZCU216 image with **Vivado 2023.1**
instead, and what had to change for it. Nothing here needs root.

Status: **in progress** — RTL generation and block-design assembly verified on 2023.1; synthesis /
implementation / bitstream results are recorded below once the first full build finishes.

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

### Expected differences from the results of record

The timing numbers in [vivado-scripts/riscvsoc-bd/README.md](../vivado-scripts/riscvsoc-bd/README.md)
were measured on 2026.1. Older placer/router releases give different results, so the 14-qubit
build may not close at 0.000 ns WNS; `close-incremental.sh` is the first lever if it misses by a
small margin.

## 4. Deploy to a ZCU216

Unchanged from [software/README.md](../software/README.md#board-install-offline): build offline
wheels, `scp` them to the board, `pip install --no-index` there, check `CmaFree` exceeds
`cores × 16 MB`, start `riscq-board-server --port 9091`, then from the host
`upload_bundle(drv, name, xsa=..., params_json=...)` and `drv.board.load(name)`, and check that
`mts_result == 0`.
