"""PySide6 GUI: manual control + scenario runner for the FPV FM video emulator.

Run:  python -m gui.app     (or  python run_gui.py)
"""
from __future__ import annotations

import os
import sys
import threading
import warnings

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6 import QtCore, QtGui, QtWidgets

from fpv_emulator import __version__
from fpv_emulator.backends import DeviceDetail, TxConfig, make_sink
from fpv_emulator.bands import load_band_table
from fpv_emulator.config import list_scenarios, load_scenario
from fpv_emulator.firmware import AUTO, PROFILE_KEYS, profile_label
from fpv_emulator.fm import occupied_bandwidth_hz
from fpv_emulator.signal_gen import required_rf_bandwidth_hz, video_bandwidth_hz
from fpv_emulator.i18n import LANGUAGES, detect_language, get_language, set_language, t
from fpv_emulator.scenarios import ScenarioRunner
from fpv_emulator.video import (
    STANDARDS,
    get_standard,
    is_color_pattern,
    list_all_patterns,
    render_pattern_image,
)

try:
    # shared aliasing criterion — keeps the readout and the generator from disagreeing
    from fpv_emulator.signal_gen import would_alias as _would_alias
except ImportError:      # older signal_gen: fall back to the local dev/2 test
    _would_alias = None


# ---------------------------------------------------------------------------
#  Background scenario worker (runs in its own QThread)
# ---------------------------------------------------------------------------
class ScenarioWorker(QtCore.QObject):
    event = QtCore.Signal(dict)
    finished = QtCore.Signal()
    error = QtCore.Signal(str)

    def __init__(self, sink, band_table, scenario):
        super().__init__()
        self.sink = sink
        self.bands = band_table
        self.scenario = scenario
        self._stop = threading.Event()
        self.runner = None

    @QtCore.Slot()
    def run(self):
        self.runner = ScenarioRunner(self.sink, self.bands, on_event=self.event.emit)
        try:
            self.runner.run(self.scenario, self._stop)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            try:
                self.sink.close()
            except Exception:
                pass
            self.finished.emit()

    def set_gain(self, gain_db: float):
        # request the desired power; the worker thread applies it (do not touch libiio here)
        if self.runner is not None:
            self.runner.set_live_gain(gain_db)

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------------------
#  Pattern preview widget
# ---------------------------------------------------------------------------
class PatternPreview(QtWidgets.QLabel):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 240)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setStyleSheet("background:#101014; border:1px solid #333;")
        # the unscaled source: show_pattern can run before the widget is laid out
        # (e.g. during a language rebuild), so the scaling is redone on every resize
        self._source: QtGui.QPixmap | None = None
        self._scaled_for = QtCore.QSize()

    def show_pattern(self, pattern: str):
        arr = render_pattern_image(pattern, 240, 320)
        img = np.ascontiguousarray((arr * 255).astype(np.uint8))
        if img.ndim == 3:  # RGB (color pattern)
            h, w, _ = img.shape
            qimg = QtGui.QImage(img.data, w, h, w * 3, QtGui.QImage.Format_RGB888)
        else:              # luma (monochrome)
            h, w = img.shape
            qimg = QtGui.QImage(img.data, w, h, w, QtGui.QImage.Format_Grayscale8)
        # QPixmap.fromImage copies, so the numpy buffer may go out of scope
        self._source = QtGui.QPixmap.fromImage(qimg)
        self._scaled_for = QtCore.QSize()
        self._rescale()

    def _rescale(self):
        if self._source is None or self._source.isNull():
            return
        size = self.size()
        if size == self._scaled_for:
            return
        self._scaled_for = size
        self.setPixmap(self._source.scaled(
            size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))

    def resizeEvent(self, ev: QtGui.QResizeEvent):
        super().resizeEvent(ev)
        self._rescale()


# ---------------------------------------------------------------------------
#  Main window
# ---------------------------------------------------------------------------
def _as_bool(value) -> bool:
    """QSettings returns "false" as a string, and bool("false") is True."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


#: what each persisted field must be turned back into when it is read.
#: The language is NOT here: it is kept as a top-level key because run_gui.py
#: reads it before this module is importable, to translate a startup failure.
_STATE_TYPES = {
    "backend": str, "uri": str, "device": str, "file": str,
    "serial": str, "amp": _as_bool,
    "band": str, "channel": str, "firmware": str, "hw": str,
    "standard": str, "pattern": str, "mode": str,
    "freq": float, "fs": float, "dev": float,
    "gain": int, "burst": _as_bool, "verbose": _as_bool,
}


class MainWindow(QtWidgets.QMainWindow):
    # generator warnings are raised on the worker thread — queued into the log
    warning_logged = QtCore.Signal(str)

    # closeEvent grace period: 30 * (100 ms wait + 200 ms timer) ~ 9 s after the first 2 s
    _CLOSE_MAX_ATTEMPTS = 30

    def __init__(self, settings: QtCore.QSettings | None = None):
        super().__init__()
        # language must be active BEFORE any widget is created
        self.settings = settings or QtCore.QSettings("fpv-fm-emulator", "gui")
        saved = str(self.settings.value("language", "") or "")
        set_language(saved if saved in LANGUAGES else detect_language())

        self.bands = load_band_table()
        self.thread = None
        self.worker = None
        self._close_attempts = 0
        self._prev_showwarning = None
        # device-detail texts already shown this session (see _install_warning_hook)
        self._detail_seen: set = set()

        self._build_ui(self._restore_state())
        self._log(t("FPV FM emulator v{version}", version=__version__))
        # run_gui.bat starts pythonw — stderr is discarded, so warnings.warn() from the
        # generator would never reach the operator. Route them into the event log.
        self.warning_logged.connect(self._log, QtCore.Qt.QueuedConnection)
        self._install_warning_hook()

    def _install_warning_hook(self) -> None:
        """Mirror every ``warnings.warn()`` into the event log.

        Device-detail warnings say how the board had to be configured — true on
        every run for a board that always needs it, and noise by the third Start.
        They are shown once per session, and on every run only when the verbose
        switch is on. Everything else always goes through: a warning that the
        signal is not what was asked for must never be filed away.
        """
        previous = warnings.showwarning

        def _to_log(message, category, filename, lineno, file=None, line=None):
            try:
                if isinstance(category, type) and issubclass(category, DeviceDetail):
                    text = str(message)
                    if not self.chk_verbose.isChecked() and text in self._detail_seen:
                        return
                    self._detail_seen.add(text)
                self.warning_logged.emit(t("[WARN] {msg}", msg=message))
            except RuntimeError:
                pass                       # window already destroyed
            try:
                previous(message, category, filename, lineno, file, line)
            except Exception:              # noqa: BLE001 — stderr may not exist (pythonw)
                pass

        # without this the aliasing warning is printed once per location per process
        warnings.simplefilter("always", UserWarning)
        warnings.showwarning = _to_log
        self._prev_showwarning = previous

    def _remove_warning_hook(self) -> None:
        if self._prev_showwarning is not None:
            warnings.showwarning = self._prev_showwarning
            self._prev_showwarning = None

    # -- ui construction (re-runnable: used to retranslate in place) ---------
    def _build_ui(self, state: dict | None = None):
        # The version goes in the title because that is where it survives a
        # screenshot, which is how a problem usually arrives from a colleague.
        self.setWindowTitle(
            t("FPV FM emulator for Pluto+ · test signal for FPV detectors")
            + f"  ·  v{__version__}")

        central = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(central)
        root.addLayout(self._build_left(), 0)
        root.addLayout(self._build_right(), 1)
        self.setCentralWidget(central)   # old central widget is deleteLater()'d

        self._refresh_channels()
        if state is None:
            # _refresh_channels() -> _on_channel_changed() has just parked sp_freq on the
            # LOWEST channel of the "— all —" list (900 MHz, a licensed band). Preselect
            # the documented default channel instead.
            i = self.cb_channel.findData("R:R1")
            if i >= 0:
                self.cb_channel.setCurrentIndex(i)
                self._on_channel_changed()   # setCurrentIndex is silent if i was current
            self.cb_pattern.setCurrentText("color_bars")   # default: realistic FPV profile
        else:
            self._apply_state(state)
        self._update_readouts()
        self._set_running(self.thread is not None)
        self._on_backend_changed(self.cb_backend.currentText())   # initial SDR field state
        # Restore the log last, and ONLY when the snapshot carries one. A language
        # rebuild does: _capture_state() hands back a log that already contains the
        # hints _on_backend_changed() has just re-appended, so replacing it is right.
        # A restored launch does not — the state read from settings has no log on
        # purpose — and the hints on the line above are new. Overwriting them there
        # threw away "backend = null — nothing goes on air" for an operator who had
        # last done a dry run and now, with the backend remembered too, would press
        # Start and see a log that reads exactly like a transmission.
        if state is not None and "log" in state:
            self.log.setPlainText(state["log"])
            self.log.moveCursor(QtGui.QTextCursor.End)

    # -- left: controls -----------------------------------------------------
    def _build_left(self) -> QtWidgets.QVBoxLayout:
        col = QtWidgets.QVBoxLayout()

        # backend
        gb_be = QtWidgets.QGroupBox(t("Output"))
        f = QtWidgets.QFormLayout(gb_be)
        self.cb_backend = QtWidgets.QComboBox()
        self.cb_backend.addItems(["pluto", "hackrf", "soapy", "null", "file"])
        self.cb_backend.currentTextChanged.connect(self._on_backend_changed)
        self.ed_uri = QtWidgets.QLineEdit("ip:192.168.2.1")
        self.ed_device = QtWidgets.QLineEdit("driver=hackrf")   # SoapySDR args
        self.ed_serial = QtWidgets.QLineEdit()                  # HackRF serial
        self.ed_serial.setPlaceholderText(t("empty = the first HackRF found"))
        self.chk_amp = QtWidgets.QCheckBox(t("RF amplifier +14 dB"))
        self.chk_amp.setToolTip(
            t("Lets the power slider reach 14 dB further: without it 0 dB is TXVGA "
              "47 dB, with it 0 dB is TXVGA 47 dB + the amplifier. The amplifier is "
              "switched on only when the slider asks for more than TXVGA alone gives. "
              "Never run it without an antenna or a load on the output."))
        # the amplifier moves the bottom of the power scale (-47 / -61 dB)
        self.chk_amp.toggled.connect(lambda _on: self._sync_backend_controls())
        self.ed_file = QtWidgets.QLineEdit("out.iq")
        # Firmware profile: which ways of setting the sample rate the sink may
        # try. Deliberately NOT called stock/hacked — those two words already
        # mean the AD9363-vs-AD9361 tuning range in the «HW range» box below,
        # and one word for two axes gets the wrong one set.
        self.cb_fw = QtWidgets.QComboBox()
        for key in PROFILE_KEYS:
            self.cb_fw.addItem(profile_label(key), key)
        self.cb_fw.setCurrentIndex(0)      # Auto; _restore_state may move it
        self.cb_fw.setToolTip(
            t("Auto tries the filtered path first and falls back if the board "
              "refuses it — right for both boards here. Pick a firmware only to "
              "force one route: Tezuka skips the attempt, Analog Devices refuses "
              "to fall back."))
        self.cb_fw.currentIndexChanged.connect(self._on_firmware_changed)
        self.cb_lang = QtWidgets.QComboBox()
        for code, label in LANGUAGES.items():
            self.cb_lang.addItem(label, code)
        i_lang = self.cb_lang.findData(get_language())
        if i_lang >= 0:
            self.cb_lang.setCurrentIndex(i_lang)
        self.cb_lang.currentIndexChanged.connect(self._on_language_changed)
        f.addRow(t("Backend:"), self.cb_backend)
        f.addRow(t("URI Pluto:"), self.ed_uri)
        f.addRow(t("Firmware:"), self.cb_fw)
        f.addRow(t("HackRF serial:"), self.ed_serial)
        f.addRow(t("HackRF:"), self.chk_amp)
        f.addRow(t("SDR (soapy):"), self.ed_device)
        f.addRow(t("File (file):"), self.ed_file)
        f.addRow(t("Language:"), self.cb_lang)
        hb_probe = QtWidgets.QHBoxLayout()
        self.btn_probe = QtWidgets.QPushButton(t("Probe Pluto"))
        self.btn_probe.clicked.connect(self._on_probe)
        self.btn_devices = QtWidgets.QPushButton(t("List SDRs"))
        self.btn_devices.clicked.connect(self._on_list_devices)
        hb_probe.addWidget(self.btn_probe)
        hb_probe.addWidget(self.btn_devices)
        f.addRow(hb_probe)
        col.addWidget(gb_be)

        # frequency
        gb_fr = QtWidgets.QGroupBox(t("Frequency"))
        f = QtWidgets.QFormLayout(gb_fr)
        self.cb_band = QtWidgets.QComboBox()
        self.cb_band.addItem(t("— all —"), None)
        for b in self.bands.list_bands():
            # show the translated band name; userData stays the raw YAML key
            self.cb_band.addItem(t(self.bands.bands[b].get("name", b)), b)
        self.cb_band.currentIndexChanged.connect(self._refresh_channels)
        self.cb_channel = QtWidgets.QComboBox()
        self.cb_channel.currentIndexChanged.connect(self._on_channel_changed)
        self.sp_freq = QtWidgets.QDoubleSpinBox()
        self.sp_freq.setRange(50.0, 6000.0)
        self.sp_freq.setDecimals(1)
        self.sp_freq.setSuffix(" " + t("MHz"))
        # no setValue() here: _refresh_channels() -> _on_channel_changed() owns this
        # field, and _build_ui() preselects the default channel (R1).
        self.cb_hw = QtWidgets.QComboBox()
        self.cb_hw.addItems(["hacked", "stock", "hackrf"])
        self.cb_hw.currentIndexChanged.connect(self._update_readouts)
        self.sp_freq.valueChanged.connect(self._update_readouts)
        f.addRow(t("Band:"), self.cb_band)
        f.addRow(t("Channel:"), self.cb_channel)
        f.addRow(t("Carrier:"), self.sp_freq)
        f.addRow(t("HW range:"), self.cb_hw)
        col.addWidget(gb_fr)

        # signal
        self.gb_sig = gb_sig = QtWidgets.QGroupBox(t("Signal"))
        f = QtWidgets.QFormLayout(gb_sig)
        self.cb_std = QtWidgets.QComboBox()
        self.cb_std.addItems(list(STANDARDS.keys()))
        self.cb_pattern = QtWidgets.QComboBox()
        self.cb_pattern.addItems(list_all_patterns())   # luma + color
        self.cb_pattern.currentTextChanged.connect(self._on_pattern_changed)
        self.sp_fs = QtWidgets.QDoubleSpinBox()
        self.sp_fs.setRange(1.0, 61.44)
        self.sp_fs.setDecimals(2)
        self.sp_fs.setSuffix(" MSPS")
        self.sp_fs.setValue(20.0)   # default for realistic FPV (color + wide bandwidth)
        self.sp_fs.valueChanged.connect(self._update_readouts)
        self.sp_dev = QtWidgets.QDoubleSpinBox()
        self.sp_dev.setRange(0.1, 30.0)
        self.sp_dev.setDecimals(2)
        self.sp_dev.setSuffix(" " + t("MHz pp"))
        self.sp_dev.setValue(7.0)
        self.sp_dev.valueChanged.connect(self._update_readouts)
        self.chk_burst = QtWidgets.QCheckBox(t("Color burst"))
        self.cb_std.currentIndexChanged.connect(self._update_readouts)
        f.addRow(t("Standard:"), self.cb_std)
        f.addRow(t("Pattern:"), self.cb_pattern)
        f.addRow(t("Sample rate:"), self.sp_fs)
        f.addRow(t("Deviation:"), self.sp_dev)
        f.addRow(self.chk_burst)
        col.addWidget(gb_sig)

        # power
        self.gb_pw = gb_pw = QtWidgets.QGroupBox(t("Power (tx_hardwaregain)"))
        v = QtWidgets.QVBoxLayout(gb_pw)
        self.sl_gain = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.sl_gain.setRange(-89, 0)
        self.sl_gain.setValue(-10)
        self.lbl_gain = QtWidgets.QLabel(t("{v} dB", v=-10))
        self.sl_gain.valueChanged.connect(
            lambda x: (self.lbl_gain.setText(t("{v} dB", v=x)), self._live_gain(x)))
        v.addWidget(self.sl_gain)
        v.addWidget(self.lbl_gain)
        col.addWidget(gb_pw)

        # mode + start/stop
        gb_run = QtWidgets.QGroupBox(t("Mode"))
        v = QtWidgets.QVBoxLayout(gb_run)
        self.cb_mode = QtWidgets.QComboBox()
        self.cb_mode.addItem(t("Manual carrier (static)"), None)
        for name in list_scenarios():
            self.cb_mode.addItem(t("Scenario: {name}", name=name), name)
        # connect AFTER filling: the readout widgets do not exist yet while filling
        self.cb_mode.currentIndexChanged.connect(self._on_mode_changed)
        v.addWidget(self.cb_mode)
        hb = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton(t("▶ Start"))
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QtWidgets.QPushButton(t("■ Stop"))
        self.btn_stop.clicked.connect(self._on_stop)
        hb.addWidget(self.btn_start)
        hb.addWidget(self.btn_stop)
        v.addLayout(hb)
        col.addWidget(gb_run)

        col.addStretch(1)
        return col

    # -- right: preview + readouts + log ------------------------------------
    def _build_right(self) -> QtWidgets.QVBoxLayout:
        col = QtWidgets.QVBoxLayout()
        self.preview = PatternPreview()
        col.addWidget(self.preview)

        self.lbl_read = QtWidgets.QLabel()
        self.lbl_read.setStyleSheet("font-family:monospace;")
        self.lbl_read.setWordWrap(True)
        col.addWidget(self.lbl_read)

        hb_log = QtWidgets.QHBoxLayout()
        hb_log.addWidget(QtWidgets.QLabel(t("Event log:")))
        hb_log.addStretch(1)
        self.chk_verbose = QtWidgets.QCheckBox(t("Verbose"))
        self.chk_verbose.setToolTip(
            t("Repeat the notes about how the board had to be configured on every "
              "run. Off, each one is shown once per session — a board that always "
              "needs the same accommodation says so once, not every Start. Problems "
              "with the signal are never filed away here."))
        hb_log.addWidget(self.chk_verbose)
        col.addLayout(hb_log)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setStyleSheet("font-family:monospace; font-size:11px;")
        col.addWidget(self.log, 1)

        self.status = self.statusBar()
        return col

    # -- language -----------------------------------------------------------
    def _capture_state(self) -> dict:
        """Snapshot every user-set value so a rebuild does not lose it."""
        return {
            "backend": self.cb_backend.currentText(),
            "uri": self.ed_uri.text(),
            "device": self.ed_device.text(),
            "serial": self.ed_serial.text(),
            "amp": self.chk_amp.isChecked(),
            "file": self.ed_file.text(),
            # keys, not indices: an index means a different band once bands.yaml
            # gains an entry, and this snapshot now outlives the session
            "band": self.cb_band.currentData() or "",
            "channel": self.cb_channel.currentData() or "",
            "freq": self.sp_freq.value(),
            "firmware": self.cb_fw.currentData(),
            "hw": self.cb_hw.currentText(),
            "standard": self.cb_std.currentText(),
            "pattern": self.cb_pattern.currentText(),
            "fs": self.sp_fs.value(),
            "dev": self.sp_dev.value(),
            "burst": self.chk_burst.isChecked(),
            "verbose": self.chk_verbose.isChecked(),
            "gain": self.sl_gain.value(),
            "mode": self.cb_mode.currentData() or "",
            "log": self.log.toPlainText(),
        }

    def _save_state(self) -> None:
        """Persist the window's settings so the next launch starts where this one left off."""
        state = self._capture_state()
        # The log is captured for the in-session rebuild only. Persisting it would
        # grow without bound in the registry, and a log from yesterday restored
        # into a fresh window reads as if it belonged to this session.
        state.pop("log", None)
        self.settings.beginGroup("state")
        for key, value in state.items():
            self.settings.setValue(key, "" if value is None else value)
        self.settings.endGroup()
        self.settings.sync()

    def _restore_state(self) -> dict | None:
        """Read back what :meth:`_save_state` wrote, or None on a first launch.

        Every value is cast on the way in. QSettings hands most things back as
        text on Windows, and bool("false") is True — a checkbox restored without
        this would be stuck on. A value that no longer fits (a pattern that was
        renamed, a band that was removed) is dropped rather than applied: the
        widget keeps its default, which is always a valid one.
        """
        self.settings.beginGroup("state")
        present = set(self.settings.childKeys())
        out: dict = {}
        for key, cast in _STATE_TYPES.items():
            if key not in present:
                continue
            try:
                out[key] = cast(self.settings.value(key))
            except (TypeError, ValueError):
                pass
        self.settings.endGroup()
        return out or None

    def _apply_state(self, state: dict):
        """Restore a snapshot taken by :meth:`_capture_state` without firing handlers."""
        for widget, value in (
            (self.cb_backend, state.get("backend")),
            (self.cb_hw, state.get("hw")),
            (self.cb_std, state.get("standard")),
            (self.cb_pattern, state.get("pattern")),
        ):
            if value:
                widget.blockSignals(True)
                widget.setCurrentText(value)
                widget.blockSignals(False)
        # findData, not setCurrentText: the labels are translated, the keys are not
        i_fw = self.cb_fw.findData(state.get("firmware"))
        if i_fw >= 0:
            self.cb_fw.blockSignals(True)
            self.cb_fw.setCurrentIndex(i_fw)
            self.cb_fw.blockSignals(False)
        self.ed_uri.setText(state.get("uri", ""))
        self.ed_device.setText(state.get("device", ""))
        self.ed_serial.setText(state.get("serial", ""))
        self.chk_amp.setChecked(bool(state.get("amp", False)))
        self.ed_file.setText(state.get("file", ""))

        # "" is the «— all —» row, whose userData is None
        i_band = self.cb_band.findData(state.get("band") or None)
        if i_band >= 0:
            self.cb_band.blockSignals(True)
            self.cb_band.setCurrentIndex(i_band)
            self.cb_band.blockSignals(False)
        self._refresh_channels()
        chan = state.get("channel")
        if chan:
            i = self.cb_channel.findData(chan)
            if i >= 0:
                self.cb_channel.blockSignals(True)
                self.cb_channel.setCurrentIndex(i)
                self.cb_channel.blockSignals(False)

        for spin, value in ((self.sp_freq, state.get("freq")),
                            (self.sp_fs, state.get("fs")),
                            (self.sp_dev, state.get("dev"))):
            if value is not None:
                spin.blockSignals(True)
                spin.setValue(float(value))
                spin.blockSignals(False)

        self.chk_burst.setChecked(bool(state.get("burst")))
        self.chk_verbose.setChecked(bool(state.get("verbose")))

        gain = int(state.get("gain", self.sl_gain.value()))
        self.sl_gain.blockSignals(True)
        self.sl_gain.setValue(gain)
        self.sl_gain.blockSignals(False)
        self.lbl_gain.setText(t("{v} dB", v=gain))

        i_mode = self.cb_mode.findData(state.get("mode") or None)
        if i_mode >= 0:
            self.cb_mode.setCurrentIndex(i_mode)

        self.preview.show_pattern(self.cb_pattern.currentText())

    def _on_firmware_changed(self, index: int):
        key = self.cb_fw.itemData(index) or AUTO
        if key != AUTO:
            self._log(t("[info] firmware profile forced to «{name}» — it stays set "
                        "for the next launch too. Auto is right for both boards.",
                        name=profile_label(key)))

    def _on_language_changed(self, index: int):
        code = self.cb_lang.itemData(index)
        if not code or code == get_language():
            return
        self.settings.setValue("language", code)
        set_language(code)
        state = self._capture_state()
        # rebuild outside this signal handler — the combo itself is about to be replaced
        QtCore.QTimer.singleShot(0, lambda: self._build_ui(state))

    # -- helpers ------------------------------------------------------------
    def _refresh_channels(self):
        self.cb_channel.blockSignals(True)
        self.cb_channel.clear()
        band = self.cb_band.currentData()
        chans = (self.bands.channels_in_band(band) if band
                 else sorted((c for g in self.bands.groups()
                              for c in self.bands.channels_in_group(g)),
                             key=lambda c: c.freq_hz))
        for ch in chans:
            # userData = unambiguous 'band:channel' key (channel names repeat across bands)
            self.cb_channel.addItem(f"{ch.name}  ({ch.freq_mhz:.0f})", f"{ch.band}:{ch.name}")
        self.cb_channel.blockSignals(False)
        self._on_channel_changed()

    def _on_channel_changed(self):
        key = self.cb_channel.currentData()
        if key:
            self.sp_freq.blockSignals(True)
            self.sp_freq.setValue(self.bands.channel(key).freq_mhz)
            self.sp_freq.blockSignals(False)
            self._update_readouts()

    def _on_pattern_changed(self, pattern: str):
        self.preview.show_pattern(pattern)
        # never touch fs while transmitting: sp_fs is disabled, but setValue() would
        # still work on it and the running signal would not follow
        if (self.thread is None and is_color_pattern(pattern)
                and self.sp_fs.value() < 18.0):
            # realistic FPV: 4.43 MHz subcarrier + wide bandwidth -> fs 20 MSPS
            self.sp_fs.setValue(20.0)
            self._log(t("[info] Color pattern — fs raised to 20 MSPS "
                        "(subcarrier + wide bandwidth)."))
        self._sync_burst()
        self._update_readouts()   # bandwidth depends on the pattern (color is wider)

    def _on_mode_changed(self, _index: int = 0):
        """Manual <-> scenario: the scenario file owns the whole Signal group."""
        self._set_running(self.thread is not None)
        self._update_readouts()

    def _sync_burst(self):
        """«Color burst» is meaningless for color patterns — they always carry it."""
        color = is_color_pattern(self.cb_pattern.currentText())
        usable = (self.thread is None
                  and self.cb_mode.currentData() is None
                  and not color)
        self.chk_burst.setEnabled(usable)
        self.chk_burst.setToolTip(
            t("Color patterns always transmit the burst and the chroma subcarrier — "
              "this switch only affects black-and-white patterns.") if color else "")

    def _current_signal(self) -> dict:
        return {
            "standard": self.cb_std.currentText(),
            "pattern": self.cb_pattern.currentText(),
            "sample_rate": self.sp_fs.value() * 1e6,
            "deviation_pp_hz": self.sp_dev.value() * 1e6,
            "gain_db": float(self.sl_gain.value()),
            "color_burst": self.chk_burst.isChecked(),
        }

    # -- scenario introspection (the readout must describe what really goes on air) --
    def _selected_scenario(self) -> dict | None:
        """The scenario picked in the Mode combo, or None for the manual carrier."""
        name = self.cb_mode.currentData()
        if name is None:
            return None
        try:
            return load_scenario(list_scenarios()[name])
        except Exception:   # noqa: BLE001 — a broken file must not blank the readout
            return None

    def _scenario_first_channel(self, stype: str, cfg: dict):
        """(label, freq_hz) of the first frequency the scenario transmits on, or None."""
        try:
            if stype == "sweep":
                if cfg.get("channels"):
                    ch = self.bands.channel(cfg["channels"][0])
                    return ch.name, ch.freq_hz
                if cfg.get("band"):
                    chans = self.bands.channels_in_band(cfg["band"])
                    return (chans[0].name, chans[0].freq_hz) if chans else None
                if cfg.get("group"):
                    chans = self.bands.channels_in_group(cfg["group"])
                    return (chans[0].name, chans[0].freq_hz) if chans else None
                if cfg.get("freq_list_mhz"):
                    f = float(cfg["freq_list_mhz"][0])
                    return f"{f:.0f}MHz", f * 1e6
                if cfg.get("ranges"):
                    f = float(cfg["ranges"][0]["start_mhz"])
                    return f"{f:.0f}MHz", f * 1e6
                return None
            key = "center_channel" if stype == "multi_drone" else "channel"
            if cfg.get(key):
                ch = self.bands.channel(cfg[key])
                return ch.name, ch.freq_hz
            if cfg.get("freq_mhz") is not None:
                f = float(cfg["freq_mhz"])
                return f"{f:.0f}MHz", f * 1e6
        except Exception:   # noqa: BLE001
            return None
        return None

    def _scenario_view(self, scen: dict) -> dict:
        """What the scenario will actually transmit (mirrors SignalParams/ScenarioRunner)."""
        sig = scen.get("signal") or {}
        stype = str(scen.get("type", "static")).lower()
        cfg = scen.get(stype) or {}
        view = {
            "standard": str(sig.get("standard", "PAL50")),
            "pattern": str(sig.get("pattern", "color_bars")),
            "fs": float(sig.get("sample_rate", 20e6)),
            "dev": float(sig.get("deviation_pp_hz", 6e6)),
            "offsets_hz": [],
            "channel": None,
            "freq_hz": None,
        }
        view["alias_patterns"] = [view["pattern"]]
        if stype == "multi_drone":
            drones = cfg.get("drones") or []
            if drones:
                # generate_multi_drone_iq() always uses the LUMA generator
                pats = [str(d.get("pattern", view["pattern"])) for d in drones]
                view["pattern"] = "+".join(pats)
                view["alias_patterns"] = pats   # the joined name is not a real pattern
                view["offsets_hz"] = [float(d.get("offset_mhz", 0.0)) * 1e6 for d in drones]
        ref = self._scenario_first_channel(stype, cfg)
        if ref:
            view["channel"], view["freq_hz"] = ref
        return view

    def _alias_check(self, pattern: str, std, fs: float, dev: float,
                     max_offset_hz: float = 0.0):
        """(aliases, peak_hz) — via the shared generator helper when it is available."""
        if _would_alias is not None:
            try:
                aliases, peak = _would_alias(pattern, std, fs, dev,
                                             max_offset_hz=max_offset_hz)
                return bool(aliases), float(peak)
            except Exception:   # noqa: BLE001 — never let the readout die
                pass
        peak = dev / 2.0 + abs(max_offset_hz)
        return peak > 0.45 * fs, peak

    def _update_readouts(self):
        scen = self._selected_scenario()
        head: list[str] = []
        offsets: list[float] = []
        chan = None
        if scen is None:
            std_name = self.cb_std.currentText()
            pattern = self.cb_pattern.currentText()
            fs = self.sp_fs.value() * 1e6
            dev = self.sp_dev.value() * 1e6
            freq = self.sp_freq.value() * 1e6
            alias_patterns = [pattern]
        else:
            view = self._scenario_view(scen)
            std_name = view["standard"]
            pattern = view["pattern"]
            alias_patterns = view["alias_patterns"]
            fs = view["fs"]
            dev = view["dev"]
            offsets = view["offsets_hz"]
            chan = view["channel"]
            # a scenario without a resolvable frequency still opens the sink on sp_freq
            freq = view["freq_hz"] if view["freq_hz"] else self.sp_freq.value() * 1e6
            head.append(t("Scenario «{name}» — values below come from the scenario file.",
                          name=t(str(scen.get("name", "")))))
        try:
            std = get_standard(std_name)
        except Exception:   # noqa: BLE001 — malformed scenario: keep the panel alive
            std = get_standard(self.cb_std.currentText())

        n = int(round(std.line_us * 1e-6 * fs)) * std.total_lines
        mb = n * 4 / 1e6  # complex int16 = 4 bytes/sample
        # alias_patterns, not `pattern`: a multi-drone run joins the names into
        # "color_bars+color_bars100", which is not a pattern, so asking about it
        # answered "monochrome" and understated the width by 7.9 MHz — enough to
        # withhold the too-wide warning below on exactly the colour multi-drone runs.
        video_bw = video_bandwidth_hz(alias_patterns, std)
        bw = occupied_bandwidth_hz(dev, video_bw)
        max_offset = max((abs(o) for o in offsets), default=0.0)
        if offsets:
            bw += max(offsets) - min(offsets)   # multi-drone span, as in signal_gen
        ok, warn = self.bands.check_reachable(freq, self.cb_hw.currentText())
        aliases = any(self._alias_check(p, std, fs, dev, max_offset)[0]
                      for p in alias_patterns)
        alias = ("  " + t("⚠ ALIASING (raise fs)")) if aliases else ""

        lines = list(head)
        if chan:
            lines.append(t("Carrier:     {mhz} MHz  ({ch})",
                           mhz=f"{freq/1e6:.1f}", ch=chan))
        else:
            lines.append(t("Carrier:     {mhz} MHz", mhz=f"{freq/1e6:.1f}"))
        if scen is not None:
            lines.append(t("Signal:      {std} · {pattern} · deviation {dev} MHz pp",
                           std=std.name, pattern=pattern, dev=f"{dev/1e6:.2f}"))
        lines += [
            t("Frame:       {n} samples · {ms} ms · buffer ~{mb} MB",
              n=n, ms=f"{std.frame_period_s*1e3:.1f}", mb=f"{mb:.2f}"),
            t("Occupied bandwidth ~{bw} MHz (fs={fs} MSPS){alias}",
              bw=f"{bw/1e6:.1f}", fs=f"{fs/1e6:.1f}", alias=alias),
            t("Line rate:   {khz} kHz", khz=f"{std.line_rate_hz/1e3:.2f}"),
        ]
        if bw > fs:
            lines.append(t("⚠ Occupied bandwidth is wider than the sample rate — "
                           "raise fs or lower the deviation."))
        if not ok:
            lines.append(f"⚠ {warn}")
        self.lbl_read.setText("\n".join(lines))

    def _build_scenario(self) -> dict:
        mode = self.cb_mode.currentData()
        if mode is None:
            return {
                "name": "GUI-static",
                "type": "static",
                "signal": self._current_signal(),
                "static": {"freq_mhz": self.sp_freq.value(), "hold_s": 0},
            }
        return load_scenario(list_scenarios()[mode])

    def _rf_bw_for(self, scenario, fs: float) -> float:
        """The TX analog filter width this run needs.

        It used to be min(fs, 20e6), which has nothing to do with the signal: a
        two-drone run at +-9 MHz occupies 27.6 MHz and was radiated through a
        20 MHz filter. Harmless there — the filter is symmetric about the LO, so
        both carriers lose the same fraction of a dB — but it does not scale, and
        a wider split would have come back attenuated and been read as "spreading
        them out does not help".
        """
        sig = (scenario or {}).get("signal") or {}
        dev = float(sig.get("deviation_pp_hz", self.sp_dev.value() * 1e6))
        std = get_standard(str(sig.get("standard", self.cb_std.currentText())))
        drones = ((scenario or {}).get("multi_drone") or {}).get("drones") or []
        offsets = [float(d.get("offset_mhz", 0.0)) * 1e6 for d in drones]
        pats = [str(d.get("pattern", "")) for d in drones] or                [str(sig.get("pattern", self.cb_pattern.currentText()))]
        return required_rf_bandwidth_hz(dev, video_bandwidth_hz(pats, std), offsets, fs)

    def _make_sink(self, fs: float, scenario: dict | None = None):
        kind = self.cb_backend.currentText()
        cfg = TxConfig(fs=fs, freq_hz=self.sp_freq.value() * 1e6,
                       gain_db=float(self.sl_gain.value()),
                       uri=self.ed_uri.text(), rf_bw_hz=self._rf_bw_for(scenario, fs),
                       device=self.ed_device.text(),
                       serial=self.ed_serial.text().strip(),
                       amp=self.chk_amp.isChecked(),
                       firmware=str(self.cb_fw.currentData() or AUTO))
        return make_sink(kind, cfg, file_path=self.ed_file.text())

    #: «HW range» presets that describe a Pluto, and the one that describes a HackRF
    _PLUTO_HW = ("hacked", "stock")
    _HACKRF_HW = "hackrf"

    def _sync_backend_controls(self) -> None:
        """Enable only the settings the selected backend actually reads.

        A Pluto's URI, firmware profile, AD9363/AD9361 range, 61.44 MSPS ceiling
        and -89 dB attenuation mean nothing to a HackRF, and its serial, amplifier,
        range, 20 MSPS ceiling and 0..-47/-61 dB scale mean nothing to a Pluto.
        Left editable, a field the sink ignores reads as a setting that applied.
        Everything here is also frozen while on air: the device is opened with
        these values at Start.
        """
        from fpv_emulator.hackrf import AMP_GAIN_DB, FS_MAX_HZ, TXVGA_MAX_DB

        name = self.cb_backend.currentText()
        idle = self.thread is None
        pluto, hackrf, soapy = (name == "pluto"), (name == "hackrf"), (name == "soapy")

        self.ed_uri.setEnabled(pluto and idle)
        self.cb_fw.setEnabled(pluto and idle)
        self.ed_serial.setEnabled(hackrf and idle)
        self.chk_amp.setEnabled(hackrf and idle)
        self.ed_device.setEnabled(soapy and idle)
        self.btn_devices.setEnabled(soapy and idle)
        self.btn_probe.setText(t("Probe HackRF") if hackrf else t("Probe Pluto"))

        # HW range: a HackRF has exactly one, and the Pluto presets cannot be
        # picked for it — nor its preset for anything else
        model = self.cb_hw.model()
        for i in range(self.cb_hw.count()):
            is_hackrf_item = self.cb_hw.itemText(i) == self._HACKRF_HW
            model.item(i).setEnabled(is_hackrf_item == hackrf)
        if hackrf:
            self.cb_hw.setCurrentText(self._HACKRF_HW)
        elif self.cb_hw.currentText() == self._HACKRF_HW:
            self.cb_hw.setCurrentText(self._PLUTO_HW[0])
        self.cb_hw.setEnabled(not hackrf)

        # sample rate ceiling; Qt clamps a value above it
        fs_max = FS_MAX_HZ / 1e6 if hackrf else 61.44
        if self.sp_fs.maximum() != fs_max:
            if self.sp_fs.value() > fs_max:
                self._log(t("[info] HackRF: sample rate lowered to {max} MSPS — its maximum.",
                            max=f"{fs_max:.0f}"))
            self.sp_fs.setMaximum(fs_max)

        # power scale: what the device can actually do, 0 = maximum either way
        if hackrf:
            g_min = -(TXVGA_MAX_DB + (AMP_GAIN_DB if self.chk_amp.isChecked() else 0))
            self.gb_pw.setTitle(t("Power (TXVGA + RF amplifier)") if self.chk_amp.isChecked()
                                else t("Power (TXVGA)"))
        else:
            g_min = -89
            self.gb_pw.setTitle(t("Power (tx_hardwaregain)"))
        self.sl_gain.setMinimum(g_min)

    def _on_backend_changed(self, name: str):
        soapy = (name == "soapy")
        hackrf = (name == "hackrf")
        self._sync_backend_controls()
        if name == "null":
            self._log(t("[WARN] backend = null — dry run, nothing goes on air."))
        elif hackrf:
            self._log(t("[info] backend = hackrf — 2–20 MSPS, power 0..-47 dB "
                        "(0..-61 dB with the RF amplifier). «Probe HackRF» checks the "
                        "board."))
        elif soapy:
            self._log(t("[info] backend = soapy — set the device in the «SDR (soapy)» field "
                        "(e.g. driver=hackrf|lime|uhd). «List SDRs» shows what is available."))

    def _on_list_devices(self):
        if self.thread is not None:
            self.status.showMessage(
                t("Not available while transmitting — press Stop first."), 4000)
            return
        from fpv_emulator.backends import soapy_enumerate
        devs = soapy_enumerate()
        if not devs:
            self._log(t("No SoapySDR devices found (or SoapySDR is not installed)."))
            return
        self._log(t("SDRs found (SoapySDR):"))
        for d in devs:
            self._log(f"  driver={d.get('driver','?')}  {d.get('label','')}")

    # -- actions ------------------------------------------------------------
    def _on_probe(self):
        # the probe opens a SECOND adi.Pluto on the same URI and sweeps tx_lo over
        # 70 MHz .. 6 GHz — it would drag the live carrier away while transmitting
        if self.thread is not None:
            self.status.showMessage(
                t("Not available while transmitting — press Stop first."), 4000)
            return
        if self.cb_backend.currentText() == "hackrf":
            from fpv_emulator.hackrf import probe_hackrf
            self.status.showMessage(t("Probing HackRF…"))
            QtWidgets.QApplication.processEvents()
            res = probe_hackrf(serial=self.ed_serial.text().strip() or None)
        else:
            from fpv_emulator.probe import probe
            self.status.showMessage(t("Probing Pluto…"))
            QtWidgets.QApplication.processEvents()
            res = probe(uri=self.ed_uri.text())
        self._log(res.summary())
        if res.inferred_preset:
            self.cb_hw.setCurrentText(res.inferred_preset)
        self.status.showMessage(t("Probe finished"), 4000)

    def _commit_spin_edits(self) -> None:
        """Force every spin box to accept what is typed in it.

        A QDoubleSpinBox only turns its edit text into a value when editing ends
        (focus change, Enter). Pressing Start with the caret still in the field
        would otherwise transmit the PREVIOUS frequency while the box shows the
        new one — indistinguishable, from the outside, from "it did not start".
        """
        for box in (self.sp_freq, self.sp_fs, self.sp_dev):
            box.interpretText()

    def _on_start(self):
        if self.thread is not None:
            return
        self._commit_spin_edits()
        # A forced profile persists across launches, so it could silently apply
        # to a board that was never meant to have it. Say so every time.
        fw_key = str(self.cb_fw.currentData() or AUTO)
        if fw_key != AUTO and self.cb_backend.currentText() == "pluto":
            self._log(t("[info] firmware profile: {name} (not auto)",
                        name=profile_label(fw_key)))
        # anything warned on the way to the sink must be visible (stderr is discarded
        # under pythonw); warnings raised later, on the worker thread, come through
        # the showwarning hook installed in __init__
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            failure = None
            try:
                scenario = self._build_scenario()
                fs = float(scenario.get("signal", {}).get(
                    "sample_rate", self.sp_fs.value() * 1e6))
                sink = self._make_sink(fs, scenario)
            except Exception as exc:  # noqa: BLE001
                failure = exc
        for w in caught:
            self._log(t("[WARN] {msg}", msg=w.message))
        if failure is not None:
            self._log(t("[ERROR] {msg}", msg=failure))
            return

        self.thread = QtCore.QThread()
        self.worker = ScenarioWorker(sink, self.bands, scenario)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.event.connect(self._on_event)
        self.worker.error.connect(lambda m: self._log(t("[ERROR] {msg}", msg=m)))
        self.worker.finished.connect(self._on_finished)
        self.thread.start()
        # Also save here, not only on close: a run that is killed, or that takes
        # the process down inside libiio, would otherwise lose the configuration
        # it was started with — which is the one worth keeping.
        self._save_state()
        self._set_running(True)
        self._log(t("— start: {name} —", name=t(scenario.get("name", ""))))

    def _on_stop(self):
        if self.worker:
            self.worker.stop()
            self.status.showMessage(t("Stopping…"))

    def _on_finished(self):
        if self.thread:
            self.thread.quit()
            self.thread.wait(2000)
        self.thread = None
        self.worker = None
        self._close_attempts = 0
        self._set_running(False)
        self.status.showMessage(t("Stopped"), 3000)

    def _live_gain(self, gain_db: int):
        # live power change while transmitting — routed through the worker thread
        if self.worker:
            self.worker.set_gain(float(gain_db))

    def _on_event(self, e: dict):
        from fpv_emulator.cli import _fmt_event
        self._log(_fmt_event(e))
        if e.get("action") in ("tune", "power"):
            self.status.showMessage(t(
                "{ch} @ {mhz} MHz, {gain} dB",
                ch=e.get("channel", ""),
                mhz=f"{e.get('freq_mhz', 0):.1f}",
                gain=e.get("gain_db", "")))

    def _log(self, msg: str):
        self.log.appendPlainText(msg)

    def _set_running(self, running: bool):
        """Single source of truth for what the operator may touch right now."""
        scenario_mode = self.cb_mode.currentData() is not None
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        # frozen while on air: everything the running sink would NOT follow
        for w in (self.cb_backend, self.cb_mode, self.cb_lang,
                  self.btn_probe, self.sp_freq, self.cb_band, self.cb_channel):
            w.setEnabled(not running)
        # backend-specific fields: only the selected backend's, and none on air
        self._sync_backend_controls()
        # in scenario mode the YAML file owns the whole Signal group
        manual_signal = (not running) and (not scenario_mode)
        for w in (self.cb_std, self.cb_pattern, self.sp_fs, self.sp_dev):
            w.setEnabled(manual_signal)
        self.gb_sig.setToolTip(
            t("The scenario file sets the standard, pattern, sample rate and "
              "deviation. Choose «Manual carrier (static)» to set them by hand.")
            if scenario_mode else "")
        self._sync_burst()

    def closeEvent(self, ev: QtGui.QCloseEvent):
        # Once, on the way in: a close that has to wait for the device comes back
        # through here up to _CLOSE_MAX_ATTEMPTS times, and by then _on_stop() has
        # already run.
        if self._close_attempts == 0:
            self._save_state()
        if self.thread is None:
            self._remove_warning_hook()
            ev.accept()
            return

        self._on_stop()
        self.thread.quit()
        # the worker still has to leave sink.stop()/sink.close() — closing before that
        # leaves the device claimed and can take the process down inside libiio
        first = (self._close_attempts == 0)
        if self.thread.wait(2000 if first else 100):
            self.thread = None
            self.worker = None
            self._remove_warning_hook()
            ev.accept()
            return

        self._close_attempts += 1
        if self._close_attempts <= self._CLOSE_MAX_ATTEMPTS:
            self.status.showMessage(
                t("Stopping the transmission — the window will close once the "
                  "device is released…"))
            ev.ignore()
            QtCore.QTimer.singleShot(200, self.close)
            return

        self._log(t("[WARN] The device was not released in time — closing anyway."))
        self.thread = None
        self.worker = None
        self._remove_warning_hook()
        ev.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.resize(920, 640)
    win.show()
    win.preview.show_pattern(win.cb_pattern.currentText())
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
