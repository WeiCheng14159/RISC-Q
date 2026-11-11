package riscq.dsp.pulse

import spinal.core._
import spinal.lib._
import riscq.dsp._

/**
 * Carrier batch generator:
 *
 * {{{
 *   carrier[k](t) = amp · exp(iπ·gPhase) · phasor[k] ,   gPhase = freq·(N·t) + phase  (mod 2)
 *               = amp·Amax · exp(iπ·(freq·(N·t + k) + phase))     (with phasor[k] = Amax·exp(iπ f k))
 * }}}
 *
 * '''Intra-batch factorization''': one CORDIC computes the per-batch, time-dependent factor
 * `amp·exp(iπ·gPhase)` and `N` `ComplexMul`s combine it with the static per-lane phasors. The
 * '''time-product phase''' `gPhase = freq·((t·N) mod 2^w) + phase` is recomputed from absolute time
 * every cycle and truncated to SF(w): `(a·(b mod 2^w)) mod 2^w = a·b mod 2^w`, and one wrap of the
 * truncated factor is a whole number of turns, so the phase is '''exact mod 2π''' — pulses are
 * phase-coherent across arbitrary time gaps and channels, the property qubit control needs.
 * '''Amplitude rides the CORDIC `x` input''' — no separate amplitude multiply.
 *
 * Operand-magnitude contract for the `ComplexMul` saturation: `|amp·exp| ≤ amp < 1` and
 * `|phasor[k]| < 1`, so the carrier product never saturates; the overall carrier scale is
 * `amp·(Amax·2^-(w-1))²` — a pure gain the golden model includes.
 *
 * `io.carrier` is a plain (always-valid) batch — validity is the duration gate's job. Inputs
 * are held in parameter registers (`RegNextWhen` on each Flow); `io.phasors` is snapshotted on valid.
 * Per-input latencies are exported as sums of sub-module latencies (no hard-coded literals).
 *
 * '''Fine frequency''' (`freqWidth = 2·dataWidth`, e.g. 32): `freq` is a 2w-bit per-sample phase
 * advance (2^(2w) = one turn), so the tone grid is fs/2^32 (1.86 Hz at 8 GS/s) instead of fs/2^16.
 * The time product is taken mod 2^(2w) — still exact mod 2π — and only its TOP w bits feed the
 * CORDIC: with F = Fh·2^w + Fl and B = (t·N) mod 2^(2w) = Bh·2^w + Bl,
 * {{{
 *   ⌊(F·B mod 2^2w) / 2^w⌋ = (⌊Fl·Bl / 2^w⌋ + Fh·Bl + Fl·Bh)  mod 2^w
 * }}}
 * three w×w products (one DSP each, MREG+PREG pipelined) and a 3-input w-bit add. That is
 * `extraStages` = 2 cycles more than the narrow path, so the time operand enters `extraStages` batches
 * EARLY (`io.time + extraStages`): `timeLatency` — the carrier's absolute-time reference, what golden
 * models call timeToPulse — is unchanged, and fine- and coarse-frequency channels stay phase-aligned.
 * Only `freqLatency` grows (the freq queue's lead re-derives). The truncation to w bits costs < 1 SF(w)
 * LSB of phase per term (≤ 2 LSB total, 1e-4 turn) and never accumulates: every batch is recomputed
 * from absolute time.
 */
case class CarrierBatchGenerator(batchSize: Int, dataWidth: Int, timeWidth: Int, correctGain: Boolean = true,
    saturate: Boolean = true, freqWidth: Int = 0) extends Component {
  require(isPow2(batchSize), "batchSize must be a power of two (time·N is an exact left shift)")
  val N     = batchSize
  val w     = dataWidth
  val log2N = log2Up(N)
  val amax  = (BigInt(1) << (w - 1)) - 1
  val fw    = if (freqWidth == 0) dataWidth else freqWidth
  require(fw == w || fw == 2 * w, s"freqWidth $fw: $w (coarse) or ${2 * w} (fine)")
  val fine  = fw > w
  require(!fine || timeWidth >= fw - log2N, "time too narrow for the fine product: (t·N) mod 2^fw")
  /** pipeline stages the fine product adds over the narrow one (time is fed that much early) */
  val extraStages: Int = if (fine) 2 else 0

  val io = new Bundle {
    val time    = in port UInt(timeWidth bits)
    val amp     = slave port Flow(SInt(w bits))
    val phase   = slave port Flow(SInt(w bits))
    val freq    = slave port Flow(SInt(fw bits))
    val phasors = slave port Flow(ComplexBatch(N, w))
    val carrier = out port ComplexBatch(N, w)
  }

  // Held parameter registers. No reset init: each is write-before-read — popped from its TimedQueue at
  // lead time, before the (duration-gated) carrier is ever observed at the DAC — so the reset value is
  // never seen. Dropping the inits keeps these FFs out of the async-reset group, which packs denser and
  // lightens the reset net for the replicated instances. Do not restore.
  val ampReg   = RegNextWhen(io.amp.payload, io.amp.valid)
  val phaseReg = RegNextWhen(io.phase.payload, io.phase.valid)
  val freqReg  = RegNextWhen(io.freq.payload, io.freq.valid)
  // Phasor snapshot, taken on a BUFFERED enable. `io.phasors.valid` (= phasorGen `!regen`) is the
  // snapshot enable for all N·2 lanes — one cross-module net fanning out to N·2·w = 512 phRe/phIm FF
  // clock-enables. Rather than drive 512 clock-enables off a single combinational net, register the
  // enable (`phValBuf`, max_fanout-capped so the tool can replicate it beside the lanes) and delay the
  // payload one matching stage (`phIn`) so the snapshot stays aligned. Cost: the batch lands one cycle
  // later, folded into `phasorLatency` below (so PulseGenerator's freq-phasor lead time re-derives
  // automatically, with no literals). No reset init on the snapshot/payload: write-before-read, never
  // observed before the first `freq` write regenerates a real batch (the pulse is duration-gated to
  // zero), so the boot value is irrelevant.
  val phValBuf = RegNext(io.phasors.valid) init False    // buffered snapshot enable (drives the 512 FFs)
  phValBuf.addAttribute("max_fanout", 32)
  val phIn     = RegNext(io.phasors.payload)             // payload delayed to match the buffered enable
  val phRe     = Vec.fill(N)(Reg(SInt(w bits)))
  val phIm     = Vec.fill(N)(Reg(SInt(w bits)))
  when(phValBuf)(for (k <- 0 until N) {
    phRe(k) := phIn(k).re
    phIm(k) := phIn(k).im
  })

  // phase pipeline → gPhase (all truncating-wrap = exact phase wrap):
  val gPhase = SInt(w bits)
  if (!fine) {
    val batchTime = RegNext(((io.time << log2N).resize(w bits)).asSInt)        // (t·N) mod 2^w, SF(w)
    // use_dsp on the product only (as in ComplexMul): at the 14q SoC's DSP demand the global DSP
    // balancer otherwise demotes it to fabric — Vivado 2023.1 did, a −0.32 ns CARRY8 chain.
    val timePhaseMul = freqReg * batchTime
    timePhaseMul.addAttribute("use_dsp", "yes")
    val timePhase = RegNext(timePhaseMul)                                     // 1 DSP, full width
    gPhase := RegNext(timePhase.resize(w bits) + phaseReg)                    // low w bits + phase, mod 2^w
  } else {
    // (t + extraStages)·N mod 2^fw: the operand leads by the extra product stages (see the class doc)
    val batchTime = RegNext(((io.time + extraStages) << log2N).resize(fw bits))  // unsigned, mod 2^fw
    val fU = freqReg.asUInt
    val (fl, fh) = (fU(w - 1 downto 0), fU(fw - 1 downto w))
    val (bl, bh) = (batchTime(w - 1 downto 0), batchTime(fw - 1 downto w))
    // three w×w products, each MREG+PREG-registered so it packs into one DSP48E2 (2 stages)
    def dspMul(a: UInt, b: UInt): UInt = {
      val m = a * b
      m.addAttribute("use_dsp", "yes")
      RegNext(RegNext(m))
    }
    val pLL = dspMul(fl, bl)                                                  // 2w bits
    val pHL = dspMul(fh, bl)(w - 1 downto 0)                                  // only the low w bits survive
    val pLH = dspMul(fl, bh)(w - 1 downto 0)
    val timeTop = RegNext(pLL(2 * w - 1 downto w) + pHL + pLH)                // ⌊F·B mod 2^2w / 2^w⌋, mod 2^w
    // phase joins at the last add, exactly as on the narrow path (phaseLatency unchanged: its own queue)
    gPhase := RegNext(timeTop.asSInt + phaseReg)
  }

  // One CORDIC: amp·exp(iπ·gPhase), amplitude on the x input. When correctGain = false the
  // gain stage is dropped — software must prescale amp by 1/K so K·(amp/K) = amp.
  // resetValid = false on both the CORDIC and the ComplexMuls: their rsp.valid is unused
  // (payload read at fixed latency; the carrier feeds the duration-gated pulse output), so the reset-free
  // valid chains shed the global reset and infer SRLs.
  val cordic = Cordic(CordicParams(xyWidth = w, zWidth = w, correctGain = correctGain, saturate = saturate,
                                   resetValid = false))
  cordic.io.cmd.valid         := True
  cordic.io.cmd.payload.xy.re := ampReg
  cordic.io.cmd.payload.xy.im := S(0, w bits)
  cordic.io.cmd.payload.z     := gPhase

  // Broadcast register (N-lane fanout) then N ComplexMuls against the static phasors.
  val cBase = RegNext(cordic.io.rsp.payload)
  val muls  = Array.fill(N)(ComplexMul(w, saturate, resetValid = false))
  for (k <- 0 until N) {
    muls(k).io.cmd.valid     := True
    muls(k).io.cmd.payload.a := cBase
    muls(k).io.cmd.payload.b.re := phRe(k)
    muls(k).io.cmd.payload.b.im := phIm(k)
    io.carrier(k) := muls(k).io.rsp.payload
  }

  // exported per-input latencies (param-Flow fire → carrier), each a sum of sub-module latencies.
  private def tail: Int = cordic.latency + 1 + ComplexMul.latency(saturate) // cordic + broadcast + ComplexMul
  def ampLatency: Int    = 1 + tail            // ampReg → cordic.xy
  def phaseLatency: Int  = 2 + tail            // phaseReg → gPhase → cordic.z
  def freqLatency: Int   = 3 + extraStages + tail // freqReg → timePhase → gPhase → cordic.z
  def phasorLatency: Int = 2 + ComplexMul.latency(saturate) // phValBuf + snapshot → ComplexMul.b
  def timeLatency: Int   = 3 + tail            // time → batchTime → timePhase → gPhase (fine: time
                                               // enters extraStages early, so the reference is unchanged)
}
