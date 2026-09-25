"""TX backends: Pluto (pyadi-iio), file, and null.

An abstraction over the IQ sink. The core (video/fm/signal_gen) does not depend
on hardware; the hardware import (``adi``) is deferred, so offline development
and the tests work without libiio-Python and without a device.
"""
from __future__ import annotations

import gc
import os
import threading
import time
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from . import firmware as firmware_mod
from . import iio_host
from .i18n import t


@dataclass
class TxConfig:
    fs: float
    freq_hz: float
    gain_db: float = -10.0          # tx_hardwaregain (0 = max, -89 = min)
    rf_bw_hz: Optional[float] = None
    uri: str = "ip:192.168.2.1"     # typical Pluto URI over USB-Ethernet
    device: str = "driver=hackrf"   # SoapySDR device args (e.g. driver=hackrf|lime|uhd)
    # Which ways of setting the sample rate we may try — see firmware.py.
    # Only PlutoSink reads it; for the soapy/file/null sinks it means nothing.
    firmware: str = firmware_mod.AUTO


class BaseSink(ABC):
    """Common interface of an IQ-buffer sink."""

    def __init__(self, cfg: TxConfig):
        self.cfg = cfg
        self._running = False
        # Error channel: a background TX thread (or a swallowed teardown failure)
        # stores its exception here so the caller can notice that the RF is gone.
        self._error: Optional[BaseException] = None

    @property
    def running(self) -> bool:
        return self._running

    def poll_error(self) -> Optional[BaseException]:
        """Return and CLEAR the last background error (None if there was none).

        The scenario engine polls this while it sleeps: a sink whose TX thread
        died must not keep looking armed while nothing is radiated.
        """
        err, self._error = self._error, None
        return err

    def _record_error(self, exc: BaseException) -> None:
        """Store a background failure (first one wins — it is the root cause)."""
        if self._error is None:
            self._error = exc

    @abstractmethod
    def start(self, iq_int16: np.ndarray) -> None:
        """Upload the cyclic buffer and start transmitting."""

    @abstractmethod
    def reload(self, iq_int16: np.ndarray) -> None:
        """Re-upload the buffer (pattern / multi-drone change)."""

    @abstractmethod
    def set_freq(self, freq_hz: float) -> None:
        """Retune the carrier (without re-uploading the buffer)."""

    @abstractmethod
    def set_gain(self, gain_db: float) -> None:
        """Change the power (tx_hardwaregain)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop transmitting."""

    def close(self) -> None:
        self.stop()

    def info(self) -> dict:
        return {
            "backend": type(self).__name__,
            "fs": self.cfg.fs,
            "freq_hz": self.cfg.freq_hz,
            "gain_db": self.cfg.gain_db,
            "running": self._running,
        }


class DeviceDetail(UserWarning):
    """How the device had to be configured — not a problem with the signal.

    A board that always needs the same accommodation says so on every run, and
    after the third time it is noise the operator reads past. Marking the class
    lets the GUI file these under its verbose switch while leaving anything that
    means "what is on air is not what you asked for" in plain sight.
    """


# ---------------------------------------------------------------------------
#  Pluto backend (pyadi-iio)
# ---------------------------------------------------------------------------
def set_sample_rate_without_fir(sdr, phy_name: str, rate: float) -> Optional[float]:
    """Write ``sampling_frequency`` directly, with the FIR filter switched off.

    Setting ``sdr.sample_rate`` does more than set a rate. pyadi's setter is
    ad9361_set_bb_rate() from libad9361: at or below 20 MSPS it loads a 128-tap
    FIR interpolating by 4 and switches it on, between 20 and 40 it loads a
    different one interpolating by 2, and so on. A Tezuka build on a Nano
    PlutoSDR refuses that with EINVAL at 20 MSPS while the same board advertises
    2.083–61.44 MSPS as available — which is exactly the range an AD9361 has with
    the FIR *off*. The rate is fine; the filter is what it will not take.

    So this is the fallback path: drop the filter and set the rate alone. The FIR
    is an anti-image filter ahead of the half-band chain — losing it costs some
    rejection of the images either side of the passband, and changes nothing
    about the video we generate.

    Returns the rate the board reports back, or None if it could not be written.
    """
    try:
        phy = sdr.ctx.find_device(phy_name)
        for ch in phy.channels:
            if ch.id == "out" and "voltage_filter_fir_en" in ch.attrs:
                ch.attrs["voltage_filter_fir_en"].value = "0"
        # RX and TX share one clock on the AD9361, so voltage0 in is the rate.
        for ch in phy.channels:
            if not ch.output and ch.id == "voltage0" and "sampling_frequency" in ch.attrs:
                ch.attrs["sampling_frequency"].value = str(int(rate))
                got = float(ch.attrs["sampling_frequency"].value)
                # The board quantises the rate; a small step is normal, but a
                # write that silently landed somewhere else is not usable — the
                # buffer was generated for `rate` and the line timing scales with
                # it, so a wrong rate means a signal the detector cannot match.
                return got if abs(got - rate) <= 0.005 * rate else None
    except Exception:
        pass
    return None


#: pyadi property -> the phy channel attribute that publishes its permitted values
_CHAN_ATTR = {
    "sample_rate": "sampling_frequency_available",
    "tx_rf_bandwidth": "rf_bandwidth_available",
    "tx_hardwaregain_chan0": "hardwaregain_available",
}


def available_text(sdr, phy_name: str, attr: str) -> str:
    """What the board publishes as acceptable for ``attr``, verbatim.

    The permitted range lives on the phy channel, not on the pyadi object:
    sample_rate maps to voltage0's sampling_frequency and pyadi exposes no
    ``*_available`` for it at all. Reading it directly is the one thing that
    turns an EINVAL into something actionable — "it accepts [2083333 1 61440000]"
    rather than "invalid argument", which is what four different causes all
    looked like before.
    """
    try:
        chan_attr = _CHAN_ATTR.get(attr)
        if chan_attr is None:
            return str(getattr(sdr, attr + "_available", "") or "")
        phy = sdr.ctx.find_device(phy_name)
        for ch in phy.channels:
            if ch.output and ch.id == "voltage0" and chan_attr in ch.attrs:
                return str(ch.attrs[chan_attr].value)
    except Exception:
        pass
    return ""


def rejected_error(attr: str, value, unit: str, err, avail: str = "",
                   hint: str = "") -> RuntimeError:
    """The 'device said no' error, with the value, the range and any advice."""
    return RuntimeError(
        t("The device rejected {attr} = {value}{unit} ({err}).{avail}",
          attr=attr, value=value, unit=unit, err=str(err),
          avail=(" " + t("It accepts: {range}", range=avail)) if avail else "")
        + ((" " + hint) if hint else "")
    )


def _parse_range(text: str) -> Optional[Tuple[float, float]]:
    """(min, max) from an IIO "[min step max]" string, or None if it is not one."""
    try:
        nums = [float(x) for x in text.strip("[] ").split()]
    except ValueError:
        return None
    return (nums[0], nums[2]) if len(nums) >= 3 else None


def allowed_sample_rates(sdr, phy_name: str) -> Optional[Tuple[float, float]]:
    """(min, max) sample rate the board publishes, or None if it publishes none."""
    return _parse_range(available_text(sdr, phy_name, "sample_rate"))


def apply_sample_rate(sdr, phy_name: str, rate: float,
                      profile_key: str = firmware_mod.AUTO) -> Optional[str]:
    """Set the data rate under a firmware profile; return a warning text or None.

    The profile decides which routes may be tried, not what the board is:
    ``auto`` tries pyadi's filtered path and falls back, ``adi`` only tries
    pyadi's, ``tezuka`` goes straight to the unfiltered write. Under ``auto``
    this is byte-for-byte the try-then-fall-back order that the Pluto+ is known
    to work with, so the default moves nothing.

    Raises RuntimeError naming the value, the range the board publishes, and —
    when a manual profile is what closed the door — how to reopen it.
    """
    prof = firmware_mod.profile(profile_key)
    refused: Optional[BaseException] = None

    if prof.try_pyadi_fir:
        try:
            sdr.sample_rate = int(rate)
            return None
        except Exception as exc:
            refused = exc
            if not prof.allow_fir_off:
                raise rejected_error(
                    "sample_rate", int(rate), " Hz", exc,
                    available_text(sdr, phy_name, "sample_rate"),
                    t("The {profile} firmware profile does not set the rate any "
                      "other way. Switch the profile to {auto} to let the board "
                      "be asked without the FIR filter.",
                      profile=firmware_mod.profile_label(prof.key),
                      auto=t("Auto")),
                ) from exc

    if prof.allow_fir_off:
        got = set_sample_rate_without_fir(sdr, phy_name, rate)
        if got is not None and refused is not None:
            return t("This board refused the interpolating FIR filter that comes "
                     "with the sample rate; {rate} MSPS was set with the filter "
                     "off. Image rejection either side of the passband is a little "
                     "weaker — the video itself is unchanged.",
                     rate=f"{got/1e6:.2f}")
        if got is not None:
            return t("The {profile} firmware profile set {rate} MSPS with the FIR "
                     "filter off, without trying the filtered path first. Image "
                     "rejection either side of the passband is a little weaker — "
                     "the video itself is unchanged.",
                     profile=firmware_mod.profile_label(prof.key),
                     rate=f"{got/1e6:.2f}")

    if refused is not None:
        raise rejected_error("sample_rate", int(rate), " Hz", refused,
                             available_text(sdr, phy_name, "sample_rate"))
    # Nothing raised, nothing took: the unfiltered write is the only route this
    # profile allows and the board did not land on the rate we asked for.
    raise RuntimeError(t(
        "Setting the sample rate to {rate} MSPS with the FIR filter off did not "
        "take (firmware profile: {profile}).{avail}",
        rate=f"{rate/1e6:.2f}", profile=firmware_mod.profile_label(prof.key),
        avail=(" " + t("It accepts: {range}",
                       range=available_text(sdr, phy_name, "sample_rate")))
        if available_text(sdr, phy_name, "sample_rate") else ""))


class PlutoSink(BaseSink):
    """Transmit through an ADALM-Pluto / Pluto+ (pyadi-iio)."""

    def __init__(self, cfg: TxConfig):
        super().__init__(cfg)
        self._sdr = None
        self._firmware = firmware_mod.Firmware()
        self._warned_mismatch = False
        self._rf_bw_hz: Optional[float] = None
        self._rate_note: Optional[str] = None

    def info(self) -> dict:
        d = super().info()
        # The profile as requested and the firmware as reported, separately: an
        # operator debugging a board needs to see that those two can differ.
        d["firmware_profile"] = firmware_mod.profile(self.cfg.firmware).key
        d["firmware"] = self._firmware.describe()
        # the analog filter width appeared in no log, no readout and no info dict
        d["rf_bw_hz"] = self._rf_bw_hz
        return d

    def _ensure_open(self) -> None:
        if self._sdr is not None:
            return
        try:
            # A missing native libiio arrives from here as HostLibraryError — a
            # RuntimeError that already says what to install — not as the ctypes
            # TypeError pyadi's own import of iio would raise.
            adi = iio_host.import_adi()
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                t("pyadi-iio is not installed. Install it with: "
                  "pip install pyadi-iio pylibiio")
            ) from exc

        # Which devices this board actually has. Alternative firmwares (Tezuka
        # builds, PlutoSky boards) and custom FPGA images do not have to use the
        # stock names that pyadi hard-codes; without this, a mismatch surfaces
        # from inside pyadi as "argument of type 'NoneType' is not iterable".
        pluto_cls = adi.Pluto
        phy_name = "ad9361-phy"
        refused: Optional[BaseException] = None     # why the board did not open
        try:
            import iio  # noqa: WPS433
            from .iio_layout import detect_layout, pluto_class_for
            # One context, used for both questions. Opening a second one on a
            # device whose DMA is being torn down is how the buffer ends up
            # stale, so the identity read borrows the context rather than
            # making its own. It only labels things — see firmware.py.
            try:
                ctx = iio.Context(self.cfg.uri)
            except Exception as exc:
                refused = exc
                raise
            self._firmware = firmware_mod.identify(ctx)
            layout = detect_layout(ctx)
            phy_name = layout.phy or phy_name
            if not layout.can_transmit:
                raise RuntimeError(
                    t("This device has no transmit path — the emulator cannot use "
                      "it. {details}", details=layout.describe())
                )
            pluto_cls = pluto_class_for(layout)
        except RuntimeError:
            raise
        except Exception:
            pass          # cannot inspect: fall back to the stock class
        finally:
            # ...and it is closed before pyadi opens its own. Over USB a board
            # takes ONE context at a time: while this one was alive, pyadi's
            # open below failed with a bare "No device found", so nothing could
            # transmit over usb: at all. A network link takes two, which is why
            # it went unseen. gc.collect() for the reason _reopen() gives.
            ctx = None
            gc.collect()

        # pyadi would make the very same call and fail the same way — after the
        # same timeout, and as "No device found". An empty URI is the exception:
        # pyadi then looks for the board itself, which iio.Context cannot.
        if refused is not None and self.cfg.uri:
            raise RuntimeError(
                iio_host.open_failure(self.cfg.uri, refused).message()) from refused
        try:
            sdr = pluto_cls(uri=self.cfg.uri)
        except Exception as exc:
            raise RuntimeError(
                iio_host.open_failure(self.cfg.uri, exc).message()) from exc

        def _set(attr: str, value, unit: str = ""):
            """Apply one setting, naming it if the device rejects it.

            These writes went one after another inside a single try, so a device
            that refused any of them produced a bare '[Errno 22] Invalid
            argument' — true, useless, and identical for four different causes.
            Boards differ in what they accept (sample rate ceilings, gain and
            bandwidth ranges), so the value that was refused is the whole story.
            """
            try:
                setattr(sdr, attr, value)
            except Exception as exc:
                raise rejected_error(attr, value, unit, exc,
                                     available_text(sdr, phy_name, attr)) from exc

        # A manual profile that contradicts the board is worth saying out loud
        # once — but only once per sink, because _reopen() comes back through
        # here on every arming retry and a repeated notice would push the rest
        # of the log out of view.
        if not self._warned_mismatch:
            note = firmware_mod.mismatch_warning(self.cfg.firmware, self._firmware)
            if note:
                warnings.warn(note, DeviceDetail, stacklevel=2)
            self._warned_mismatch = True

        # Boards disagree about the sample rate: the Pluto+ here reaches 30.72
        # MSPS, this Nano publishes 2.083–61.44. Refuse an out-of-range rate up
        # front instead of clamping to fit — the buffer has already been built
        # for the requested rate, and the 15.625 kHz line rate the detector
        # matches on scales with it. A quietly substituted rate would transmit a
        # carrier that looks right on a spectrum analyser and that the detector
        # cannot recognise, which is the worst failure this tool can have.
        rng = allowed_sample_rates(sdr, phy_name)
        if rng and not (rng[0] <= self.cfg.fs <= rng[1]):
            del sdr
            raise RuntimeError(t(
                "This board does not accept {wanted} MSPS — it allows {min}–{max}. "
                "Set the sample rate within that range (in the GUI: 'Sample rate'). "
                "It is not adjusted for you: the video timing is generated for the "
                "rate you asked for, so substituting another one here would "
                "transmit a picture the detector no longer recognises.",
                wanted=f"{self.cfg.fs/1e6:.2f}", min=f"{rng[0]/1e6:.2f}",
                max=f"{rng[1]/1e6:.2f}"))

        try:
            # Warn when the answer CHANGES, not on every open. _reopen() comes back
            # through here on each arming retry, and the GUI turns off warning
            # de-duplication (it has to, or a repeated aliasing warning would be
            # swallowed), so three identical lines per Start would push the rest of
            # the log out of view. A note that differs from last time is new
            # information and still gets through.
            note = apply_sample_rate(sdr, phy_name, self.cfg.fs, self.cfg.firmware)
            if note and note != self._rate_note:
                warnings.warn(note, DeviceDetail, stacklevel=2)
            self._rate_note = note
            _set("tx_lo", int(self.cfg.freq_hz), " Hz")
            # Clamp rather than refuse, unlike the sample rate. Nothing in the
            # buffer depends on the analog filter: not one sample changes, and the
            # 15.625 kHz line rate the detector matches on is untouched. A narrow
            # filter costs a fraction of a dB, symmetrically about the LO — so
            # refusing would brick a board for a defect that is worth less than
            # the warning. Say what was asked for and what was applied, because a
            # silently narrow filter is what this replaced.
            want = float(self.cfg.rf_bw_hz or min(self.cfg.fs, 20e6))
            rf_bw = want
            rng = _parse_range(available_text(sdr, phy_name, "tx_rf_bandwidth"))
            if rng:
                rf_bw = min(max(want, rng[0]), rng[1])
            if abs(rf_bw - want) > 1e3:
                warnings.warn(
                    t("This signal needs a {want} MHz transmit filter; the board "
                      "allows up to {max} MHz, so {used} MHz was applied. The "
                      "outermost sidebands are attenuated — the video and the line "
                      "rate are unchanged.",
                      want=f"{want/1e6:.2f}", max=f"{rng[1]/1e6:.2f}",
                      used=f"{rf_bw/1e6:.2f}"),
                    DeviceDetail, stacklevel=2)
            self._rf_bw_hz = rf_bw
            _set("tx_rf_bandwidth", int(rf_bw), " Hz")
            _set("tx_hardwaregain_chan0", float(self.cfg.gain_db), " dB")
            sdr.tx_cyclic_buffer = True
            # Changing tx_lo makes the AD9361 recalibrate, and how long that takes
            # depends on the frequency (VCO band selection). Arming the buffer in
            # the middle of it can leave the DMA not running while tx() still
            # returns success — the "it only starts on the third try" symptom.
            time.sleep(0.25)
        except Exception:
            # Release the half-configured handle, otherwise the next attempt
            # finds the device busy and the real cause is masked.
            del sdr
            raise
        self._sdr = sdr

    def _reopen(self) -> None:
        """Drop the device context and open a fresh one.

        ``del`` alone is not enough: the iio context lives until its last
        reference goes, and a lingering one keeps the DMA bound to the previous
        buffer, so gc.collect() is deliberate rather than superstition.
        """
        try:
            self._sdr.tx_destroy_buffer()
        except Exception:
            pass
        self._sdr = None
        gc.collect()
        time.sleep(0.3)
        self._ensure_open()

    def _tx_is_playing(self) -> Optional[bool]:
        """Is the TX DMA actually playing the buffer?

        tx() can return success while the DMA never starts — the transmitter then
        stays silent and nothing in the logs says so, which makes every later
        measurement wrong. Digital loopback answers it directly and, unlike
        listening for RF leakage, works at any frequency: with loopback on, the
        receiver gets the transmit stream itself, so samples flowing means the
        buffer really is playing.

        Returns None when the check cannot be performed (then trust tx()).
        """
        sdr = self._sdr
        try:
            old_loop = sdr.loopback
            old_mode = sdr.gain_control_mode_chan0
            old_size = sdr.rx_buffer_size
            sdr.rx_lo = int(self.cfg.freq_hz)
            sdr.gain_control_mode_chan0 = "manual"
            sdr.rx_hardwaregain_chan0 = 20.0
            sdr.rx_buffer_size = 4096
            sdr.loopback = 1
            try:
                for _ in range(3):
                    sdr.rx()                      # flush stale buffers
                power = float(np.mean(np.abs(sdr.rx()) ** 2))
            finally:
                sdr.loopback = old_loop
                sdr.gain_control_mode_chan0 = old_mode
                sdr.rx_buffer_size = old_size
        except Exception:
            return None                            # cannot tell — do not block TX
        # a stopped DMA loops back essentially nothing; a playing one is orders
        # of magnitude above it
        return power > 1e4

    def _arm(self, iq_int16: np.ndarray, attempts: int = 3) -> None:
        """Push the cyclic buffer, clearing any stale one first, and retry.

        Field symptom this fixes: "the transmitter does not always start"
        (confirmed on a spectrum analyser). A buffer left allocated by an earlier
        run — or by a run that was killed — makes tx() fail with EBUSY
        ("Open unlocked: -16"). Destroying before arming removes that class of
        failure, and the retry covers a device that needs a moment to settle.
        Silence is the real danger here: a transmitter that quietly does nothing
        makes every measurement taken afterwards wrong, so a failure to arm must
        raise rather than leave _running set.
        """
        last: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                # A retry on the same context keeps failing: observed in the field,
                # the DMA stays attached to the previous run's buffer and a fresh
                # tx() is accepted without ever starting. Dropping the context (and
                # forcing the collection that actually frees the iio handles) is
                # what clears it — that is why a manual restart used to "fix" it.
                self._reopen()
            try:
                self._sdr.tx_destroy_buffer()
            except Exception:
                pass                      # nothing to clear — normal on a fresh start
            try:
                self._sdr.tx(iq_int16)
            except Exception as exc:      # OSError/EBUSY and friends
                last = exc
                time.sleep(0.5 * attempt)
                continue
            # tx() returning is not proof that anything is on air
            playing = self._tx_is_playing()
            if playing is not False:      # True, or "cannot tell"
                self._running = True
                return
            last = RuntimeError(
                t("tx() succeeded but the TX buffer is not playing (attempt {n})",
                  n=attempt)
            )
            time.sleep(0.5 * attempt)
        self._running = False
        raise RuntimeError(
            t("The transmitter did not start after {n} attempts — the device is busy "
              "or in a stale state. Close anything else using the Pluto (the GUI, "
              "another run); if nothing is, unplug the USB, wait ~10 s and plug it "
              "back in. ({err})", n=attempts, err=str(last))
        )

    def start(self, iq_int16: np.ndarray) -> None:
        self._ensure_open()
        self._arm(iq_int16)

    def reload(self, iq_int16: np.ndarray) -> None:
        self._ensure_open()
        self._arm(iq_int16)

    def set_freq(self, freq_hz: float) -> None:
        self.cfg.freq_hz = freq_hz
        if self._sdr is not None:
            self._sdr.tx_lo = int(freq_hz)

    def set_gain(self, gain_db: float) -> None:
        self.cfg.gain_db = gain_db
        if self._sdr is not None:
            self._sdr.tx_hardwaregain_chan0 = float(gain_db)

    def stop(self) -> None:
        if self._sdr is not None:
            try:
                self._sdr.tx_destroy_buffer()
            except Exception as exc:  # pragma: no cover - needs a device
                # Destroying the cyclic buffer is what switches the RF off. If it
                # fails the device keeps radiating, so this must not be silent —
                # but stop() is called from a finally block and must not raise.
                self._record_error(RuntimeError(
                    t("Failed to stop the Pluto TX buffer ({err}) — "
                      "the device may still be radiating", err=str(exc))
                ))
        self._running = False

    def close(self) -> None:
        self.stop()
        self._sdr = None
        # free the iio context now rather than whenever the collector gets to it:
        # the next Start opens a new one, and an overlapping pair is what leaves
        # the DMA attached to the old buffer
        gc.collect()


# ---------------------------------------------------------------------------
#  SoapySDR backend — HackRF, LimeSDR, BladeRF, USRP, Pluto etc. (multi-SDR)
# ---------------------------------------------------------------------------

#: consecutive non-positive writeStream returns tolerated before the TX loop
#: gives up (SOAPY_SDR_TIMEOUT is -1; with timeoutUs=1 s this is several
#: seconds of a device that accepts nothing).
_WRITE_FAIL_LIMIT = 5

#: how long stop() waits for the TX loop to leave writeStream (seconds)
_STOP_JOIN_TIMEOUT_S = 5.0

#: shorter re-join budget once a thread is already known to be stuck, so that
#: repeated stop()/close() calls do not add minutes to a shutdown
_STUCK_JOIN_TIMEOUT_S = 0.5


def _write_ret(r) -> int:
    """Normalise the many shapes of a SoapySDR writeStream() return value."""
    if isinstance(r, (tuple, list)):
        r = r[0] if r else 0
    r = getattr(r, "ret", r)
    try:
        return int(r)
    except (TypeError, ValueError):
        return 0


class SoapySink(BaseSink):
    """Transmit through any SDR that has a SoapySDR driver.

    Unlike the Pluto (cyclic buffer inside the device), there is no hardware
    looping here — the host continuously pushes one frame into the TX stream from
    a background thread. The frame tiles seamlessly (phase is continuous), so the
    loop is clean.

    device args: ``driver=hackrf`` | ``driver=lime`` | ``driver=uhd`` | ...
    Power: the -89..0 slider (Pluto convention) is mapped onto the real gain
    range of the device (0 dB = maximum).
    """

    def __init__(self, cfg: TxConfig):
        super().__init__(cfg)
        self._dev = None
        self._stream = None         # owned by THIS object, not by the TX thread
        self._thread = None
        # Event of the current run. start() always creates a NEW one, so a
        # thread that outlived its stop() can never be un-cancelled.
        self._stop_evt = threading.Event()
        self._stop_evt.set()        # nothing is running yet
        self._lock = threading.Lock()
        self._buf = None            # complex64, normalised frame
        self._gmin = 0.0
        self._gmax = 47.0
        self._stuck = False         # a previous TX thread refused to join

    def _soapy(self):
        try:
            import SoapySDR  # noqa: WPS433
            return SoapySDR
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                t("SoapySDR is not installed. Install SoapySDR + the device driver: "
                  "Windows — PothosSDR (contains SoapySDR and the drivers); "
                  "Linux — apt install python3-soapysdr soapysdr-module-hackrf "
                  "(or -lime/-uhd/-bladerf).")
            ) from exc

    def _ensure_open(self) -> None:
        if self._dev is not None:
            return
        S = self._soapy()
        self._dev = S.Device(self.cfg.device or "driver=hackrf")
        try:
            self._dev.setSampleRate(S.SOAPY_SDR_TX, 0, float(self.cfg.fs))
            self._dev.setFrequency(S.SOAPY_SDR_TX, 0, float(self.cfg.freq_hz))
            try:
                rng = self._dev.getGainRange(S.SOAPY_SDR_TX, 0)
                self._gmin, self._gmax = float(rng.minimum()), float(rng.maximum())
            except Exception:  # pragma: no cover
                self._gmin, self._gmax = 0.0, 47.0
            self._apply_gain(self.cfg.gain_db)
        except Exception:
            # A half-configured device must not be reused: the next start()
            # would skip _ensure_open() and transmit at the wrong fs/frequency.
            self._dev = None
            raise

    def _apply_gain(self, gain_db: float) -> None:
        S = self._soapy()
        # -89..0 (Pluto convention, 0 = max) -> the real range of the device
        norm = max(0.0, min(1.0, (float(gain_db) + 89.0) / 89.0))
        g = self._gmin + norm * (self._gmax - self._gmin)
        self._dev.setGain(S.SOAPY_SDR_TX, 0, float(g))

    @staticmethod
    def _to_cf32(iq_int16: np.ndarray) -> np.ndarray:
        return (iq_int16.astype(np.complex64)) / 32768.0

    # -- stream ownership (this object, never the thread) --------------------
    def _open_stream(self):
        S = self._soapy()
        st = self._dev.setupStream(S.SOAPY_SDR_TX, S.SOAPY_SDR_CF32, [0])
        try:
            self._dev.activateStream(st)
        except Exception:
            try:
                self._dev.closeStream(st)
            except Exception:
                pass
            raise
        self._stream = st
        return st

    def _close_stream(self) -> None:
        st, self._stream = self._stream, None
        if st is None or self._dev is None:
            return
        try:
            self._dev.deactivateStream(st)
        except Exception as exc:  # pragma: no cover - device teardown
            self._record_error(RuntimeError(
                t("Failed to deactivate the TX stream ({err}) — "
                  "the device may still be radiating", err=str(exc))
            ))
        try:
            self._dev.closeStream(st)
        except Exception:  # pragma: no cover - device teardown
            pass

    def _reap_thread(self) -> bool:
        """Join a TX loop that has already exited. True if none is alive."""
        th = self._thread
        if th is None:
            return True
        if th.is_alive():
            return False
        th.join(timeout=1.0)
        self._thread = None
        self._stuck = False
        self._close_stream()        # the dead loop's stream belongs to us
        return True

    def start(self, iq_int16: np.ndarray) -> None:
        self._ensure_open()
        buf = self._to_cf32(iq_int16)
        if not self._reap_thread():
            if not self._stop_evt.is_set():
                # A live loop that was never asked to stop: just swap the frame.
                with self._lock:
                    self._buf = buf
                self._running = True
                return
            # Asked to stop, still alive: it owns the stream and the device, so
            # starting a second loop would open a second stream on one device.
            raise RuntimeError(t(
                "The previous transmission thread is still running — "
                "TX was not restarted (device {device} is busy)",
                device=self.cfg.device or "driver=hackrf",
            ))
        with self._lock:
            self._buf = buf
        st = self._open_stream()
        stop_evt = threading.Event()
        self._stop_evt = stop_evt
        self._thread = threading.Thread(
            target=self._tx_loop, args=(self._dev, st, stop_evt),
            daemon=True, name="soapy-tx",
        )
        self._running = True
        self._thread.start()

    def _tx_loop(self, dev, st, stop_evt) -> None:
        """Push the frame into the TX stream until stopped or until it fails.

        Every failure — including a device that stops accepting samples — is
        stored in the error channel and clears ``running``: a dead TX loop must
        never look like a transmitting one.
        """
        try:
            try:
                mtu = int(dev.getStreamMTU(st))
            except Exception:
                mtu = 0
            mtu = mtu or 4096
            pos = 0
            fails = 0
            while not stop_evt.is_set():
                with self._lock:
                    buf = self._buf
                if buf is None or len(buf) == 0:
                    stop_evt.wait(0.01)
                    continue
                n = len(buf)
                if pos >= n:
                    pos = 0
                end = min(pos + mtu, n)
                chunk = np.ascontiguousarray(buf[pos:end])
                written = _write_ret(
                    dev.writeStream(st, [chunk], len(chunk), timeoutUs=1_000_000)
                )
                if written > 0:
                    pos += written
                    fails = 0
                else:
                    # <0 is a Soapy error code (SOAPY_SDR_TIMEOUT == -1); 0 means
                    # nothing was accepted. Either way, do not spin silently.
                    fails += 1
                    if fails >= _WRITE_FAIL_LIMIT:
                        raise RuntimeError(t(
                            "SoapySDR writeStream failed {n} times in a row "
                            "(code {code}) — transmission stopped",
                            n=fails, code=written,
                        ))
                if pos >= n:
                    pos = 0
        except BaseException as exc:  # noqa: BLE001 - the thread must not die mute
            self._record_error(exc)
        finally:
            # The stream is owned by the sink object: stop()/start() close it.
            # Closing it here would race with a concurrent stop().
            self._running = False

    def reload(self, iq_int16: np.ndarray) -> None:
        self._ensure_open()
        with self._lock:
            self._buf = self._to_cf32(iq_int16)
        if self._thread is None or not self._thread.is_alive():
            self.start(iq_int16)

    def set_freq(self, freq_hz: float) -> None:
        self.cfg.freq_hz = freq_hz
        if self._dev is not None:
            S = self._soapy()
            self._dev.setFrequency(S.SOAPY_SDR_TX, 0, float(freq_hz))

    def set_gain(self, gain_db: float) -> None:
        self.cfg.gain_db = gain_db
        if self._dev is not None:
            self._apply_gain(gain_db)

    def stop(self) -> None:
        # Called from ScenarioRunner.run()'s finally — it must never raise, or it
        # would mask the scenario outcome. Failures go to the error channel.
        self._stop_evt.set()
        self._running = False
        th = self._thread
        if th is not None:
            th.join(timeout=_STUCK_JOIN_TIMEOUT_S if self._stuck
                    else _STOP_JOIN_TIMEOUT_S)
            if th.is_alive():
                # Keep the reference (dropping it would let the next start()
                # resurrect an orphan) and keep its stream open — closing a
                # stream inside a live writeStream() is a use-after-free.
                self._stuck = True
                self._record_error(RuntimeError(t(
                    "The transmission thread did not stop within {sec} s — "
                    "the device may still be radiating", sec=f"{_STOP_JOIN_TIMEOUT_S:.0f}",
                )))
                return
            self._thread = None
            self._stuck = False
        self._close_stream()

    def close(self) -> None:
        self.stop()
        if self._thread is not None and self._thread.is_alive():
            # Do not drop the device while a thread may still be writing to it:
            # a fresh _ensure_open() would open a second handle on one radio.
            return
        self._dev = None


# ---------------------------------------------------------------------------
#  File backend — write IQ for inspection / GNU Radio replay
# ---------------------------------------------------------------------------
class FileSink(BaseSink):
    """Write the cyclic buffer to a file instead of the air.

    The ``.iq`` format is interleaved int16 (I,Q,I,Q...), compatible with GNU
    Radio / inspectrum. The ``.npy`` format is complex64 numpy.
    """

    def __init__(self, cfg: TxConfig, path: str):
        super().__init__(cfg)
        self.path = path
        self._last: Optional[np.ndarray] = None

    def _write(self, iq_int16: np.ndarray) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        if self.path.endswith(".npy"):
            np.save(self.path, iq_int16.astype(np.complex64))
        else:
            inter = np.empty(iq_int16.size * 2, dtype=np.int16)
            inter[0::2] = iq_int16.real.astype(np.int16)
            inter[1::2] = iq_int16.imag.astype(np.int16)
            inter.tofile(self.path)
        self._last = iq_int16

    def start(self, iq_int16: np.ndarray) -> None:
        self._write(iq_int16)
        self._running = True

    def reload(self, iq_int16: np.ndarray) -> None:
        self._write(iq_int16)

    def set_freq(self, freq_hz: float) -> None:
        self.cfg.freq_hz = freq_hz

    def set_gain(self, gain_db: float) -> None:
        self.cfg.gain_db = gain_db

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
#  Null backend — dry-run / GUI without hardware
# ---------------------------------------------------------------------------
class NullSink(BaseSink):
    def start(self, iq_int16: np.ndarray) -> None:
        self._running = True

    def reload(self, iq_int16: np.ndarray) -> None:
        pass

    def set_freq(self, freq_hz: float) -> None:
        self.cfg.freq_hz = freq_hz

    def set_gain(self, gain_db: float) -> None:
        self.cfg.gain_db = gain_db

    def stop(self) -> None:
        self._running = False


def soapy_enumerate() -> list:
    """List the available SoapySDR devices (empty if SoapySDR is not installed)."""
    try:
        import SoapySDR
    except ImportError:
        return []
    try:
        return [dict(d) for d in SoapySDR.Device.enumerate()]
    except Exception:
        return []


def make_sink(kind: str, cfg: TxConfig, file_path: Optional[str] = None) -> BaseSink:
    kind = kind.lower()
    if kind == "pluto":
        return PlutoSink(cfg)
    if kind == "soapy":
        return SoapySink(cfg)
    if kind == "file":
        return FileSink(cfg, file_path or "out.iq")
    if kind == "null":
        return NullSink(cfg)
    raise ValueError(
        t("Unknown backend '{kind}' (pluto|soapy|file|null)", kind=kind)
    )
