import hashlib
import io
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from test_pipeline import TestDirectory
from voxlate.app_settings import prepare_settings
from voxlate.common import VoxlateError, load_config
from voxlate.diagnostics import ResourceStatus
from voxlate.installer import Installer, download, planned_stages, relocate, resource_root, safe_extract, PYPI_MIRROR, PYPI_OFFICIAL, HF_MIRROR, HF_OFFICIAL
from voxlate.media import set_cancel_event
from voxlate.installer import DownloadActivity, EnvironmentActivity


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(data))}


class InstallerTests(unittest.TestCase):
    def test_environment_progress_ignores_old_cache_and_does_not_advance_with_time(self):
        with TestDirectory() as folder:
            cache, env = Path(folder) / "cache", Path(folder) / "env"
            cache.mkdir()
            (cache / "old-package").write_bytes(b"x" * 1000)
            now = [0]
            activity = EnvironmentActivity(cache, env, 1000, clock=lambda: now[0])
            activity.observe("Downloading torch (3.2GiB)")
            now[0] = 65
            fraction, detail = activity.sample()
            self.assertEqual(fraction, 0.2)
            self.assertIn("65 秒无新增文件", detail)
            self.assertIn("torch", detail)
            (cache / "new-package").write_bytes(b"x" * 500)
            now[0] = 70
            self.assertEqual(activity.sample()[0], 0.5)
            env.mkdir()
            (env / "linked-package").write_bytes(b"x" * 500)
            now[0] = 75
            self.assertEqual(activity.sample()[0], 0.5)
            activity.observe("Prepared 1 package in 1s")
            self.assertEqual(activity.sample()[0], 0.88)
            activity.observe("Installed 1 package in 1s")
            self.assertEqual(activity.sample()[0], 0.97)

    def test_first_dependency_group_cannot_finish_entire_environment(self):
        with TestDirectory() as folder:
            activity = EnvironmentActivity(Path(folder) / "cache", Path(folder) / "env", 1000)
            activity.ceiling = 0.82
            activity.observe("Installed 2 packages in 1s")
            self.assertEqual(activity.sample()[0], 0.82)

    def test_environment_monitor_updates_during_quiet_subprocess(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            updates = []
            installer = Installer(load_config(path), path, lambda _: None, progress=updates.append)
            installer._environment_activity = EnvironmentActivity(installer.cache / "uv", Path(folder) / "env", 1000)
            installer.run([sys.executable, "-u", "-c", "import time; print('Downloading torch (3.2GiB)'); time.sleep(5.5)"], "test")
            self.assertTrue(any("环境准备约 20%" in item["detail"] and "torch" in item["detail"] for item in updates))
            self.assertTrue(all(item["percent"] < 100 for item in updates))

    def test_failed_environment_does_not_leave_monitor_attached(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            installer = Installer(load_config(path), path, lambda _: None)
            installer._environment_activity = object()
            with patch.object(installer, "_prepare_environment", side_effect=VoxlateError("已取消")):
                with self.assertRaises(VoxlateError):
                    installer.environment("tts")
            self.assertIsNone(installer._environment_activity)

    def test_file_count_is_not_reported_as_byte_percentage(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            updates, messages = [], []
            installer = Installer(load_config(path), path, messages.append, progress=updates.append)
            installer._download_range = (0.15, 0.9)
            installer.emit("Fetching 10 files: 80%|#### | 8/10 [02:31<00:37, 18.93s/it]")
            self.assertEqual(updates[-1]["percent"], 0)
            self.assertIn("8/10", updates[-1]["detail"])
            self.assertNotIn("80%", messages[-1])

    def test_model_activity_reports_saved_bytes_growth_and_idle(self):
        with TestDirectory() as folder:
            now = [0]
            partial = Path(folder) / "model.incomplete"
            partial.write_bytes(b"x" * 1_000_000)
            activity = DownloadActivity(Path(folder), clock=lambda: now[0])
            with partial.open("ab") as stream:
                stream.write(b"x" * 1_000_000)
            now[0] = 5
            self.assertIn("2.0 MB · 约 0.2 MB/秒", activity.sample())
            partial.rename(Path(folder) / "model.bin")
            now[0] = 65
            self.assertIn("60 秒未新增数据", activity.sample())

    def test_cancel_download_keeps_partial_for_next_attempt(self):
        with TestDirectory() as folder:
            target = Path(folder) / "model.bin"
            event = threading.Event()
            set_cancel_event(event)
            try:
                with patch("urllib.request.urlopen", return_value=Response(b"x" * 300_000)):
                    with self.assertRaisesRegex(VoxlateError, "已取消"):
                        download("https://example.test/file", target, lambda _: event.set())
                self.assertFalse(target.exists())
                self.assertEqual(target.with_name("model.bin.part").stat().st_size, 256 * 1024)
            finally:
                set_cancel_event(None)

    def test_hy_translation_download_needs_no_python_or_conversion_environment(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            installer = Installer(load_config(path), path, lambda _: None)
            with patch.object(installer, "venv", side_effect=AssertionError("unneeded environment")), patch.object(installer, "pip") as pip, patch("voxlate.installer.download") as download, patch('voxlate.installer.preserve_model_terms'):
                installer.native_translation("hy7_model")
            pip.assert_not_called()
            self.assertIn("Hy-MT2-7B-GGUF", download.call_args.args[0])

    def test_progress_is_monotonic_and_leaves_final_verification_unfinished(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            updates = []
            installer = Installer(load_config(path), path, lambda _: None, progress=updates.append)
            def prepare(kind):
                installer.phase(0.15, "下载")
                installer._download_range = (0.15, 0.9)
                installer.emit("下载 model.bin：50 MB / 100 MB（50%）")
                installer.emit("下载 model.bin：100 MB / 100 MB（100%）")
                installer.phase(0.1, "镜像重试")
            with patch.object(installer, "models", side_effect=prepare), patch.object(installer, "recognition_model", side_effect=prepare):
                installer.install(["asr_turbo_model", "tts_model"], accepted_terms=True)
            values = [item["percent"] for item in updates]
            self.assertEqual(values, sorted(values))
            self.assertEqual(values[-1], 95)
            self.assertTrue(any(item["detail"] == "当前下载 50%" for item in updates))
            self.assertTrue(all(value < 100 for value in values))

    def test_mirror_failure_retries_official_and_off_switch_skips_mirror(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            messages = []
            installer = Installer(load_config(path), path, messages.append)
            with patch.object(installer, "run", side_effect=[VoxlateError("network failure"), None]) as run:
                installer.pip("python.exe", "some-package==1.0")
            self.assertIn(PYPI_MIRROR, run.call_args_list[0].args[0])
            self.assertIn(PYPI_OFFICIAL, run.call_args_list[1].args[0])
            self.assertIn("some-package==1.0", run.call_args_list[1].args[0])
            self.assertTrue(any("改用原站" in line for line in messages))
            installer.cfg["try_mirrors"] = False
            with patch.object(installer, "run") as run:
                installer.pip("python.exe", "some-package==1.0")
            run.assert_called_once()
            self.assertIn(PYPI_OFFICIAL, run.call_args.args[0])

    def test_cancel_does_not_trigger_mirror_fallback(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            installer = Installer(load_config(path), path, lambda _: None)
            with patch.object(installer, "run", side_effect=VoxlateError("已取消")) as run:
                with self.assertRaisesRegex(VoxlateError, "已取消"):
                    installer.pip("python.exe", "some-package")
                run.assert_called_once()

    def test_hf_models_fallback_uses_official_env_and_other_sources_stay_official(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            installer = Installer(load_config(path), path, lambda _: None)
            with patch.object(installer, "venv", return_value="python.exe"), patch.object(installer, "pip"), patch.object(installer, "run", side_effect=[VoxlateError("mirror unavailable"), None]) as run:
                installer.models("tts")
            self.assertEqual(run.call_args_list[0].kwargs["env"]["HF_ENDPOINT"], HF_MIRROR)
            self.assertEqual(installer.env["HF_ENDPOINT"], HF_OFFICIAL)
            self.assertNotIn("env", run.call_args_list[1].kwargs)
            self.assertEqual(run.call_args_list[0].args[0], run.call_args_list[1].args[0])
            with patch.object(installer, "run") as run:
                installer.pip("python.exe", "torch", index="https://download.pytorch.org/whl/cpu")
            run.assert_called_once()
            self.assertIn("https://download.pytorch.org/whl/cpu", run.call_args.args[0])

    def test_inherited_mirror_variables_cannot_override_official_choice(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            cfg = load_config(path)
            cfg["try_mirrors"] = False
            with patch.dict("os.environ", {"HF_ENDPOINT": "https://example.test", "UV_INDEX_URL": "https://example.test", "PIP_INDEX_URL": "https://example.test", "UV_PYTHON_INSTALL_MIRROR": "https://example.test"}):
                installer = Installer(cfg, path, lambda _: None)
            self.assertEqual(installer.env["HF_ENDPOINT"], HF_OFFICIAL)
            self.assertNotIn("UV_INDEX_URL", installer.env)
            self.assertNotIn("PIP_INDEX_URL", installer.env)
            self.assertNotIn("UV_PYTHON_INSTALL_MIRROR", installer.env)

    def test_download_resumes_and_validates_before_publishing(self):
        with TestDirectory() as folder:
            target = Path(folder) / "resource.zip"
            partial = target.with_name(target.name + ".part")
            partial.write_bytes(b"abc")
            response = Response(b"def", 206, {"Content-Range": "bytes 3-5/6", "Content-Length": "3"})
            with patch("urllib.request.urlopen", return_value=response) as open_url:
                download("https://example.test/file", target, lambda _: None, hashlib.sha256(b"abcdef").hexdigest())
                self.assertEqual(open_url.call_args.args[0].get_header("Range"), "bytes=3-")
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertFalse(partial.exists())

    def test_server_ignoring_range_restarts_without_corrupting_file(self):
        with TestDirectory() as folder:
            target = Path(folder) / "resource.zip"
            target.with_name(target.name + ".part").write_bytes(b"old")
            with patch("urllib.request.urlopen", return_value=Response(b"new")):
                download("https://example.test/file", target, lambda _: None, hashlib.sha256(b"new").hexdigest())
            self.assertEqual(target.read_bytes(), b"new")

    def test_bad_checksum_never_replaces_existing_resource(self):
        with TestDirectory() as folder:
            target = Path(folder) / "resource.zip"
            target.write_bytes(b"original")
            with patch("urllib.request.urlopen", return_value=Response(b"bad")):
                with self.assertRaisesRegex(VoxlateError, "校验失败"):
                    download("https://example.test/file", target, lambda _: None, "0" * 64)
            self.assertEqual(target.read_bytes(), b"original")

    def test_archive_rejects_traversal_before_writing_any_files(self):
        with TestDirectory() as folder:
            archive = Path(folder) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as source:
                source.writestr("ok.txt", "ok")
                source.writestr("../escape.txt", "bad")
            with self.assertRaisesRegex(VoxlateError, "不安全路径"):
                safe_extract(archive, Path(folder) / "extract")
            self.assertFalse((Path(folder) / "extract/ok.txt").exists())
            self.assertFalse((Path(folder) / "escape.txt").exists())

    def test_plan_skips_ready_and_combines_ffmpeg_pair(self):
        statuses = [ResourceStatus(k, k, ready, "", "") for k, ready in
                    (("ffmpeg", False), ("ffprobe", False), ("runtime", True), ("tts_model", False))]
        self.assertEqual(planned_stages(statuses), ["ffmpeg", "tts_model"])
        self.assertEqual(planned_stages(statuses, "ffprobe"), ["ffmpeg"])

    def test_failure_preserves_successful_configuration(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            installer = Installer(load_config(path), path, lambda _: None)
            def fake_ffmpeg():
                installer.cfg["ffmpeg"] = str(Path(folder) / "new-ffmpeg.exe")
            with patch.object(installer, "ffmpeg", fake_ffmpeg), patch.object(installer, "environment", side_effect=RuntimeError("offline")):
                with self.assertRaisesRegex(VoxlateError, "识别与翻译环境准备未完成"):
                    installer.install(["ffmpeg", "runtime"], accepted_terms=True)
            self.assertEqual(load_config(path)["ffmpeg"], str(Path(folder) / "new-ffmpeg.exe"))

    def test_cancel_installer_process_without_waiting_for_completion(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            installer = Installer(load_config(path), path, lambda _: None)
            event = threading.Event()
            timer = threading.Timer(0.5, event.set)
            set_cancel_event(event)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(VoxlateError, "已取消"):
                    installer.run([sys.executable, "-u", "-c", "import time; print('started'); time.sleep(30)"], "test")
                self.assertLess(time.monotonic() - started, 8)
            finally:
                set_cancel_event(None)
                timer.cancel()

    def test_storage_change_keeps_devices_and_uses_chosen_folder(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            cfg = load_config(path)
            original_python = cfg["runtime"]["python"]
            changed = relocate(cfg, Path(folder) / "资源 空格")
            self.assertEqual(cfg["runtime"]["python"], original_python)
            self.assertEqual(changed["tts"]["device"], cfg["tts"]["device"])
            self.assertTrue(Path(changed["tts"]["model_path"]).is_relative_to(Path(folder) / "资源 空格"))
            cfg["ffmpeg"] = str(resource_root(cfg) / "tools/ffmpeg/ffmpeg.exe")
            cfg["ffprobe"] = "C:/system/ffprobe.exe"
            with patch("voxlate.installer.shutil.which", side_effect=lambda value: value):
                changed = relocate(cfg, Path(folder) / "资源 空格")
            self.assertEqual(Path(changed["ffmpeg"]), Path(folder) / "资源 空格/tools/ffmpeg/ffmpeg.exe")
            self.assertEqual(changed["ffprobe"], cfg["ffprobe"])
