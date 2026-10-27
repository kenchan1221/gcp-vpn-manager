"""Private, redacted diagnostic logs with seven calendar days of retention."""

from datetime import datetime, timedelta
import logging
import os
import re
import time

LOG = logging.getLogger("gcp_vpn_manager")
LOG.addHandler(logging.NullHandler())
OWN_LOG = re.compile(r"vpn-(\d{4}-\d{2}-\d{2})-\d+\.log")


def redact(text):
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]", text, flags=re.DOTALL,
    )
    text = re.sub(r"\b[A-Za-z0-9+/]{43}=", "[REDACTED KEY]", text)
    text = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]", text)
    return re.sub(
        r"""(?i)((?:private_key|privatekey|presharedkey|access_token|refresh_token|id_token|"""
        r"""client_secret|authorization)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,}]+)""",
        r"\1[REDACTED]", text,
    )


class Formatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class DailyLogs(logging.Handler):
    def __init__(self, directory, report_error, clock=time.time):
        super().__init__()
        self.directory = directory
        directory.mkdir(exist_ok=True)
        self.report_error = report_error
        self.clock = clock
        self.last_cleanup = 0
        self.last_error = None
        self.setFormatter(Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
        self.cleanup()
        self.path().touch(exist_ok=True)

    def path(self):
        day = datetime.fromtimestamp(self.clock()).date()
        return self.directory / f"vpn-{day}-{os.getpid()}.log"

    def cleanup(self):
        cutoff = datetime.fromtimestamp(self.clock()).date() - timedelta(days=6)
        for path in self.directory.iterdir():
            match = OWN_LOG.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            try:
                day = datetime.strptime(match[1], "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)
        self.last_cleanup = self.clock()

    def failure(self, exc):
        message = "Cannot write/clean logs: " + str(exc)
        if message != self.last_error:
            self.last_error = message
            self.report_error(message)

    def maintain(self):
        self.acquire()
        try:
            self.cleanup()
        except OSError as exc:
            self.failure(exc)
        finally:
            self.last_cleanup = self.clock()
            self.release()

    def emit(self, record):
        try:
            if self.clock() - self.last_cleanup >= 3600:
                self.maintain()
            with self.path().open("a", encoding="utf-8") as stream:
                stream.write(self.format(record) + "\n")
            self.last_error = None
        except OSError as exc:
            self.failure(exc)
