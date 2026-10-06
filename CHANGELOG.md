# Changelog

Versions follow [semantic versioning](https://semver.org): `MAJOR.MINOR.PATCH`.
While the number starts with `0.`, the tool is still a bench instrument under
active development — the minor number moves whenever behaviour on air, or the
meaning of a setting, changes.

The version lives in one place, `fpv_emulator/__init__.py`. **Bump it together
with the entry below** — a test fails if the two disagree, because the point of a
version is to answer "what exactly do you have running?" from a screenshot or a
pasted log, and a number that lags is worse than none.

Shown in the window title and the first line of the event log, in the startup
error dialog, at the top of `probe`, and via `--version`.

---

## 0.6.0 — 2026-10-06

### Added
- **HackRF One as its own backend, `hackrf`** — in the GUI's backend list, in
  `tx --backend hackrf` and in `probe --device hackrf`. It talks to libhackrf
  directly (ctypes), so it works without SoapySDR, which had no installer path on
  Windows short of PothosSDR. The frame is looped on the host by libhackrf's
  transfer callback; retuning, power and frame changes apply on air.
- **Power keeps its meaning on a HackRF:** one slider dB is one dB from the maximum —
  0..−47 dB on TXVGA, 0..−61 dB with the new **RF amplifier +14 dB** switch (GUI)
  or `--amp` (CLI). The amplifier is switched on only when TXVGA alone is not
  enough. (`soapy` still stretches −89..0 over the device's range.)
- **A HackRF serial** field / `--serial`, for a bench with more than one board.
- **The GUI offers only what the selected backend reads.** With `hackrf`: no Pluto
  URI, no firmware profile, no AD9363/AD9361 range, the sample rate tops out at
  20 MSPS and the power slider at -47 dB (-61 dB with the amplifier). With `pluto`:
  no HackRF serial, amplifier or range, and the usual 61.44 MSPS / -89 dB. A field
  the sink ignores, left editable, reads as a setting that applied.
- **«Probe HackRF»** reads the board, firmware and serial, and selects the new
  `hackrf` HW range (1 MHz – 6 GHz).
- **`scripts/fetch_hackrf.py`** fetches libhackrf, libusb and libwinpthread for
  Windows from conda-forge, checks each against a pinned sha256 and puts only the
  DLLs into `third_party/hackrf` (gitignored — they are not redistributed here).

### Notes
- A sample rate outside 2–20 MSPS is refused at Start, not adjusted, for the same
  reason as on the Pluto: the line timing is generated for the rate asked for.
- A stream that libhackrf drops (USB error, unplug) is reported through the error
  channel and stops the run, instead of leaving a silent board looking armed.
- A process killed mid-transmission can leave a HackRF stuck in TX; it then answers
  "not found" until RESET. The open error says so.

## 0.5.3 — 2026-09-25

### Fixed
- **Nothing could transmit over `usb:`.** Opening a board took two IIO contexts at
  once — one to identify the board and read its layout, then pyadi's own — and over
  USB a board takes ONE at a time. pyadi's open failed with a bare `No device found`
  every time. A network link takes two, which is why it went unseen. The inspection
  context is now closed before pyadi opens its own; the same fix is in `bench-tx`.
- **The probe's range check vanished over USB, without a word.** Same cause, and the
  summary of a board that had answered never showed an error, so `TX LO` and
  `Direct 5.8 GHz` were simply missing. It now says `TX range check failed: …` with
  the details.
- **A missing libiio read as a missing board.** A freshly installed Windows without
  libiio printed `Pluto not found (…): TypeError: LoadLibrary() argument 1 must be
  str, not None` — which sends people to check a cable that is fine. pylibiio is
  only a ctypes wrapper, and without the native library `import iio` raises that
  TypeError rather than ImportError (on Linux and macOS an AttributeError). The
  probe, Start and `bench-tx` now all say that libiio is not installed on this
  computer, which version to install (the one pylibiio was written for — not 1.x,
  which heads the release page) and where to get it. A libiio that is present but
  unusable is named, with its path.
- **A board that does not answer is described in words.** On Windows libiio reports
  every failed open as `OSError: [Errno 0] No error`, and pyadi reduces them all to
  `No device found`. The probe and Start now say `nothing answers at 192.168.2.1`,
  look at what is on the USB bus, and say what to do: `Set the URI to usb:` when the
  board is there (the address `usb:1.6.5` changes on every replug; `usb:` follows the
  board), the Pluto USB network adapter in Device Manager when that is what failed
  to start, the cable and the drivers when nothing is visible. Start no longer asks
  a silent board twice — pyadi would repeat the same open after the same timeout.
- Both READMEs say that transmitting on Windows needs two installers from Analog
  Devices once per computer — the Pluto USB drivers and libiio 0.25 — which
  `setup.bat` cannot provide, and which a reinstalled Windows needs again.

---

## 0.5.2 — 2026-08-10

### Fixed
- **`setup.bat` destroyed itself on a machine where it had never run.** It printed
  `'ho' is not recognized`, `'errorlevel' is not recognized` and so on, then gave up.
  cmd.exe tracks its position in a batch file by BYTE offset, and under `chcp 65001`
  a multi-byte character makes it resume two bytes late on every following line —
  so `echo` was read as `ho`. The file contained three em dashes. Both launchers are
  pure ASCII now, and a test enforces it.
- **`.bat` files are pinned to CRLF** via `.gitattributes`. A GitHub ZIP hands out
  exactly what the repository stores, and these were stored with LF — which cmd.exe
  also mis-parses. Nobody saw it because on a `git clone` they are converted on
  checkout; the ZIP download the README recommends does not do that.
- **`setup.bat` finds a Python that is not on the PATH.** It falls back to the `py`
  launcher, which the python.org installer always provides — "Add python.exe to
  PATH" is the box people miss, and it is exactly how a machine ends up with Python
  installed and setup unable to find it. It also prints which interpreter it used,
  and says that 3.11 or 3.12 is the safe choice.

---

## 0.5.1 — 2026-08-10

### Fixed
- **A `.venv` copied from another computer now says so.** Moving the project folder
  to a second PC with `.venv` inside gave a forty-line numpy C-extension traceback
  ending in `No module named 'numpy._core._multiarray_umath'` — which reads as a
  broken program, not a mismatched environment. It cannot be copied: it records the
  path of the Python that built it and holds binaries compiled for that exact
  version.
  - `run_gui.bat` now imports numpy as well as PySide6. PySide6 ships stable-ABI
    wheels that import under any Python, so the old check passed and let the GUI
    start and then die inside numpy.
  - `setup.bat` rebuilds a `.venv` whose numpy does not import. `python -m venv`
    leaves an existing directory alone, so running setup again would have changed
    nothing — the advice everyone gives first.
  - The startup dialog recognises the mismatch and says to delete `.venv`, instead
    of the generic "run setup.bat again" that is wrong in this exact case.
- Both READMEs say not to copy `.venv`, and that a Python released in the last few
  months often has no numpy or PySide6 wheels yet.

---

## 0.5.0 — 2026-08-10

Found by an adversarial review of the claim that a hard-coded 20 MHz transmit
filter was merging the two multi-drone carriers. **That claim was wrong** — the
filter is symmetric about the LO and both carriers sit at the same offset, so it
attenuates them equally to within 0.05 dB and cannot turn two targets into one.
The setting was a real defect for a different reason.

### Changed
- **The transmit filter width now comes from the signal, not from the sample rate.**
  It was `min(fs, 20e6)` in the GUI and the CLI, while the same codebase wrote
  `min(fs, 40e6)` on the measurement path — a number nobody in the project believed
  in. It is now `2 x furthest carrier + occupied bandwidth`, capped at 0.9·fs and at
  the AD9361's real 40 MHz TX ceiling. The span between carriers is the wrong
  quantity: two drones at 0 and +18 MHz span the same 18 MHz as a pair at ±9 and
  need twice the filter.
  At today's ±9 MHz this is worth a fraction of a dB. It matters for what comes
  next: with the old cap, widening the split to ±16 MHz at 61.44 MSPS would have
  come back several dB down and been read as "spreading them out does not help".
- A filter wider than the board allows is clamped and stated, not refused. Nothing
  in the buffer depends on it — unlike the sample rate, where a substitution moves
  the 15.625 kHz line rate the detector matches on.

### Fixed
- The GUI readout asked `is_color_pattern()` about the joined name
  `"color_bars+color_bars100"`, which is not a pattern, so it used the monochrome
  video bandwidth and understated multi-drone occupancy by 7.9 MHz — enough to
  withhold its own "wider than the sample rate" warning on exactly the colour
  multi-drone runs.
- `multi_drone.yaml` and both READMEs still said 30.72 MSPS is the ceiling and that
  61.44 is rejected. True of the Pluto+, false of the Nano on Tezuka, and precisely
  the comment that would stop the wider split from being tried.
- The test fixture published 56 MHz as the TX bandwidth ceiling. That is the RX
  figure; ADI's driver halves `tx_rf_bandwidth` and clamps the result to 20 MHz, so
  40 MHz is the widest the part takes. The fixture would have blessed a value a real
  board refuses.

### Added
- The applied filter width is in `info()`. It appeared in no log, no readout and no
  diagnostic before.

---

## 0.4.0 — 2026-08-10

### Fixed
- **The shipped scenarios transmitted a signal no detector was going to recognise.**
  `static_R1`, `power_ramp_R4` and `sweep_raceband` ran at **8 MSPS** with a
  monochrome pattern, and `sweep_allbands` at 10 MSPS with `multiburst`. All four
  date from before the colour work and were never revisited, so on the bench they
  showed no picture while manual mode was fine. Every scenario now uses the profile
  that is actually recognised: `color_bars`, at least 20 MSPS, 7 MHz deviation.
  Measured: one carrier is 11.9 MHz wide at −30 dB, and at 30.72 MSPS the two-drone
  case still folds only 0.49 % past 0.45·fs with the carriers 66 dB apart.

### Changed
- The default deviation is 7 MHz, in the GUI and for a scenario that omits it. It
  was 6.

### Added
- A scenario below 20 MSPS now warns and says why — the colour subcarrier aliases
  and the occupied bandwidth is too narrow, so the carrier reads as strong and is
  not recognised as video. A narrow signal is not an error, which is exactly why
  nothing said anything for months. A test also asserts that nothing we ship trips
  the guard.

---

## 0.3.1 — 2026-08-10

### Added
- Two multi-drone scenarios that make "the detector reported one target" mean
  something. On its own that result cannot tell "the second carrier is not on air"
  from "the detector only reports the strongest", and a spectrum analyser is not
  always at hand.
  - `multi_drone_swap` — the same pair with the levels mirrored. Run it straight
    after `multi_drone`: if the reported frequency moves, both carriers are
    radiating and the question is the detector's, not the signal's.
  - `multi_drone_equal` — both targets at the same level. `multi_drone` makes the
    second one 6 dB weaker to stand for a drone further away, which at 5.8 GHz —
    where the Pluto output is weakest — can put it under the threshold on its own.

---

## 0.3.0 — 2026-08-10

### Added
- **A «Verbose» switch beside the event log.** Notes about how the board had to be
  configured — the FIR filter being dropped, a firmware profile contradicting the
  board — are true on every run for a board that always needs them, and noise by
  the third Start. They now appear once per session, or on every run with the
  switch on. Anything meaning "what is on air is not what you asked for" is never
  filed away: aliasing, a transmitter that did not start, a rate that does not fit.
  The distinction is a warning class, not a text match.

---

## 0.2.1 — 2026-08-10

Found by an adversarial review of 0.2.0: 29 candidate findings, 26 refuted, 3 real.

### Fixed
- **A restored dry run no longer looks like a transmission.** Because the backend
  is remembered now, an operator whose last session used `null` reopened the GUI
  with `null` still selected — and the restore replaced the event log immediately
  after the "nothing goes on air" warning was written into it. They would press
  Start and read a log identical to a real run. The log is now only replaced when
  the snapshot actually carries one, which is the language-rebuild case it was
  written for.
- **The Ukrainian startup-failure dialog was half English.** Its title and trailer
  were translated, the message and the hint were not — because they are passed as
  arguments to `_show_error()` rather than sitting inside a `t()` call, and the
  test that guards the catalogue could not see them. It sees wrapper arguments now.
- The notice that a board refused the FIR filter repeated on every arming retry.
  It is reported when the answer *changes*, so a board that starts refusing between
  opens is still announced.

- A test that timed genuine randomness to prove the inter-trial pause is
  jittered, and failed about once in 140 runs when four draws happened to land
  close together. The draw is scripted now: a suite that cries wolf is one nobody
  reads.

### Added
- Tests covering `PlutoSink` against a fake board: that it identifies the firmware,
  passes the profile through instead of defaulting to auto, warns once rather than
  per retry, and refuses an out-of-range rate before writing anything. Four separate
  mutations of that code used to pass the whole suite.

---

## 0.2.0 — 2026-08-10

Everything between the first working version and today. There were no releases in
between, so this is one entry rather than invented history.

### Added
- **Colour video.** `color_bars` / `color_bars100` with a 4.43 MHz subcarrier
  (PAL) / 3.58 MHz (NTSC), burst and U/V chroma, including PAL's line-by-line V
  phase alternation. This is what a detector matching on the modulation profile
  actually recognises — a narrow B/W signal gives a high RSSI and is not seen.
- **PAL50 / NTSC60** progressive standards, 50/60 Hz fields. The old `PAL`/`NTSC`
  names map onto them. The field rate is what removes the vertical roll on the
  detector's monitor.
- **SoapySDR backend** — HackRF, LimeSDR, BladeRF, USRP and others, with the
  power slider mapped onto each device's own gain range.
- **Firmware profiles** (`auto` / `adi` / `tezuka`): which ways of setting the
  sample rate may be tried. `auto` asks the board and falls back, and is right for
  every board tested.
- **Support for boards whose IIO device names differ** (Tezuka builds, PlutoSky,
  custom FPGA images) — the control and DMA devices are found by shape rather than
  by the names pyadi hard-codes. An image with no transmit path is now named as
  such instead of failing with `argument of type 'NoneType' is not iterable`.
- **The window reopens where it was left** — every setting is saved on close and
  on Start, and restored next launch.
- **Bilingual interface**, English and Ukrainian, in the GUI and the CLI.
- **One-click Windows launchers** (`setup.bat`, `run_gui.bat`) and a GUI-first
  README; MIT licence.

### Changed
- **Two-drone spacing is now 18 MHz** (±9). Below ~14 MHz the carriers merge and
  the detector reports one target instead of two.
- **An out-of-range sample rate is refused, not clamped.** The buffer is generated
  for the rate that was asked for and the 15.625 kHz line rate scales with it, so a
  substituted rate would radiate a carrier that looks correct on a spectrum
  analyser and that the detector cannot see.
- **Power defaults are lower.** On 1.2 GHz, −30 dB measured better than −10 dB:
  the compressed PA puts video on its harmonics and the detector can lock onto one
  of those while the fundamental shows a black screen.
- Channel names are qualified as `BAND:CHANNEL`. Seven bare `C1`…`C7` names existed
  in four bands, and a sweep could transmit on 3440 MHz while reporting 1200.

### Fixed
- **A transmitter that reported success and stayed silent.** `tx()` returns without
  error while the DMA never starts. The buffer is now verified through digital
  loopback, and a stale context is dropped and reopened before retrying.
- **A device that rejects a setting now says which one**, and quotes the range it
  publishes, instead of one `[Errno 22] Invalid argument` for four different causes.
- A Tezuka build refusing pyadi's interpolating FIR filter at 20 MSPS — the rate is
  set with the filter off instead, and the difference is stated.
- `probe` reports the real traceback rather than a bare "not found", and opens the
  device the same way the transmitter does, so it cannot succeed where `tx` fails.

---

## 0.1.0 — 2026-07-25

First working version: composite video, FM modulation, cyclic buffer on the Pluto,
band/channel tables, the scenario engine (static, sweep with pause, power ramp,
multi-drone), the CLI and the PySide6 GUI.
