from pathlib import Path
import os
import subprocess
import unittest
from unittest.mock import patch

from test_pipeline import TestDirectory
from voxlate.app_settings import prepare_settings
from voxlate.common import VoxlateError, load_config
from voxlate.resources import (CleanupEntry, cleanup_inventory, clean_resources, current_root,
                               remember_root, validate_target, measure_resources)
from voxlate.diagnostics import ResourceStatus


class CleanupTests(unittest.TestCase):
    def test_targeted_measurement_updates_deleted_item_and_reuses_other_sizes(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            model = Path(cfg["asr"]["model_path"])
            model.mkdir(parents=True)
            (model / "model.bin").write_bytes(b"123")
            cache = current_root(cfg) / "setup-cache"
            cache.mkdir()
            (cache / "data").write_bytes(b"1234")
            cached = measure_resources(cfg, config, [])
            (model / "model.bin").unlink()
            model.rmdir()
            with patch("voxlate.resources.os.walk", side_effect=AssertionError("unrelated directory scanned")):
                updated = measure_resources(cfg, config, [], keys={"asr_turbo_model"}, cached=cached)
            self.assertNotIn("asr_turbo_model", updated.sizes)
            self.assertEqual(updated.sizes["download_cache"], 4)

    def test_cleanup_review_scans_only_requested_resource(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            model = Path(cfg["asr"]["model_path"])
            model.mkdir(parents=True)
            other = Path(cfg["tts"]["model_path"])
            other.mkdir(parents=True)
            with patch("voxlate.resources.folder_size", return_value=12) as measure:
                entries = cleanup_inventory(cfg, config, keys={"asr_turbo_model"})
            self.assertEqual([entry.key for entry in entries], ["asr_turbo_model"])
            measure.assert_called_once_with(model)

    def test_actual_sizes_calibrate_only_completed_resources_and_survive_deletion(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            model = Path(cfg["asr"]["model_path"])
            model.mkdir(parents=True)
            partial = model / "weights.incomplete"
            partial.write_bytes(b"x" * 2_000_000)
            missing = [ResourceStatus("asr_turbo_model", "model", False, "", "")]
            first = measure_resources(cfg, config, missing)
            self.assertEqual(first.sizes["asr_turbo_model"], 2_000_000)
            self.assertEqual(first.estimates["asr_turbo_model"], "约 1.62 GB")
            ready = [ResourceStatus("asr_turbo_model", "model", True, "", "")]
            self.assertEqual(measure_resources(cfg, config, ready).estimates["asr_turbo_model"], "约 1.62 GB")
            partial.rename(model / "weights.bin")
            completed = measure_resources(cfg, config, ready)
            self.assertEqual(completed.estimates["asr_turbo_model"], "约 2.0 MB")
            (model / "weights.bin").unlink()
            model.rmdir()
            after_delete = measure_resources(cfg, config, missing)
            self.assertNotIn("asr_turbo_model", after_delete.sizes)
            self.assertEqual(after_delete.estimates["asr_turbo_model"], "约 2.0 MB")

    def test_cache_and_old_roots_are_counted_separately_from_current_model(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            root = current_root(cfg)
            old = Path(folder) / "old"
            remember_root(config, old)
            for location in (root / "setup-cache", old / "setup-cache", old / "models/demucs"):
                location.mkdir(parents=True)
                (location / "data").write_bytes(b"123")
            result = measure_resources(cfg, config, [])
            self.assertEqual(result.sizes["download_cache"], 6)
            self.assertNotIn("separator_model", result.sizes)
            self.assertIn("空间统计完成", result.summary)

    def test_cleanup_removes_reviewed_model_but_keeps_video_and_config(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            model = Path(cfg["asr"]["model_path"])
            model.mkdir(parents=True)
            (model / "weights.bin").write_bytes(b"model")
            video = Path(folder) / "my-video.mp4"
            video.write_bytes(b"video")
            entries = cleanup_inventory(cfg, config, [video])
            selected = [e for e in entries if e.key == "asr_turbo_model"]
            self.assertEqual(selected[0].size, 5)
            self.assertEqual(clean_resources(selected, cfg, config, lambda _: None, [video]), 1)
            self.assertFalse(model.exists())
            self.assertTrue(config.is_file())
            self.assertEqual(video.read_bytes(), b"video")

    def test_external_environment_is_only_locatable(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            external = Path(folder) / "shared-env"
            external.mkdir()
            cfg["separator"]["python"] = str(external / "Scripts/python.exe")
            entries = cleanup_inventory(cfg, config)
            selected = [e for e in entries if e.path == external]
            self.assertFalse(selected[0].deletable)
            with self.assertRaises(VoxlateError):
                clean_resources(selected, cfg, config, lambda _: None)
            self.assertTrue(external.exists())

    def test_root_and_unexpected_subdirectories_cannot_be_deleted(self):
        with TestDirectory() as folder:
            root = Path(folder)
            for target in (root, root.parent, root / "personal-files"):
                with self.assertRaises(VoxlateError):
                    validate_target(target, root)

    def test_protected_media_under_model_is_not_removed(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            model = Path(cfg["asr"]["model_path"])
            model.mkdir(parents=True)
            video = model / "keep.mp4"
            video.write_bytes(b"keep")
            entry = next(e for e in cleanup_inventory(cfg, config, [video]) if e.key == "asr_turbo_model")
            self.assertFalse(entry.deletable)
            with self.assertRaises(VoxlateError):
                clean_resources([entry], cfg, config, lambda _: None, [video])
            self.assertTrue(video.exists())

    def test_old_resource_locations_remain_available_for_cleanup(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            old_root = Path(folder) / "old-resources"
            old_model = old_root / "models/demucs"
            old_model.mkdir(parents=True)
            (old_model / "model.th").write_bytes(b"old")
            remember_root(config, old_root)
            entries = cleanup_inventory(cfg, config)
            self.assertTrue(any(e.path == old_model and e.deletable for e in entries))

    def test_shared_python_requires_dependent_environment_selection(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            root = current_root(cfg)
            python = root / "tools/python"
            python.mkdir(parents=True)
            (python / "python.exe").write_bytes(b"python")
            env = root / ".venv"
            env.mkdir()
            (env / "pyvenv.cfg").write_text(f"home = {python}\n", encoding="utf-8")
            entries = cleanup_inventory(cfg, config)
            selected = [e for e in entries if e.key == "python"]
            with self.assertRaisesRegex(VoxlateError, "仍被此环境使用"):
                clean_resources(selected, cfg, config, lambda _: None)
            self.assertTrue(python.exists())
            selected += [e for e in entries if e.key == "runtime"]
            clean_resources(selected, cfg, config, lambda _: None)
            self.assertFalse(python.exists())
            self.assertFalse(env.exists())

    def test_revalidation_rejects_new_link_before_delete(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            model = Path(cfg["asr"]["model_path"])
            model.mkdir(parents=True)
            outside = Path(folder) / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep")
            entries = [e for e in cleanup_inventory(cfg, config) if e.key == "asr_turbo_model"]
            link = model / "link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    self.skipTest("Symlink creation is unavailable")
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            try:
                with self.assertRaisesRegex(VoxlateError, "链接"):
                    clean_resources(entries, cfg, config, lambda _: None)
                self.assertTrue((outside / "keep.txt").exists())
            finally:
                if link.is_symlink():
                    link.unlink()
                else:
                    os.rmdir(link)

    @unittest.skipUnless(os.name == "nt", "Windows managed Python junction")
    def test_internal_python_junction_can_be_cleaned(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            python = current_root(cfg) / "tools/python"
            version = python / "cpython-3.11.16"
            version.mkdir(parents=True)
            (version / "python.exe").write_bytes(b"python")
            alias = python / "cpython-3.11"
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(version)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            entries = [e for e in cleanup_inventory(cfg, config) if e.key == "python"]
            self.assertTrue(entries[0].deletable)
            clean_resources(entries, cfg, config, lambda _: None)
            self.assertFalse(python.exists())
