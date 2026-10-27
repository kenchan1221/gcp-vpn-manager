"""Validated, per-user configuration. No deployment-specific defaults."""

import base64
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

APP_NAME = "GcpVpnManager"
PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
ZONE = re.compile(r"[a-z]+-[a-z0-9]+[0-9]-[a-z]")
INSTANCE = re.compile(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?")
NODE_ID = re.compile(r"gvpn-[a-f0-9]{10}")


def data_directory():
    if os.name != "nt":
        raise RuntimeError("This release supports Windows only.")
    return Path(os.environ["LOCALAPPDATA"]) / APP_NAME


def secure_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
        text=True, errors="replace", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    sid = re.search(r"S-1-5-[\d-]+", result.stdout)
    if result.returncode or not sid:
        raise RuntimeError("Cannot determine the current Windows user's SID.")
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r",
         f"*{sid.group()}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
        capture_output=True, text=True, errors="replace", timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Cannot restrict private data directory permissions: " + result.stderr)


def crypt(data, decrypt=False):
    """DPAPI binds settings to the Windows account and machine."""
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    free = ctypes.WinDLL("kernel32").LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free(target.data)


def key_is_valid(value):
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (ValueError, TypeError):
        return False


def normalize_conf(text):
    """Accept a single-peer official WireGuard client profile, not shell hooks."""
    allowed = {
        "Interface": {"PrivateKey", "Address", "DNS", "MTU"},
        "Peer": {"PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive"},
    }
    sections = {}
    current = None
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("["):
            if line not in ("[Interface]", "[Peer]") or line[1:-1] in sections:
                raise ValueError("Import one [Interface] and one [Peer] only.")
            current = line[1:-1]
            sections[current] = {}
            continue
        if current is None or "=" not in line:
            raise ValueError("Invalid WireGuard configuration line.")
        name, value = (part.strip() for part in line.split("=", 1))
        if name not in allowed[current] or name in sections[current] or not value:
            raise ValueError(f"Unsupported, empty or duplicate field: {name}. Shell hooks are not allowed.")
        sections[current][name] = value
    if set(sections) != {"Interface", "Peer"}:
        raise ValueError("A client profile must contain [Interface] and [Peer].")
    interface, peer = sections["Interface"], sections["Peer"]
    if not {"PrivateKey", "Address"} <= interface.keys():
        raise ValueError("Missing client PrivateKey or Address.")
    if not {"PublicKey", "AllowedIPs", "Endpoint"} <= peer.keys():
        raise ValueError("Missing server PublicKey, AllowedIPs or Endpoint.")
    for group, name in ((interface, "PrivateKey"), (peer, "PublicKey"), (peer, "PresharedKey")):
        if name in group and not key_is_valid(group[name]):
            raise ValueError(f"{name} must be a base64-encoded 32-byte WireGuard key.")
    for value in interface["Address"].split(","):
        ipaddress.ip_interface(value.strip())
    for value in peer["AllowedIPs"].split(","):
        ipaddress.ip_network(value.strip(), strict=False)
    for value in interface.get("DNS", "").split(","):
        if value.strip():
            ipaddress.ip_address(value.strip())
    if "MTU" in interface and not 576 <= int(interface["MTU"]) <= 9000:
        raise ValueError("MTU must be between 576 and 9000.")
    if "PersistentKeepalive" in peer and not 0 <= int(peer["PersistentKeepalive"]) <= 65535:
        raise ValueError("PersistentKeepalive must be between 0 and 65535.")
    endpoint = peer["Endpoint"]
    host, separator, port = endpoint.rpartition(":")
    if not separator or not host or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("Endpoint must contain a hostname/IP and port from 1 to 65535.")
    # The VM's current public IPv4 replaces the imported host, never its port.
    peer["Endpoint"] = "{IP}:" + port
    return "\n\n".join(
        "[" + section + "]\n" + "\n".join(f"{name} = {value}" for name, value in values.items())
        for section, values in sections.items()
    ) + "\n"


@dataclass
class Node:
    id: str
    label: str
    zone: str
    instance: str
    template: str

    @classmethod
    def new(cls, label, zone, instance, conf):
        return cls("gvpn-" + uuid.uuid4().hex[:10], label.strip(), zone.strip(),
                   instance.strip(), normalize_conf(conf))

    def validate(self):
        if not NODE_ID.fullmatch(self.id):
            raise ValueError("Invalid internal tunnel identifier.")
        if not self.label.strip() or len(self.label) > 50:
            raise ValueError("Node name must be 1-50 characters.")
        if not ZONE.fullmatch(self.zone):
            raise ValueError("Enter a zone such as asia-east1-b, not a region.")
        if not INSTANCE.fullmatch(self.instance):
            raise ValueError("Invalid Compute Engine instance name.")
        self.template = normalize_conf(self.template)


@dataclass
class Settings:
    project: str
    auth: str
    nodes: list[Node]
    service_account: dict | None = None

    def validate(self):
        if not PROJECT.fullmatch(self.project):
            raise ValueError("Enter a valid Google Cloud project ID, not its display name.")
        if self.auth not in ("adc", "service_account"):
            raise ValueError("Choose ADC or a service-account JSON key.")
        if self.auth == "service_account":
            from google.oauth2 import service_account
            key = self.service_account
            if not isinstance(key, dict) or key.get("type") != "service_account":
                raise ValueError("Import a service-account JSON key, not an OAuth client secret.")
            if key.get("token_uri") != "https://oauth2.googleapis.com/token":
                raise ValueError("Only Google's official OAuth token endpoint is accepted.")
            service_account.Credentials.from_service_account_info(key)
        elif self.service_account is not None:
            raise ValueError("ADC configuration must not retain a service-account key.")
        if not 1 <= len(self.nodes) <= 20:
            raise ValueError("Configure between 1 and 20 servers.")
        seen_ids, seen_vms = set(), set()
        for node in self.nodes:
            node.validate()
            if node.id in seen_ids or (node.zone, node.instance) in seen_vms:
                raise ValueError("Duplicate tunnel ID or VM.")
            seen_ids.add(node.id)
            seen_vms.add((node.zone, node.instance))


class SettingsStore:
    def __init__(self, directory, encode=crypt, decode=None):
        self.directory = directory
        self.path = directory / "settings.dat"
        self.encode = encode
        self.decode = decode or (lambda data: crypt(data, decrypt=True))

    def save(self, settings):
        settings.validate()
        content = {"version": 1, **asdict(settings)}
        encrypted = self.encode(json.dumps(content).encode("utf-8"))
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_bytes(encrypted)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self):
        if not self.path.exists():
            return None
        content = json.loads(self.decode(self.path.read_bytes()))
        if content.pop("version", None) != 1:
            raise ValueError("Unsupported settings version.")
        content["nodes"] = [Node(**node) for node in content["nodes"]]
        settings = Settings(**content)
        settings.validate()
        return settings
