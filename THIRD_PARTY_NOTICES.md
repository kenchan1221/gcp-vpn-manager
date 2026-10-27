# Third-party notices

The application's MIT license does not replace third-party licenses.
The Windows EXEs bundle Python, Tcl/Tk, PyInstaller's bootloader and Python
dependencies. WireGuard and the Google Cloud CLI are installed separately and
are not bundled.

Major components:

| Component | License / reference |
|---|---|
| CPython | Python Software Foundation License; includes historical notices |
| Tcl / Tk | BSD-style Tcl/Tk license notices |
| OpenSSL | Apache-2.0 |
| zlib | zlib license |
| PyInstaller bootloader | GPL with the bootloader distribution exception |
| google-auth | Apache-2.0 |
| requests | Apache-2.0 |
| urllib3 | MIT |
| certifi / CA bundle | MPL-2.0 and included notices |
| idna | BSD-3-Clause |
| charset-normalizer | MIT |
| cachetools | MIT |
| pyasn1 / pyasn1-modules | BSD-2-Clause |
| rsa | Apache-2.0 |

Resolved package inventories are included beside each built EXE. Redistribution
notices copied from the actual build distributions are in each
`releases/windows-*/licenses/` directory.
Python's incorporated-software notices and upstream native-runtime licenses
are collected at their runtime version tags during builds; this step needs
internet access to the official upstream repositories. Keep those directories
with redistributed binaries. Build-only packages in the
inventory are not necessarily embedded in the executable.

Review notices again whenever dependencies/runtime versions change. Source and
license locations: [Python](https://docs.python.org/3/license.html),
[Tcl/Tk](https://www.tcl.tk/software/tcltk/license.html),
[PyInstaller](https://pyinstaller.org/en/stable/license.html),
[Google auth](https://github.com/googleapis/google-auth-library-python),
[Requests](https://github.com/psf/requests).
