"""Unit tests that do not require elevated privileges."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from ramfix.automation import GuardConfig, add_watch, load_config, remove_watch, set_threshold
from ramfix.cleaner import CACHE, GC, clean, clean_action
from ramfix.compression import get_zram_devices
from ramfix.limits import sanitize_name
from ramfix.stats import get_memory_stats, get_top_processes
from ramfix.swap import get_swap_devices, get_swappiness, parse_size
from ramfix.utils import bar, get_platform, human_bytes, privilege_wrap


class TestFormatting(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(1023), "1023 B")
        self.assertEqual(human_bytes(1024), "1.0 KiB")
        self.assertEqual(human_bytes(1536), "1.5 KiB")
        self.assertEqual(human_bytes(1024**3), "1.0 GiB")

    def test_bar(self):
        self.assertEqual(bar(0, 10), "[----------]")
        self.assertEqual(bar(100, 10), "[##########]")
        self.assertEqual(bar(50, 10), "[#####-----]")


class TestPlatform(unittest.TestCase):
    def test_known_platform(self):
        self.assertIn(get_platform(), {"linux", "windows", "darwin"})

    def test_privilege_wrap(self):
        wrapped = privilege_wrap(["echo", "hi"])
        self.assertTrue(wrapped == ["echo", "hi"] or wrapped[:3] == ["sudo", "-n", "echo"])


class TestCleaner(unittest.TestCase):
    def test_dry_run(self):
        results = clean([CACHE, GC], dry_run=True)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results))
        self.assertTrue(all("[dry run]" in r.message for r in results))

    def test_unknown_action(self):
        result = clean_action("bogus")
        self.assertFalse(result.ok)
        self.assertIn("Unknown action", result.message)


class TestSwapHelpers(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(parse_size("1024"), 1024)
        self.assertEqual(parse_size("1K"), 1024)
        self.assertEqual(parse_size("512M"), 512 * 1024**2)
        self.assertEqual(parse_size("2G"), 2 * 1024**3)
        self.assertEqual(parse_size("1.5GiB"), int(1.5 * 1024**3))
        with self.assertRaises(ValueError):
            parse_size("bogus")

    def test_swap_reads(self):
        # Read-only probes should be safe on any platform.
        self.assertIsInstance(get_swap_devices(), list)
        value = get_swappiness()
        self.assertTrue(value is None or 0 <= value <= 200)


class TestV2Helpers(unittest.TestCase):
    def test_sanitize_name(self):
        self.assertEqual(sanitize_name("My App!"), "My-App")
        self.assertEqual(sanitize_name("  "), "app")
        self.assertNotIn("/", sanitize_name("a/b"))

    def test_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
                config = load_config()
                self.assertIsInstance(config, GuardConfig)
                self.assertEqual(config.threshold, 85.0)
                self.assertTrue(set_threshold(90)[0])
                self.assertTrue(add_watch("firefox")[0])
                self.assertIn("firefox", load_config().watch_apps)
                self.assertTrue(remove_watch("firefox")[0])
                self.assertFalse(set_threshold(500)[0])

    def test_zram_devices(self):
        devices = get_zram_devices()
        self.assertIsInstance(devices, list)
        for device in devices:
            self.assertGreaterEqual(device.ratio, 0.0)


class TestStats(unittest.TestCase):
    def test_memory_stats(self):
        stats = get_memory_stats()
        self.assertGreater(stats.total, 0)
        self.assertGreaterEqual(stats.available, 0)
        self.assertGreaterEqual(stats.swap_used, 0)

    def test_top_processes(self):
        processes = get_top_processes(3)
        self.assertLessEqual(len(processes), 3)
        for proc in processes:
            self.assertGreaterEqual(proc.rss, 0)


if __name__ == "__main__":
    unittest.main()
