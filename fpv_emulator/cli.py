"""Command-line interface for the FPV FM video emulator.

Examples:
  python -m fpv_emulator.cli probe
  python -m fpv_emulator.cli list-bands
  python -m fpv_emulator.cli list-scenarios
  python -m fpv_emulator.cli gen --pattern bars --standard PAL --sample-rate 8e6 --out out.iq
  python -m fpv_emulator.cli tx --backend null --channel R1 --pattern bars
  python -m fpv_emulator.cli tx --backend pluto --uri ip:192.168.2.1 --scenario sweep_raceband
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading

from . import __version__
from .backends import TxConfig, make_sink
from .bands import load_band_table
from .config import (
    GAIN_MAX_DB,
    GAIN_MIN_DB,
    clamp_gain,
    list_scenarios,
    load_scenario,
)
from .firmware import AUTO, PROFILE_KEYS
from .fm import to_int16_iq
from .i18n import available_languages, detect_language, set_language, t
from .probe import probe
from .scenarios import ScenarioRunner, SignalParams
from .signal_gen import generate_frame_iq, required_rf_bandwidth_hz, video_bandwidth_hz
from .video import get_standard, list_all_patterns, list_color_patterns, list_patterns


def _fmt_event(e: dict) -> str:
    action = e.get("action")
    if action == "tune":
        s = t("[TUNE] {channel} @ {freq} MHz",
              channel=e.get("channel", "?"), freq=f"{e.get('freq_mhz', 0):.1f}")
        if "gain_db" in e:
            s += t(", gain {gain} dB", gain=e["gain_db"])
        if "bw_mhz" in e:
            s += t(", bw ~{bw} MHz", bw=f"{e['bw_mhz']:.1f}")
        if "lap" in e:
            s += t(" (lap {lap})", lap=e["lap"])
        return s
    if action == "power":
        return t("[PWR ] {channel} @ {freq} MHz, gain {gain} dB",
                 channel=e.get("channel", "?"), freq=f"{e.get('freq_mhz', 0):.1f}",
                 gain=e.get("gain_db"))
    if action == "live_gain":
        return t("[GAIN] power -> {gain} dB (applied)", gain=e.get("gain_db"))
    if action == "pause":
        return t("[PAUSE] silence {seconds} s (RF off) @ {freq} MHz",
                 seconds=e.get("seconds"), freq=f"{e.get('freq_mhz', 0):.0f}")
    if action == "multi":
        return t("[MULTI] center {center} MHz, {n_drones} drone(s), "
                 "offsets {offsets} MHz, span ~{span} MHz",
                 center=f"{e.get('center_mhz', 0):.1f}", n_drones=e.get("n_drones"),
                 offsets=e.get("offsets_mhz"), span=f"{e.get('span_mhz', 0):.1f}")
    if action == "range":
        return t("[RANGE] {start}–{stop} MHz, step {step} MHz -> {n} point(s), "
                 "actually {first}–{last} MHz",
                 start=f"{e.get('start_mhz', 0):.0f}", stop=f"{e.get('stop_mhz', 0):.0f}",
                 step=f"{e.get('step_mhz', 0):g}", n=e.get("n"),
                 first=f"{e.get('first_mhz', 0):.0f}", last=f"{e.get('last_mhz', 0):.0f}")
    if action == "start":
        return t("[START] scenario '{name}' (type {type})",
                 name=e.get("scenario"), type=e.get("type"))
    if action == "stop":
        return t("[STOP ] scenario '{name}'", name=e.get("scenario"))
    if action == "error":
        return t("[ERROR] transmission failed: {message}", message=e.get("message"))
    return f"[{action}] {e}"


def cmd_probe(args) -> int:
    # printed with the result, so a pasted probe says which build produced it
    print(t("FPV FM emulator v{version}", version=__version__))
    if args.device == "hackrf":
        from .hackrf import probe_hackrf
        res = probe_hackrf(serial=args.serial or None)
        print(res.summary())
        return 0 if res.connected else 2
    res = probe(uri=args.uri, do_range_test=not args.no_range_test)
    print(res.summary())
    return 0 if res.connected else 2


def cmd_list_bands(args) -> int:
    bt = load_band_table()
    for group in bt.groups():
        print(f"\n=== {group} ===")
        for ch in bt.channels_in_group(group):
            # C1..C8 exist in several bands — print the form the user must type
            name = f"{ch.band}:{ch.name}" if bt.is_ambiguous(ch.name) else ch.name
            print(t("  {name}  {freq} MHz   (band {band})",
                    name=f"{name:8s}", freq=f"{ch.freq_mhz:7.1f}", band=ch.band))
    return 0


def cmd_list_patterns(args) -> int:
    print(t("Luma patterns (B/W): {items}", items=", ".join(list_patterns())))
    print(t("Color patterns:  {items}   (requires fs >= ~13 MSPS)",
            items=", ".join(list_color_patterns())))
    return 0


def cmd_list_scenarios(args) -> int:
    sc = list_scenarios()
    if not sc:
        print(t("No scenarios found in config/scenarios"))
        return 0
    print(t("Available scenarios:"))
    for name, path in sc.items():
        print(f"  {name:20s}  {path}")
    return 0


def cmd_list_devices(args) -> int:
    from .backends import soapy_enumerate
    devs = soapy_enumerate()
    if not devs:
        print(t("No SoapySDR devices found (or SoapySDR is not installed)."))
        print(t("Windows: install PothosSDR. "
                "Linux: apt install python3-soapysdr soapysdr-module-<driver>."))
        return 0
    print(t("SDRs found (SoapySDR):"))
    for d in devs:
        drv = d.get("driver", "?")
        label = d.get("label", "")
        print(f"  driver={drv}   {label}")
    return 0


def cmd_serial_ports(args) -> int:
    from .logsource import list_serial_ports
    ports = list_serial_ports()
    if not ports:
        print(t("No serial ports found (or pyserial is not installed)."))
        return 0
    print(t("Serial ports:"))
    for p in ports:
        print(f"  {p['device']:10s}  {p['description']}")
    return 0


def cmd_listen(args) -> int:
    """Dump the detector's log with arrival timestamps — used to find the pattern."""
    import time
    from .logsource import SerialLogSource
    src = SerialLogSource(args.port, int(args.baud))
    print(t("Listening on {port} at {baud} baud — Ctrl+C to stop.",
            port=args.port, baud=args.baud))
    t0 = time.perf_counter()
    try:
        with src:
            while True:
                line = src.get(timeout=0.5)
                err = src.poll_error()
                if err is not None:
                    print(t("[ERROR] {msg}", msg=str(err)), file=sys.stderr)
                    return 1
                if line is not None:
                    print(f"  {(line.t - t0)*1e3:9.1f} ms  {line.text}")
    except KeyboardInterrupt:
        print("\n" + t("Stopping…"))
    return 0


def _latency_signal(args):
    """Build the IQ buffer shared by the latency commands."""
    std = get_standard(args.standard)
    fs = float(args.sample_rate)
    frame = generate_frame_iq(args.pattern, std, fs, float(args.deviation))
    return to_int16_iq(frame.iq), fs, frame


def cmd_bench_tx(args) -> int:
    """command -> RF, witnessed by the Pluto's own receiver (no detector involved)."""
    from .latency import bench_command_to_rf, format_summary, summarize, write_csv
    buf, fs, frame = _latency_signal(args)
    freq_hz = float(args.freq_mhz) * 1e6
    print(t("Buffer: {n} samples = {mb} MB | {trials} trials @ {mhz} MHz",
            n=buf.size, mb=f"{buf.size*4/1e6:.2f}", trials=args.trials,
            mhz=f"{freq_hz/1e6:.0f}"))

    def show(tr):
        if tr.ok:
            print(f"  #{tr.n:3d}  {tr.latency_s*1e3:8.2f} ms"
                  f"   (upload {tr.upload_s*1e3:7.2f} + after {tr.post_arm_s*1e3:7.2f})")
        else:
            print(f"  #{tr.n:3d}  {tr.note}")

    trials = bench_command_to_rf(
        buf, args.uri, freq_hz, fs, trials=int(args.trials),
        tx_gain_db=float(args.gain), rx_gain_db=float(args.rx_gain),
        gap_s=float(args.gap), timeout_s=float(args.timeout),
        firmware=args.firmware, on_trial=show)
    ok = [x for x in trials if x.ok]
    print()
    print(format_summary(t("command -> RF"), summarize([x.latency_s for x in ok]),
                         len(trials) - len(ok)))
    print(format_summary(t("  buffer upload"), summarize([x.upload_s for x in ok])))
    print(format_summary(t("  after DMA armed"), summarize([x.post_arm_s for x in ok])))
    if args.out:
        write_csv(args.out, trials, {
            "measurement": "command_to_rf", "uri": args.uri,
            "freq_mhz": freq_hz / 1e6, "sample_rate": fs, "pattern": args.pattern,
            "standard": args.standard, "tx_gain_db": args.gain,
            "buffer_samples": buf.size,
        })
        print(t("Written: {path}", path=args.out))
    return 0


def cmd_latency(args) -> int:
    try:
        return _cmd_latency(args)
    except RuntimeError as exc:
        print(t("[ERROR] {msg}", msg=str(exc)), file=sys.stderr)
        return 1


def _cmd_latency(args) -> int:
    """The real measurement: RF on -> the detector reports it, N times."""
    from .latency import (format_summary, measure_detection_latency, summarize,
                          write_csv)
    from .logsource import SerialLogSource
    buf, fs, frame = _latency_signal(args)
    freq_list = [float(x) * 1e6 for x in args.freq_list.split(",") if x.strip()]
    gain_list = [float(x) for x in args.gain_list.split(",") if x.strip()]
    freq_hz = freq_list[0] if freq_list else float(args.freq_mhz) * 1e6

    # the same sink the GUI uses — one arming path, with the DMA-playing check
    sink = make_sink("pluto", TxConfig(
        fs=fs, freq_hz=freq_hz, gain_db=float(args.gain),
        uri=args.uri, rf_bw_hz=min(fs, 40e6), firmware=args.firmware))

    src = SerialLogSource(args.port, int(args.baud))
    print(t("Detector log: {port}@{baud} | pattern: {pattern}",
            port=args.port, baud=args.baud, pattern=args.pattern_re))
    print(t("{trials} trials @ {mhz} MHz, gain {gain} dB, gap {gap} s",
            trials=args.trials, mhz=f"{freq_hz/1e6:.1f}", gain=args.gain, gap=args.gap))

    done: list = []
    csv_meta = {
        "measurement": "detection_latency", "uri": args.uri, "port": args.port,
        "pattern_re": args.pattern_re, "confirm_re": args.confirm_re,
        "freq_mhz": freq_hz / 1e6, "sample_rate": fs,
        "video_pattern": args.pattern, "standard": args.standard,
        "tx_gain_db": args.gain, "gain_list": args.gain_list,
        "offset_ms": args.offset_ms, "gap_s": args.gap,
    }

    def show(tr):
        if not tr.ok:
            line = f"  #{tr.n:3d}  {tr.note}"
        else:
            line = f"  #{tr.n:3d}  video {tr.latency_s:7.3f} s"
            if tr.confirm_s is not None:
                line += f"   confirmed {tr.confirm_s:7.3f} s"
            if tr.reported_mhz:
                line += f"   @{tr.reported_mhz:.0f} MHz"
        # flush: a run takes minutes and its output is usually redirected, where
        # python block-buffers stdout and the operator sees nothing until the end
        print(line, flush=True)
        # rewrite the CSV after every trial — losing a 10-minute run to a late
        # error would be worse than the cost of rewriting 30 rows
        done.append(tr)
        if args.out:
            write_csv(args.out, done, csv_meta)

    try:
        with src:
            trials = measure_detection_latency(
                sink, buf, src, args.pattern_re, freq_hz,
                tx_gain_db=float(args.gain), trials=int(args.trials),
                timeout_s=float(args.timeout), gap_s=float(args.gap),
                offset_s=float(args.offset_ms) / 1e3,
                confirm_pattern=(args.confirm_re or None),
                freq_tol_mhz=float(args.freq_tol_mhz),
                release_pattern=(args.release_re or None),
                gap_jitter_s=float(args.gap_jitter),
                freq_list_hz=freq_list, gain_list_db=gain_list, on_trial=show)
    finally:
        sink.close()

    ok = [x for x in trials if x.ok]
    conf = [x.confirm_s for x in ok if x.confirm_s is not None]
    print()
    print(format_summary(t("VIDEO ACQUIRED"), summarize([x.latency_s for x in ok]),
                         len(trials) - len(ok)))
    if conf:
        print(format_summary(t("DETECTION CONFIRMED"), summarize(conf)))
    print(t("Note: a sweeping detector adds where-in-the-sweep it was, so the spread "
            "reflects the sweep period rather than measurement noise."))
    if args.out:
        write_csv(args.out, trials, csv_meta)   # final rewrite, complete set
        print(t("Written: {path}", path=args.out))
    return 0


def cmd_gen(args) -> int:
    std = get_standard(args.standard)
    fs = float(args.sample_rate)
    frame = generate_frame_iq(args.pattern, std, fs, float(args.deviation))
    iq16 = to_int16_iq(frame.iq)
    # write
    if args.out.endswith(".npy"):
        import numpy as np
        np.save(args.out, iq16.astype("complex64"))
    else:
        import numpy as np
        inter = np.empty(iq16.size * 2, dtype=np.int16)
        inter[0::2] = iq16.real.astype(np.int16)
        inter[1::2] = iq16.imag.astype(np.int16)
        inter.tofile(args.out)
    print(t("Wrote {n} samples ({ms} ms/frame) -> {out}",
            n=frame.n_samples, ms=f"{frame.duration_s * 1e3:.2f}", out=args.out))
    print(t("  standard={std}, pattern={pattern}, fs={fs} MSPS",
            std=frame.std_name, pattern=frame.pattern, fs=f"{fs / 1e6:.2f}"))
    print(t("  deviation pp={dev} MHz, occupied bandwidth ~{bw} MHz",
            dev=f"{frame.deviation_pp_hz / 1e6:.2f}", bw=f"{frame.occupied_bw_hz / 1e6:.1f}"))
    return 0


def _run_scenario(runner: ScenarioRunner, scenario: dict) -> int:
    stop = threading.Event()

    def _sigint(_sig, _frm):
        print("\n" + t("Stopping…"))
        stop.set()

    signal.signal(signal.SIGINT, _sigint)
    runner.run(scenario, stop=stop)
    return 0


def _gain_from_arg(value) -> float:
    """--gain -> a value tx_hardwaregain accepts (-89..0 dB), with a warning."""
    gain_db = float(value)
    clamped = clamp_gain(gain_db)
    if clamped != gain_db:
        print(t("[WARN] gain {value} dB is outside {min}..{max} dB — clamped to {clamped} dB.",
                value=gain_db, min=GAIN_MIN_DB, max=GAIN_MAX_DB, clamped=clamped),
              file=sys.stderr)
    return clamped


def cmd_tx(args) -> int:
    try:
        return _cmd_tx(args)
    except (ValueError, KeyError, FileNotFoundError, RuntimeError) as exc:
        # a bad channel/field must read as a message, not as a traceback
        msg = exc.args[0] if exc.args else str(exc)
        print(t("[ERROR] {msg}", msg=msg), file=sys.stderr)
        return 1


def _cmd_tx(args) -> int:
    bt = load_band_table()

    # build the scenario: either from a file, or a single one from the flags
    if args.scenario:
        scenario = load_scenario(args.scenario)
    else:
        if args.channel:
            ch = bt.channel(args.channel)
            freq_hz = ch.freq_hz
            ch_name = ch.name
        elif args.freq_mhz:
            freq_hz = float(args.freq_mhz) * 1e6
            ch_name = f"{args.freq_mhz}MHz"
        else:
            print(t("Specify --channel, --freq-mhz or --scenario"), file=sys.stderr)
            return 1
        scenario = {
            "name": f"static-{ch_name}",
            "type": "static",
            "signal": {
                "standard": args.standard,
                "pattern": args.pattern,
                "sample_rate": float(args.sample_rate),
                "deviation_pp_hz": float(args.deviation),
                "gain_db": _gain_from_arg(args.gain),
            },
            "static": ({"channel": args.channel} if args.channel
                       else {"freq_mhz": float(args.freq_mhz)}),
        }
        if args.duration:
            scenario["static"]["hold_s"] = float(args.duration)

    # HW range warning
    sp = SignalParams.from_dict(scenario.get("signal"))
    fs = sp.sample_rate

    drones = (scenario.get("multi_drone") or {}).get("drones") or []
    offsets = [float(d.get("offset_mhz", 0.0)) * 1e6 for d in drones]
    pats = [str(d.get("pattern", "")) for d in drones] or [sp.pattern]
    rf_bw = required_rf_bandwidth_hz(
        sp.deviation_pp_hz, video_bandwidth_hz(pats, get_standard(sp.standard)),
        offsets, fs)
    cfg = TxConfig(fs=fs, freq_hz=0.0, gain_db=sp.gain_db, uri=args.uri,
                   rf_bw_hz=rf_bw, device=args.device,
                   serial=args.serial, amp=args.amp,
                   firmware=args.firmware)
    sink = make_sink(args.backend, cfg, file_path=args.out)

    runner = ScenarioRunner(sink, bt, on_event=lambda e: print(_fmt_event(e)))
    print(t("Backend: {backend}  |  fs={fs} MSPS  |  scenario: {name}",
            backend=args.backend, fs=f"{fs / 1e6:.2f}", name=t(scenario.get("name", ""))))
    try:
        return _run_scenario(runner, scenario)
    finally:
        sink.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fpv-emulator",
        description=t("Analog FPV video (FM) emulator on Pluto+ for testing FPV detectors"),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    lang_help = t("interface language (default: from the FPV_LANG env var, else en)")
    p.add_argument("--lang", choices=available_languages(), default=detect_language(),
                   help=lang_help)
    # same flag on every subcommand, so both `--lang uk tx` and `tx --lang uk` work.
    # SUPPRESS keeps the subparser from overriding a value given before the subcommand.
    lang_parent = argparse.ArgumentParser(add_help=False)
    lang_parent.add_argument("--lang", choices=available_languages(),
                             default=argparse.SUPPRESS, help=lang_help)
    # Firmware profile — only on the subcommands that actually set a sample rate.
    # `probe` deliberately does not take it: it never sets one, so the flag would
    # parse, be accepted and change nothing.
    fw_help = t("firmware profile: which ways of setting the sample rate may be "
                "tried (default: auto — try the filtered path, fall back)")
    p.add_argument("--firmware", choices=list(PROFILE_KEYS), default=AUTO, help=fw_help)
    fw_parent = argparse.ArgumentParser(add_help=False)
    fw_parent.add_argument("--firmware", choices=list(PROFILE_KEYS),
                           default=argparse.SUPPRESS, help=fw_help)
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("probe", parents=[lang_parent],
                        help=t("Find Pluto and determine its frequency range"))
    pp.add_argument("--device", default="pluto", choices=["pluto", "hackrf"],
                    help=t("which board to probe (default: pluto)"))
    pp.add_argument("--uri", default="ip:192.168.2.1")
    pp.add_argument("--serial", default="",
                    help=t("HackRF serial (backend=hackrf); default: the first one found"))
    pp.add_argument("--no-range-test", action="store_true",
                    help=t("Do not retune TX during the probe"))
    pp.set_defaults(func=cmd_probe)

    sub.add_parser("list-bands", parents=[lang_parent],
                   help=t("List bands/channels")).set_defaults(func=cmd_list_bands)
    sub.add_parser("list-patterns", parents=[lang_parent],
                   help=t("List test patterns")).set_defaults(func=cmd_list_patterns)
    sub.add_parser("list-scenarios", parents=[lang_parent],
                   help=t("List scenarios")).set_defaults(func=cmd_list_scenarios)
    sub.add_parser("list-devices", parents=[lang_parent],
                   help=t("List SDRs via SoapySDR")).set_defaults(func=cmd_list_devices)

    # ---- detection-latency measurements --------------------------------------
    sub.add_parser("serial-ports", parents=[lang_parent],
                   help=t("List serial ports")).set_defaults(func=cmd_serial_ports)

    pl = sub.add_parser("listen", parents=[lang_parent],
                        help=t("Print the detector log with arrival timestamps"))
    pl.add_argument("--port", required=True, help=t("COM port, e.g. COM5"))
    pl.add_argument("--baud", default="115200")
    pl.set_defaults(func=cmd_listen)

    def _signal_args(p):
        p.add_argument("--pattern", default="color_bars", choices=list_all_patterns())
        p.add_argument("--standard", default="PAL50")
        p.add_argument("--sample-rate", default="20e6")
        p.add_argument("--deviation", default="6e6")
        p.add_argument("--uri", default="ip:192.168.2.1")
        p.add_argument("--trials", default="20")
        p.add_argument("--timeout", default="10")
        p.add_argument("--out", default="", help=t("write the trials to this CSV"))

    pb = sub.add_parser("bench-tx", parents=[lang_parent, fw_parent],
                        help=t("Measure command -> RF using the Pluto's own receiver"))
    _signal_args(pb)
    pb.add_argument("--freq-mhz", default="2450",
                    help=t("use a frequency where TX->RX leakage is strong"))
    pb.add_argument("--gain", default="-10")
    pb.add_argument("--rx-gain", default="40")
    pb.add_argument("--gap", default="0.3")
    pb.set_defaults(func=cmd_bench_tx)

    pd = sub.add_parser("latency", parents=[lang_parent, fw_parent],
                        help=t("Measure how long the detector takes to report the signal"))
    _signal_args(pd)
    pd.add_argument("--port", required=True, help=t("COM port of the detector, e.g. COM5"))
    pd.add_argument("--baud", default="115200")
    # Defaults match the "Kazhan"-style ESP-IDF log:
    #   W (201094) scan_eng: >>> VIDEO 3240 MHz: pulses=31/10 conf=100% line=15.625kHz
    #   W (206404) Detector: DETECTION_STARTED 3G 3239 MHz (3230-3240)
    # The (?P<mhz>...) group lets the runner reject hits on other frequencies —
    # a sweeping detector prints candidates across the whole band.
    pd.add_argument("--pattern-re", default=r">>>\s*VIDEO\s+(?P<mhz>\d+)\s*MHz",
                    help=t("regular expression marking video acquisition; capture the "
                           "frequency as (?P<mhz>...) to reject other channels"))
    # NB: the band token ("3G") sits between the marker and the frequency, so a
    # \D* separator would not get past it — hence the non-greedy .*?
    pd.add_argument("--confirm-re",
                    default=r"DETECTION_STARTED.*?(?P<mhz>\d{3,5})\s*MHz",
                    help=t("second marker: confirmed detection (empty = only the first)"))
    pd.add_argument("--freq-tol-mhz", default="20",
                    help=t("how far the reported frequency may differ from ours"))
    pd.add_argument("--freq-mhz", default="0",
                    help=t("single carrier, MHz (ignored when --freq-list is given)"))
    pd.add_argument("--gain", default="-30",
                    help=t("power, dB — -30 measured optimal for the 1.2 GHz band"))
    # 5 s: after the carrier goes the detector still holds the target for a while
    # ("SEEK all 1 win LOST -> rescan") — starting a trial before it has released
    # would measure a detector that was already triggered.
    pd.add_argument("--freq-list", default="",
                    help=t("cycle across several carriers, MHz, comma separated "
                           "(overrides --freq-mhz)"))
    # Too much power drives the PA into compression: the detector then locks onto
    # harmonics that carry the video while the fundamental is too distorted to
    # decode. The right level is band dependent — measured here, -30 dB suits the
    # 1.2 GHz band, where the Pluto's output is stronger than higher up.
    pd.add_argument("--gain-list", default="",
                    help=t("per-carrier power, dB, comma separated, matching "
                           "--freq-list (overrides --gain)"))
    pd.add_argument("--gap", default="5.0",
                    help=t("silence between trials — long enough for the detector to release"))
    # A sweeping detector plus a fixed cycle lands on nearly the same sweep phase
    # every trial, so the run measures one phase and looks far more repeatable than
    # the detector is. Randomising the pause over a whole sweep period fixes it.
    pd.add_argument("--gap-jitter", default="7.0",
                    help=t("random extra pause, 0..N s — must cover one sweep period"))
    pd.add_argument("--release-re", default=r"DETECTION_LOST|rescan",
                    help=t("line meaning the detector dropped the target; the next "
                           "trial waits for it instead of guessing with a fixed pause"))
    pd.add_argument("--offset-ms", default="0",
                    help=t("subtract our own command -> RF delay (see bench-tx)"))
    pd.set_defaults(func=cmd_latency)

    pg = sub.add_parser("gen", parents=[lang_parent],
                        help=t("Generate IQ into a file (no hardware)"))
    pg.add_argument("--pattern", default="color_bars", choices=list_all_patterns())
    pg.add_argument("--standard", default="PAL50")
    pg.add_argument("--sample-rate", default="20e6")
    pg.add_argument("--deviation", default="6e6", help=t("pp deviation, Hz"))
    pg.add_argument("--out", default="out.iq")
    pg.set_defaults(func=cmd_gen)

    pt = sub.add_parser("tx", parents=[lang_parent, fw_parent],
                        help=t("Transmit (Pluto/HackRF/file/null) or run a scenario"))
    pt.add_argument("--backend", default="null",
                    choices=["pluto", "hackrf", "soapy", "file", "null"])
    pt.add_argument("--uri", default="ip:192.168.2.1", help=t("Pluto URI (backend=pluto)"))
    pt.add_argument("--device", default="driver=hackrf",
                    help=t("SoapySDR device args (backend=soapy): driver=hackrf|lime|uhd|bladerf"))
    pt.add_argument("--serial", default="",
                    help=t("HackRF serial (backend=hackrf); default: the first one found"))
    pt.add_argument("--amp", action="store_true",
                    help=t("HackRF: allow the +14 dB RF amplifier (power reaches 0..-61 dB)"))
    pt.add_argument("--channel", help=t("e.g. R1"))
    pt.add_argument("--freq-mhz", help=t("manual carrier, MHz"))
    pt.add_argument("--pattern", default="color_bars", choices=list_all_patterns())
    pt.add_argument("--standard", default="PAL50")
    pt.add_argument("--sample-rate", default="20e6")
    pt.add_argument("--deviation", default="6e6")
    pt.add_argument("--gain", default="-10", help=t("tx_hardwaregain, dB (0=max)"))
    pt.add_argument("--duration", help=t("hold for N seconds (static)"))
    pt.add_argument("--scenario", help=t("scenario file or name from config/scenarios"))
    pt.add_argument("--out", default="out.iq", help=t("file for backend=file"))
    pt.set_defaults(func=cmd_tx)

    return p


def _force_utf8_stdout() -> None:
    """Windows consoles (cp1251/cp866) mangle non-ASCII text — switch them to UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def _preapply_language(argv=None) -> None:
    """Apply --lang before build_parser() runs.

    argparse renders every help string at parser-construction time, so the language
    has to be active before the parser is built. Scan argv for ``--lang X`` or
    ``--lang=X`` and fall back to the environment default.
    """
    items = list(sys.argv[1:] if argv is None else argv)
    lang = None
    for i, item in enumerate(items):
        if item == "--lang" and i + 1 < len(items):
            lang = items[i + 1]
        elif item.startswith("--lang="):
            lang = item.split("=", 1)[1]
    set_language(lang if lang in available_languages() else detect_language())


def main(argv=None) -> int:
    _force_utf8_stdout()
    _preapply_language(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    set_language(getattr(args, "lang", detect_language()))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
