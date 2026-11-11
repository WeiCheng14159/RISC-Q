package riscq.soc.dio

import spinal.core._
import spinal.lib._
import riscq.dsp.Complex
import riscq.dsp.pulse.TimedQueue
import riscq.soc.link.{EventLink, EventSource, Put}
import riscq.soc.rf.{Channel, PulseParamBuffer, PulseParamBufferParams}

/**
 * Timed digital I/O — the first non-pulse channel kind. A bank of 16 output lines and 16 input lines
 * behind the SAME posted register file as a pulse channel ([[PulseParamBuffer]]): a slot is `{mask (the phase word), value (the amp
 * word), dur}`, `fire` schedules it at the channel's `startTime` and auto-advances `startTime` by `dur`,
 * so a TTL train is one `play` + N−1 bare fires exactly like a pulse train, and every existing op
 * (`set_start`, `fire`, `play`, `init_pulse_params`) drives it unchanged. Two limits it shares with the
 * pulse channels: at most `queueDepth` (4) entries scheduled ahead (a push into a full queue is
 * dropped — pace trains as `riscq.cal.base.TRAIN_AHEAD` does), and the queue pops at most every other
 * cycle, so the shortest hold (`dur`) of an entry followed by another is 2 batches. At the scheduled batch the
 * output register takes `(out & ~mask) | (value & mask)` — placed at cycle precision by a
 * [[TimedQueue]], the core only schedules by lead time.
 *
 * Inputs are sampled every cycle; any change posts an event `{changed lines, new levels}` stamped
 * with the batch time of the sample, into the core's `fifo` sink (consume-on-read, with a sequence
 * number) — "wait for exposure-out" and "when exactly did the shutter open" are one halting read.
 *
 * '''Board inputs''' pass a `syncStages`-flop synchronizer (ASYNC_REG) before sampling: a PMT or any
 * external TTL is asynchronous to dspClk. The stages delay every input timestamp by `syncStages` batches
 * (`TimedDio.inputDelay`), which software subtracts.
 *
 * '''Gated edge counters''' (`countMask`): an input line j in the mask is a photon counter, the ARTIQ
 * `EdgeCounter` model. Its gate is OUTPUT bit j of the bank — scheduled like any TTL edge (`play` an entry
 * setting bit j at the gate open, another clearing it at the close), so the gate lands at batch
 * precision on the timeline. While the gate is high the counter counts rising edges of input j
 * (saturating at 0xFFFF); on the gate's falling edge it posts ONE event `{changed = 1 << j, levels =
 * count}` stamped with the close batch, and clears. A counted line posts no change events (a PMT would
 * flood the fifo), so an event whose `changed` has a counted bit set IS that line's count. Change events
 * win a same-cycle collision; a closed count waits (one pending count per line) and goes out on the next
 * free cycle with its own close time.
 *
 * '''Loopback''' (the buffer's otherwise-unused `dcOffset` register, `set_dc_offset(ch, mask << 16)`):
 * input j with mask bit j set reads OUTPUT line `j ^ 8` instead of its pin, after the synchronizer — a
 * board self-test of the counters and input timestamps with nothing wired (out 0..7 → in 8..15).
 */
case class TimedDio(
    slots: Int,
    timeWidth: Int = 32,
    durWidth: Int = 16,
    queueDepth: Int = 4,
    putAddrWidth: Int = 16,
    countMask: Int = 0,         // input lines that are gated edge counters (gate = the same output bit)
    syncStages: Int = 2         // input synchronizer flops (board pins are asynchronous)
) extends Component with Channel {
  val lines = 16                      // the buffer's 16-bit mask/value fields
  val io = new Bundle {
    val cmd       = slave port Flow(Put(putAddrWidth))
    val timeBcast = in    port UInt(timeWidth bits)
    val dout      = out   port Bits(lines bits)
    val din       = in    port Bits(lines bits)
    val event     = master port Flow(Bits(2 * lines bits))   // {changed, levels} on any input change
    val eventTime = out   port UInt(timeWidth bits)          // the batch the changed sample was taken
    val time      = out   port UInt(timeWidth bits)          // the buffer's local time copy (sims align on it)
  }
  def cmd = io.cmd; def timeBcast = io.timeBcast
  def memPort = None; def envLanes = 0; def dacOut = None; def carrier = None

  val buf = PulseParamBuffer(PulseParamBufferParams(
    pulseNum = slots, dataWidth = lines, envAddrWidth = 1, durWidth = durWidth,
    timeWidth = timeWidth, addrWidth = putAddrWidth))
  buf.io.cmd << io.cmd
  buf.io.timeBcast := io.timeBcast

  // ── the scheduled output update: {mask, value} due at the fired slot's startTime ──
  case class Term() extends Bundle {
    val mask  = Bits(lines bits)
    val value = Bits(lines bits)
  }
  val queue = TimedQueue(Term(), timeWidth, queueDepth, leadTime = TimedDio.leadTime)
  queue.io.time := buf.io.time
  queue.io.push.valid := buf.io.phase.valid                 // the fired slot's fields land together
  queue.io.push.payload.data.mask  := buf.io.phase.payload.asBits
  queue.io.push.payload.data.value := buf.io.amp.payload.asBits
  queue.io.push.payload.startTime  := buf.io.startTime      // a push into a full queue is dropped (posted)
  val outReg = Reg(Bits(lines bits)) init 0
  when(queue.io.pop.valid) {
    outReg := (outReg & ~queue.io.pop.payload.mask) | (queue.io.pop.payload.value & queue.io.pop.payload.mask)
  }
  io.dout := outReg

  // ── inputs: loopback mux → synchronizer → sample ──
  // The loopback joins BEFORE the synchronizer, so a looped edge has exactly a wired pin's timing.
  require(syncStages >= 0 && (countMask >> lines) == 0)
  val loopMask = buf.io.dcOffset.asBits                                       // runtime: 0 = pins
  val looped   = Bits(lines bits)                                             // in j ← out j^8
  for (j <- 0 until lines) looped(j) := outReg(j ^ 8)
  val pinOrLoop = (io.din & ~loopMask) | (looped & loopMask)
  val synced = (0 until syncStages).foldLeft(pinOrLoop) { (d, i) =>
    val r = RegNext(d) init 0
    r.addAttribute("ASYNC_REG", "TRUE")
    r.setName(s"inSync$i")
    r
  }
  val inReg  = RegNext(synced) init 0
  val inPrev = RegNext(inReg) init 0
  val sampleTime = RegNext(buf.io.time)                                       // the batch inReg was taken
  val cntBits = B(countMask, lines bits)
  val changed = (inReg ^ inPrev) & ~cntBits                                   // counted lines post no edges

  // ── gated edge counters (one per countMask line) ──
  // An edge at pin/output time T rises in `rising` at cycle T + syncStages + 1; the gate bit is delayed by
  // the same amount, so the window is EXACTLY [gate open, gate close) on the timeline (ARTIQ gate_rising).
  val gateLag  = syncStages + 1
  val rising   = inReg & ~inPrev
  val gate     = Delay(outReg & cntBits, gateLag, init = B(0, lines bits))
  val gatePrev = RegNext(gate) init 0
  val closing  = gatePrev & ~gate                                             // gate fell (lagged) this cycle
  val closeTime = buf.io.time - gateLag                                       // = the gate's timeline close
  val counts   = Vec.fill(lines)(Reg(UInt(16 bits)) init 0)
  val doneCnt  = Vec.fill(lines)(Reg(UInt(16 bits)) init 0)
  val doneTime = Vec.fill(lines)(Reg(UInt(timeWidth bits)) init 0)
  val pending  = Reg(Bits(lines bits)) init 0
  val sendCnt  = Bool()
  val sendSel  = UInt(log2Up(lines) bits)
  for (j <- 0 until lines if ((countMask >> j) & 1) == 1) {
    when(closing(j)) {
      doneCnt(j)  := counts(j)                                                // the count up to the close
      doneTime(j) := closeTime
      counts(j)   := 0
    }.elsewhen(gate(j) && rising(j) && counts(j) =/= 0xFFFF) {
      counts(j) := counts(j) + 1
    }
  }
  // the lowest pending count goes out on a cycle with no change event
  sendSel := OHToUInt(OHMasking.first(pending))
  sendCnt := pending.orR && changed === 0
  pending := (pending & ~Mux(sendCnt, UIntToOh(sendSel, lines), B(0, lines bits))) | closing

  io.event.valid   := changed =/= 0 || sendCnt
  io.event.payload := Mux(changed =/= 0, changed ## inReg, UIntToOh(sendSel, lines) ## doneCnt(sendSel).asBits)
  io.eventTime     := Mux(changed =/= 0, sampleTime, doneTime(sendSel))
  io.time          := buf.io.time
  override def event: Option[EventSource] = Some(EventSource(EventLink.fifoKind, io.event, Some(io.eventTime)))
}

object TimedDio {
  /** The queue pops one cycle before the scheduled batch so the registered output changes ON it. */
  val leadTime = 1
  val eventDataWidth = 32                                    // {changed[15:0], levels[15:0]}
  /** batches an input pin edge's event stamp trails the pre-synchronizer stamp: the synchronizer stages.
    * (Counter windows are compensated in hardware — they need no correction.) */
  def inputDelay(syncStages: Int = 2): Int = syncStages
}
