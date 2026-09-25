"""What the probe reports — against stand-in modules, no hardware and no libiio.

The probe is what an operator runs first when something does not work, so how
it fails matters more than how it succeeds. Two of its failures pointed at the
wrong thing: a missing library was reported as "Pluto not found", and over USB
the range check vanished without a word.
"""
import os
import sys
import types
import weakref

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpv_emulator import iio_host
from fpv_emulator.i18n import get_language, set_language
from fpv_emulator.iio_layout import STOCK_PHY, STOCK_RX, STOCK_TX
from fpv_emulator.probe import probe

PLUTO_ON_USB = {"usb:1.6.5": "0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), "
                             "serial=03df6412741b1c5933d76c1a7c0304412b"}


class Chan:
    def __init__(self, output):
        self.output = output
        self.scan_element = True


class Dev:
    def __init__(self, name, channels=()):
        self.name = self.id = name
        self.channels = list(channels)


class Ctx:
    """An open context. libiio frees a board when the last reference to its
    context goes, so that is what is tracked: which ones are still referenced."""

    alive = weakref.WeakSet()
    attrs = {"hw_model": "Analog Devices PlutoSDR Rev.B (Z7010-AD9363A)",
             "hw_serial": "03df6412741b1c5933d76c1a7c0304412b", "fw_version": "v0.39"}
    version = (0, 25, "b6028fd")

    def __init__(self, uri):
        self.devices = [Dev(STOCK_PHY), Dev(STOCK_RX, [Chan(False)]),
                        Dev(STOCK_TX, [Chan(True)])]
        Ctx.alive.add(self)


class Ad9363:
    """pyadi's Pluto for the range check: tunes 325 MHz – 3.8 GHz, as the chip does."""

    contexts_alive_at_open = None

    def __init__(self, uri=None):
        Ad9363.contexts_alive_at_open = len(Ctx.alive)
        self._lo = 2.4e9

    @property
    def tx_lo(self):
        return self._lo

    @tx_lo.setter
    def tx_lo(self, value):
        if 325e6 <= value <= 3.8e9:
            self._lo = value


def _iio(**overrides):
    fields = dict(Context=Ctx, version=(0, 25, "b6028fd"), scan_contexts=lambda: {})
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


@pytest.fixture
def bench(monkeypatch):
    saved = get_language()
    set_language("en")
    Ad9363.contexts_alive_at_open = None
    monkeypatch.setitem(sys.modules, "iio", _iio())
    monkeypatch.setitem(sys.modules, "adi", types.SimpleNamespace(Pluto=Ad9363))
    yield monkeypatch
    set_language(saved)


def _refuse(uri):
    raise OSError(0, "No error")       # what Windows libiio says for every failure


# --------------------------- a board that answers ---------------------------
def test_a_board_that_answers_gets_its_range(bench):
    res = probe("usb:1.6.5")
    assert res.connected and res.inferred_preset == "stock"
    assert (res.tx_lo_min_hz, res.tx_lo_max_hz) == (325e6, 3.8e9)


def test_the_range_check_opens_the_board_only_after_the_first_context_is_gone(bench):
    """Over USB a board takes one context at a time. With step 1's still open,
    pyadi failed with "No device found" and the range lines were simply missing."""
    probe("usb:1.6.5")
    assert Ad9363.contexts_alive_at_open == 0


def test_a_failed_range_check_is_shown_not_swallowed(bench):
    """The summary of a connected board never showed an error, so this one was
    invisible — the operator saw a clean result with two lines missing."""
    class Refuses(Ad9363):
        def __init__(self, uri=None):
            raise Exception("No device found")
    bench.setitem(sys.modules, "adi", types.SimpleNamespace(Pluto=Refuses))
    res = probe("usb:1.6.5")
    assert res.connected
    assert "TX range check failed: Exception: No device found" in res.summary()


# --------------------------- a board that does not ---------------------------
def test_a_board_that_does_not_answer_is_described_in_words(bench):
    bench.setitem(sys.modules, "iio",
                  _iio(Context=_refuse, scan_contexts=lambda: dict(PLUTO_ON_USB)))
    res = probe("ip:192.168.2.1")
    text = res.summary()
    assert not res.connected
    assert "nothing answers at 192.168.2.1" in text and "usb:1.6.5" in text
    assert "Errno 0" not in text and "Traceback" not in text


def test_a_board_that_does_not_answer_is_not_asked_twice(bench):
    """pyadi would make the same call, and wait out the same timeout again."""
    bench.setitem(sys.modules, "iio", _iio(Context=_refuse))
    probe("ip:192.168.2.1")
    assert Ad9363.contexts_alive_at_open is None


# --------------------------- this computer -----------------------------------
def test_a_missing_library_is_not_reported_as_a_missing_board(bench):
    """"Pluto not found" sent the operator to check a cable that was fine."""
    def no_library():
        raise iio_host.HostLibraryError("The libiio library is not installed on this computer.")
    bench.setattr(iio_host, "import_iio", no_library)
    res = probe()
    text = res.summary()
    assert not res.connected
    assert "not found" not in text and "this computer" in text
    assert "The libiio library is not installed" in text


def test_a_missing_iio_module_says_to_install_it(bench):
    bench.setitem(sys.modules, "iio", None)
    text = probe().summary()
    assert "pip install" in text and "not found" not in text
