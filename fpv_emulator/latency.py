"""Detection-latency measurements.

Two related measurements:

1. :func:`bench_command_to_rf` — how long after WE issue the transmit command the RF
   actually appears. The Pluto's own receiver is the witness, so no detector is
   involved. This is the systematic offset that sits inside every detector figure;
   measuring it separately keeps it from being silently attributed to the detector.

2. :func:`measure_detection_latency` — the real test: transmit, then wait for the
   detector to report through its log (COM port), N times, and write every trial to
   CSV together with the statistics.

What the numbers mean — the chain being timed is:

    t0  command issued (python)
      → buffer uploaded over USB, DMA armed          [t_armed - t0]
      → LO/PA settle, RF on air                      [measured by (1)]
      → the detector acquires and decides            [what we actually want]
      → the detector prints, USB-serial delivers it  [transport, unavoidable]
    t1  the line reaches us

So a detector figure is an upper bound: it includes the transport and the
detector's own print cadence. (1) lets you subtract our side of it.
All timestamps use ``time.perf_counter()`` (monotonic).
"""
from __future__ import annotations

import csv
import gc
import os
import random
import re
import statistics
import time
import warnings
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from . import firmware as firmware_mod
from . import iio_host
from .backends import apply_sample_rate
from .i18n import t
from .logsource import LogSource


@dataclass
class Trial:
    """One measurement."""

    n: int
    ok: bool
    latency_s: Optional[float] = None      # command -> event (video acquired)
    confirm_s: Optional[float] = None      # command -> confirmed detection
    upload_s: Optional[float] = None       # command -> buffer armed
    post_arm_s: Optional[float] = None     # buffer armed -> event
    freq_mhz: Optional[float] = None
    gain_db: Optional[float] = None        # power this trial actually used
    reported_mhz: Optional[float] = None   # frequency the detector named
    note: str = ""
    matched_line: str = ""
    confirm_line: str = ""


def _line_freq_mhz(match: "re.Match") -> Optional[float]:
    """Frequency the detector named, if the pattern captured an ``mhz`` group."""
    if "mhz" not in (match.re.groupindex or {}):
        return None
    try:
        return float(match.group("mhz"))
    except (TypeError, ValueError):
        return None


def summarize(values: Sequence[float]) -> Dict[str, float]:
    """min / median / p95 / max / mean / stdev over the successful trials."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {}
    def pct(p: float) -> float:
        if len(vals) == 1:
            return vals[0]
        k = (len(vals) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
        return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)
    return {
        "n": len(vals),
        "min_ms": vals[0] * 1e3,
        "median_ms": statistics.median(vals) * 1e3,
        "p95_ms": pct(0.95) * 1e3,
        "max_ms": vals[-1] * 1e3,
        "mean_ms": statistics.fmean(vals) * 1e3,
        "stdev_ms": (statistics.stdev(vals) * 1e3) if len(vals) > 1 else 0.0,
    }


def write_csv(path: str, trials: Sequence[Trial], meta: Dict[str, object]) -> None:
    """Write every trial plus a metadata header, so a run is self-describing."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        for k, v in meta.items():
            fh.write(f"# {k}: {v}\n")
        w = csv.DictWriter(fh, fieldnames=list(asdict(Trial(n=0, ok=False)).keys()))
        w.writeheader()
        for tr in trials:
            w.writerow(asdict(tr))


def format_summary(title: str, stats: Dict[str, float], misses: int = 0) -> str:
    if not stats:
        return f"{title}: " + t("no successful measurements")
    return (f"{title}: n={stats['n']}"
            + (f" (+{misses} " + t("missed") + ")" if misses else "")
            + f"  min={stats['min_ms']:.1f}  median={stats['median_ms']:.1f}"
            + f"  p95={stats['p95_ms']:.1f}  max={stats['max_ms']:.1f}"
            + f"  σ={stats['stdev_ms']:.1f} ms")


# ---------------------------------------------------------------------------
#  1) command -> RF, witnessed by the Pluto's own receiver
# ---------------------------------------------------------------------------
def bench_command_to_rf(
    iq_int16: np.ndarray,
    uri: str,
    freq_hz: float,
    fs: float,
    trials: int = 20,
    tx_gain_db: float = -30.0,
    rx_gain_db: float = 40.0,
    rx_buffer_size: int = 4096,
    gap_s: float = 0.30,
    timeout_s: float = 2.0,
    threshold_db: float = 10.0,
    firmware: str = firmware_mod.AUTO,
    on_trial: Optional[Callable[[Trial], None]] = None,
) -> List[Trial]:
    """Measure command -> RF-present, using the Pluto RX as the witness.

    The receiver sees the transmitter through internal leakage, so no antenna and no
    external emission is required (keep tx_gain low). RX AGC is disabled — an AGC
    would add its own settling time to every number.

    Resolution is bounded by one RX buffer (``rx_buffer_size / fs``); the rise is then
    located INSIDE the buffer, which recovers most of it. What cannot be removed is
    the constant RX pipeline delay, so treat the result as "our side of the chain,
    upper bound".
    """
    adi = iio_host.import_adi()   # a missing libiio in words, not a ctypes TypeError

    # This benchmark needs RX as well as TX, so it cannot borrow PlutoSink. It
    # must still open the device the same way the sink does, or a board whose
    # IIO devices are named differently — or that refuses pyadi's FIR filter —
    # would fail here while `tx` worked, and the --firmware flag would be a
    # control that visibly does nothing.
    pluto_cls, phy_name = adi.Pluto, "ad9361-phy"
    try:
        import iio  # noqa: WPS433
        from .iio_layout import detect_layout, pluto_class_for
        layout = detect_layout(iio.Context(uri))
        phy_name = layout.phy or phy_name
        pluto_cls = pluto_class_for(layout)
    except Exception:
        pass          # cannot inspect: fall back to the stock class
    # The inspection's context must be gone before pyadi opens its own — over USB
    # a board takes one at a time (see PlutoSink._ensure_open).
    gc.collect()

    try:
        sdr = pluto_cls(uri=uri)
    except Exception as exc:
        raise RuntimeError(iio_host.open_failure(uri, exc).message()) from exc
    note = apply_sample_rate(sdr, phy_name, fs, firmware)
    if note:
        warnings.warn(note, stacklevel=2)
    sdr.tx_rf_bandwidth = int(min(fs, 40e6))
    sdr.rx_rf_bandwidth = int(min(fs, 40e6))
    sdr.tx_lo = int(freq_hz)
    sdr.rx_lo = int(freq_hz)
    sdr.tx_hardwaregain_chan0 = float(tx_gain_db)
    sdr.gain_control_mode_chan0 = "manual"      # no AGC: it would add settling time
    sdr.rx_hardwaregain_chan0 = float(rx_gain_db)
    sdr.rx_buffer_size = int(rx_buffer_size)
    sdr.loopback = 0
    sdr.tx_cyclic_buffer = True

    buf_dt = rx_buffer_size / fs
    out: List[Trial] = []
    try:
        for i in range(1, trials + 1):
            # --- RF off and settled -----------------------------------------
            try:
                sdr.tx_destroy_buffer()
            except Exception:
                pass
            time.sleep(gap_s)

            # --- noise floor with the transmitter off ------------------------
            for _ in range(3):
                sdr.rx()
            noise = float(np.mean(np.abs(sdr.rx()) ** 2)) + 1e-12
            threshold = noise * (10 ** (threshold_db / 10.0))

            # --- fire ---------------------------------------------------------
            t0 = time.perf_counter()
            sdr.tx(iq_int16)
            t_armed = time.perf_counter()

            deadline = t0 + timeout_s
            tr = Trial(n=i, ok=False, freq_mhz=freq_hz / 1e6,
                       upload_s=t_armed - t0, note="timeout")
            while time.perf_counter() < deadline:
                x = sdr.rx()
                t_rx = time.perf_counter()
                p = np.abs(x) ** 2
                if float(p.mean()) <= threshold:
                    continue
                # locate the rise inside this buffer to beat the buffer granularity
                idx = int(np.argmax(p > threshold))
                t_rise = t_rx - (len(x) - idx) / fs
                tr = Trial(n=i, ok=True, freq_mhz=freq_hz / 1e6,
                           latency_s=t_rise - t0,
                           upload_s=t_armed - t0,
                           post_arm_s=t_rise - t_armed,
                           note=f"rx_buf={buf_dt*1e3:.2f}ms")
                break
            out.append(tr)
            if on_trial:
                on_trial(tr)
    finally:
        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass
        del sdr
    return out


# ---------------------------------------------------------------------------
#  2) command -> the detector reports it
# ---------------------------------------------------------------------------
#: Gain that counts as "RF off" while the cyclic buffer stays armed. The carrier is
#: not literally absent, it is 89 dB down — far below any detector's threshold.
GAIN_OFF_DB = -89.0


def measure_detection_latency(
    sink,
    iq_int16: np.ndarray,
    source: LogSource,
    pattern: str,
    freq_hz: float,
    tx_gain_db: float = -10.0,
    trials: int = 20,
    timeout_s: float = 10.0,
    gap_s: float = 3.0,
    settle_s: float = 0.2,
    offset_s: float = 0.0,
    confirm_pattern: Optional[str] = None,
    freq_tol_mhz: float = 15.0,
    release_pattern: Optional[str] = None,
    release_timeout_s: float = 25.0,
    gap_jitter_s: float = 0.0,
    freq_list_hz: Optional[Sequence[float]] = None,
    gain_list_db: Optional[Sequence[float]] = None,
    on_trial: Optional[Callable[[Trial], None]] = None,
    stop_flag: Optional[Callable[[], bool]] = None,
) -> List[Trial]:
    """Switch the carrier on, wait for a matching log line, repeat.

    The buffer is uploaded ONCE and the RF is gated with ``tx_hardwaregain``.
    Re-uploading per trial would be the obvious implementation and it is wrong here:
    measured on this hardware, pushing a 1.6 MB cyclic buffer over USB 2.0 takes
    ~105 ms (median 139 ms command->RF, spread 101-255 ms), which is far larger than
    the detector latency we are trying to resolve. Gating with the gain register
    costs ~1 ms instead.

    ``sdr`` is an open ``adi.Pluto`` (not a Sink — this needs direct register access).
    ``pattern`` is a regular expression; the first matching line ends the trial.
    ``gap_s`` is the silence between trials and must be long enough for the detector
    to drop the previous target, otherwise the next trial starts already triggered
    and reports an implausibly small latency.
    ``offset_s`` is subtracted from every result — put our own command->RF delay
    there (see :func:`bench_command_to_rf`) to describe the detector alone.
    """
    rx = re.compile(pattern, re.IGNORECASE)
    rx_confirm = re.compile(confirm_pattern, re.IGNORECASE) if confirm_pattern else None
    rx_release = re.compile(release_pattern, re.IGNORECASE) if release_pattern else None
    freqs = list(freq_list_hz) if freq_list_hz else [freq_hz]
    tx_mhz = freqs[0] / 1e6                  # updated per trial below
    out: List[Trial] = []

    def freq_ok(m) -> bool:
        """Reject a hit the detector reported on a different frequency.

        A sweeping detector prints candidates across the band (2780, 3230, 4200 …)
        and some are spurious; without this check a trial would end on somebody
        else's signal and report a latency that never happened.
        """
        got = _line_freq_mhz(m)
        if got is None:
            return True                      # pattern captured no frequency
        return abs(got - tx_mhz) <= freq_tol_mhz

    def wait_until_released(deadline_s: float) -> bool:
        """Block until the detector says it dropped the target.

        Once it finds something the detector parks on it and stops sweeping — and
        while parked it no longer prints the acquisition line at all. Starting the
        next trial then measures a detector that is already triggered (or never
        reports). Waiting for its own release message is the only reliable
        synchronisation; a fixed sleep is a guess.
        """
        if rx_release is None:
            return True
        end = time.perf_counter() + deadline_s
        while time.perf_counter() < end:
            line = source.get(timeout=min(0.25, max(0.01, end - time.perf_counter())))
            if line is not None and rx_release.search(line.text):
                return True
            if stop_flag and stop_flag():
                return False
        return False                          # timed out: proceed anyway, note it

    # Arm through the SINK, not through adi.Pluto directly. There used to be two
    # arming paths — the GUI's (which verifies that the DMA is really playing and
    # reopens the context when it is not) and this one (which just trusted tx()).
    # That is why a measurement run could sit there detecting nothing while the GUI
    # worked fine on the same frequency: only one of the two paths had the fix.
    sink.cfg.gain_db = GAIN_OFF_DB         # arm muted, then gate with the gain
    sink.set_freq(freqs[0])
    sink.start(iq_int16)                   # verified: raises if TX never starts
    try:
        for i in range(1, trials + 1):
            if stop_flag and stop_flag():
                break
            # --- silence, so the detector releases the previous target ---------
            sink.set_gain(GAIN_OFF_DB)
            released = wait_until_released(release_timeout_s)
            time.sleep(gap_s)
            # Jitter the pause over at least one sweep period. Without it a fixed
            # cycle lands on nearly the same phase of the detector's sweep every
            # time, so the run measures one phase instead of the distribution and
            # looks far more repeatable than the detector really is.
            if gap_jitter_s > 0:
                time.sleep(random.uniform(0.0, gap_jitter_s))

            # retune while the carrier is muted, then let the LO settle
            idx = (i - 1) % len(freqs)
            freq_hz = freqs[idx]
            tx_mhz = freq_hz / 1e6
            # The right power is band dependent: the Pluto's output rises towards
            # the low end of its range, and too much of it drives the PA into
            # compression. The detector then locks onto harmonics that carry the
            # video while the fundamental is too distorted to decode — RF present,
            # black screen. Measured on this setup: -30 dB suits the 1.2 GHz band.
            trial_gain = (float(gain_list_db[idx % len(gain_list_db)])
                          if gain_list_db else float(tx_gain_db))
            sink.set_freq(freq_hz)
            time.sleep(max(settle_s, 0.25))

            source.drain()                # discard anything from the previous trial

            t0 = time.perf_counter()
            sink.set_gain(trial_gain)
            t_armed = time.perf_counter()

            deadline = t0 + timeout_s
            tr = Trial(n=i, ok=False, freq_mhz=tx_mhz, gain_db=trial_gain,
                       upload_s=t_armed - t0,
                       note="timeout" if released else "timeout;not-released")
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                line = source.get(timeout=min(0.25, remaining))
                if line is None:
                    if stop_flag and stop_flag():
                        tr.note = "stopped"
                        break
                    continue

                if not tr.ok:
                    m = rx.search(line.text)
                    if m and freq_ok(m):
                        tr = Trial(n=i, ok=True, freq_mhz=tx_mhz, gain_db=trial_gain,
                                   latency_s=(line.t - t0) - offset_s,
                                   upload_s=t_armed - t0,
                                   post_arm_s=line.t - t_armed,
                                   reported_mhz=_line_freq_mhz(m),
                                   matched_line=line.text[:160],
                                   note=("offset_subtracted" if offset_s else ""))
                        if rx_confirm is None:
                            break
                        continue          # keep waiting for the confirmed event

                if rx_confirm is not None and tr.ok and tr.confirm_s is None:
                    mc = rx_confirm.search(line.text)
                    if mc and freq_ok(mc):
                        tr.confirm_s = (line.t - t0) - offset_s
                        tr.confirm_line = line.text[:160]
                        break
            out.append(tr)
            if on_trial:
                on_trial(tr)
    finally:
        try:
            sink.set_gain(GAIN_OFF_DB)
        except Exception:
            pass
        sink.stop()
    return out
