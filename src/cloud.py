"""Compute REST control with short-lived Google auth tokens, not gcloud subprocesses."""

import http.client
import ipaddress
import time
import uuid

import google.auth
from google.auth.exceptions import TransportError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
import requests

from diagnostics import LOG

API = "https://compute.googleapis.com/compute/v1/projects"
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class CloudNetworkError(RuntimeError):
    pass


def public_ip(instance):
    for interface in instance.get("networkInterfaces", []):
        for config in interface.get("accessConfigs", []):
            if config.get("natIP"):
                return str(ipaddress.IPv4Address(config["natIP"]))
    return ""


def network_probe(timeout):
    connection = http.client.HTTPSConnection("compute.googleapis.com", timeout=timeout)
    try:
        connection.connect()
    finally:
        connection.close()


class Cloud:
    def __init__(self, settings, session_factory=None, clock=time.monotonic, sleep=time.sleep):
        self.settings = settings
        self.session_factory = session_factory or self._new_session
        self.session = None
        self.clock = clock
        self.sleep = sleep

    def _new_session(self):
        if self.settings.auth == "adc":
            credentials, _ = google.auth.default(scopes=SCOPES)
        else:
            credentials = service_account.Credentials.from_service_account_info(
                self.settings.service_account, scopes=SCOPES,
            )
        return AuthorizedSession(credentials, refresh_timeout=10)

    def reset(self):
        if self.session is not None:
            self.session.close()
            self.session = None

    def request(self, method, path, **kwargs):
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                if self.session is None:
                    self.session = self.session_factory()
                LOG.debug("Cloud %s %s attempt=%s", method, path, attempt + 1)
                response = self.session.request(
                    method, f"{API}/{self.settings.project}/{path}",
                    timeout=(5, 10), **kwargs,
                )
                try:
                    if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                        LOG.warning("Transient HTTP %s; retrying cloud read", response.status_code)
                        self.sleep(attempt + 1)
                        continue
                    response.raise_for_status()
                    return response.json()
                finally:
                    response.close()
            except requests.exceptions.SSLError:
                raise
            except (requests.ConnectionError, requests.Timeout, TransportError) as exc:
                LOG.warning("Cloud transport failure", exc_info=True)
                self.reset()
                if attempt + 1 == attempts:
                    raise CloudNetworkError(
                        "Google Cloud is unreachable. If VPN is enabled but offline, disconnect "
                        "this PC (keep the VM), then reconnect. See logs for details."
                    ) from exc
                self.sleep(attempt + 1)

    def get(self, node):
        instance = self.request("GET", f"zones/{node.zone}/instances/{node.instance}")
        LOG.info("VM %s status=%s public_ip=%s", node.label, instance["status"], public_ip(instance))
        return instance

    def wait_operation(self, node, operation, deadline):
        while operation.get("status") != "DONE":
            if self.clock() >= deadline:
                raise TimeoutError("Cloud operation timed out; it may still be running. Refresh before retrying.")
            self.sleep(2)
            operation = self.request("GET", f"zones/{node.zone}/operations/{operation['name']}")
        if operation.get("error"):
            errors = operation["error"].get("errors", [])
            raise RuntimeError("Cloud operation failed: " + "; ".join(
                f"{error.get('code', 'ERROR')}: {error.get('message', '')}" for error in errors
            ))

    def start(self, node, report):
        deadline = self.clock() + 180
        requested = False
        while self.clock() < deadline:
            instance = self.get(node)
            status = instance["status"]
            if status == "RUNNING":
                address = public_ip(instance)
                if not address:
                    raise RuntimeError("VM is running but has no external IPv4 address.")
                report(f"{node.label} VM running at {address}")
                return address
            if status == "TERMINATED" and not requested:
                report(f"Starting {node.label} VM...")
                operation = self.request(
                    "POST", f"zones/{node.zone}/instances/{node.instance}/start",
                    params={"requestId": str(uuid.uuid4())},
                )
                requested = True
                self.wait_operation(node, operation, deadline)
            elif status in ("STOPPING", "STAGING", "PROVISIONING", "TERMINATED"):
                report(f"Waiting for VM: {status}")
                self.sleep(3)
            else:
                raise RuntimeError(f"Cannot start VM in state {status}. Use Cloud Console.")
        raise TimeoutError("VM startup timed out. It may still be starting; refresh its status.")

    def stop(self, node, report):
        status = self.get(node)["status"]
        if status == "TERMINATED":
            report(f"{node.label} VM is already stopped.")
            return
        if status != "RUNNING":
            raise RuntimeError(f"VM is {status}; wait and refresh before stopping it.")
        report(f"Stopping {node.label} VM...")
        operation = self.request(
            "POST", f"zones/{node.zone}/instances/{node.instance}/stop",
            params={"requestId": str(uuid.uuid4())},
        )
        self.wait_operation(node, operation, self.clock() + 180)
        if self.get(node)["status"] != "TERMINATED":
            raise RuntimeError("VM stop is not yet confirmed. Refresh its status.")
        report(f"{node.label} VM stopped. Disks and reserved IPs can still incur charges.")
