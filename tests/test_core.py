"""Offline unit tests for the emulator core (no hardware needed)."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpv_emulator.bands import load_band_table
from fpv_emulator.fm import fm_modulate, to_int16_iq, occupied_bandwidth_hz
from fpv_emulator.signal_gen import (
    DroneSpec,
    generate_frame_iq,
    generate_multi_drone_iq,
)
from fpv_emulator.video import (
    PAL50,
    NTSC60,
    generate_composite,
    generate_composite_color,
    list_patterns,
    get_standard,
)


# --------------------------- bands ----------------------------------------
def test_band_lookup_and_case_insensitive():
    bt = load_band_table()
    assert bt.channel("R1").freq_mhz == pytest.approx(5658.0)
    assert bt.channel("r1").freq_hz == bt.channel("R1").freq_hz
    assert bt.channel("R:R8").freq_mhz == pytest.approx(5917.0)


def test_ambiguous_bare_channel_name_raises():
    """'C4' exists in the 1.3G, 900M, 2.4G and 3.3G tables.

    Resolving it silently picks whichever band was indexed last and transmits up
    to 2.5 GHz away from what the operator asked for — it has to be refused, while
    every unambiguous spelling keeps working.
    """
    bt = load_band_table()
    with pytest.raises((KeyError, ValueError)) as exc:
        bt.channel("C4")
    msg = str(exc.value)
    assert "C4" in msg
    # the message must offer the qualified alternatives, not just complain
    assert msg.count(":C4") >= 2, msg

    assert bt.channel("G13:C4").freq_mhz == pytest.approx(1200.0)
    assert bt.channel("g13:c4").freq_hz == bt.channel("G13:C4").freq_hz
    assert bt.channel("G09:C4").freq_mhz == pytest.approx(945.0)
    assert bt.channel("R1").freq_mhz == pytest.approx(5658.0)
    assert bt.channel("r1").freq_hz == bt.channel("R1").freq_hz
    assert bt.channel("R:R8").freq_mhz == pytest.approx(5917.0)


def test_hw_range_guard():
    bt = load_band_table()
    ok_stock, warn = bt.check_reachable(5800e6, "stock")
    assert not ok_stock and warn is not None
    ok_hacked, _ = bt.check_reachable(5800e6, "hacked")
    assert ok_hacked
    ok_low, _ = bt.check_reachable(1200e6, "stock")
    assert ok_low


def test_group_and_band_listing():
    bt = load_band_table()
    assert "5.8G" in bt.groups()
    rb = bt.channels_in_band("R")
    assert len(rb) == 8
    assert rb[0].freq_mhz == pytest.approx(5658.0)


# --------------------------- video ----------------------------------------
@pytest.mark.parametrize("std", [PAL50, NTSC60])
def test_composite_length_is_frame(std):
    fs = 8e6
    comp = generate_composite("bars", std, fs)
    n_line = int(round(std.line_us * 1e-6 * fs))
    assert comp.size == n_line * std.total_lines


@pytest.mark.parametrize("pattern", list_patterns())
def test_composite_levels_in_range(pattern):
    comp = generate_composite(pattern, PAL50, 8e6)
    assert comp.min() >= PAL50.sync_level - 1e-6
    assert comp.max() <= PAL50.white_level + 0.2  # +burst tolerance
    # sync pulse present
    assert np.isclose(comp.min(), PAL50.sync_level, atol=1e-6)


def test_line_rate_close_to_spec():
    fs = 8e6
    n_line = int(round(PAL50.line_us * 1e-6 * fs))
    line_rate = fs / n_line
    assert line_rate == pytest.approx(15625.0, rel=0.01)


def test_low_fs_raises():
    with pytest.raises(ValueError):
        generate_composite("bars", PAL50, 100e3)  # too few samples per line


# --------------------------- fm -------------------------------------------
def test_fm_constant_gives_single_tone():
    fs = 4e6
    dev = 1e6
    v = np.full(200000, 0.25)          # constant
    iq = fm_modulate(v, fs, dev, center=False)
    # constant envelope
    assert np.allclose(np.abs(iq), 1.0, atol=1e-4)
    # frequency = dev * v
    spec = np.abs(np.fft.fftshift(np.fft.fft(iq)))
    freqs = np.fft.fftshift(np.fft.fftfreq(iq.size, 1 / fs))
    peak = freqs[np.argmax(spec)]
    assert peak == pytest.approx(dev * 0.25, abs=fs / iq.size * 4)


def test_fm_centering_keeps_the_cyclic_seam_continuous():
    """center=True is what makes one frame loop seamlessly in the cyclic buffer.

    A constant envelope proves nothing here — fm_modulate produces one for either
    value of ``center``. What matters is the phase where the device wraps from the
    last sample of the frame back to the first: with center=True the wrap simply
    continues the modulation (seam error ~4e-8 rad, mean phase step ~2e-6
    rad/sample); with center=False the same frame jumps ~1.1 rad at the seam and
    drifts 0.32 rad/sample, i.e. a spur comb repeating at the 50 Hz field rate.
    Measured on the production profile: color_bars + PAL50 + 20 MSPS + 6 MHz.
    """
    fs, dev = 20e6, 6e6
    comp = generate_composite_color("color_bars", PAL50, fs)
    iq = fm_modulate(comp, fs, dev, center=True)

    # the phase step fm_modulate applies to sample 0 — exactly what the wrap
    # from the last sample back to the first has to reproduce
    v = np.asarray(comp, dtype=np.float64)
    v -= v.mean()
    natural = 2.0 * np.pi * dev * v[0] / fs
    seam = np.angle(complex(iq[0]) * np.conj(complex(iq[-1])))
    seam_err = np.angle(np.exp(1j * (seam - natural)))
    assert abs(seam_err) < 1e-4, f"seam discontinuity {seam_err:.6f} rad"

    # and no residual carrier offset: the mean phase step across the frame is ~0
    phase = np.unwrap(np.angle(iq.astype(np.complex128)))
    mean_step = float(np.mean(np.diff(phase)))
    assert abs(mean_step) < 1e-5, f"carrier offset {mean_step:.6e} rad/sample"

    assert np.allclose(np.abs(iq), 1.0, atol=1e-4)   # still a constant envelope


def test_to_int16_range():
    iq = np.exp(1j * np.linspace(0, 100, 1000)).astype(np.complex64)
    out = to_int16_iq(iq, scale=2 ** 14)
    assert out.real.max() <= 32767 and out.real.min() >= -32768
    assert np.abs(out).max() <= 2 ** 14 + 2


def test_carson_bandwidth():
    bw = occupied_bandwidth_hz(6e6, 5e6)
    assert bw == pytest.approx(2 * (3e6 + 5e6))


# --------------------------- signal_gen -----------------------------------
def test_frame_iq_matches_composite():
    fs = 8e6
    frame = generate_frame_iq("grid", PAL50, fs, 6e6)
    comp = generate_composite("grid", PAL50, fs)
    assert frame.iq.size == comp.size
    assert frame.iq.dtype == np.complex64
    assert frame.duration_s == pytest.approx(1.0 / PAL50.fps, rel=0.01)


def test_multi_drone_peak_normalised():
    std = get_standard("PAL50")
    specs = [
        DroneSpec("bars", offset_hz=-4e6, level_db=0),
        DroneSpec("grid", offset_hz=0.0, level_db=-3),
        DroneSpec("checker", offset_hz=4e6, level_db=-6),
    ]
    frame = generate_multi_drone_iq(specs, std, 20e6, 6e6)
    assert np.max(np.abs(frame.iq)) <= 1.0 + 1e-6
    assert frame.iq.size > 0


_MULTI_N = 1 << 16          # FFT length used to locate the drone carriers


def _lobe_centroids(iq, count, fs=20e6):
    """Power centroids of the ``count`` strongest lobes, strongest first sorted
    by frequency. Summing magnitudes over half-planes cannot fail (they are
    non-negative by construction) — the centroid says *where* the energy is."""
    freqs = np.fft.fftshift(np.fft.fftfreq(_MULTI_N, 1 / fs))
    power = np.abs(np.fft.fftshift(np.fft.fft(iq[:_MULTI_N]))) ** 2
    work = np.convolve(power, np.ones(64) / 64, mode="same")   # find lobes, not lines
    out = []
    for _ in range(count):
        f0 = freqs[int(np.argmax(work))]
        near = np.abs(freqs - f0) <= 1.5e6
        out.append(float(np.sum(freqs[near] * power[near]) / np.sum(power[near])))
        work[np.abs(freqs - f0) <= 2.5e6] = 0.0                # next lobe
    return sorted(out)


def test_multi_drone_offsets_land_on_the_requested_carriers():
    std = get_standard("PAL50")
    fs, dev, off = 20e6, 3e6, 5e6
    bin_hz = fs / _MULTI_N

    # Reference: the same modulation with no offset. Its lobe does not sit on DC
    # (the blanking level is ~0.7 MHz below the frame mean), so the two shifted
    # carriers are measured against it rather than against 0 Hz.
    ref_frame = generate_multi_drone_iq([DroneSpec("bars", 0.0, 0.0)], std, fs, dev)
    ref = _lobe_centroids(ref_frame.iq, 1, fs)[0]

    specs = [DroneSpec("bars", offset_hz=-off, level_db=0),
             DroneSpec("bars", offset_hz=off, level_db=0)]
    frame = generate_multi_drone_iq(specs, std, fs, dev)
    low, high = _lobe_centroids(frame.iq, 2, fs)

    # both carriers within a few bins (~0.9 kHz) of where they were asked for
    assert low == pytest.approx(ref - off, abs=3 * bin_hz)
    assert high == pytest.approx(ref + off, abs=3 * bin_hz)
    assert high - low == pytest.approx(2 * off, abs=2 * bin_hz)


# ------------------------- HackRF beyond its rating ------------------------
def test_the_hackrf_range_says_unofficial_above_6_ghz_and_out_above_7250():
    from fpv_emulator.bands import load_band_table
    bt = load_band_table()
    assert bt.check_reachable(5800e6, "hackrf") == (True, None)
    ok, warn = bt.check_reachable(6500e6, "hackrf")
    assert not ok and "unofficially" in warn
    ok, warn = bt.check_reachable(7300e6, "hackrf")
    assert not ok and "outside" in warn
    # the Pluto presets have no unofficial band
    ok, warn = bt.check_reachable(6500e6, "hacked")
    assert not ok and "unofficially" not in warn
