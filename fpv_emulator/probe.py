"""Detect a connected Pluto and report chip / tuning range.

Identifies a connected Pluto+, reads the context attributes (model, firmware,
serial) and functionally probes the real TX tuning limits (to tell whether it is
a stock AD9363 or an AD9361 mod with access to 5.8 GHz). Hardware dependencies
are imported lazily — without a device the function still returns an informative
result.
"""
from __future__ import annotations

import gc
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import iio_host
from .i18n import t


@dataclass
class ProbeResult:
    connected: bool
    uri: str
    attrs: Dict[str, str] = field(default_factory=dict)
    tx_lo_min_hz: Optional[float] = None
    tx_lo_max_hz: Optional[float] = None
    inferred_preset: Optional[str] = None      # "stock" | "hacked" | None
    reaches_5g8: bool = False
    messages: List[str] = field(default_factory=list)
    error: Optional[str] = None
    # A reachable device whose driver layer then failed is NOT "not found" —
    # saying so sends the user hunting for a cable when the problem is software.
    ctx_ok: bool = False                       # the raw IIO context opened
    traceback_text: Optional[str] = None       # full traceback of the first failure
    lib_versions: Dict[str, str] = field(default_factory=dict)
    can_transmit: Optional[bool] = None        # False when the image is RX-only
    # What the board says it runs, as a ready-made line. Kept apart from
    # inferred_preset on purpose: that one is the AD9363-vs-AD9361 tuning range
    # ('stock'/'hacked'), a different axis that happens to share the vocabulary.
    firmware_text: Optional[str] = None
    firmware_key: Optional[str] = None         # "" when the version is unrecognised
    # The board was never asked, because this computer cannot ask: the native
    # libiio (or the iio module) is missing. Kept apart from error so that the
    # summary does not say "not found" — which is the same cable hunt again.
    host_error: Optional[str] = None
    # The range check failed on a board that had answered. The summary of a
    # connected board never showed error, so this failure used to vanish.
    range_error: Optional[str] = None

    def summary(self) -> str:
        if self.host_error:
            return "\n".join([
                t("Pluto was not checked: the problem is on this computer, "
                  "not with the board."),
                "  " + self.host_error,
            ])
        if not self.connected:
            if self.ctx_ok:
                # we did reach it — the failure is on our side of the wire
                head = t("Pluto is reachable at {uri}, but opening it failed: {err}",
                         uri=self.uri, err=self.error or "?")
            else:
                head = t("Pluto not found ({uri}): {err}",
                         uri=self.uri, err=self.error or t("no context"))
            lines = [head]
            for k, v in self.lib_versions.items():
                lines.append(f"  {k}: {v}")
            for m in self.messages:
                lines.append(f"  · {m}")
            if self.traceback_text:
                lines.append("  " + t("Details (send this when reporting the problem):"))
                lines.extend("    " + ln for ln in self.traceback_text.strip().splitlines())
            return "\n".join(lines)
        lines = [t("Pluto connected: {uri}", uri=self.uri)]
        for k in ("hw_model", "hw_serial", "fw_version"):
            if k in self.attrs:
                lines.append(f"  {k}: {self.attrs[k]}")
        if self.firmware_text:
            lines.append("  " + self.firmware_text)
        if self.tx_lo_max_hz:
            lines.append(
                "  " + t("TX LO: {min} – {max} MHz",
                         min=f"{self.tx_lo_min_hz/1e6:.0f}",
                         max=f"{self.tx_lo_max_hz/1e6:.0f}")
            )
        if self.inferred_preset:
            lines.append("  " + t("Likely type: {preset}", preset=self.inferred_preset))
        # Only claim anything about 5.8 GHz when the range test actually ran —
        # otherwise "NO" is not a measurement, it is an uninitialised default.
        if self.tx_lo_max_hz is not None:
            lines.append(
                "  " + t("Direct 5.8 GHz: {answer}",
                         answer=t("YES") if self.reaches_5g8
                         else t("NO (a mod / up-converter is required)"))
            )
        if self.range_error:
            lines.append("  " + t("TX range check failed: {err}", err=self.range_error))
        for m in self.messages:
            lines.append(f"  · {m}")
        if self.range_error and self.traceback_text:
            lines.append("  " + t("Details (send this when reporting the problem):"))
            lines.extend("    " + ln for ln in self.traceback_text.strip().splitlines())
        return "\n".join(lines)


# frequencies for the functional limit check (Hz)
_TEST_FREQS_HZ = [70e6, 325e6, 1200e6, 2450e6, 3300e6, 3800e6, 5800e6, 6000e6]


def probe(uri: str = "ip:192.168.2.1", do_range_test: bool = True) -> ProbeResult:
    res = ProbeResult(connected=False, uri=uri)

    # 1) context via libiio (pylibiio)
    ctx = None
    layout = None
    try:
        iio = iio_host.import_iio()
        res.lib_versions["libiio (host)"] = ".".join(str(x) for x in iio.version[:2])
        try:
            ctx = iio.Context(uri)
        except Exception as exc:
            # A board that does not answer is not a bug, and its traceback says
            # nothing: on Windows it is "OSError: [Errno 0] No error" whatever
            # the cause. Say it in words, with what is on the USB bus — and skip
            # the range check, which would only wait out the same timeout again.
            failure = iio_host.open_failure(uri, exc)
            res.error = failure.reason
            res.messages.extend(failure.hints)
            return res
        res.ctx_ok = True
        res.connected = True
        for name in ("hw_model", "hw_serial", "fw_version", "uri"):
            try:
                res.attrs[name] = ctx.attrs.get(name, "")
            except Exception:
                pass
        try:
            res.lib_versions["libiio (device)"] = ".".join(str(x) for x in ctx.version[:2])
        except Exception:
            pass
        # Which IIO devices this image exposes. On alternative firmwares (Tezuka,
        # PlutoSky) or a custom FPGA image the names differ, and some RX-oriented
        # images have no transmit DMA at all — worth saying outright rather than
        # letting it fail later with an unrelated-looking error.
        try:
            from .firmware import identify
            fw = identify(ctx)
            res.firmware_text = fw.describe()
            res.firmware_key = fw.key
            from .iio_layout import detect_layout
            layout = detect_layout(ctx)
            res.messages.extend(layout.describe().splitlines())
            res.can_transmit = layout.can_transmit
        except Exception:
            pass
    except ImportError:
        res.host_error = t("pylibiio (the iio module) is not installed. Install it "
                           "with: pip install pyadi-iio pylibiio")
        return res
    except iio_host.HostLibraryError as exc:
        res.host_error = str(exc)
        return res
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {exc}"
        res.traceback_text = traceback.format_exc()

    # Close it before pyadi opens its own. Over USB a board takes ONE context at
    # a time: with this one still alive, the range check below failed with "No
    # device found" — and silently, since the board had answered.
    ctx = None
    gc.collect()

    # 2) functional TX limit check via pyadi
    if do_range_test and res.ctx_ok:
        sdr = None
        try:
            import adi  # type: ignore
            # Open it the way the transmitter would. A board whose IIO devices
            # are named differently must not fail here while `tx` works — a
            # diagnostic that stops predicting the thing it exists to predict is
            # worse than no diagnostic. The layout read in step 1 is reused, so
            # no second context is opened to find it again.
            pluto_cls = adi.Pluto
            if layout is not None:
                try:
                    from .iio_layout import pluto_class_for
                    pluto_cls = pluto_class_for(layout)
                except Exception:
                    pass
            sdr = pluto_cls(uri=uri)
            res.connected = True
            reachable: List[float] = []
            for f in _TEST_FREQS_HZ:
                try:
                    sdr.tx_lo = int(f)
                    if abs(int(sdr.tx_lo) - int(f)) < 1e6:
                        reachable.append(f)
                except Exception:
                    pass
            if reachable:
                res.tx_lo_min_hz = min(reachable)
                res.tx_lo_max_hz = max(reachable)
                res.reaches_5g8 = any(f >= 5.7e9 for f in reachable)
                res.inferred_preset = "hacked" if res.reaches_5g8 else "stock"
        except ImportError:
            res.messages.append(
                t("pyadi-iio is not installed — skipping the TX range check")
            )
        except Exception as exc:
            res.range_error = f"{type(exc).__name__}: {exc}"
            res.traceback_text = traceback.format_exc()
        finally:
            # Give the board back before returning, for the same reason: Start
            # pressed right after a probe must not find it still held over USB.
            sdr = None
            gc.collect()

    return res
