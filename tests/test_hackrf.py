"""backend=hackrf without a board: gain mapping, 8-bit conversion, the host-side
frame loop, and the sink's contract with the scenario engine.

The device is replaced by a fake that records what the sink asked of it; libhackrf
itself is never loaded here.
"""
import ctypes
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpv_emulator import hackrf as hk
from fpv_emulator.backends import HackRFSink, TxConfig, make_sink
from fpv_emulator.fm import to_int16_iq


# ------------------------------- gain --------------------------------------
@pytest.mark.parametrize("gain, amp, expected", [
    (0, False, (47, False)),
    (-10, False, (37, False)),
    (-47, False, (0, False)),
    (-89, False, (0, False)),       # below the hardware minimum
    (5, False, (47, False)),        # above the maximum
    (0, True, (47, True)),          # 61 = 47 + 14
    (-10, True, (37, True)),        # 51 = 37 + 14
    (-14, True, (47, False)),       # exactly 47: TXVGA alone, amplifier stays off
    (-20, True, (41, False)),
    (-61, True, (0, False)),
])
def test_one_slider_db_is_one_db_from_the_maximum(gain, amp, expected):
    assert hk.split_gain(gain, amp) == expected


# --------------------------- 8-bit conversion ------------------------------
def test_a_full_scale_frame_uses_the_full_int8_range():
    iq = np.exp(1j * np.linspace(0, 2 * np.pi, 64, endpoint=False))
    i8 = hk.int16_iq_to_int8_interleaved(to_int16_iq(iq))
    assert i8.dtype == np.int8 and i8.size == 128
    assert np.abs(i8).max() == 127
    assert (i8[0], i8[1]) == (127, 0)           # 1+0j, interleaved I then Q


# ------------------------------ frame loop ---------------------------------
def _pull(feeder, sizes):
    out = bytearray()
    for n in sizes:
        buf = (ctypes.c_uint8 * n)()
        feeder.fill(ctypes.addressof(buf), n)
        out += bytes(buf)
    return bytes(out)


def test_the_frame_loops_seamlessly_across_any_transfer_size():
    data = np.arange(10, dtype=np.int8)
    got = _pull(hk.CyclicFeeder(data), (4, 7, 13, 6))
    assert got == np.tile(data, 4)[: len(got)].astype(np.uint8).tobytes()


def test_a_new_frame_starts_from_its_beginning():
    feeder = hk.CyclicFeeder(np.arange(10, dtype=np.int8))
    _pull(feeder, (6,))
    feeder.set_data(np.full(4, 9, dtype=np.int8))
    assert _pull(feeder, (6,)) == bytes([9] * 6)


def test_an_empty_frame_is_refused():
    with pytest.raises(ValueError):
        hk.CyclicFeeder(np.zeros(0, dtype=np.int8))


# --------------------------------- sink ------------------------------------
class FakeDevice:
    """Stands in for hk.HackRFDevice and records every call."""

    def __init__(self, serial=None):
        self.serial = serial
        self.calls = []
        self.streaming = False
        self.alive = True
        self.closed = False

    def set_sample_rate(self, fs, filter_hz=None):
        self.calls.append(("fs", fs, filter_hz))
        return min(filter_hz or 0.75 * fs, hk.BB_FILTER_MAX_HZ)

    def set_freq(self, f):
        self.calls.append(("freq", f))

    def set_gain(self, vga, amp_on):
        self.calls.append(("gain", vga, amp_on))

    def start_tx(self, feeder):
        self.feeder = feeder
        self.streaming = True
        self.calls.append(("start",))

    def is_streaming(self):
        return self.alive

    def stop_tx(self):
        self.streaming = False
        self.calls.append(("stop",))

    def close(self):
        self.stop_tx()
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    made = []

    def _factory(serial=None):
        made.append(FakeDevice(serial))
        return made[-1]

    monkeypatch.setattr(hk, "HackRFDevice", _factory)
    return made


def _frame():
    return to_int16_iq(np.exp(1j * np.linspace(0, 2 * np.pi, 100, endpoint=False)))


def test_a_rate_the_hackrf_cannot_do_is_refused_up_front():
    with pytest.raises(RuntimeError, match="HackRF"):
        make_sink("hackrf", TxConfig(fs=30.72e6, freq_hz=5.8e9))


def test_the_sink_opens_with_the_configured_rate_frequency_gain_and_serial(fake):
    sink = make_sink("hackrf", TxConfig(fs=20e6, freq_hz=5658e6, gain_db=-10,
                                        rf_bw_hz=17e6, serial="abc", amp=True))
    assert isinstance(sink, HackRFSink)
    sink.start(_frame())
    dev = fake[0]
    assert dev.serial == "abc"
    assert ("fs", 20e6, 17e6) in dev.calls
    assert ("freq", 5658e6) in dev.calls
    assert ("gain", 37, True) in dev.calls
    assert sink.running and dev.streaming


def test_start_while_on_air_swaps_the_frame_instead_of_a_second_stream(fake):
    sink = make_sink("hackrf", TxConfig(fs=20e6, freq_hz=1.2e9))
    sink.start(_frame())
    sink.start(_frame())
    assert [c for c in fake[0].calls if c[0] == "start"] == [("start",)]
    assert len(fake) == 1


def test_live_retune_and_power_reach_the_open_device(fake):
    sink = make_sink("hackrf", TxConfig(fs=20e6, freq_hz=1.2e9, gain_db=-30))
    sink.start(_frame())
    sink.set_freq(4.5e9)
    sink.set_gain(0)
    assert fake[0].calls[-2:] == [("freq", 4.5e9), ("gain", 47, False)]


def test_a_too_wide_signal_is_clamped_to_the_widest_filter_and_said(fake):
    sink = make_sink("hackrf", TxConfig(fs=20e6, freq_hz=5.8e9, rf_bw_hz=35e6))
    with pytest.warns(UserWarning, match="HackRF allows up to 28"):
        sink.start(_frame())


def test_a_stream_that_died_is_reported_and_not_left_looking_armed(fake):
    sink = make_sink("hackrf", TxConfig(fs=20e6, freq_hz=5.8e9))
    sink.start(_frame())
    assert sink.poll_error() is None
    fake[0].alive = False                     # libhackrf gave up (USB error)
    err = sink.poll_error()
    assert err is not None and "nothing is on air" in str(err)
    assert not sink.running


def test_stop_switches_rf_off_and_close_releases_the_device(fake):
    sink = make_sink("hackrf", TxConfig(fs=20e6, freq_hz=5.8e9))
    sink.start(_frame())
    sink.stop()
    assert not sink.running and not fake[0].streaming
    sink.close()
    assert fake[0].closed


def test_a_device_that_fails_to_configure_is_released(fake, monkeypatch):
    def broken(self, f):
        raise RuntimeError("set_freq: HACKRF_ERROR_INVALID_PARAM (-2)")
    monkeypatch.setattr(FakeDevice, "set_freq", broken)
    sink = make_sink("hackrf", TxConfig(fs=20e6, freq_hz=7e9))
    with pytest.raises(RuntimeError):
        sink.start(_frame())
    assert fake[0].closed


def test_the_probe_reports_a_missing_board_in_words(monkeypatch):
    def nothing(serial=None):
        raise RuntimeError("HackRF could not be opened (HACKRF_ERROR_NOT_FOUND)")
    monkeypatch.setattr(hk, "HackRFDevice", nothing)
    res = hk.probe_hackrf()
    assert not res.connected and res.inferred_preset is None
    assert "NOT_FOUND" in res.summary()
