#!/usr/bin/env python
"""Fetch libhackrf for Windows into third_party/hackrf (backend=hackrf).

Usage:
    python scripts/fetch_hackrf.py [--lang uk]

Takes three conda-forge packages — libhackrf0, and libusb and libwinpthread that
it loads — checks each against a pinned sha256, and extracts only the DLLs. Nothing
is installed system-wide; fpv_emulator/hackrf.py looks in third_party/hackrf
first. The DLLs are not committed (libhackrf is GPL-2.0, libusb LGPL-2.1): this
script is how a checkout gets them.

The packages are .conda files — a zip around zstd-compressed tars. Python 3.14
reads zstd itself; an older Python needs ``pip install zstandard``.
"""
import argparse
import hashlib
import io
import os
import sys
import tarfile
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpv_emulator.cli import _force_utf8_stdout, _preapply_language
from fpv_emulator.hackrf import BUNDLED_DIR
from fpv_emulator.i18n import available_languages, t

_BASE = "https://api.anaconda.org/download/conda-forge/"

#: (url path, sha256, files to take from Library/bin)
PACKAGES = [
    ("libhackrf0/2026.01.3/win-64/libhackrf0-2026.01.3-hfd05255_0.conda",
     "fd9a6a718cad7ff61f830e6c3789e26363d7f9e12399e25d9d5a68c06251f8f7",
     ["hackrf-0.dll"]),
    ("libusb/1.0.29/win-64/libusb-1.0.29-h1839187_0.conda",
     "9837f8e8de20b6c9c033561cd33b4554cd551b217e3b8d2862b353ed2c23d8b8",
     ["libusb-1.0.dll"]),
    ("libwinpthread/12.0.0.r4.gg4f2fc60ca/win-64/"
     "libwinpthread-12.0.0.r4.gg4f2fc60ca-h11686cb_11.conda",
     "988c8fe5fca9e3510fd0b5671d67ae42c1fd632f8bd28f832a6735970eb69a36",
     ["libwinpthread-1.dll"]),
]


def _zstd_decompress(data: bytes) -> bytes:
    try:
        from compression import zstd          # Python 3.14+
        return zstd.decompress(data)
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError:
        raise SystemExit(t("This Python cannot read zstd. Use Python 3.14+ or run: "
                           "pip install zstandard"))
    return zstandard.ZstdDecompressor().decompressobj().decompress(data)


def _extract(conda: bytes, wanted, dest: str):
    z = zipfile.ZipFile(io.BytesIO(conda))
    inner = next(n for n in z.namelist() if n.startswith("pkg-") and n.endswith(".tar.zst"))
    tar = tarfile.open(fileobj=io.BytesIO(_zstd_decompress(z.read(inner))))
    got = []
    for member in tar.getmembers():
        name = os.path.basename(member.name)
        if member.isfile() and member.name.startswith("Library/bin/") and name in wanted:
            with open(os.path.join(dest, name), "wb") as fh:
                fh.write(tar.extractfile(member).read())
            got.append(name)
    return got


def main() -> int:
    _force_utf8_stdout()
    _preapply_language()
    ap = argparse.ArgumentParser(
        description=t("Fetch libhackrf for Windows into third_party/hackrf"))
    ap.add_argument("--lang", choices=available_languages())
    ap.parse_args()

    os.makedirs(BUNDLED_DIR, exist_ok=True)
    for path, sha, wanted in PACKAGES:
        url = _BASE + path
        print(t("Downloading {url}", url=url))
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha:
            print(t("[ERROR] {name}: sha256 {got} does not match the pinned {want} — "
                    "this package was not extracted.", name=os.path.basename(path),
                    got=digest, want=sha), file=sys.stderr)
            return 1
        for name in _extract(data, wanted, BUNDLED_DIR):
            print("  -> " + os.path.join(BUNDLED_DIR, name))
    print(t("Done. Check the board with: python -m fpv_emulator.cli probe --device hackrf"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
