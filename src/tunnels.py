"""Only manage this application's explicitly configured Windows tunnel services."""

import http.client
import os
from pathlib import Path
import re
import subprocess
import time

from cloud import CloudNetworkError, network_probe
from diagnostics import LOG


def wireguard_path():
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(variable)
        if value:
            path = Path(value) / "WireGuard" / "wireguard.exe"
            if path.is_file():
                return path
    raise FileNotFoundError("Install official WireGuard for Windows before connecting this PC.")


class Controller:
    def __init__(self, settings, cloud, directory, run=subprocess.run,
                 clock=time.monotonic, sleep=time.sleep, probe=network_probe):
        self.settings = settings
        self.cloud = cloud
        self.directory = directory
        self.run = run
        self.clock = clock
        self.sleep = sleep
        self.probe = probe

    def command(self, args):
        LOG.debug("Local command: %s", args)
        result = self.run(
            [str(arg) for arg in args], capture_output=True, text=True,
            errors="replace", timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        LOG.debug("Local exit=%s stdout=%s stderr=%s",
                  result.returncode, result.stdout, result.stderr)
        return result

    def local_tunnels(self):
        tunnels = {}
        for node in self.settings.nodes:
            result = self.command(["sc.exe", "query", "WireGuardTunnel$" + node.id])
            if result.returncode == 1060:
                continue
            if result.returncode:
                raise RuntimeError("Cannot read tunnel service: " + result.stdout + result.stderr)
            state = re.search(
                r":\s+[1-7]\s+(STOPPED|START_PENDING|STOP_PENDING|RUNNING|"
                r"CONTINUE_PENDING|PAUSE_PENDING|PAUSED)\b", result.stdout,
            )
            if not state:
                raise RuntimeError("Unrecognized WireGuard service state.")
            tunnels[node.id] = state[1]
        return tunnels

    def wait_network(self, report):
        deadline = self.clock() + 30
        last_error = None
        while self.clock() < deadline:
            try:
                self.probe(timeout=max(0.1, min(5, deadline - self.clock())))
                LOG.info("Fresh DNS/TLS connection to Google succeeded")
                return
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
                LOG.warning("Waiting for DNS/TLS after route change", exc_info=True)
                report("Waiting for network / DNS...")
                self.sleep(min(2, max(0, deadline - self.clock())))
        raise CloudNetworkError(
            "DNS/TLS did not recover after switching the tunnel. The VM is NOT stopped. "
            "Disconnect this PC to restore normal networking; then check the server, peer and DNS."
        ) from last_error

    def disconnect(self, report):
        tunnels = self.local_tunnels()
        if tunnels:
            executable = wireguard_path()
        for name in tunnels:
            report("Removing local tunnel " + name)
            result = self.command([executable, "/uninstalltunnelservice", name])
            if result.returncode:
                raise RuntimeError("Cannot remove tunnel: " + result.stdout + result.stderr)
        deadline = self.clock() + 30
        while self.local_tunnels():
            if self.clock() >= deadline:
                raise TimeoutError("Tunnel service removal timed out. Refresh before retrying.")
            self.sleep(1)
        for node in self.settings.nodes:
            (self.directory / f"{node.id}.conf").unlink(missing_ok=True)
        self.cloud.reset()
        report("This PC is disconnected. VMs stay running for phones and other devices.")

    def start_only(self, node, report):
        return self.cloud.start(node, report)

    def connect(self, node, report):
        executable = wireguard_path()
        node.validate()
        report("Reconnecting: removing old tunnels; normal non-VPN networking will be used briefly.")
        self.disconnect(report)
        self.wait_network(report)
        address = self.cloud.start(node, report)
        # The installed service references this path, including after a reboot.
        # Keep it in the owner/SYSTEM-only directory; never put it beside the EXE.
        runtime = self.directory / f"{node.id}.conf"
        runtime.write_text(node.template.replace("{IP}", address), encoding="utf-8")
        result = self.command([executable, "/installtunnelservice", runtime])
        if result.returncode:
            raise RuntimeError("Cannot install tunnel: " + result.stdout + result.stderr)
        deadline = self.clock() + 30
        while self.local_tunnels().get(node.id) != "RUNNING":
            if self.clock() >= deadline:
                raise TimeoutError("WireGuard did not become ready.")
            self.sleep(1)
        self.cloud.reset()
        report("Tunnel service enabled; checking DNS / Google reachability...")
        self.wait_network(report)
        report(f"{node.label}: tunnel enabled; DNS/TLS reachable. WireGuard handshake not verified.")
        return address

    def stop(self, node, report):
        if node.id in self.local_tunnels():
            raise RuntimeError("Disconnect this PC from this node before stopping its VM.")
        self.cloud.stop(node, report)
