"""First-run setup and subsequent configuration editing share the same dialog."""

import copy
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from diagnostics import LOG
from settings import Node, Settings


class SetupDialog(tk.Toplevel):
    def __init__(self, parent, store, current=None):
        super().__init__(parent)
        self.store = store
        self.result = None
        self.nodes = copy.deepcopy(current.nodes) if current else []
        self.key = copy.deepcopy(current.service_account) if current else None
        self.conf = ""
        self.edit_id = None
        self.title("GCP VPN Manager | Setup")
        self.resizable(True, True)
        self.transient(parent)
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Your cloud. Your VPN.", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Set up one or more existing VMs. Settings are remembered on this Windows account.").pack(anchor="w", pady=(4, 14))
        self.project = tk.StringVar(value=current.project if current else "")
        ttk.Label(body, text="Google Cloud project ID").pack(anchor="w")
        ttk.Entry(body, textvariable=self.project, width=65).pack(fill="x", pady=(3, 10))
        self.auth = tk.StringVar(value=current.auth if current else "adc")
        ttk.Radiobutton(body, text="Application Default Credentials (recommended; gcloud auth application-default login)",
                        variable=self.auth, value="adc").pack(anchor="w")
        auth_row = ttk.Frame(body)
        auth_row.pack(fill="x", pady=(4, 8))
        ttk.Radiobutton(auth_row, text="Service-account JSON key", variable=self.auth,
                        value="service_account").pack(side="left")
        ttk.Button(auth_row, text="Import JSON...", command=self.import_key).pack(side="left", padx=12)
        self.key_status = tk.StringVar(value="Key saved" if self.key else "No key imported")
        ttk.Label(auth_row, textvariable=self.key_status).pack(side="left")
        ttk.Separator(body).pack(fill="x", pady=10)
        ttk.Label(body, text="Servers (1-20) - select a row to edit").pack(anchor="w")
        self.table = ttk.Treeview(body, columns=("zone", "vm"), height=4, selectmode="browse")
        self.table.heading("#0", text="Display name")
        self.table.heading("zone", text="Zone")
        self.table.heading("vm", text="VM instance")
        self.table.pack(fill="both", expand=True, pady=6)
        self.table.bind("<<TreeviewSelect>>", self.select_node)
        fields = ttk.Frame(body)
        fields.pack(fill="x")
        self.label = tk.StringVar()
        self.zone = tk.StringVar()
        self.instance = tk.StringVar()
        for row, (name, variable) in enumerate((
            ("Display name", self.label), ("Zone (e.g. asia-east1-b)", self.zone),
            ("VM instance name", self.instance),
        )):
            ttk.Label(fields, text=name).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            ttk.Entry(fields, textvariable=variable, width=48).grid(row=row, column=1, sticky="ew")
        fields.columnconfigure(1, weight=1)
        conf_row = ttk.Frame(body)
        conf_row.pack(fill="x", pady=10)
        ttk.Button(conf_row, text="Import WireGuard .conf...", command=self.import_conf).pack(side="left")
        self.conf_status = tk.StringVar(value="No profile imported")
        ttk.Label(conf_row, textvariable=self.conf_status).pack(side="left", padx=12)
        ttk.Label(body, text="One client peer per VM. Endpoint IP is updated automatically; the port is preserved.").pack(anchor="w")
        editor_actions = ttk.Frame(body)
        editor_actions.pack(fill="x", pady=10)
        ttk.Button(editor_actions, text="Add / update server", command=self.commit_node).pack(side="left")
        ttk.Button(editor_actions, text="New server", command=self.new_node).pack(side="left", padx=8)
        ttk.Button(editor_actions, text="Remove selected", command=self.remove_node).pack(side="left")
        ttk.Label(body, text="Import your PC's profile, NOT your phone's. Each device needs a unique key and address.").pack(anchor="w", pady=6)
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Save settings", command=self.save).pack(side="right")
        ttk.Button(actions, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.grab_set()

    def import_key(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[("Service account", "*.json")])
        if path:
            try:
                key = json.loads(Path(path).read_text(encoding="utf-8-sig"))
                if not isinstance(key, dict) or key.get("type") != "service_account":
                    raise ValueError("Select a service-account JSON key.")
                self.key = key
                self.auth.set("service_account")
                self.key_status.set("Key imported (not displayed)")
            except (OSError, ValueError) as exc:
                messagebox.showerror("Import failed", str(exc), parent=self)

    def import_conf(self):
        from settings import normalize_conf
        path = filedialog.askopenfilename(parent=self, filetypes=[("WireGuard profile", "*.conf")])
        if path:
            try:
                self.conf = normalize_conf(Path(path).read_text(encoding="utf-8-sig"))
                self.conf_status.set("Profile imported (private keys hidden)")
            except (OSError, ValueError) as exc:
                messagebox.showerror("Import failed", str(exc), parent=self)

    def refresh(self):
        self.table.delete(*self.table.get_children())
        for node in self.nodes:
            self.table.insert("", "end", iid=node.id, text=node.label, values=(node.zone, node.instance))

    def select_node(self, _=None):
        selected = self.table.selection()
        if not selected:
            return
        node = next(node for node in self.nodes if node.id == selected[0])
        self.edit_id = node.id
        self.label.set(node.label)
        self.zone.set(node.zone)
        self.instance.set(node.instance)
        self.conf = node.template
        self.conf_status.set("Saved profile (private keys hidden)")

    def commit_node(self):
        try:
            node = Node.new(self.label.get(), self.zone.get(), self.instance.get(), self.conf)
            if self.edit_id:
                node.id = self.edit_id
            node.validate()
            others = [old for old in self.nodes if old.id != node.id]
            if any((old.zone, old.instance) == (node.zone, node.instance) for old in others):
                raise ValueError("This VM is already configured.")
            if len(others) >= 20:
                raise ValueError("Maximum 20 servers.")
            if self.edit_id:
                self.nodes = [node if old.id == node.id else old for old in self.nodes]
            else:
                self.nodes.append(node)
            self.new_node()
            self.refresh()
            return True
        except ValueError as exc:
            messagebox.showerror("Invalid server", str(exc), parent=self)
            return False

    def new_node(self):
        self.edit_id = None
        self.label.set("")
        self.zone.set("")
        self.instance.set("")
        self.conf = ""
        self.conf_status.set("No profile imported")
        self.table.selection_remove(self.table.selection())

    def remove_node(self):
        selected = self.table.selection()
        if selected and messagebox.askyesno(
            "Remove server?", "Remove this saved server/profile? The cloud VM is NOT deleted.", parent=self,
        ):
            self.nodes = [node for node in self.nodes if node.id != selected[0]]
            self.new_node()
            self.refresh()

    def save(self):
        if self.label.get() or self.zone.get() or self.instance.get() or self.conf:
            if not self.commit_node():
                return
        try:
            result = Settings(
                self.project.get().strip(), self.auth.get(), self.nodes,
                self.key if self.auth.get() == "service_account" else None,
            )
            self.store.save(result)
        except Exception as exc:
            LOG.exception("Saving setup failed")
            messagebox.showerror("Cannot save settings", str(exc), parent=self)
            return
        LOG.info("Settings saved: %s node(s), auth=%s", len(result.nodes), result.auth)
        self.result = result
        self.destroy()
