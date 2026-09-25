"""A missing libiio, and a board that does not open, must read as sentences.

Both reached the operator as noise. A freshly installed Windows without libiio
printed "TypeError: LoadLibrary() argument 1 must be str, not None" under the
heading "Pluto not found", and a board whose USB network adapter had failed to
start read "OSError: [Errno 0] No error" — while it sat on USB, reachable the
whole time as usb:.

Everything here runs against stand-in modules: no libiio, no board.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpv_emulator import iio_host
from fpv_emulator.i18n import get_language, set_language

#: what libiio 0.25's scan returned for the board on the bench
PLUTO_ON_USB = {"usb:1.6.5": "0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), "
                             "serial=03df6412741b1c5933d76c1a7c0304412b"}
SILENT = OSError(0, "No error")        # Windows libiio, whatever went wrong


@pytest.fixture(autouse=True)
def english():
    saved = get_language()
    set_language("en")
    yield
    set_language(saved)


def _usb_bus(monkeypatch, found):
    """An iio module whose scan finds ``found`` on the bus."""
    monkeypatch.setitem(sys.modules, "iio",
                        types.SimpleNamespace(scan_contexts=lambda: dict(found)))


@pytest.fixture
def broken_iio(monkeypatch, tmp_path):
    """Install an ``iio`` that fails to import the way pylibiio does."""
    def install(error):
        (tmp_path / "iio.py").write_text(f"raise {error}\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(sys.modules, "iio", raising=False)
        monkeypatch.delitem(sys.modules, "adi", raising=False)
        monkeypatch.setattr(iio_host, "bindings_version", lambda: "0.25")
        return tmp_path
    return install


# --------------------------- the native library -----------------------------
@pytest.mark.parametrize("error", [
    'TypeError("LoadLibrary() argument 1 must be str, not None")',       # Windows
    'AttributeError("python: undefined symbol: iio_get_backends_count")',  # Linux
])
def test_a_missing_native_library_is_said_in_words(broken_iio, monkeypatch, error):
    broken_iio(error)
    monkeypatch.setattr(iio_host, "native_library", lambda: None)
    with pytest.raises(iio_host.HostLibraryError) as exc:
        iio_host.import_iio()
    msg = str(exc.value)
    assert "not installed" in msg and "pylibiio 0.25" in msg
    assert iio_host.RELEASES_URL + "/tag/v0.25" in msg
    assert "LoadLibrary" not in msg and "undefined symbol" not in msg


def test_pyadi_is_not_reached_before_the_library_is_checked(broken_iio, monkeypatch):
    """pyadi imports iio at module level. Importing it first handed over the same
    ctypes TypeError, one level deeper — and the transmitter showed exactly that."""
    where = broken_iio('TypeError("LoadLibrary() argument 1 must be str, not None")')
    (where / "adi.py").write_text("import iio\n", encoding="utf-8")
    monkeypatch.setattr(iio_host, "native_library", lambda: None)
    with pytest.raises(iio_host.HostLibraryError):
        iio_host.import_adi()


def test_a_library_that_is_there_but_unusable_is_named(broken_iio, monkeypatch):
    """A libiio of another version, or a 32-bit one: the file exists, and "not
    installed" would send the operator to install what is already there."""
    broken_iio('AttributeError("function \'iio_get_backends_count\' not found")')
    monkeypatch.setattr(iio_host, "native_library",
                        lambda: r"C:\Windows\System32\libiio.dll")
    with pytest.raises(iio_host.HostLibraryError) as exc:
        iio_host.import_iio()
    msg = str(exc.value)
    assert r"C:\Windows\System32\libiio.dll" in msg and "iio_get_backends_count" in msg
    assert "not installed" not in msg


def test_a_missing_python_package_is_still_an_import_error(monkeypatch):
    """The callers answer that one with "pip install" — it is not a native library."""
    monkeypatch.setitem(sys.modules, "iio", None)
    with pytest.raises(ImportError):
        iio_host.import_iio()


def test_windows_is_pointed_at_the_installer_of_the_matching_release(monkeypatch):
    monkeypatch.setattr(iio_host, "_on_windows", lambda: True)
    hint = iio_host.install_hint("0.25")
    assert "setup.exe" in hint and iio_host.RELEASES_URL + "/tag/v0.25" in hint
    # the newest release heads that page, and it is 1.x
    assert "not 1.x" in hint


def test_other_systems_are_pointed_at_their_package(monkeypatch):
    monkeypatch.setattr(iio_host, "_on_windows", lambda: False)
    assert "apt install libiio0" in iio_host.install_hint("0.25")


def test_the_release_follows_the_installed_bindings(monkeypatch):
    """pylibiio 0.23.1 wraps libiio 0.23 — and the release tag has no patch number."""
    monkeypatch.setattr(iio_host.metadata, "version", lambda _name: "0.23.1")
    assert iio_host.bindings_version() == "0.23"


# --------------------------- a board that does not open ---------------------
def test_a_silent_network_address_offers_the_board_found_on_usb(monkeypatch):
    """The case this was written for: the board's USB network adapter failed to
    start on a fresh Windows, and the board itself was fine."""
    _usb_bus(monkeypatch, PLUTO_ON_USB)
    monkeypatch.setattr(iio_host, "_on_windows", lambda: True)
    msg = iio_host.open_failure("ip:192.168.2.1", SILENT).message()
    assert "nothing answers at 192.168.2.1" in msg
    assert "usb:1.6.5 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO))" in msg
    assert "Set the URI to usb:" in msg and "RNDIS" in msg
    assert "Errno 0" not in msg and "serial=" not in msg


def test_the_usb_network_adapter_is_not_blamed_for_an_address_it_does_not_serve(monkeypatch):
    """A Pluto+ on its Ethernet port is not behind the USB network adapter."""
    _usb_bus(monkeypatch, PLUTO_ON_USB)
    monkeypatch.setattr(iio_host, "_on_windows", lambda: True)
    msg = iio_host.open_failure("ip:10.0.0.7", SILENT).message()
    assert "usb:1.6.5" in msg and "RNDIS" not in msg


def test_nothing_anywhere_says_what_to_check(monkeypatch):
    _usb_bus(monkeypatch, {})
    failure = iio_host.open_failure("ip:192.168.2.1", SILENT)
    assert any("cable" in h and "drivers" in h for h in failure.hints)


def test_a_board_held_by_another_program_is_not_reported_as_absent(monkeypatch):
    """A board another program holds does not show up in a scan, so the message
    has to name that possibility itself."""
    _usb_bus(monkeypatch, {})
    assert "another program" in iio_host.open_failure("usb:1.6.5", SILENT).reason


def test_a_replugged_board_is_found_at_its_new_address(monkeypatch):
    """The address changes on every replug; usb: alone follows the board."""
    _usb_bus(monkeypatch, {"usb:1.7.5": PLUTO_ON_USB["usb:1.6.5"]})
    msg = iio_host.open_failure("usb:1.6.5", SILENT).message()
    assert "usb:1.7.5" in msg and "Set the URI to usb:" in msg


def test_a_failed_usb_colon_is_not_told_to_use_usb_colon(monkeypatch):
    """The scan saw a board that plain usb: could not open: name its exact address."""
    _usb_bus(monkeypatch, PLUTO_ON_USB)
    msg = iio_host.open_failure("usb:", SILENT).message()
    assert "usb:1.6.5" in msg and "Set the URI to usb:" not in msg


def test_several_boards_are_all_listed(monkeypatch):
    _usb_bus(monkeypatch, {"usb:1.6.5": "0456:b673 (A)", "usb:1.9.5": "0456:b673 (B)"})
    msg = iio_host.open_failure("usb:", SILENT).message()
    assert "usb:1.6.5 (A)" in msg and "usb:1.9.5 (B)" in msg


def test_what_the_library_did_say_is_kept(monkeypatch):
    """Linux libiio sets a real errno, and that one is worth passing on."""
    _usb_bus(monkeypatch, {})
    failure = iio_host.open_failure("ip:192.168.2.1", OSError(110, "Connection timed out"))
    assert "Connection timed out" in failure.reason


def test_pyadis_catch_all_is_dropped(monkeypatch):
    _usb_bus(monkeypatch, {})
    failure = iio_host.open_failure("ip:192.168.2.1", Exception("No device found"))
    assert "No device found" not in failure.message()


def test_a_scan_that_cannot_run_claims_nothing_about_usb(monkeypatch):
    """"No Pluto is visible on USB" is a finding; without a scan it would be a guess."""
    def broken():
        raise OSError(0, "No error")
    monkeypatch.setitem(sys.modules, "iio", types.SimpleNamespace(scan_contexts=broken))
    failure = iio_host.open_failure("ip:192.168.2.1", SILENT)
    assert "192.168.2.1" in failure.message() and not failure.hints
