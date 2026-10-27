"""GCP VPN Manager: Windows desktop app with persistent first-run setup."""

import ctypes
from ctypes import wintypes
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from cloud import Cloud, CloudNetworkError, public_ip
from diagnostics import DailyLogs, LOG, redact
from settings import SettingsStore, data_directory, secure_directory
from setup_ui import SetupDialog
from tunnels import Controller

VERSION = "1.0.0"


class App:
    def __init__(self, root, store, settings, log_handler, log_errors):
        self.root, self.store, self.settings = root, store, settings
        self.log_handler, self.log_errors = log_handler, log_errors
        self.events = queue.Queue()
        self.busy = False
        self.known_local = False
        self.tunnels = {}
        self.instances = {}
        self.controller = Controller(settings, Cloud(settings), store.directory)
        root.title(f"GCP VPN Manager {VERSION}")
        root.configure(bg="#f3f6fb")
        scale = max(1, root.winfo_fpixels("1i") / 96)
        width = min(round(960 * scale), root.winfo_screenwidth() - 80)
        height = min(round(710 * scale), root.winfo_screenheight() - 100)
        root.geometry(f"{width}x{height}")
        root.minsize(min(width, round(900 * scale)), min(height, round(650 * scale)))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.callback_error
        self.status = tk.StringVar(value="Ready")
        self.local = tk.StringVar(value="Local tunnel: checking...")
        self.buttons = []
        self.build()
        self.populate()
        root.after(100, self.drain)
        root.after(3600000, self.maintain_logs)
        self.begin("Refresh", self.refresh)

    def button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command)
        self.buttons.append(button)
        return button

    def build(self):
        body = ttk.Frame(self.root, padding=24)
        body.pack(fill="both", expand=True)
        header = ttk.Frame(body)
        header.pack(fill="x")
        ttk.Label(header, text="GCP VPN", style="Title.TLabel").pack(side="left")
        self.settings_button = self.button(header, "Settings", self.edit_settings)
        self.settings_button.pack(side="right")
        ttk.Label(body, text="Start your cloud. Connect your devices.", style="Muted.TLabel").pack(anchor="w", pady=(2, 12))
        ttk.Label(body, textvariable=self.local, style="Accent.TLabel").pack(anchor="w", pady=(0, 12))
        table_frame = ttk.Frame(body)
        table_frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(
            table_frame, columns=("vm", "zone", "state", "ip"), height=6, selectmode="browse",
        )
        for name, label, width in (
            ("#0", "Server", 150), ("vm", "VM instance", 140), ("zone", "Zone", 150),
            ("state", "Cloud state", 110), ("ip", "External IPv4", 140),
        ):
            self.table.heading(name, text=label)
            self.table.column(name, width=width, minwidth=70)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", lambda _: self.controls())
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=14)
        self.button(actions, "Connect this PC", self.connect).pack(side="left")
        self.button(actions, "Start VM only", self.start_only).pack(side="left", padx=8)
        self.stop_button = self.button(actions, "Stop VM...", self.stop)
        self.stop_button.pack(side="right")
        self.copy_button = self.button(actions, "Copy IP", self.copy_ip)
        self.copy_button.pack(side="right", padx=8)
        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x")
        self.disconnect_button = self.button(toolbar, "Disconnect PC (keep VM)", self.disconnect)
        self.disconnect_button.pack(side="left")
        self.button(toolbar, "Refresh", lambda: self.begin("Refresh", self.refresh)).pack(side="right")
        self.button(toolbar, "Cloud Console", self.console).pack(side="right", padx=8)
        ttk.Label(
            body, text="Disconnect / exit keeps VMs running. Stopping a VM disconnects every device.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=12)
        status_row = ttk.Frame(body)
        status_row.pack(fill="x")
        self.button(status_row, "Open logs", self.open_logs).pack(side="right")
        label = ttk.Label(status_row, textvariable=self.status, wraplength=650, justify="left")
        label.pack(side="left", fill="x", expand=True)
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(200, event.width)))
        self.progress = ttk.Progressbar(body, mode="indeterminate")
        self.progress.pack(fill="x", pady=10)
        log_frame = ttk.Frame(body)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=6, wrap="word", relief="flat", state="disabled",
                           bg="white", fg="#56647c", font=("Segoe UI", 9))
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

    def populate(self):
        self.table.delete(*self.table.get_children())
        for node in self.settings.nodes:
            self.table.insert("", "end", iid=node.id, text=node.label,
                              values=(node.instance, node.zone, "Unknown", ""))
        self.table.selection_set(self.settings.nodes[0].id)

    def selected(self):
        selection = self.table.selection()
        if not selection:
            raise ValueError("Select a server.")
        return next(node for node in self.settings.nodes if node.id == selection[0])

    def controls(self):
        for button in self.buttons:
            button.configure(state="disabled" if self.busy else "normal")
        if not self.busy:
            selected = self.table.selection()
            info = self.instances.get(selected[0]) if selected else None
            self.copy_button.configure(state="normal" if info and public_ip(info) else "disabled")
            self.stop_button.configure(
                state="disabled" if not self.known_local or not selected or selected[0] in self.tunnels else "normal"
            )
            self.disconnect_button.configure(state="normal" if self.tunnels or not self.known_local else "disabled")
            self.settings_button.configure(state="normal" if self.known_local and not self.tunnels else "disabled")

    def report(self, text):
        LOG.info("%s", text)
        self.events.put(("status", text))

    def begin(self, title, action):
        if self.busy:
            return
        self.busy = True
        self.status.set(title + "...")
        self.controls()
        self.progress.start(12)
        LOG.info("Action started: %s", title)

        def worker():
            failure = None
            try:
                action()
            except Exception as exc:
                LOG.exception("Action failed: %s", title)
                failure = redact(str(exc))
                self.events.put(("error", (title, failure)))
            finally:
                if title != "Refresh":
                    try:
                        self.refresh(announce=False)
                    except Exception as exc:
                        LOG.exception("Post-action refresh failed")
                        prefix = "Action completed; refresh failed" if failure is None else "Refresh also failed"
                        self.events.put(("status", prefix + ": " + redact(str(exc))))
                LOG.info("Action ended: %s result=%s", title, "failed" if failure else "completed")
                self.events.put(("done", failure))
        threading.Thread(target=worker, daemon=True).start()

    def refresh(self, announce=True):
        errors = []
        try:
            tunnels = self.controller.local_tunnels()
        except Exception as exc:
            LOG.exception("Local status unavailable")
            tunnels = None
            errors.append(str(exc))
        self.events.put(("local", tunnels))
        network_failed = False
        for node in self.settings.nodes:
            info = None
            if not network_failed:
                try:
                    info = self.controller.cloud.get(node)
                except Exception as exc:
                    LOG.exception("Cloud status unavailable: %s", node.label)
                    errors.append(node.label + ": " + str(exc))
                    network_failed = isinstance(exc, CloudNetworkError)
            self.events.put(("vm", (node.id, info)))
        if errors:
            raise RuntimeError("\n".join(errors))
        if announce:
            self.report("Status updated. Cloud state and local tunnel state are independent.")

    def drain(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "status":
                    self.status.set(data)
                    self.append_log(data)
                elif kind == "error":
                    title, text = data
                    self.status.set(title + " failed")
                    self.append_log(text)
                    messagebox.showerror(title, text, parent=self.root)
                elif kind == "local":
                    self.known_local = data is not None
                    self.tunnels = data or {}
                    names = {node.id: node.label for node in self.settings.nodes}
                    self.local.set("Local tunnel: " + (
                        "unknown" if data is None else
                        ", ".join(f"{names[name]} / {state}" for name, state in data.items()) or "disconnected"
                    ))
                elif kind == "vm":
                    name, info = data
                    self.instances[name] = info
                    self.table.set(name, "state", info["status"] if info else "Unknown")
                    self.table.set(name, "ip", public_ip(info) if info else "")
                elif kind == "done":
                    self.busy = False
                    self.progress.stop()
                    if data:
                        self.status.set("Action incomplete. See error details / logs.")
                    self.controls()
        except queue.Empty:
            pass
        try:
            error = self.log_errors.get_nowait()
        except queue.Empty:
            pass
        else:
            messagebox.showerror("Log error", error, parent=self.root)
        self.root.after(100, self.drain)

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", redact(text) + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 200:
            self.log.delete("1.0", f"{lines - 199}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def connect(self):
        node = self.selected()
        self.begin("Connect", lambda: self.controller.connect(node, self.report))

    def start_only(self):
        node = self.selected()
        self.begin("Start VM", lambda: self.controller.start_only(node, self.report))

    def disconnect(self):
        self.begin("Disconnect", lambda: self.controller.disconnect(self.report))

    def stop(self):
        node = self.selected()
        if messagebox.askyesno(
            "Stop shared VM?", f"Stop {node.label}?\nAll phones and other devices will disconnect."
            "\nDisks and reserved IPs may still incur charges.", icon="warning", parent=self.root,
        ):
            self.begin("Stop VM", lambda: self.controller.stop(node, self.report))

    def copy_ip(self):
        info = self.instances.get(self.selected().id)
        if info and public_ip(info):
            self.root.clipboard_clear()
            self.root.clipboard_append(public_ip(info))
            self.report("IP copied. Preserve the phone profile's endpoint port.")

    def console(self):
        node = self.selected()
        url = ("https://console.cloud.google.com/compute/instancesDetail/zones/"
               f"{node.zone}/instances/{node.instance}?project={self.settings.project}")
        if not webbrowser.open(url):
            raise RuntimeError("Cannot open browser: " + url)
        LOG.info("Opened Cloud Console")

    def open_logs(self):
        os.startfile(str(self.store.directory / "logs"))

    def edit_settings(self):
        if self.controller.local_tunnels():
            raise RuntimeError("Disconnect all managed tunnels before editing settings.")
        dialog = SetupDialog(self.root, self.store, self.settings)
        self.root.wait_window(dialog)
        if dialog.result:
            self.controller.cloud.reset()
            self.settings = dialog.result
            self.controller = Controller(self.settings, Cloud(self.settings), self.store.directory)
            self.instances = {}
            self.populate()
            self.begin("Refresh", self.refresh)

    def maintain_logs(self):
        self.log_handler.maintain()
        self.root.after(3600000, self.maintain_logs)

    def callback_error(self, kind, error, traceback):
        LOG.error("UI callback failed", exc_info=(kind, error, traceback))
        messagebox.showerror("Error", redact(str(error)), parent=self.root)

    def close(self):
        if self.busy:
            messagebox.showinfo("Working", "Wait for the current operation to finish.", parent=self.root)
        elif messagebox.askokcancel(
            "Exit?", "Closing the app keeps tunnels and VMs running.", parent=self.root,
        ):
            LOG.info("Exiting without stopping tunnels or VMs")
            self.controller.cloud.reset()
            self.root.destroy()


def main():
    if os.name != "nt":
        raise SystemExit("This release supports Windows x64/x86 only.")
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    root.withdraw()
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=("Segoe UI", 10))
    style.configure("TFrame", background="#f3f6fb")
    style.configure("TLabel", background="#f3f6fb", foreground="#182640")
    style.configure("Title.TLabel", font=("Segoe UI", 25, "bold"))
    style.configure("Muted.TLabel", foreground="#63718a")
    style.configure("Accent.TLabel", foreground="#2563eb", font=("Segoe UI", 11, "bold"))
    style.configure("TButton", padding=(10, 8))
    style.configure("Treeview", rowheight=30)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, False, "Local\\GcpVpnManager")
    already_running = ctypes.get_last_error() == 183
    if not mutex or already_running:
        messagebox.showerror("Already running", "Only one GCP VPN Manager may run at a time.", parent=root)
        if mutex:
            kernel.CloseHandle(mutex)
        root.destroy()
        return
    handler = None
    try:
        directory = data_directory()
        secure_directory(directory)
        log_errors = queue.Queue()
        handler = DailyLogs(directory / "logs", log_errors.put)
        LOG.setLevel("DEBUG")
        LOG.propagate = False
        LOG.addHandler(handler)
        LOG.info("App %s started; Python %s; %s-bit", VERSION, sys.version, ctypes.sizeof(ctypes.c_void_p) * 8)
        store = SettingsStore(directory)
        settings = store.load()
        root.deiconify()
        if settings is None:
            dialog = SetupDialog(root, store)
            root.wait_window(dialog)
            settings = dialog.result
            if settings is None:
                root.destroy()
                return
        App(root, store, settings, handler, log_errors)
        root.mainloop()
    except Exception as exc:
        LOG.exception("Startup/fatal error")
        messagebox.showerror(
            "Cannot start", redact(str(exc)) + "\nExisting settings have not been reset.", parent=root,
        )
        root.destroy()
    finally:
        if handler:
            LOG.info("App exited")
            LOG.removeHandler(handler)
            handler.close()
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    main()
