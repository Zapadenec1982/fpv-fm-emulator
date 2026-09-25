"""What PlutoSink does when it opens a board, against a fake one.

tests/test_firmware.py covers identify() and mismatch_warning() as pure functions,
and tests/test_backends.py covers apply_sample_rate — but nothing connected any of
them to the sink, so four separate mutations of _ensure_open passed the whole
suite: dropping the identification, warning on every arming retry instead of once,
never reporting that the FIR was disabled, and ignoring cfg.firmware altogether.

The warnings matter as much as the values: warnings.warn is the only route these
messages have to the GUI log (through the showwarning hook installed in
gui/app.py), and under pythonw there is no stderr to fall back on.
"""
import os
import sys
import types
import warnings
import weakref

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpv_emulator import backends
from fpv_emulator.backends import PlutoSink, TxConfig
from fpv_emulator.firmware import ADI, AUTO, TEZUKA
from fpv_emulator.iio_layout import STOCK_PHY, STOCK_RX, STOCK_TX

RATES = "[2083333 1 61440000]"


# --------------------------- a board made of dictionaries -------------------
class FakeAttr:
    def __init__(self, value):
        self.value = str(value)


class FakeChan:
    def __init__(self, cid, output, scan_element=False, attrs=None):
        self.id = cid
        self.output = output
        self.scan_element = scan_element
        self.attrs = attrs or {}


class FakeDev:
    def __init__(self, name, channels):
        self.name = self.id = name
        self.channels = list(channels)


class FakeCtx:
    def __init__(self, attrs, devices):
        self.attrs = attrs
        self.devices = list(devices)

    def find_device(self, name):
        for dev in self.devices:
            if dev.name == name:
                return dev
        raise KeyError(name)


class Board:
    """One fake Pluto. ``takes_fir`` is the thing the two real boards differ on."""

    current = None            # the board the fake `adi`/`iio` modules hand out

    def __init__(self, fw_version="v0.35", takes_fir=True, rates=RATES):
        self.takes_fir = takes_fir
        self.pyadi_attempts = 0
        self.opens = 0
        self.contexts_open_at_pyadi = None   # inspection contexts alive when pyadi opened
        self.settings = {}
        phy = FakeDev(STOCK_PHY, [
            FakeChan("voltage0", output=False, attrs={"sampling_frequency": FakeAttr(3000000)}),
            FakeChan("voltage0", output=True, attrs={
                "sampling_frequency_available": FakeAttr(rates),
                # 40 MHz, not the 56 MHz often quoted for these chips: that is the RX path.
        "rf_bandwidth_available": FakeAttr("[200000 1 40000000]"),
                "hardwaregain_available": FakeAttr("[-89.750000 0.250000 0.000000]"),
            }),
            FakeChan("out", output=False, attrs={"voltage_filter_fir_en": FakeAttr(1)}),
        ])
        dma = lambda name, out: FakeDev(name, [FakeChan("voltage0", out, scan_element=True)])
        self.ctx = FakeCtx({"fw_version": fw_version, "hw_model": "PlutoSDR"},
                           [phy, dma(STOCK_RX, False), dma(STOCK_TX, True)])

    # what the phy currently holds, read back the way the code writes it
    def rate(self):
        for ch in self.ctx.find_device(STOCK_PHY).channels:
            if ch.id == "voltage0" and not ch.output:
                return float(ch.attrs["sampling_frequency"].value)

    def fir_enabled(self):
        for ch in self.ctx.find_device(STOCK_PHY).channels:
            if ch.id == "out":
                return ch.attrs["voltage_filter_fir_en"].value == "1"


class OpenCtx:
    """What iio.Context(uri) hands out: the board's context, opened.

    libiio frees a board when the last reference to its context goes — which is
    what lets a USB board be opened again — so openness is tracked the same way
    here: by reference, not by an explicit close.
    """
    alive = weakref.WeakSet()

    def __init__(self, board):
        self.attrs = board.ctx.attrs
        self.devices = board.ctx.devices
        OpenCtx.alive.add(self)


class FakePluto:
    def __init__(self, uri=None):
        self.board = Board.current
        self.board.opens += 1
        self.board.contexts_open_at_pyadi = len(OpenCtx.alive)
        self.ctx = self.board.ctx
        self.uri = uri

    @property
    def sample_rate(self):
        return self.board.rate()

    @sample_rate.setter
    def sample_rate(self, value):
        self.board.pyadi_attempts += 1
        if not self.board.takes_fir:
            raise OSError(22, "Invalid argument")
        for ch in self.ctx.find_device(STOCK_PHY).channels:
            if ch.id == "voltage0" and not ch.output:
                ch.attrs["sampling_frequency"].value = str(int(value))

    def __setattr__(self, name, value):
        if name in ("tx_lo", "tx_rf_bandwidth", "tx_hardwaregain_chan0", "tx_cyclic_buffer"):
            self.board.settings[name] = value
            return
        super().__setattr__(name, value)


@pytest.fixture
def board(monkeypatch):
    """Install fake `adi` and `iio`. Both are imported inside _ensure_open, so
    patching sys.modules is enough — no hardware and no libiio anywhere near this."""
    b = Board()
    Board.current = b
    monkeypatch.setitem(sys.modules, "adi", types.SimpleNamespace(Pluto=FakePluto))
    monkeypatch.setitem(sys.modules, "iio", types.SimpleNamespace(
        Context=lambda uri: OpenCtx(Board.current), scan_contexts=lambda: {}))
    monkeypatch.setattr(backends.time, "sleep", lambda *_a, **_k: None)
    yield b
    Board.current = None


def _sink(fs=20e6, firmware=AUTO):
    return PlutoSink(TxConfig(fs=fs, freq_hz=1200e6, gain_db=-30.0, firmware=firmware))


def _open(sink):
    """Open, collecting whatever was warned. Returns (messages, sink)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sink._ensure_open()
    return [str(w.message) for w in caught]


# --------------------------- identification ---------------------------------
def test_the_board_is_identified_when_it_is_opened(board):
    sink = _sink()
    _open(sink)
    assert "v0.35" in sink.info()["firmware"]
    assert sink.info()["firmware_profile"] == AUTO


def test_identification_borrows_the_context_and_does_not_open_a_second_device(board):
    """Two live contexts on one device is the EBUSY that costs a USB re-plug."""
    _open(_sink())
    assert board.opens == 1


def test_the_inspection_context_is_closed_before_pyadi_opens_its_own(board):
    """Over USB a board takes ONE context at a time. The one opened to identify
    the board was still alive when pyadi opened its own, which then failed with
    a bare "No device found" — nothing could transmit over usb:. A network link
    takes two, so it went unseen."""
    _open(_sink())
    assert board.contexts_open_at_pyadi == 0


# --------------------------- a board that does not open ---------------------
def _refuse(uri):
    raise OSError(0, "No error")       # what Windows libiio says for every failure


def test_a_board_that_does_not_answer_is_described_not_handed_to_pyadi(board, monkeypatch):
    """pyadi would repeat the very same open, wait out the same timeout, and
    report "No device found"."""
    monkeypatch.setitem(sys.modules, "iio",
                        types.SimpleNamespace(Context=_refuse, scan_contexts=lambda: {}))
    with pytest.raises(RuntimeError) as exc:
        _open(_sink())
    assert board.opens == 0
    assert "192.168.2.1" in str(exc.value) and "Errno 0" not in str(exc.value)


def test_a_refusal_from_pyadi_is_described_too(board, monkeypatch):
    """The board answered the inspection and then would not open for pyadi —
    e.g. something else took it in between."""
    class Refuses(FakePluto):
        def __init__(self, uri=None):
            raise Exception("No device found")
    monkeypatch.setitem(sys.modules, "adi", types.SimpleNamespace(Pluto=Refuses))
    with pytest.raises(RuntimeError) as exc:
        _open(_sink())
    assert "192.168.2.1" in str(exc.value) and "No device found" not in str(exc.value)


# --------------------------- the mismatch notice ----------------------------
def test_a_profile_contradicting_the_board_warns(board):
    notes = _open(_sink(firmware=TEZUKA))
    assert any("Tezuka" in n and "Analog Devices" in n for n in notes)


def test_auto_never_warns_about_a_mismatch(board):
    assert not [n for n in _open(_sink()) if "profile is set to" in n]


def test_the_mismatch_is_announced_once_not_once_per_arming_retry(board):
    """_arm() retries up to three times and _reopen() comes back through
    _ensure_open each time; a notice per retry pushes the rest of the log away."""
    sink = _sink(firmware=TEZUKA)
    first = _open(sink)
    sink._sdr = None                       # what _reopen() leaves behind
    second = _open(sink)
    assert len([n for n in first if "profile is set to" in n]) == 1
    assert not [n for n in second if "profile is set to" in n]


# --------------------------- the profile reaches the board ------------------
def test_a_board_that_takes_the_filter_is_left_on_the_path_that_works(board):
    """The Pluto+ path. It works today; nothing here may move it."""
    notes = _open(_sink())
    assert board.pyadi_attempts == 1 and board.rate() == 20e6
    assert board.fir_enabled() and not notes


def test_the_profile_is_passed_through_not_defaulted_to_auto(board):
    board.takes_fir = True                 # would succeed if auto were used
    _open(_sink(firmware=TEZUKA))
    assert board.pyadi_attempts == 0, "cfg.firmware was ignored"
    assert not board.fir_enabled() and board.rate() == 20e6


def test_dropping_the_filter_is_reported_to_the_operator(board):
    """Under pythonw this warning is the only way the operator ever learns of it."""
    board.takes_fir = False
    notes = _open(_sink())
    assert board.rate() == 20e6 and not board.fir_enabled()
    assert any("20.00" in n and "refused" in n for n in notes)


def test_a_forced_adi_profile_fails_instead_of_falling_back(board):
    board.takes_fir = False
    with pytest.raises(RuntimeError) as exc:
        _open(_sink(firmware=ADI))
    assert "Analog Devices" in str(exc.value) and RATES in str(exc.value)
    assert board.fir_enabled(), "it fell back despite the profile"


# --------------------------- the rest of the open ---------------------------
def test_the_carrier_and_power_are_applied_after_the_rate(board):
    _open(_sink())
    assert board.settings["tx_lo"] == 1200000000
    assert board.settings["tx_hardwaregain_chan0"] == -30.0
    assert board.settings["tx_cyclic_buffer"] is True


def test_a_rate_outside_the_published_range_is_refused_before_anything_is_written(board):
    """It must not half-configure the board: the buffer is generated for the rate
    that was asked for, and a substituted one radiates a picture the detector
    cannot see."""
    with pytest.raises(RuntimeError) as exc:
        _open(_sink(fs=80e6))
    assert "61.44" in str(exc.value)
    assert board.pyadi_attempts == 0 and "tx_lo" not in board.settings


def test_the_fir_notice_is_not_repeated_on_every_reopen(board):
    """Same reasoning as the mismatch notice, and the GUI cannot rely on warning
    de-duplication — it turns that off so a repeated aliasing warning gets through."""
    board.takes_fir = False
    sink = _sink()
    first = _open(sink)
    sink._sdr = None
    second = _open(sink)
    assert len([n for n in first if "refused" in n]) == 1
    assert not [n for n in second if "refused" in n]


def test_a_notice_that_changes_still_gets_through(board):
    """Suppressing repeats must not suppress news: a board that stops taking the
    filter between opens is exactly what the operator needs to hear about."""
    sink = _sink()
    assert not _open(sink)
    board.takes_fir = False
    sink._sdr = None
    assert any("refused" in n for n in _open(sink))


# --------------------------- the transmit filter ---------------------------
def test_the_filter_width_comes_from_the_signal_not_the_sample_rate(board):
    """It was min(fs, 20e6), which has nothing to do with how wide the signal is."""
    sink = PlutoSink(TxConfig(fs=30.72e6, freq_hz=5769e6, gain_db=-6.0,
                              rf_bw_hz=27.65e6))
    _open(sink)
    assert board.settings["tx_rf_bandwidth"] == 27650000
    assert sink.info()["rf_bw_hz"] == 27.65e6


def test_a_filter_wider_than_the_board_allows_is_clamped_and_said_out_loud(board):
    """Clamped, not refused: nothing in the buffer depends on the analog filter,
    unlike the sample rate, where a substitution changes the line rate."""
    sink = PlutoSink(TxConfig(fs=61.44e6, freq_hz=5769e6, gain_db=-6.0,
                              rf_bw_hz=52e6))
    notes = _open(sink)
    assert board.settings["tx_rf_bandwidth"] == 40000000
    assert any("40.00" in n and "52.00" in n for n in notes), notes


def test_a_filter_the_board_allows_passes_silently(board):
    sink = PlutoSink(TxConfig(fs=61.44e6, freq_hz=5769e6, gain_db=-6.0, rf_bw_hz=40e6))
    assert not [n for n in _open(sink) if "transmit filter" in n]
