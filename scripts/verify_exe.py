"""Verify PE architecture, GUI/admin flags and absence of bundled user data."""

from pathlib import Path
import struct
import sys
from PyInstaller.archive.readers import CArchiveReader

path = Path(sys.argv[1])
architecture = sys.argv[2]
data = path.read_bytes()
offset = struct.unpack_from("<I", data, 0x3C)[0]
assert data[offset:offset + 4] == b"PE\0\0"
assert struct.unpack_from("<H", data, offset + 4)[0] == {"x86": 0x14C, "x64": 0x8664}[architecture]
assert struct.unpack_from("<H", data, offset + 24 + 68)[0] == 2
assert b"requireAdministrator" in data
archive = CArchiveReader(str(path))
for name in archive.toc:
    if name.replace("\\", "/") == "certifi/cacert.pem":
        assert b"PRIVATE KEY" not in archive.extract(name)
        continue
    assert not name.endswith((".conf", ".key", ".pem", ".log", "settings.dat", "gcp_key.json")), name
modules = archive.open_embedded_archive("PYZ.pyz").toc
assert all(name in modules for name in ("settings", "cloud", "tunnels", "diagnostics", "setup_ui"))
print(f"Verified {path.name}: {architecture}, GUI, administrator manifest, required modules, no user data.")
