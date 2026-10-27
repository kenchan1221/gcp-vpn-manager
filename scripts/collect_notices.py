"""Copy unmodified redistribution notices from the actual build environment."""

import importlib.metadata
from pathlib import Path
import shutil
import ssl
import sys
import tkinter
import urllib.request
import zlib

destination = Path(sys.argv[1])
destination.mkdir(parents=True, exist_ok=True)
prefix = Path(sys.base_prefix)


def copy(source, relative):
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def download_notice(url, relative):
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as response:
        target.write_bytes(response.read())


python_license = prefix / "LICENSE.txt"
if not python_license.is_file():
    raise SystemExit("Cannot find the Python redistribution license.")
copy(python_license, "python/LICENSE.txt")
tk_licenses = list((prefix / "tcl").rglob("license.terms"))
if not tk_licenses:
    raise SystemExit("Cannot find Tcl/Tk distribution notices.")
for source in tk_licenses:
    copy(source, Path("tcl-tk") / source.relative_to(prefix / "tcl"))
python_version = ".".join(str(value) for value in sys.version_info[:3])
tcl_tag = "core-" + tkinter.Tcl().eval("info patchlevel").replace(".", "-")
openssl_tag = "openssl-" + ".".join(str(value) for value in ssl.OPENSSL_VERSION_INFO[:3])
download_notice(
    f"https://raw.githubusercontent.com/python/cpython/v{python_version}/Doc/license.rst",
    "python/incorporated-software-notices.rst",
)
download_notice(
    f"https://raw.githubusercontent.com/tcltk/tcl/{tcl_tag}/license.terms",
    "tcl-tk/tcl-license.terms",
)
download_notice(
    f"https://raw.githubusercontent.com/openssl/openssl/{openssl_tag}/LICENSE.txt",
    "openssl/LICENSE.txt",
)
download_notice(
    f"https://raw.githubusercontent.com/madler/zlib/v{zlib.ZLIB_RUNTIME_VERSION}/LICENSE",
    "zlib/LICENSE",
)
inventory = []
for distribution in importlib.metadata.distributions():
    name = distribution.metadata["Name"]
    inventory.append(f"{name}=={distribution.version}")
    for path in distribution.files or []:
        if any(word in path.name.lower() for word in ("license", "copying", "notice")):
            source = Path(distribution.locate_file(path))
            if source.is_file() and source.suffix not in (".py", ".pyc"):
                copy(source, Path("packages") / name / Path(*path.parts))
(destination / "build-environment.txt").write_text(
    "\n".join(sorted(inventory, key=str.lower)) + "\n", encoding="utf-8",
)
print("Copied Python, Tcl/Tk and installed distribution notices to", destination)
