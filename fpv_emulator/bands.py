"""FPV band / channel frequency tables and hardware-range guard.

Loads ``config/bands.yaml`` into a queryable table. Channel names are
case-insensitive (``"r1"`` == ``"R1"``). Frequencies are stored in Hz.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import yaml

from .i18n import t

_DEFAULT_BANDS_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "bands.yaml"
)


@dataclass(frozen=True)
class Channel:
    """A single tunable channel."""

    band: str          # band key, e.g. "R"
    name: str          # channel name, e.g. "R1"
    freq_hz: float     # centre frequency in Hz
    group: str         # coarse group, e.g. "5.8G"

    @property
    def freq_mhz(self) -> float:
        return self.freq_hz / 1e6

    @property
    def id(self) -> str:
        return self.name


@dataclass
class HardwareRange:
    name: str
    min_hz: float
    max_hz: float
    chip: str
    #: how far the device tunes beyond its rating, if it does (HackRF: 7.25 GHz)
    tune_max_hz: Optional[float] = None

    def covers(self, freq_hz: float) -> bool:
        return self.min_hz <= freq_hz <= self.max_hz


@dataclass
class BandTable:
    """All bands/channels plus hardware-range presets."""

    bands: Dict[str, dict] = field(default_factory=dict)
    hw_ranges: Dict[str, HardwareRange] = field(default_factory=dict)
    _by_channel: Dict[str, Channel] = field(default_factory=dict, repr=False)
    # bare name -> ["BAND:CHANNEL", ...] for names used by more than one band
    _ambiguous: Dict[str, List[str]] = field(default_factory=dict, repr=False)

    # -- construction -------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict) -> "BandTable":
        hw = {}
        for key, r in (data.get("hardware_ranges") or {}).items():
            hw[key] = HardwareRange(
                name=key,
                min_hz=float(r["min_mhz"]) * 1e6,
                max_hz=float(r["max_mhz"]) * 1e6,
                chip=str(r.get("chip", "")),
                tune_max_hz=(float(r["tune_max_mhz"]) * 1e6
                             if r.get("tune_max_mhz") else None),
            )
        table = cls(bands=dict(data.get("bands") or {}), hw_ranges=hw)
        table._index()
        return table

    def _index(self) -> None:
        self._by_channel = {}
        self._ambiguous = {}
        for band_key, band in self.bands.items():
            group = band.get("group", "")
            for ch_name, freq_mhz in (band.get("channels") or {}).items():
                ch = Channel(
                    band=band_key,
                    name=ch_name,
                    freq_hz=float(freq_mhz) * 1e6,
                    group=group,
                )
                # index by bare channel name and by "BAND:CHANNEL"
                bare = ch_name.upper()
                qualified = f"{band_key.upper()}:{bare}"
                if bare in self._by_channel:
                    # the same bare name in several bands (C1..C8 live in G13/G09/
                    # G24/G33). Keep the FIRST binding and remember the collision so
                    # a bare lookup fails loudly instead of silently transmitting on
                    # another band's frequency.
                    first = self._by_channel[bare]
                    alts = self._ambiguous.setdefault(
                        bare, [f"{first.band.upper()}:{bare}"]
                    )
                    if qualified not in alts:
                        alts.append(qualified)
                else:
                    self._by_channel[bare] = ch
                self._by_channel[qualified] = ch

    # -- queries ------------------------------------------------------------
    def is_ambiguous(self, name: str) -> bool:
        """True when a bare channel name is used by more than one band."""
        key = name.strip().upper()
        return ":" not in key and key in self._ambiguous

    def qualified_names(self, name: str) -> List[str]:
        """The ``"BAND:CHANNEL"`` alternatives of an ambiguous bare name."""
        return list(self._ambiguous.get(name.strip().upper(), []))

    def channel(self, name: str) -> Channel:
        """Look up a channel by ``"R1"`` or ``"R:R1"`` (case-insensitive)."""
        key = name.strip().upper()
        if ":" not in key and key in self._ambiguous:
            raise KeyError(
                t(
                    "Channel name '{name}' is ambiguous — use a qualified name: {list}",
                    name=name,
                    list=", ".join(self._ambiguous[key]),
                )
            )
        if key not in self._by_channel:
            raise KeyError(
                t(
                    "Unknown channel '{name}'. Available: {list}",
                    name=name,
                    list=", ".join(sorted(self.list_channel_names())),
                )
            )
        return self._by_channel[key]

    def list_channel_names(self) -> List[str]:
        return [k for k in self._by_channel if ":" not in k]

    def list_bands(self) -> List[str]:
        return list(self.bands.keys())

    def channels_in_band(self, band_key: str) -> List[Channel]:
        band = self.bands.get(band_key) or self.bands.get(band_key.upper())
        if band is None:
            raise KeyError(t("Unknown band '{band}'", band=band_key))
        group = band.get("group", "")
        return [
            Channel(band=band_key, name=n, freq_hz=float(f) * 1e6, group=group)
            for n, f in (band.get("channels") or {}).items()
        ]

    def channels_in_group(self, group: str) -> List[Channel]:
        out: List[Channel] = []
        for band_key, band in self.bands.items():
            if band.get("group") == group:
                out.extend(self.channels_in_band(band_key))
        return sorted(out, key=lambda c: c.freq_hz)

    def groups(self) -> List[str]:
        seen: List[str] = []
        for band in self.bands.values():
            g = band.get("group", "")
            if g and g not in seen:
                seen.append(g)
        return seen

    # -- hardware guard -----------------------------------------------------
    def hw_range(self, preset: str) -> HardwareRange:
        if preset not in self.hw_ranges:
            raise KeyError(
                t("No HW preset '{preset}'. Available: {list}",
                  preset=preset, list=list(self.hw_ranges))
            )
        return self.hw_ranges[preset]

    def check_reachable(self, freq_hz: float, preset: str) -> Tuple[bool, Optional[str]]:
        """Return (ok, warning). warning is a translated string when out of range."""
        rng = self.hw_range(preset)
        if rng.covers(freq_hz):
            return True, None
        if rng.tune_max_hz and rng.max_hz < freq_hz <= rng.tune_max_hz:
            return False, t(
                "Frequency {freq} MHz is above the {chip} rating ({max} MHz). It "
                "tunes up to {tune} MHz unofficially, with far less output power. "
                "On this bench the detector still saw it at 6500, 7000 and 7250 MHz "
                "with the power at maximum (0 dB, RF amplifier on) — keep it there "
                "up here.",
                freq=f"{freq_hz/1e6:.1f}", chip=rng.chip,
                max=f"{rng.max_hz/1e6:.0f}", tune=f"{rng.tune_max_hz/1e6:.0f}")
        msg = t(
            "Frequency {freq} MHz is outside the {chip} range ({min}–{max} MHz).",
            freq=f"{freq_hz/1e6:.1f}",
            chip=rng.chip,
            min=f"{rng.min_hz/1e6:.0f}",
            max=f"{rng.max_hz/1e6:.0f}",
        )
        hint = (
            t("An AD9361 mod or an external up-converter is required.")
            if preset == "stock"
            else t("Check the settings / up-converter.")
        )
        return False, msg + " " + hint


def load_band_table(path: Optional[str] = None) -> BandTable:
    """Load the band table from YAML (defaults to ``config/bands.yaml``)."""
    path = path or _DEFAULT_BANDS_YAML
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return BandTable.from_dict(data)
