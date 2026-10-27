import ctypes
import os
from pathlib import Path
import queue
import sys
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import App
from settings import SettingsStore
from setup_ui import SetupDialog
from test_manager import profile, settings


@unittest.skipUnless(os.name == "nt", "Windows UI")
class SetupUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ctypes.windll.shcore.SetProcessDpiAwareness(1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SettingsStore(Path(self.temp.name))
        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        ttk.Style(self.root).theme_use("clam")
        self.root.update()

    def test_first_run_save_one_server_and_reload_without_reentry(self):
        with patch("setup_ui.messagebox.showerror") as errors:
            dialog = SetupDialog(self.root, self.store)
            self.root.update()
            dialog.project.set("example-project")
            dialog.label.set("My only server")
            dialog.zone.set("asia-east1-b")
            dialog.instance.set("vpn-one")
            dialog.conf = profile()
            dialog.save()
            self.root.update()
            errors.assert_not_called()
        config = SettingsStore(Path(self.temp.name)).load()
        self.assertEqual(len(config.nodes), 1)
        self.assertEqual(config.nodes[0].label, "My only server")
        self.assertEqual(config, dialog.result)

    def test_edit_does_not_change_tunnel_identifier(self):
        config = settings()
        original_id = config.nodes[0].id
        dialog = SetupDialog(self.root, self.store, config)
        dialog.table.selection_set(original_id)
        dialog.select_node()
        dialog.label.set("Renamed")
        dialog.save()
        saved = self.store.load()
        self.assertEqual(saved.nodes[0].id, original_id)
        self.assertEqual(saved.nodes[0].label, "Renamed")

    def test_cancel_does_not_persist_or_modify_original(self):
        config = settings()
        self.store.save(config)
        dialog = SetupDialog(self.root, self.store, config)
        dialog.project.set("changed-project")
        dialog.nodes.clear()
        dialog.destroy()
        self.assertEqual(self.store.load(), config)

    def test_one_and_twenty_node_layouts_and_control_states(self):
        for count in (1, 20):
            config = settings(count)
            with patch.object(App, "begin"):
                app = App(self.root, self.store, config, Mock(), queue.Queue())
            self.root.update()
            self.assertEqual(len(app.table.get_children()), count)
            app.known_local = True
            app.controls()
            self.assertEqual(str(app.settings_button["state"]), "normal")
            app.tunnels = {config.nodes[0].id: "RUNNING"}
            app.controls()
            self.assertEqual(str(app.settings_button["state"]), "disabled")
            self.assertEqual(str(app.stop_button["state"]), "disabled")
            self.assertEqual(str(app.disconnect_button["state"]), "normal")
            app.status.set("Action completed; refresh failed. " * 5)
            for size in ((self.root.winfo_width(), self.root.winfo_height()), self.root.minsize()):
                self.root.geometry(f"{size[0]}x{size[1]}")
                self.root.update()
                for button in app.buttons:
                    self.assertTrue(button.winfo_viewable())
                    self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
                self.assertGreater(app.log.winfo_height(), 30)
            for child in self.root.winfo_children():
                child.destroy()
            for callback in self.root.tk.call("after", "info"):
                self.root.after_cancel(callback)


if __name__ == "__main__":
    unittest.main()
