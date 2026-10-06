"""HackRF One straight through libhackrf (ctypes) — no SoapySDR, no pip package.

The only thing this needs is the native library: ``hackrf-0.dll`` / ``hackrf.dll``
on Windows, ``libhackrf.so.0`` on Linux, ``libhackrf.dylib`` on macOS. On Windows
``scripts/fetch_hackrf.py`` puts it (with libusb and libwinpthread, which it
loads) into ``third_party/hackrf``, which is searched first. ``HACKRF_LIB`` names
a library explicitly.

A HackRF has no cyclic buffer in the device, unlike the Pluto. The frame is looped
on the host: libhackrf calls back for every USB transfer, and the callback copies
the next stretch of the frame into it, wrapping around at the end. The frame tiles
seamlessly (continuous phase), so the loop is clean.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .i18n import t

HACKRF_SUCCESS = 0
HACKRF_TRUE = 1

# HackRF One hardware limits
FREQ_MIN_HZ = 1e6
FREQ_MAX_HZ = 6e9
FS_MIN_HZ = 2e6
FS_MAX_HZ = 20e6
TXVGA_MAX_DB = 47        # TX IF amplifier, 0..47 dB in 1 dB steps
AMP_GAIN_DB = 14         # RF amplifier, on or off
BB_FILTER_MAX_HZ = 28e6  # widest baseband filter (MAX2837)

#: where scripts/fetch_hackrf.py puts the Windows DLLs
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "third_party", "hackrf")


class _HackrfTransfer(ctypes.Structure):
    _fields_ = [
        ("device", ctypes.c_void_p),
        ("buffer", ctypes.POINTER(ctypes.c_uint8)),
        ("buffer_length", ctypes.c_int),
        ("valid_length", ctypes.c_int),
        ("rx_ctx", ctypes.c_void_p),
        ("tx_ctx", ctypes.c_void_p),
    ]


SAMPLE_BLOCK_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_HackrfTransfer))


class _PartIdSerial(ctypes.Structure):
    _fields_ = [("part_id", ctypes.c_uint32 * 2), ("serial_no", ctypes.c_uint32 * 4)]


_BOARD_NAMES = {0: "Jellybean", 1: "Jawbreaker", 2: "HackRF One (r1–r8)",
                3: "rad1o", 4: "HackRF One (r9)", 5: "HackRF One"}

_lib = None


def _candidate_paths() -> List[str]:
    out: List[str] = []
    env = os.environ.get("HACKRF_LIB")
    if env:
        out.append(env)
    if sys.platform.startswith("win"):
        out += [os.path.join(BUNDLED_DIR, n) for n in ("hackrf-0.dll", "hackrf.dll")]
    found = ctypes.util.find_library("hackrf") or ctypes.util.find_library("hackrf-0")
    if found:
        out.append(found)
    if sys.platform.startswith("win"):
        for base in (r"C:\Program Files\PothosSDR\bin",
                     os.path.expanduser(r"~\radioconda\Library\bin"),
                     r"C:\ProgramData\radioconda\Library\bin"):
            out += [os.path.join(base, n) for n in ("hackrf.dll", "hackrf-0.dll")]
    elif sys.platform == "darwin":
        out += ["libhackrf.dylib", "/opt/homebrew/lib/libhackrf.dylib",
                "/usr/local/lib/libhackrf.dylib"]
    else:
        out += ["libhackrf.so.0", "libhackrf.so"]
    return out


def load_lib():
    """Load libhackrf and declare its signatures (once per process)."""
    global _lib
    if _lib is not None:
        return _lib
    lib = None
    for path in _candidate_paths():
        try:
            lib = ctypes.CDLL(path)
            break
        except OSError:
            continue
    if lib is None:
        raise RuntimeError(t(
            "libhackrf was not found. Windows: run «python scripts/fetch_hackrf.py» "
            "(it puts the library into third_party/hackrf) or install PothosSDR / "
            "radioconda. Linux: apt install libhackrf0. Or set HACKRF_LIB to the "
            "full path of the library."))

    dev_pp = ctypes.POINTER(ctypes.c_void_p)
    sigs = {
        "hackrf_init": ([], ctypes.c_int),
        "hackrf_open": ([dev_pp], ctypes.c_int),
        "hackrf_open_by_serial": ([ctypes.c_char_p, dev_pp], ctypes.c_int),
        "hackrf_close": ([ctypes.c_void_p], ctypes.c_int),
        "hackrf_set_sample_rate": ([ctypes.c_void_p, ctypes.c_double], ctypes.c_int),
        "hackrf_set_baseband_filter_bandwidth": ([ctypes.c_void_p, ctypes.c_uint32],
                                                 ctypes.c_int),
        "hackrf_compute_baseband_filter_bw": ([ctypes.c_uint32], ctypes.c_uint32),
        "hackrf_set_freq": ([ctypes.c_void_p, ctypes.c_uint64], ctypes.c_int),
        "hackrf_set_txvga_gain": ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
        "hackrf_set_amp_enable": ([ctypes.c_void_p, ctypes.c_uint8], ctypes.c_int),
        "hackrf_start_tx": ([ctypes.c_void_p, SAMPLE_BLOCK_CB, ctypes.c_void_p],
                            ctypes.c_int),
        "hackrf_stop_tx": ([ctypes.c_void_p], ctypes.c_int),
        "hackrf_is_streaming": ([ctypes.c_void_p], ctypes.c_int),
        "hackrf_error_name": ([ctypes.c_int], ctypes.c_char_p),
        "hackrf_board_id_read": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8)],
                                 ctypes.c_int),
        "hackrf_version_string_read": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint8],
                                       ctypes.c_int),
        "hackrf_board_partid_serialno_read": ([ctypes.c_void_p,
                                               ctypes.POINTER(_PartIdSerial)],
                                              ctypes.c_int),
    }
    for name, (argtypes, restype) in sigs.items():
        fn = getattr(lib, name)
        fn.argtypes = argtypes
        fn.restype = restype

    _check(lib, lib.hackrf_init(), "hackrf_init")
    _lib = lib
    return lib


def _check(lib, rc: int, what: str) -> None:
    if rc != HACKRF_SUCCESS:
        try:
            name = lib.hackrf_error_name(rc).decode(errors="replace")
        except Exception:  # pragma: no cover
            name = str(rc)
        raise RuntimeError(f"{what}: {name} ({rc})")


def split_gain(gain_db: float, amp: bool) -> Tuple[int, bool]:
    """The -89..0 slider (Pluto convention, 0 = maximum) as (txvga_db, amp_on).

    One slider dB is one dB on air, counted down from the maximum: without the
    amplifier 0..-47 maps onto TXVGA 47..0; with it 0..-61 covers TXVGA plus the
    14 dB amplifier, which is switched on only when more than 47 dB is asked for.
    Below that the hardware minimum is used. SoapySink stretches the whole slider
    over the device's range instead, so -10 there is not -10 here.
    """
    total_max = TXVGA_MAX_DB + (AMP_GAIN_DB if amp else 0)
    total = int(round(max(0.0, min(total_max, total_max + float(gain_db)))))
    if amp and total > TXVGA_MAX_DB:
        return total - AMP_GAIN_DB, True
    return min(total, TXVGA_MAX_DB), False


def int16_iq_to_int8_interleaved(iq_int16: np.ndarray, scale_in: float = 2 ** 14) -> np.ndarray:
    """complex IQ on the int16 scale (fm.to_int16_iq peaks at ~2^14) -> int8 I,Q,I,Q…"""
    k = 127.0 / scale_in
    out = np.empty(iq_int16.size * 2, dtype=np.int8)
    out[0::2] = np.clip(np.round(iq_int16.real * k), -127, 127).astype(np.int8)
    out[1::2] = np.clip(np.round(iq_int16.imag * k), -127, 127).astype(np.int8)
    return out


class CyclicFeeder:
    """Loops an int8 frame seamlessly into libhackrf's transfer buffers."""

    def __init__(self, data: np.ndarray):
        self.set_data(data)

    def set_data(self, data: np.ndarray) -> None:
        data = np.ascontiguousarray(data, dtype=np.int8)
        if data.size == 0 or data.size % 2:
            raise ValueError("empty or odd-length IQ buffer")
        # One tuple, swapped in one assignment: the callback sees either the old
        # frame or the new one, never half of each.
        self._state = (data, data.ctypes.data, data.size)
        self._pos = 0

    def fill(self, dst_addr: int, length: int) -> None:
        _data, src, n = self._state      # _data keeps the array alive for the copy
        pos = self._pos if self._pos < n else 0
        off = 0
        while off < length:
            chunk = min(length - off, n - pos)
            ctypes.memmove(dst_addr + off, src + pos, chunk)
            off += chunk
            pos += chunk
            if pos >= n:
                pos = 0
        self._pos = pos


class HackRFDevice:
    """A thin wrapper around one opened HackRF."""

    def __init__(self, serial: Optional[str] = None):
        self.lib = load_lib()
        self._dev = ctypes.c_void_p()
        if serial:
            rc = self.lib.hackrf_open_by_serial(serial.encode(), ctypes.byref(self._dev))
        else:
            rc = self.lib.hackrf_open(ctypes.byref(self._dev))
        if rc != HACKRF_SUCCESS:
            raise RuntimeError(t(
                "HackRF could not be opened ({err}). It is unplugged, held by another "
                "program, or stuck in transmit after a run that was killed — press "
                "RESET on the board (or replug it) and try again.",
                err=self.lib.hackrf_error_name(rc).decode(errors="replace")))
        self._cb = None
        self.streaming = False

    def _check(self, rc: int, what: str) -> None:
        _check(self.lib, rc, what)

    def set_sample_rate(self, fs: float, filter_hz: Optional[float] = None) -> float:
        """Set the rate and the baseband filter; return the filter actually set."""
        self._check(self.lib.hackrf_set_sample_rate(self._dev, float(fs)), "set_sample_rate")
        want = min(float(filter_hz or 0.75 * fs), BB_FILTER_MAX_HZ)
        bw = self.lib.hackrf_compute_baseband_filter_bw(int(want))
        self._check(self.lib.hackrf_set_baseband_filter_bandwidth(self._dev, bw),
                    "set_baseband_filter_bandwidth")
        return float(bw)

    def set_freq(self, freq_hz: float) -> None:
        self._check(self.lib.hackrf_set_freq(self._dev, int(freq_hz)), "set_freq")

    def set_gain(self, txvga_db: int, amp_on: bool) -> None:
        self._check(self.lib.hackrf_set_txvga_gain(self._dev, int(txvga_db)),
                    "set_txvga_gain")
        self._check(self.lib.hackrf_set_amp_enable(self._dev, 1 if amp_on else 0),
                    "set_amp_enable")

    def start_tx(self, feeder: CyclicFeeder) -> None:
        def _cb(transfer_p):
            tr = transfer_p.contents
            feeder.fill(ctypes.addressof(tr.buffer.contents), tr.buffer_length)
            tr.valid_length = tr.buffer_length
            return 0

        # keep a reference: a collected CFUNCTYPE object crashes the next callback
        self._cb = SAMPLE_BLOCK_CB(_cb)
        self._check(self.lib.hackrf_start_tx(self._dev, self._cb, None), "start_tx")
        self.streaming = True

    def is_streaming(self) -> bool:
        """False once libhackrf has given up on the stream (USB error, unplug)."""
        return self.lib.hackrf_is_streaming(self._dev) == HACKRF_TRUE

    def stop_tx(self) -> None:
        if self.streaming:
            self.streaming = False
            self._check(self.lib.hackrf_stop_tx(self._dev), "stop_tx")

    def board_info(self) -> Dict[str, str]:
        info: Dict[str, str] = {}
        bid = ctypes.c_uint8()
        if self.lib.hackrf_board_id_read(self._dev, ctypes.byref(bid)) == HACKRF_SUCCESS:
            info["board"] = _BOARD_NAMES.get(bid.value, f"id {bid.value}")
        ver = ctypes.create_string_buffer(255)
        if self.lib.hackrf_version_string_read(self._dev, ver, 255) == HACKRF_SUCCESS:
            info["firmware"] = ver.value.decode(errors="replace")
        ps = _PartIdSerial()
        if self.lib.hackrf_board_partid_serialno_read(self._dev,
                                                      ctypes.byref(ps)) == HACKRF_SUCCESS:
            info["serial"] = "".join(f"{x:08x}" for x in ps.serial_no).lstrip("0") or "0"
        return info

    def close(self) -> None:
        try:
            self.stop_tx()
        finally:
            if self._dev:
                self.lib.hackrf_close(self._dev)
                self._dev = ctypes.c_void_p()


# ---------------------------------------------------------------------------
#  Probe
# ---------------------------------------------------------------------------
@dataclass
class HackRFProbeResult:
    connected: bool
    info: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def inferred_preset(self) -> Optional[str]:
        """The «HW range» preset to select — same role as ProbeResult's."""
        return "hackrf" if self.connected else None

    def summary(self) -> str:
        if not self.connected:
            return t("HackRF not found: {err}", err=self.error or "?")
        lines = [t("HackRF connected")]
        for key in ("board", "firmware", "serial"):
            if key in self.info:
                lines.append(f"  {key}: {self.info[key]}")
        lines.append(t("  TX: {fmin}–{fmax} MHz, {smin}–{smax} MSPS, 8-bit IQ",
                       fmin=f"{FREQ_MIN_HZ/1e6:.0f}", fmax=f"{FREQ_MAX_HZ/1e6:.0f}",
                       smin=f"{FS_MIN_HZ/1e6:.0f}", smax=f"{FS_MAX_HZ/1e6:.0f}"))
        lines.append(t("  Direct 5.8 GHz: YES (output power there is low)"))
        return "\n".join(lines)


def probe_hackrf(serial: Optional[str] = None) -> HackRFProbeResult:
    res = HackRFProbeResult(connected=False)
    try:
        dev = HackRFDevice(serial)
    except Exception as exc:
        res.error = str(exc)
        return res
    try:
        res.connected = True
        res.info = dev.board_info()
    finally:
        dev.close()
    return res
