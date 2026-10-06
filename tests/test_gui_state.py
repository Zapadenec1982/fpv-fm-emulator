"""The window comes back the way it was left.

Settings are written to a temporary .ini here, never to the real QSettings store —
a test that persisted "tezuka" or a scenario mode into the operator's own settings
would silently change what their next launch does.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# must be set before QApplication exists — there is no display on CI or in a shell
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtCore = pytest.importorskip("PySide6.QtCore")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from fpv_emulator.i18n import get_language, set_language, t
from gui.app import MainWindow, _as_bool


@pytest.fixture(scope="module")
def qt_app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def keep_the_process_language():
    """Constructing a MainWindow sets the language for the whole process, from its
    settings or from the environment. Left alone it leaks into every test module
    that asserts on an English string — and only when the machine's locale is not
    English, which is the machine this is developed on."""
    saved = get_language()
    yield
    set_language(saved)


@pytest.fixture
def settings(tmp_path):
    return QtCore.QSettings(str(tmp_path / "gui.ini"), QtCore.QSettings.IniFormat)


def _window(settings):
    """A window on throwaway settings. Not shown — offscreen and never mapped."""
    return MainWindow(settings=settings)


# --------------------------- the round trip --------------------------------
def test_a_first_launch_has_nothing_to_restore(qt_app, settings):
    w = _window(settings)
    assert w._restore_state() is None
    assert w.cb_pattern.currentText() == "color_bars", "the shipped default moved"


def test_every_captured_field_survives_a_restart(qt_app, settings):
    w = _window(settings)
    w.cb_backend.setCurrentText("soapy")
    w.ed_uri.setText("ip:192.168.3.7")
    w.ed_device.setText("driver=lime")
    w.cb_std.setCurrentText("NTSC60")
    w.cb_pattern.setCurrentText("smpte75")
    w.sp_fs.setValue(30.72)
    w.sp_dev.setValue(4.5)
    w.sp_freq.setValue(1200.0)
    w.sl_gain.setValue(-30)
    w.cb_fw.setCurrentIndex(w.cb_fw.findData("tezuka"))
    w._save_state()

    again = _window(settings)
    assert again.cb_backend.currentText() == "soapy"
    assert again.ed_uri.text() == "ip:192.168.3.7"
    assert again.ed_device.text() == "driver=lime"
    assert again.cb_std.currentText() == "NTSC60"
    assert again.cb_pattern.currentText() == "smpte75"
    assert again.sp_fs.value() == pytest.approx(30.72)
    assert again.sp_dev.value() == pytest.approx(4.5)
    assert again.sp_freq.value() == pytest.approx(1200.0)
    assert again.sl_gain.value() == -30
    assert again.cb_fw.currentData() == "tezuka"


def test_the_log_is_not_persisted(qt_app, settings):
    """It would grow without bound, and yesterday's log in a fresh window reads
    as if it belonged to this session."""
    w = _window(settings)
    w._log("something that happened last time")
    w._save_state()
    settings.beginGroup("state")
    assert "log" not in set(settings.childKeys())
    settings.endGroup()
    assert "last time" not in _window(settings).log.toPlainText()


@pytest.mark.parametrize("checked", [True, False])
def test_the_burst_checkbox_survives_both_ways(qt_app, settings, checked):
    """The one field where a naive restore is always True: QSettings hands back
    the string "false", and bool("false") is True."""
    w = _window(settings)
    w.chk_burst.setChecked(checked)
    w._save_state()
    assert _window(settings).chk_burst.isChecked() is checked


def test_as_bool_reads_what_qsettings_writes():
    assert _as_bool("false") is False and _as_bool("true") is True
    assert _as_bool(False) is False and _as_bool("0") is False


# --------------------------- selections by key ------------------------------
def test_the_band_is_restored_by_key_not_by_position(qt_app, settings):
    """An index would name a different band as soon as bands.yaml gains an entry,
    and this snapshot now outlives the session that made it."""
    w = _window(settings)
    assert w.cb_band.count() > 2
    w.cb_band.setCurrentIndex(2)
    key = w.cb_band.currentData()
    w._save_state()

    settings.beginGroup("state")
    assert settings.value("band") == key, "the position was stored, not the key"
    settings.endGroup()
    assert _window(settings).cb_band.currentData() == key


def test_the_all_bands_row_is_restored_too(qt_app, settings):
    """Its userData is None, which must not be mistaken for 'nothing was saved'."""
    w = _window(settings)
    w.cb_band.setCurrentIndex(0)
    w._save_state()
    again = _window(settings)
    assert again.cb_band.currentIndex() == 0
    assert again.cb_band.currentData() is None


def test_the_channel_is_restored(qt_app, settings):
    w = _window(settings)
    i = w.cb_channel.findData("R:R4")
    assert i >= 0, "the qualified channel key moved"
    w.cb_channel.setCurrentIndex(i)
    w._save_state()
    assert _window(settings).cb_channel.currentData() == "R:R4"


def test_the_scenario_mode_is_restored_by_name(qt_app, settings):
    w = _window(settings)
    assert w.cb_mode.count() > 1
    w.cb_mode.setCurrentIndex(1)
    name = w.cb_mode.currentData()
    w._save_state()
    again = _window(settings)
    assert again.cb_mode.currentData() == name
    assert not again.sp_fs.isEnabled(), "scenario mode must still own the Signal group"


# --------------------------- stale and broken state -------------------------
def test_a_value_that_no_longer_exists_is_ignored(qt_app, settings):
    """A pattern removed between versions must leave the default standing, not
    blank the combo or raise on startup."""
    settings.beginGroup("state")
    settings.setValue("pattern", "a_pattern_that_was_deleted")
    settings.setValue("standard", "PAL")          # retired in favour of PAL50
    settings.endGroup()
    w = _window(settings)
    assert w.cb_pattern.currentText() in [w.cb_pattern.itemText(i)
                                          for i in range(w.cb_pattern.count())]
    assert w.cb_std.currentText() in [w.cb_std.itemText(i)
                                      for i in range(w.cb_std.count())]


def test_an_unparsable_number_does_not_stop_the_window_opening(qt_app, settings):
    settings.beginGroup("state")
    settings.setValue("fs", "not a number")
    settings.setValue("gain", "")
    settings.endGroup()
    w = _window(settings)                  # must not raise
    assert w.sp_fs.value() == pytest.approx(20.0)


def test_an_out_of_range_number_is_clamped_not_applied(qt_app, settings):
    """sp_fs stops at 61.44 MSPS; a stored 999 must not become the transmitted rate."""
    settings.beginGroup("state")
    settings.setValue("fs", "999")
    settings.endGroup()
    assert _window(settings).sp_fs.value() <= 61.44


def test_saving_writes_nothing_outside_its_own_group(qt_app, settings):
    """The language lives at the top level because run_gui.py reads it before this
    module can be imported — the state group must not disturb it."""
    settings.setValue("language", "uk")
    _window(settings)._save_state()
    assert settings.value("language") == "uk"


def test_a_restored_dry_run_still_warns_that_nothing_goes_on_air(qt_app, settings):
    """The backend is remembered now, so a dry run follows the operator to the next
    session. Restoring an empty log over the hints that were just appended left them
    pressing Start on backend=null with a log that reads like a transmission."""
    w = _window(settings)
    w.cb_backend.setCurrentText("null")
    w._save_state()

    again = _window(settings)
    assert again.cb_backend.currentText() == "null"
    assert "null" in again.log.toPlainText(), "the dry-run warning was thrown away"


def test_a_language_rebuild_still_replaces_the_log_rather_than_doubling_it(qt_app, settings):
    """The other side of the same line: there the snapshot does carry the log, and
    the hints are already in it."""
    w = _window(settings)
    w.cb_backend.setCurrentText("null")
    before = w.log.toPlainText().count("null")
    w._build_ui(w._capture_state())          # what switching language does
    assert w.log.toPlainText().count("null") == before


# --------------------------- the verbose switch -----------------------------
def _warn_through_the_hook(w, text, category):
    """Raise a warning the way the sink does and let the queued signal arrive."""
    import warnings
    warnings.warn(text, category)
    QtWidgets.QApplication.processEvents()
    return w.log.toPlainText().count(text)


@pytest.mark.parametrize("checked", [True, False])
def test_the_verbose_switch_survives_a_restart(qt_app, settings, checked):
    w = _window(settings)
    w.chk_verbose.setChecked(checked)
    w._save_state()
    assert _window(settings).chk_verbose.isChecked() is checked


def test_a_device_detail_is_shown_once_per_session_by_default(qt_app, settings):
    """A board that always needs the same accommodation says so on every Start,
    and by the third one it is noise the operator reads past."""
    from fpv_emulator.backends import DeviceDetail
    w = _window(settings)
    text = "the FIR filter was switched off"
    assert _warn_through_the_hook(w, text, DeviceDetail) == 1
    assert _warn_through_the_hook(w, text, DeviceDetail) == 1


def test_verbose_repeats_the_detail_on_every_run(qt_app, settings):
    from fpv_emulator.backends import DeviceDetail
    w = _window(settings)
    w.chk_verbose.setChecked(True)
    text = "the FIR filter was switched off, again"
    assert _warn_through_the_hook(w, text, DeviceDetail) == 1
    assert _warn_through_the_hook(w, text, DeviceDetail) == 2


def test_an_ordinary_warning_is_never_filed_away(qt_app, settings):
    """Aliasing, a dead transmitter, a rate that does not fit — those repeat
    whatever the switch says, because they mean the signal is wrong."""
    w = _window(settings)
    assert not w.chk_verbose.isChecked()
    text = "Aliasing risk: the pattern folds past Nyquist"
    assert _warn_through_the_hook(w, text, UserWarning) == 1
    assert _warn_through_the_hook(w, text, UserWarning) == 2


# ------------------------------- hackrf ------------------------------------
@pytest.mark.parametrize("amp", [True, False])
def test_the_hackrf_serial_and_amplifier_survive_a_restart(qt_app, settings, amp):
    w = _window(settings)
    w.cb_backend.setCurrentText("hackrf")
    w.ed_serial.setText("b65c67dc327cba5f")
    w.chk_amp.setChecked(amp)
    w._save_state()

    again = _window(settings)
    assert again.cb_backend.currentText() == "hackrf"
    assert again.ed_serial.text() == "b65c67dc327cba5f"
    assert again.chk_amp.isChecked() is amp


def test_choosing_hackrf_unlocks_its_fields_and_its_range(qt_app, settings):
    w = _window(settings)
    w.cb_backend.setCurrentText("pluto")
    assert not w.ed_serial.isEnabled() and not w.chk_amp.isEnabled()

    w.cb_backend.setCurrentText("hackrf")
    assert w.ed_serial.isEnabled() and w.chk_amp.isEnabled()
    assert w.cb_hw.currentText() == "hackrf"
    assert w.btn_probe.text() == t("Probe HackRF")
    assert not w.cb_fw.isEnabled(), "the firmware profile is Pluto-only"

    # leaving hackrf must not keep a HackRF range for a Pluto
    w.cb_backend.setCurrentText("pluto")
    assert w.cb_hw.currentText() == "hacked"
    assert w.btn_probe.text() == t("Probe Pluto")


def test_the_sink_gets_the_serial_and_the_amplifier(qt_app, settings):
    w = _window(settings)
    w.cb_backend.setCurrentText("hackrf")
    w.ed_serial.setText("  abc  ")
    w.chk_amp.setChecked(True)
    sink = w._make_sink(20e6)
    assert type(sink).__name__ == "HackRFSink"
    assert sink.cfg.serial == "abc" and sink.cfg.amp is True

def _hw_item_enabled(w, text):
    i = w.cb_hw.findText(text)
    return w.cb_hw.model().item(i).isEnabled()


def test_a_hackrf_gets_none_of_the_pluto_settings(qt_app, settings):
    w = _window(settings)
    w.cb_backend.setCurrentText("hackrf")
    assert not w.ed_uri.isEnabled(), "URI is Pluto-only"
    assert not w.cb_fw.isEnabled(), "firmware profile is Pluto-only"
    assert not w.cb_hw.isEnabled() and w.cb_hw.currentText() == "hackrf"
    assert not _hw_item_enabled(w, "hacked") and not _hw_item_enabled(w, "stock")
    assert w.sp_fs.maximum() == pytest.approx(20.0)
    assert w.sl_gain.minimum() == -47
    assert not w.ed_device.isEnabled() and not w.btn_devices.isEnabled()


def test_a_pluto_gets_none_of_the_hackrf_settings(qt_app, settings):
    w = _window(settings)
    w.cb_backend.setCurrentText("hackrf")
    w.cb_backend.setCurrentText("pluto")
    assert w.ed_uri.isEnabled() and w.cb_fw.isEnabled()
    assert not w.ed_serial.isEnabled() and not w.chk_amp.isEnabled()
    assert w.cb_hw.isEnabled() and w.cb_hw.currentText() in ("hacked", "stock")
    assert not _hw_item_enabled(w, "hackrf")
    assert w.sp_fs.maximum() == pytest.approx(61.44)
    assert w.sl_gain.minimum() == -89


def test_the_amplifier_extends_the_hackrf_power_scale(qt_app, settings):
    w = _window(settings)
    w.cb_backend.setCurrentText("hackrf")
    w.chk_amp.setChecked(True)
    assert w.sl_gain.minimum() == -61
    w.sl_gain.setValue(-55)
    w.chk_amp.setChecked(False)
    assert w.sl_gain.minimum() == -47 and w.sl_gain.value() == -47


def test_a_pluto_rate_is_brought_into_the_hackrf_range(qt_app, settings):
    w = _window(settings)
    w.cb_backend.setCurrentText("pluto")
    w.sp_fs.setValue(30.72)
    w.cb_backend.setCurrentText("hackrf")
    assert w.sp_fs.value() == pytest.approx(20.0)


def test_a_restored_hackrf_comes_back_with_its_own_limits(qt_app, settings):
    w = _window(settings)
    w.cb_backend.setCurrentText("hackrf")
    w._save_state()
    again = _window(settings)
    assert again.cb_hw.currentText() == "hackrf" and not again.ed_uri.isEnabled()
    assert again.sp_fs.maximum() == pytest.approx(20.0)


def test_nothing_backend_specific_can_be_changed_on_air(qt_app, settings):
    w = _window(settings)
    w.cb_backend.setCurrentText("hackrf")
    w.thread = object()                      # what _on_start sets; no real run
    try:
        w._set_running(True)
        assert not w.ed_serial.isEnabled() and not w.chk_amp.isEnabled()
    finally:
        w.thread = None
        w._set_running(False)
    assert w.ed_serial.isEnabled()
