"""The host side of reaching a Pluto: the native libiio, and a URI that answers.

pylibiio (the ``iio`` module) is a ctypes wrapper and nothing more. When the
native libiio is missing — which is what a freshly installed Windows looks like —
``import iio`` does not raise ImportError: ctypes is handed ``None`` for the
library path and raises ``TypeError: LoadLibrary() argument 1 must be str, not
None``. Linux and macOS fail on the first missing symbol instead, with an
AttributeError. pyadi imports iio at module level, so ``import adi`` fails the
same way. The probe then printed "Pluto not found", which sends the operator to
check a cable when the problem is a library on this computer.

Opening a board failed just as unhelpfully. On Windows libiio reports every
failed open as errno 0, so an unreachable board read "OSError: [Errno 0] No
error", and pyadi reduces any failure to open to "No device found". Over USB
there is a trap on top of that: a board takes ONE context at a time, so a
context still open for inspection makes pyadi's own open fail — with that same
bare message.

So the hardware imports go through here, and a failed open is described in
words, together with what is actually on the USB bus.
"""
from __future__ import annotations

import re
import sys
from ctypes.util import find_library
from dataclasses import dataclass, field
from importlib import metadata
from typing import Dict, List, Optional

from .i18n import t

#: where Analog Devices publishes libiio, the Windows installers included
RELEASES_URL = "https://github.com/analogdevicesinc/libiio/releases"

#: how libiio's USB backend describes a board: "0456:b673 (Analog Devices Inc.
#: PlutoSDR (ADALM-PLUTO)), serial=..." — the part in the parentheses is the name
_USB_DESCRIPTION = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{4} \((.*)\)(?:, serial=.*)?$")


class HostLibraryError(RuntimeError):
    """The iio module is there, but the native libiio behind it is missing or unusable."""


def _on_windows() -> bool:
    return sys.platform == "win32"


def bindings_version() -> Optional[str]:
    """``major.minor`` of the installed pylibiio, e.g. ``"0.25"`` — the libiio it wraps."""
    try:
        m = re.match(r"(\d+)\.(\d+)", metadata.version("pylibiio"))
    except Exception:
        return None
    return f"{m.group(1)}.{m.group(2)}" if m else None


def native_library() -> Optional[str]:
    """Where the native libiio is, looked up the way pylibiio looks it up."""
    try:
        return find_library("libiio.dll" if _on_windows() else "iio")
    except Exception:
        return None


def install_hint(ver: Optional[str]) -> str:
    """How to install the libiio that pylibiio ``ver`` was written for."""
    shown = ver or "0.x"
    url = f"{RELEASES_URL}/tag/v{ver}" if ver else RELEASES_URL
    if _on_windows():
        text = t("Install libiio {ver} for Windows from Analog Devices — the file "
                 "ending in setup.exe at {url} — then restart the emulator.",
                 ver=shown, url=url)
    else:
        text = t("Install libiio {ver} (Debian/Ubuntu: apt install libiio0; other "
                 "systems: {url}), then restart the emulator.", ver=shown, url=url)
    if shown.startswith("0."):
        # The newest release sits at the top of the releases page, and it is 1.x.
        text += " " + t("Take that version, not 1.x: pylibiio {ver} was written for "
                        "libiio 0.x, and 1.x changed the API.", ver=shown)
    return text


def native_library_problem(exc: BaseException) -> str:
    """Say what is wrong with the native libiio, given how ``import iio`` failed."""
    ver = bindings_version()
    path = native_library()
    if not path:
        return t("The libiio library is not installed on this computer. The iio "
                 "module in .venv (pylibiio {ver}) is only the Python side of it and "
                 "does nothing on its own. {install}",
                 ver=ver or "?", install=install_hint(ver))
    return t("libiio is installed ({path}), but pylibiio {ver} cannot use it: {err}. "
             "That is usually a libiio of another version — 1.x changed the API — or "
             "a 32-bit build under a 64-bit Python. {install}",
             path=path, ver=ver or "?", err=f"{type(exc).__name__}: {exc}",
             install=install_hint(ver))


def import_iio():
    """``import iio``, with a missing or unusable native libiio said in words.

    ImportError is left alone: it still means the Python package itself is
    absent, and the callers already say "pip install" for that.
    """
    try:
        import iio  # noqa: WPS433 — hardware dependency, imported lazily
    except ImportError:
        raise
    except Exception as exc:      # TypeError / AttributeError / OSError from ctypes
        raise HostLibraryError(native_library_problem(exc)) from exc
    return iio


def import_adi():
    """``import adi`` — after iio, which pyadi imports at module level anyway.

    Importing iio first is what turns a missing libiio into a sentence instead
    of a ctypes TypeError raised from inside pyadi.
    """
    import_iio()
    import adi  # noqa: WPS433
    return adi


# --------------------------------------------------------------------------
#  a board that did not open
# --------------------------------------------------------------------------
@dataclass
class OpenFailure:
    """Why a board did not open, in words, and what to try instead."""

    uri: str
    reason: str
    hints: List[str] = field(default_factory=list)

    def message(self) -> str:
        head = t("Pluto not found ({uri}): {err}", uri=self.uri, err=self.reason)
        return " ".join([head + "."] + self.hints)


def _library_words(exc: BaseException) -> str:
    """What the library itself said, when it said anything.

    On Windows libiio reports every failed open as errno 0 — "[Errno 0] No
    error" — and pyadi reduces all of them to "No device found". Repeating
    either only pushes the useful part of the message further away.
    """
    if isinstance(exc, OSError) and not exc.errno:
        return ""
    text = str(exc).strip()
    return "" if text in ("", "No device found") else text


def usb_boards() -> Optional[Dict[str, str]]:
    """IIO devices on USB as ``{uri: name}``; None when the scan cannot run.

    A board that another program holds open is not listed: libiio cannot read
    its descriptors then. The scan takes a couple of seconds, which is why only
    the failure path pays for it.
    """
    try:
        found = import_iio().scan_contexts()
    except Exception:
        return None
    out = {}
    for uri, desc in found.items():
        if uri.startswith("usb:"):
            m = _USB_DESCRIPTION.match(desc)
            out[uri] = m.group(1) if m else desc
    return out


def open_failure(uri: str, exc: BaseException) -> OpenFailure:
    """Describe a failed open of ``uri`` — with what the USB bus holds right now."""
    scheme, _, rest = uri.partition(":")
    words = _library_words(exc)
    if scheme == "ip":
        reason = t("nothing answers at {host}", host=rest or uri)
    elif scheme == "usb":
        reason = t("the USB device could not be opened — it is unplugged, or another "
                   "program is holding it (a second copy of the emulator, IIO "
                   "Oscilloscope, other SDR software)")
    else:
        return OpenFailure(uri=uri, reason=words or f"{type(exc).__name__}: {exc}")
    if words:
        reason += f" ({words})"

    hints: List[str] = []
    visible = usb_boards()
    if visible is None:           # could not look: better no hint than a wrong one
        return OpenFailure(uri=uri, reason=reason)
    others = {u: name for u, name in visible.items() if u != uri}
    if len(others) == 1 and uri != "usb:":      # "usb:" itself just failed — name it
        (other, name), = others.items()
        hints.append(t("There is a board on USB, though: {found}. Set the URI to usb: "
                       "to use it — that means the board on USB, whatever address it "
                       "gets.", found=f"{other} ({name})"))
    elif others:
        hints.append(t("Boards on USB: {found}. Set the URI to one of these.",
                       found=", ".join(f"{u} ({name})" for u, name in others.items())))
    elif uri not in visible:
        hints.append(t("No Pluto is visible on USB. Check the cable and give the board "
                       "~20 s to boot after plugging it in; on Windows the Pluto USB "
                       "drivers (PlutoSDR-M2k-USB-Drivers from Analog Devices) must be "
                       "installed."))
    # 192.168.2.x is the board's own address on its USB network link. A board that
    # answers over USB but not there has that link down — on Windows, a network
    # adapter that failed to start, which nothing else points at.
    if others and scheme == "ip" and rest.startswith("192.168.2.") and _on_windows():
        hints.append(t("The address {host} goes through the board's USB network "
                       "adapter — «PlutoSDR USB Ethernet/RNDIS Gadget» in Device "
                       "Manager. If it shows an error there, replug the board or try "
                       "another USB port.", host=rest))
    return OpenFailure(uri=uri, reason=reason, hints=hints)
