"""FPV analog-video FM emulator for ADALM-Pluto+.

A generator of test analog FPV video signals (composite video + FM) for checking
how FPV detectors behave. The core (video/fm/signal_gen/bands/scenarios) does not
depend on hardware — it can be run offline and covered by tests. Transmission
over the air goes through the Pluto backend (pyadi-iio) or into a file.
"""

#: Single source of truth. Bump it together with the CHANGELOG entry — a test
#: checks that the two agree. Read without importing by run_gui.py, which needs
#: it precisely when this package is what failed to import.
__version__ = "0.5.3"

from .bands import BandTable, load_band_table
from .video import VideoStandard, PAL50, NTSC60, list_patterns, generate_composite
from .fm import fm_modulate, to_int16_iq, occupied_bandwidth_hz
from .signal_gen import (
    FrameSignal,
    generate_frame_iq,
    generate_multi_drone_iq,
)

__all__ = [
    "__version__",
    "BandTable",
    "load_band_table",
    "VideoStandard",
    "PAL50",
    "NTSC60",
    "list_patterns",
    "generate_composite",
    "fm_modulate",
    "to_int16_iq",
    "occupied_bandwidth_hz",
    "FrameSignal",
    "generate_frame_iq",
    "generate_multi_drone_iq",
]
