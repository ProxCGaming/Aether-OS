import sys
import unittest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from aether_ui.main import AetherWindow


class TestAetherWindowMaximize(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not QApplication.instance():
            cls.app = QApplication(sys.argv)
        else:
            cls.app = QApplication.instance()

    def setUp(self):
        self.win = AetherWindow()
        self.win.show()
        self.app.processEvents()

    def tearDown(self):
        self.win.close()
        self.app.processEvents()

    def test_single_click_maximize_and_single_click_restore(self):
        # Initial windowed state
        self.assertFalse(self.win.is_maximized_or_fullscreen)
        self.assertEqual(self.win.btn_max.text(), "🗖")

        # Click 1: maximize
        self.win.btn_max.click()
        self.app.processEvents()
        self.assertTrue(self.win.is_maximized_or_fullscreen)
        self.assertEqual(self.win.btn_max.text(), "🗗")

        # Click 2: restore to windowed mode directly in 1 click
        self.win.btn_max.click()
        self.app.processEvents()
        self.assertFalse(self.win.is_maximized_or_fullscreen)
        self.assertEqual(self.win.btn_max.text(), "🗖")

        # Click 3: maximize again
        self.win.btn_max.click()
        self.app.processEvents()
        self.assertTrue(self.win.is_maximized_or_fullscreen)
        self.assertEqual(self.win.btn_max.text(), "🗗")

        # Click 4: restore again
        self.win.btn_max.click()
        self.app.processEvents()
        self.assertFalse(self.win.is_maximized_or_fullscreen)
        self.assertEqual(self.win.btn_max.text(), "🗖")


if __name__ == "__main__":
    unittest.main()
