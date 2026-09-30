import copy
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch, Mock

from test_pipeline import TestDirectory
from voxlate.app_settings import prepare_settings
from voxlate.common import VoxlateError, load_config, read_json, write_json, digest
from voxlate.diagnostics import check_resources, install_command, probe_runtime
from voxlate.diagnostics import ResourceStatus
from voxlate.media import run_process, set_cancel_event
from voxlate.runtime import worker_command


class ResourceTests(unittest.TestCase):
    def test_development_settings_reuse_resources_without_changing_release_settings(self):
        from voxlate.app_settings import default_data_dir
        with TestDirectory() as folder, patch.dict(os.environ, LOCALAPPDATA=folder), \
             patch('voxlate.app_settings.__version__', '0.2.0-dev'):
            stable, _ = prepare_settings(Path(folder)/'voxlate')
            before = stable.read_bytes()
            cfg = load_config(stable)
            development, _ = prepare_settings(default_data_dir())
            self.assertEqual(development.parent.name, 'voxlate-dev')
            self.assertEqual(load_config(development)['tts']['model_path'], cfg['tts']['model_path'])
            modified = read_json(development)
            modified.update(source_lang='zh', target_lang='ja')
            write_json(development, modified)
            self.assertEqual(stable.read_bytes(), before)
            self.assertEqual(prepare_settings(default_data_dir())[0], development)
            self.assertEqual(load_config(development)['target_lang'], 'ja')

    def test_targeted_model_check_reuses_environments_and_reports_only_changed_item(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            cfg = load_config(path)
            cached = check_resources(cfg, path, probe=lambda *args: (True, "ready"))
            directory = Path(cfg["asr"]["model_path"])
            directory.mkdir(parents=True)
            for name in ("model.bin", "config.json", "tokenizer.json"):
                (directory / name).write_bytes(b"model")
            reported = []
            with patch("voxlate.diagnostics.run_external", side_effect=AssertionError("unexpected program check")), patch("voxlate.diagnostics.missing_files", return_value=[]):
                updated = check_resources(cfg, path, keys={"asr_turbo_model"}, cached=cached,
                    probe=lambda *args: self.fail("unchanged environment was checked"), report=reported.append)
            self.assertEqual([r.key for r in reported], ["asr_turbo_model"])
            self.assertTrue(next(r.ready for r in updated if r.key == "asr_turbo_model"))
            self.assertEqual(next(r for r in updated if r.key == "tts"), next(r for r in cached if r.key == "tts"))
            self.assertEqual(updated.sizes["asr_turbo_model"], 15)

    def test_device_change_checks_only_its_environment_without_disk_scan(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            cfg = load_config(path)
            cached = check_resources(cfg, path, probe=lambda *args: (True, "ready"))
            calls = []
            with patch("voxlate.resources.os.walk", side_effect=AssertionError("unexpected size scan")):
                updated = check_resources(cfg, path, keys={"tts"}, size_keys=set(), cached=cached,
                    probe=lambda python, kind, cfg: (calls.append(kind) or False, "new device unavailable"))
            self.assertEqual(calls, ["tts"])
            self.assertFalse(next(r.ready for r in updated if r.key == "tts"))
            self.assertEqual(updated.summary, "")

    def test_shared_python_change_rechecks_dependent_environments(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            cfg = load_config(path)
            cached = check_resources(cfg, path, probe=lambda *args: (True, "ready"))
            calls = []
            check_resources(cfg, path, keys={"python"}, cached=cached,
                            probe=lambda python, kind, cfg: (calls.append(kind) or False, "removed"))
            self.assertEqual(calls, ["runtime", "qwen", "separator", "tts"])

    def test_missing_resources_have_specific_guidance(self):
        with TestDirectory() as folder:
            path, defaults = prepare_settings(folder)
            cfg = load_config(path)
            cfg["ffmpeg"] = "missing-ffmpeg-voxlate.exe"
            cfg["ffprobe"] = "missing-ffprobe-voxlate.exe"
            results = check_resources(cfg, path, probe=lambda *args: (False, "test missing runtime"))
            self.assertEqual(len(results), 13)
            self.assertFalse(any(s.ready for s in results))
            self.assertTrue(all(s.instructions for s in results))
            self.assertIn("gpt.pth", next(r.detail for r in results if r.key == "tts_model"))
            command = install_command(path, "tts")
            self.assertIn("support", command)
            self.assertIn("setup_windows.py", command)

    def test_probe_detects_real_missing_package(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            cfg = load_config(path)
            ready, detail = probe_runtime(sys.executable, "tts", cfg)
            self.assertFalse(ready)
            self.assertIn("源码缺失", detail)

    def test_external_worker_never_launches_gui_as_python(self):
        cfg = load_config(Path(__file__).resolve().parents[1] / "config.json")
        support = Path(__file__).resolve().parents[1] / "temp/test-external-support"
        with patch.object(sys, "frozen", True, create=True), patch("voxlate.runtime._external_root", support):
            command = worker_command(cfg, "asr", "job.json")
            self.assertEqual(command[0], cfg["runtime"]["python"])
            self.assertTrue(command[2].endswith("worker.py"))
            self.assertTrue(Path(command[2]).is_relative_to(support))
            cfg["runtime"]["python"] = sys.executable
            with self.assertRaises(VoxlateError):
                worker_command(cfg, "asr", "job.json")

    def test_cancellation_terminates_media_subprocess(self):
        event = threading.Event()
        timer = threading.Timer(0.4, event.set)
        set_cancel_event(event)
        started = time.monotonic()
        timer.start()
        try:
            with self.assertRaises(VoxlateError):
                run_process([sys.executable, "-c", "import time; time.sleep(30)"], "test")
            self.assertLess(time.monotonic() - started, 5)
        finally:
            timer.cancel()
            set_cancel_event(None)

    def test_incomplete_config_has_readable_error(self):
        with TestDirectory() as folder:
            path = Path(folder) / "config.json"
            write_json(path, {"asr": {}})
            with self.assertRaisesRegex(VoxlateError, "配置缺少"):
                load_config(path)


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox, QLabel, QLineEdit, QCheckBox, QPushButton, QRadioButton
    from PySide6.QtCore import QMimeData, QUrl, QPoint, QPointF, Qt, QTimer
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from voxlate.resources import CATALOG
    from voxlate.gui import MainWindow, TaskThread, ResourceSettingsDialog
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "Install requirements-gui.txt for GUI tests")
class GuiTests(unittest.TestCase):
    def test_dragging_moved_video_keeps_new_paths_and_allows_editing(self):
        import shutil
        from voxlate.common import file_hash
        from voxlate.project_storage import project_root
        root = Path(self.directory.name)
        old = root/'before'/'synthetic.AVI'
        old.parent.mkdir()
        old.write_bytes(b'synthetic fixture')
        self.window.video_selected(old)
        path = self.window.project_path
        project = dict(schema_version=1, name='voxlate', input=str(old), input_hash=file_hash(old),
            duration=2, segments=[dict(id=1, start=0, end=1, source_text='Hello', target_text='你好')])
        write_json(path, project)
        new = root/'after 日本' / old.name
        new.parent.mkdir()
        old.rename(new)
        shutil.move(project_root(old), project_root(new))
        with patch.object(QMessageBox, 'warning') as warning:
            self.window.video_selected(new)
            self.assertEqual(self.window.video.text(), str(new))
            self.assertEqual(self.window.selected_video, str(new))
            moved_path = project_root(new)/path.parent.name/'project.json'
            self.assertEqual(self.window.project_path, moved_path)
            self.assertEqual(self.window.project_hash, digest(read_json(moved_path)))
            self.window.table.item(0, 2).setText('你好！')
            self.assertEqual(read_json(moved_path)['segments'][0]['target_text'], '你好！')
            self.assertEqual(read_json(moved_path)['input'], str(new))
            warning.assert_not_called()

    def test_audio_track_switch_preserves_projects_and_remembers_per_video(self):
        self.track_probe.return_value = [dict(index=0, label='音轨 1 · 英语 · 立体声'),
                                        dict(index=1, label='音轨 2 · 日语 · 5.1')]
        video = Path(self.directory.name)/'双语.mkv'
        video.write_bytes(b'synthetic')
        self.window.video_selected(video)
        first = self.window.project_path
        self.assertTrue(first.parent.name.endswith('-audio-1'))
        self.assertEqual(self.window.output_path.name, '双语.audio-1.en-zh.mp4')
        project = dict(schema_version=1, name='voxlate', input=str(video), duration=2,
            segments=[dict(id=1,start=0,end=1,source_text='Hello',target_text='你好')])
        write_json(first, project)  # Legacy projects implicitly used audio track 1.
        self.window.load_project(first)
        before = first.read_bytes()
        self.window.audio_track_box.setCurrentIndex(1)
        second = self.window.project_path
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), before)
        self.assertIsNone(self.window.project)
        self.assertEqual(self.window.output_path.name, '双语.audio-2.en-zh.mp4')
        self.assertEqual(self.window.cfg['audio_track'], 1)
        self.window.save_settings()
        self.assertEqual(self.window.cfg['audio_track'], 1)
        self.assertNotIn('audio_track', read_json(self.window.config_path))
        self.assertNotIn('audio_track_count', read_json(self.window.config_path))
        with patch.object(self.window, 'restore_resources'):
            self.window.change_storage(Path(self.directory.name)/'new-resources')
        self.assertEqual(self.window.cfg['audio_track'], 1)
        self.assertNotIn('audio_track', read_json(self.window.config_path))
        self.window.audio_track_box.setCurrentIndex(0)
        self.assertEqual(self.window.project_path, first)
        self.assertEqual(self.window.table.item(0, 1).text(), 'Hello')
        with patch.object(self.window, 'confirm_discard', return_value=False):
            self.window.audio_track_box.setCurrentIndex(1)
        self.assertEqual(self.window.audio_track_box.currentData(), 0)
        self.window.audio_track_box.setCurrentIndex(1)
        self.window.video_selected(video)
        self.assertEqual(self.window.audio_track_box.currentData(), 1)
        self.assertEqual(self.window.project_path, second)
        other = Path(self.directory.name)/'other.mp4'
        other.write_bytes(b'synthetic')
        self.window.video_selected(other)
        self.assertEqual(self.window.cfg['audio_track'], 0)
        self.window.video_selected(video)
        self.assertEqual(self.window.cfg['audio_track'], 1)

    def test_multi_track_first_reuses_legacy_project_and_numbers_new_export(self):
        self.track_probe.return_value = [dict(index=0, label='音轨 1'), dict(index=1, label='音轨 2')]
        video = Path(self.directory.name)/'legacy.mkv'
        video.write_bytes(b'synthetic')
        legacy = self.window.default_project_path(video)
        from voxlate.project_storage import default_project_directory
        numbered = default_project_directory(video, dict(self.window.cfg, audio_track_count=2))
        numbered.mkdir(parents=True)  # Interrupted setup left no project.json.
        marker = numbered/'keep.txt'
        marker.write_text('synthetic unfinished work')
        old_export = video.with_name('legacy.zh.mp4')
        old_export.write_bytes(b'synthetic export')
        write_json(legacy, dict(schema_version=1, name='voxlate', input=str(video), duration=2,
            output=str(old_export),
            segments=[dict(id=1,start=0,end=1,source_text='Hello',target_text='旧译文')]))
        before = legacy.read_bytes()
        self.window.video_selected(video)
        self.assertEqual(self.window.project_path, legacy)
        self.assertEqual(self.window.output_path.name, 'legacy.audio-1.en-zh.mp4')
        self.assertEqual(self.window.table.item(0,2).text(), '旧译文')
        self.assertEqual(legacy.read_bytes(), before)
        self.assertTrue(marker.is_file())
        with patch.object(self.window, 'play_video') as play:
            self.window.play_output_button.click()
            play.assert_called_once_with(old_export)
        self.window.output_path.write_bytes(b'synthetic numbered export')
        with patch.object(self.window, 'play_video') as play:
            self.window.play_output_button.click()
            play.assert_called_once_with(self.window.output_path)
        self.window.audio_track_box.setCurrentIndex(1)
        self.window.audio_track_box.setCurrentIndex(0)
        self.assertEqual(self.window.project_path, legacy)
        self.assertEqual(legacy.read_bytes(), before)
        write_json(numbered/'project.json', dict(schema_version=1,name='voxlate', input=str(video),duration=2,
            audio_track=0, segments=[dict(id=1,start=0,end=1,source_text='New',target_text='新译文')]))
        self.window.video_selected(video)
        self.assertEqual(self.window.project_path, numbered/'project.json')
        self.assertEqual(self.window.table.item(0,2).text(), '新译文')
        self.track_probe.return_value = [dict(index=0, label='音轨 1')]
        single = Path(self.directory.name)/'single.mkv'
        single.write_bytes(b'synthetic')
        self.window.video_selected(single)
        self.assertEqual(self.window.output_path.name, 'single.en-zh.mp4')
        self.assertNotIn('-audio-', self.window.project_path.parent.name)

    def test_automatic_role_threshold_persists_manual_override(self):
        path = Path(self.directory.name)/'sample.mp4.voxlate/project.json'
        project = dict(schema_version=1, name='voxlate', input=str(Path(self.directory.name)/'sample.mp4'),
            duration=12, voice_mode='uniform', segments=[dict(id=i+1,start=i*2,end=i*2+1,
                source_text='Test',target_text='测试') for i in range(6)])
        write_json(path, project)
        self.window.load_project(path)
        def regroup(count):
            result = dict(roles=[dict(id=f'r{i}',name=f'角色{i+1}',reference_sentence_id=i+1) for i in range(count)],
                assignments={str(i+1):dict(role_id=f'r{i%count}',role_uncertain=False) for i in range(6)})
            with patch.object(self.window,'start_task',side_effect=lambda action,complete,**kw:complete(result)):
                self.window.auto_assign_roles()
        regroup(5)
        self.assertTrue(self.window.uniform_voice.isChecked())
        regroup(6)
        self.assertTrue(self.window.individual_voice.isChecked())
        self.assertEqual(read_json(path)['voice_mode'],'individual')
        self.window.uniform_voice.setChecked(True)
        self.window.load_project(path)
        regroup(6)
        self.assertTrue(self.window.uniform_voice.isChecked())
        self.assertEqual(read_json(path)['voice_mode'],'uniform')

    def test_video_player_volume_is_persisted_and_restored_including_mute(self):
        from test_pipeline import tone
        from voxlate.app_settings import player_volume
        audio = Path(self.directory.name)/'sample.wav'
        tone(audio, .2)
        cfg_before = self.window.config_path.read_bytes()
        for value in (27, 0):
            self.window.play_video(audio)
            player = next(iter(self.window.player_windows.values()))
            player.volume.setValue(value)
            self.assertAlmostEqual(player.audio.volume(), value/100, places=5)
            self.assertEqual(player_volume(self.window.data_dir), value)
            player.close()
            self.app.processEvents()
            self.window.play_video(audio)
            restored = next(iter(self.window.player_windows.values()))
            self.assertEqual(restored.volume.value(), value)
            self.assertAlmostEqual(restored.audio.volume(), value/100, places=5)
            restored.close()
            self.app.processEvents()
        self.assertEqual(self.window.config_path.read_bytes(), cfg_before)

    def test_roles_are_editable_persisted_and_hidden_in_other_modes(self):
        path = Path(self.directory.name)/'video.mp4.voxlate'/'project.json'
        project = dict(schema_version=1, name='voxlate', input=str(Path(self.directory.name)/'video.mp4'),
            duration=8, segments=[dict(id=1,start=0,end=2,source_text='Hello',target_text='你好'),
                dict(id=2,start=3,end=7,source_text='Thanks',target_text='谢谢')])
        write_json(path, project)
        self.window.load_project(path)
        self.assertTrue(self.window.table.isColumnHidden(5))
        with patch.object(self.window, 'auto_assign_roles') as automatic:
            self.window.role_voice.setChecked(True)
            self.app.processEvents()
            automatic.assert_called_once()
        self.assertFalse(self.window.table.isColumnHidden(5))
        self.assertEqual(self.window.table.horizontalHeader().visualIndex(5), 1)
        self.assertFalse(self.window.dub_button.isEnabled())
        roles = [dict(id='a',name='角色 A',reference_sentence_id=1),dict(id='b',name='角色 B',reference_sentence_id=2)]
        self.assertTrue(self.window.save_translations(roles=roles, assignments={1:dict(role_id='a'),2:dict(role_id='b')}, roles_initialized=True))
        self.window.refresh_role_column()
        self.assertTrue(self.window.dub_button.isEnabled())
        self.window.table.cellWidget(0,5).setCurrentIndex(1)
        self.assertEqual(read_json(path)['segments'][0]['role_id'], 'b')
        self.window.uniform_voice.setChecked(True)
        self.assertTrue(self.window.table.isColumnHidden(5))
        self.window.role_voice.setChecked(True)
        self.assertEqual(self.window.table.cellWidget(0,5).currentData(), 'b')

    def test_role_dialog_cancel_and_rename_validation(self):
        from voxlate.role_dialog import RoleDialog
        from test_roles import fixture
        project, preview = fixture(), Mock()
        dialog = RoleDialog(project, preview)
        dialog.table.cellWidget(0,0).setText('改名')
        dialog.preview(0)
        preview.assert_called_once_with(1)
        self.assertEqual(project['roles'][0]['name'], '角色 A')
        dialog.table.cellWidget(0,0).setText('角色 B')
        with patch.object(QMessageBox, 'information') as message:
            dialog.accept_roles()
            self.assertTrue(message.called)
        self.assertFalse(dialog.table.cellWidget(0,3).isEnabled())
        dialog.table.cellWidget(0,0).setText('改名')
        dialog.accept_roles()
        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        self.assertEqual(dialog.roles[0]['name'], '改名')
        dialog.deleteLater()

    def test_time_tooltip_handles_minute_and_hour_crossings(self):
        self.assertEqual(self.window.precise_timestamp(3599.9996), '01:00:00.000')
        self.assertEqual(self.window.precise_timestamp(61.123), '00:01:01.123')

    def test_role_management_limits_and_shared_scrollable_reference_list(self):
        from voxlate.role_dialog import RoleDialog
        from test_roles import fixture
        project=fixture()
        project['segments'].extend(dict(id=i,start=i*3,end=i*3+2,role_id='a') for i in range(4,3004))
        dialog=RoleDialog(project,lambda ident:None)
        first,second=dialog.table.cellWidget(0,1),dialog.table.cellWidget(1,1)
        self.assertIs(first.model(),second.model())
        self.assertEqual(first.count(),3004)
        self.assertEqual(first.maxVisibleItems(),8)
        self.assertLessEqual(first.view().maximumHeight(),240)
        first.setCurrentIndex(3000)
        self.assertEqual(first.currentData(),3000)
        self.assertEqual(second.currentData(),3)
        dialog.table.cellWidget(0,0).setText('abcdefghijk')
        self.assertEqual(dialog.table.cellWidget(0,0).text(),'abcdefgh')
        dialog.remove_role(0)
        self.assertEqual(len(dialog.roles),2)
        dialog.roles.extend(dict(id=f'extra{i}',name=f'角色{i}',reference_sentence_id=None) for i in range(1,48))
        dialog.render()
        dialog.add_role()
        self.assertEqual(len(dialog.roles),50)
        self.assertFalse(dialog.add_button.isEnabled())
        with patch.object(QMessageBox,'information'):
            dialog.add_role()
        self.assertEqual(len(dialog.roles),50)
        self.assertTrue(all(' ' not in r['name'] for r in dialog.roles[2:]))
        dialog.deleteLater()

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TestDirectory()
        tracks = patch('voxlate.gui.read_audio_tracks', return_value=[])
        self.track_probe = tracks.start()
        self.addCleanup(tracks.stop)
        self.window = MainWindow(Path(self.directory.name), auto_check=False)
        self.window.cfg['asr']['combined'] = False

    def tearDown(self):
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def test_config_persists_and_guidance_selects_missing_item(self):
        from voxlate.installer import relocate
        root = Path(self.directory.name) / "统一资源"
        self.window.cfg = relocate(self.window.cfg, root)
        self.window.save_settings()
        self.assertEqual(load_config(self.window.config_path)["asr"]["model_path"], str(root / "models/faster-whisper-large-v3-turbo"))
        results = check_resources(self.window.cfg, self.window.config_path, probe=lambda *args: (False, "missing"))
        self.window.resources_checked(results)
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.assertIn("重新检查", self.window.install_logs.toPlainText())
        self.assertEqual(self.window.tabs.tabText(1), "资源配置")
        self.assertFalse(hasattr(self.window, "resource_boxes"))

    def test_full_segmentation_available_after_separation_without_recognition(self):
        window=self.window
        window.project=dict(duration=90,segments=[])
        window.refresh_export_state()
        self.assertFalse(window.full_segmentation_button.isEnabled())
        window.project['prepared_audio_key']='synthetic-prepared-audio'
        window.refresh_export_state()
        self.assertTrue(window.full_segmentation_button.isEnabled())
        self.assertIn('手动微调',window.manual_segmentation_button.text())
        with patch.object(window,'edit_segmentation') as edit:
            window.full_segmentation_button.click()
            edit.assert_called_once_with(0,-1,full=True)
        window.task=Mock()
        window.refresh_export_state()
        self.assertFalse(window.full_segmentation_button.isEnabled())
        window.task=None
        window.project=None

    def test_four_recognition_radios_are_exclusive_and_combined_requires_both_models(self):
        results = check_resources(self.window.cfg, self.window.config_path, quick=True)
        self.window.resources_checked(results)
        heading = self.window.resource_nodes['asr_turbo_model'].parent()
        index = heading.indexOfChild(self.window.resource_nodes['asr_turbo_model'])
        self.assertIs(self.window.resource_tree.itemWidget(heading.child(index+1),0).findChild(QRadioButton),self.window.combined_asr)
        self.assertIs(heading.child(0),self.window.resource_nodes['asr_qwen_model'])
        self.assertEqual(self.window.combined_asr.text(),'综合识别（turbo + v3）')
        with patch.object(self.window, 'inspect'):
            self.window.combined_asr.click()
        self.assertTrue(self.window.cfg['asr']['combined'])
        self.assertEqual(len(self.window.asr_model_group.buttons()), 4)
        self.assertEqual(sum(b.isChecked() for b in self.window.asr_model_group.buttons()), 1)
        self.assertTrue(all(button.isEnabled() for button in self.window.asr_model_buttons.values()))
        self.assertTrue(all(button.isEnabled() for button in self.window.model_buttons.values()))
        checked = check_resources(self.window.cfg, self.window.config_path, quick=True)
        self.assertEqual({r.key for r in checked if r.required and r.key in self.window.asr_model_buttons},
                         {'asr_large_model', 'asr_turbo_model'})
        self.window.resources_checked(checked)
        self.assertTrue(self.window.combined_asr.isChecked())
        with patch.object(self.window, 'inspect') as inspect:
            self.window.asr_model_buttons['asr_turbo_model'].click()
        inspect.assert_not_called()
        self.assertFalse(self.window.cfg['asr']['combined'])
        self.assertFalse(self.window.combined_asr.isChecked())
        self.assertTrue(all(button.isEnabled() for button in self.window.asr_model_buttons.values()))

    def test_recognition_radio_switches_all_four_modes_and_preserves_settings(self):
        from voxlate.recognition_models import selected_key
        self.window.cfg['asr']['combined'] = True
        self.window.resources_checked(check_resources(self.window.cfg, self.window.config_path, quick=True))
        self.assertTrue(self.window.combined_asr.isChecked())
        for key in ('asr_qwen_model', 'asr_combined', 'asr_large_model', 'asr_combined', 'asr_turbo_model'):
            with self.subTest(key=key), patch.object(self.window, 'inspect') as inspect:
                self.window.asr_model_buttons[key].click()
                inspect.assert_not_called()
                saved = load_config(self.window.config_path)
                self.assertEqual(saved['asr']['combined'], key == 'asr_combined')
                if key == 'asr_combined':
                    self.assertNotEqual(selected_key(saved), 'asr_qwen_model')
                    self.assertNotIn('python', saved['asr'])
                else:
                    self.assertEqual(selected_key(saved), key)
                self.assertEqual(sum(b.isChecked() for b in self.window.asr_model_group.buttons()), 1)
                self.assertTrue(self.window.asr_model_buttons[key].isChecked())
        with patch.object(self.window, 'inspect') as inspect:
            self.window.asr_model_buttons['asr_turbo_model'].click()
            inspect.assert_not_called()
        self.window.set_resource_controls_enabled(False)
        self.assertTrue(all(not b.isEnabled() for b in self.window.asr_model_group.buttons()))

    def test_recognition_radio_cancel_and_save_failure_restore_selection(self):
        for combined in (False, True):
            self.window.cfg['asr']['combined'] = combined
            self.window.resources_checked(check_resources(self.window.cfg, self.window.config_path, quick=True))
            old = copy.deepcopy(self.window.cfg)
            previous = self.window.recognition_choice()
            with patch.object(self.window, 'inspect') as inspect, \
                 patch.object(self.window, 'confirm_discard', return_value=False):
                self.window.asr_model_buttons['asr_qwen_model'].click()
                inspect.assert_not_called()
            self.assertEqual(self.window.cfg, old)
            self.assertTrue(self.window.asr_model_buttons[previous].isChecked())
            with patch.object(self.window, 'inspect') as inspect, \
                 patch.object(self.window, 'save_settings', side_effect=OSError('test failure')), \
                 patch.object(QMessageBox, 'warning'):
                self.window.asr_model_buttons['asr_qwen_model'].click()
                inspect.assert_not_called()
            self.assertEqual(self.window.cfg, old)
            self.assertTrue(self.window.asr_model_buttons[previous].isChecked())

    def test_loading_combined_and_turbo_projects_updates_radio_selection(self):
        self.window.resources_checked(check_resources(self.window.cfg, self.window.config_path, quick=True))
        video = Path(self.directory.name) / 'sample.mp4'
        video.write_bytes(b'fixture')
        for key in ('asr_combined', 'asr_turbo_model', 'asr_combined'):
            path = video.with_suffix('.mp4.voxlate') / key / 'project.json'
            write_json(path, dict(schema_version=1, name='voxlate', input=str(video.resolve()), duration=2,
                                 recognition_model=key, segments=[dict(id=1, start=0, end=1,
                                                                     source_text='Hello', target_text='你好')]))
            self.window.load_project(path)
            self.assertEqual(self.window.recognition_choice(), key)
            self.assertTrue(self.window.asr_model_buttons[key].isChecked())
            self.assertEqual(sum(b.isChecked() for b in self.window.asr_model_group.buttons()), 1)

    def test_space_column_uses_measurements_and_log_reports_total(self):
        from voxlate.resources import ResourceInspection
        results = ResourceInspection([ResourceStatus("tts_model", "model", False, "missing", "")],
                                     {"tts_model": 2_180_000_000}, {}, "空间统计完成：2.18 GB")
        self.window.resources_checked(results)
        self.assertEqual(self.window.resource_nodes["tts_model"].text(1), "已有 2.18 GB")
        self.assertIn("空间统计完成：2.18 GB", self.window.install_logs.toPlainText())
        self.assertIn("install", self.window.row_buttons["tts_model"])
        self.window.install_started = time.monotonic()
        self.window.update_install_progress({"percent": 95, "detail": "准备完成", "resource_key": "tts_model", "resource_bytes": 7_000_000_000})
        self.assertEqual(self.window.resource_nodes["tts_model"].text(1), "7.00 GB")
        self.window.install_started = None

    def test_tts_progress_uses_sentence_count_without_install_timer(self):
        self.window.update_install_progress(dict(stage="tts", completed=3, total=46, detail="中文配音 3/46 句"))
        self.assertEqual(self.window.progress.maximum(), 46)
        self.assertEqual(self.window.progress.value(), 3)
        self.assertIn("3/46", self.window.activity.text())
        self.assertIsNone(self.window.install_started)
        self.window.update_install_progress(dict(stage="processing"))
        self.assertEqual(self.window.progress.maximum(), 0)

    def test_translation_snapshot_is_displayed_while_dubbing_is_still_running(self):
        path = Path(self.directory.name) / "project.mp4.voxlate" / "project.json"
        snapshot = dict(schema_version=1, name="voxlate", input=str(Path(self.directory.name) / "project.mp4"),
            duration=2, segments=[dict(id=1, start=0, end=1, source_text="Hello", target_text="你好")])
        release = threading.Event()
        try:
            self.window.start_task(lambda emit: release.wait(5), lambda result: None)
            self.window.update_install_progress(dict(stage="project_ready", path=str(path), project=snapshot))
            self.assertIsNotNone(self.window.task)
            self.assertEqual(self.window.table.rowCount(), 1)
            self.assertEqual(self.window.table.item(0, 2).text(), "你好")
            self.assertTrue(self.window.table.isEnabled())
            self.assertEqual(self.window.table.editTriggers(), self.window.table.EditTrigger.NoEditTriggers)
            self.window.toggle_sentence(0, 0)
            self.assertTrue(self.window.table.item(0, 0).data(Qt.ItemDataRole.UserRole))
            self.assertFalse(path.exists(), "Live display must not write into the active pipeline project")
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while self.window.task and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)

    def test_corrupt_settings_recover_without_losing_original(self):
        self.window.close()
        self.window.config_path.write_text("broken-json", encoding="utf-8")
        self.window = MainWindow(Path(self.directory.name), auto_check=False)
        self.assertTrue(self.window.config_error)
        self.window.save_settings()
        self.assertEqual(self.window.config_path.with_name("config.invalid.json").read_text(), "broken-json")
        self.assertFalse(self.window.config_error)

    def test_projects_live_beside_video_and_do_not_automatically_load_legacy(self):
        video = (Path(self.directory.name) / "sample.mp4").resolve()
        video.write_bytes(b"test")
        nearby = video.parent / "sample.mp4.voxlate" / "en-whisper-large-v3-turbo" / "project.json"
        self.assertEqual(self.window.default_project_path(video), nearby)
        legacy = self.window.data_dir / "projects" / (video.stem + "-" + digest(str(video))[:8]) / "project.json"
        write_json(legacy, {"keep": "existing cache"})
        self.assertEqual(self.window.default_project_path(video), nearby)
        self.window.video.setText(str(video))
        self.window.new_project()
        self.assertEqual(self.window.project_path.parent.parent, nearby.parent.parent)
        self.assertNotEqual(self.window.project_path, legacy)
        self.assertEqual(read_json(legacy), {"keep": "existing cache"})
        write_json(nearby, {})
        self.assertEqual(self.window.default_project_path(video), nearby)

    def test_translation_has_only_7b_and_needs_no_radio_choice(self):
        from voxlate.translation_models import selected_key, MODELS
        results = check_resources(self.window.cfg, self.window.config_path, probe=lambda *args: (True, "ready"))
        self.window.resources_checked(results)
        self.assertEqual(set(MODELS), {'hy7_model'})
        self.assertEqual(self.window.model_buttons,{})
        self.assertIn('hy7_model',self.window.resource_nodes)
        self.assertNotIn('hy1_model',self.window.resource_nodes)
        self.assertEqual(selected_key(self.window.cfg),'hy7_model')

    def test_recognition_choices_are_independent_and_keep_old_project(self):
        from voxlate.recognition_models import MODELS as ASR_MODELS, selected_key as asr_key
        from voxlate.translation_models import selected_key as translation_key
        video = Path(self.directory.name) / "sample.mp4"
        video.write_bytes(b"fixture")
        self.window.video_selected(video)
        original = self.window.project_path
        project = dict(schema_version=1, name="voxlate", input=str(video.resolve()), duration=2,
            recognition_model="asr_turbo_model",
            segments=[dict(id=1, start=0, end=1, source_text="Hello", target_text="你好")])
        write_json(original, project)
        self.window.load_project(original)
        results = check_resources(self.window.cfg, self.window.config_path, quick=True)
        self.window.resources_checked(results)
        translator = translation_key(self.window.cfg)
        with patch.object(self.window, "inspect") as inspect:
            self.window.asr_model_buttons["asr_large_model"].click()
        self.assertEqual(asr_key(load_config(self.window.config_path)), "asr_large_model")
        self.assertEqual(translation_key(self.window.cfg), translator)
        self.assertEqual(self.window.model_buttons, {})
        self.assertEqual(sum(b.isChecked() for b in self.window.asr_model_buttons.values()), 1)
        self.assertNotEqual(self.window.project_path, original)
        self.assertIn("en-whisper-large-v3", str(self.window.project_path))
        self.assertEqual(self.window.table.rowCount(), 0)
        self.assertEqual(read_json(original), project)
        inspect.assert_not_called()
        self.window.load_project(original)
        self.assertEqual(asr_key(self.window.cfg), "asr_turbo_model")
        self.assertEqual(self.window.table.item(0, 2).text(), "你好")

    def test_voice_sentence_selection_persists_and_resets_for_other_video(self):
        self.assertTrue(self.window.individual_voice.isChecked())
        folder = Path(self.directory.name)
        video = folder/'source.mp4'
        video.write_bytes(b'fixture')
        path = self.window.default_project_path(video)
        project = dict(schema_version=1, name='voxlate', input=str(video), duration=20,
            segments=[dict(id=1, start=1, end=4, source_text='Hello', target_text='你好'),
                      dict(id=2, start=5, end=10, source_text='Longer', target_text='长句')],
            reference_range=[3, 9], speaker_reference='old-external.wav')
        from voxlate.dubbing_state import recommended_reference
        project.update(reference_sentence_id=2,
                       reference_recommendation=recommended_reference(project, 2))
        write_json(path, project)
        self.window.load_project(path)
        self.assertTrue(self.window.uniform_voice.isChecked())
        self.assertEqual(self.window.reference_sentence.value(), 2)
        with patch.object(self.window, 'open_local') as opened:
            self.window.open_project_folder()
            opened.assert_called_once_with(video.with_name(video.name + '.voxlate'))
        self.window.reference_sentence.minus.click()
        self.assertEqual(self.window.reference_sentence.text(), '1')
        self.window.reference_sentence.plus.click()
        self.assertEqual(self.window.reference_sentence.value(), 2)
        self.window.reference_sentence.minus.click()
        saved = read_json(path)
        self.assertEqual(saved['reference_sentence_id'], 1)
        self.assertEqual(saved['speaker_reference'], '')
        self.assertIsNone(saved['reference_range'])
        from PySide6.QtTest import QTest
        from voxlate.dubbing_state import voice_selection, voice_key
        box = self.window.reference_sentence
        box.lineEdit().selectAll()
        QTest.keyClick(box.lineEdit(), Qt.Key.Key_Backspace)
        box.interpretText()
        self.assertEqual(box.value(), 0)
        self.assertEqual(box.text(), '')
        saved = read_json(path)
        self.assertIsNone(saved['reference_sentence_id'])
        self.assertTrue(saved['reference_auto_recommend'])
        self.assertEqual(voice_selection(saved), ('uniform', 2))
        automatic_key = voice_key(saved, self.window.cfg)
        self.window.load_project(path)
        self.assertEqual(box.text(), '')
        self.assertEqual(box.lineEdit().placeholderText(), '自动推荐')
        box.setValue(2)
        saved = read_json(path)
        self.assertFalse(saved['reference_auto_recommend'])
        self.assertEqual(voice_key(saved, self.window.cfg), automatic_key)
        self.window.individual_voice.click()
        self.assertFalse(self.window.reference_sentence.isEnabled())
        self.assertEqual(read_json(path)['voice_mode'], 'individual')
        self.window.load_project(path)
        self.assertTrue(self.window.individual_voice.isChecked())
        other = folder/'other.mp4'
        other.write_bytes(b'other')
        self.window.video_selected(str(other))
        self.assertTrue(self.window.individual_voice.isChecked())
        self.assertEqual(self.window.reference_sentence.value(), 0)
        self.assertEqual(self.window.reference_sentence.text(), '')
        self.assertEqual(self.window.reference_sentence.lineEdit().placeholderText(), '自动推荐')

    def test_players_are_nonmodal_and_main_window_remains_editable(self):
        from PySide6.QtMultimedia import QMediaPlayer
        from PySide6.QtTest import QTest
        folder = Path(self.directory.name)
        video = folder / 'source.mp4'
        video.write_bytes(b'fixture')
        path = self.window.default_project_path(video)
        write_json(path, dict(schema_version=1, name='voxlate', input=str(video), duration=20,
                             segments=[dict(id=1, start=1, end=4, source_text='Hello', target_text='你好')]))
        self.window.load_project(path)
        self.window.show()
        with patch.object(QMediaPlayer, 'setSource'):
            self.window.play_video(video)
            picker = next(iter(self.window.player_windows.values()))
            self.assertFalse(picker.isModal())
            self.assertIsNone(QApplication.activeModalWidget())
            self.window.play_video(video)
            self.assertEqual(len(self.window.player_windows), 1)
            QTest.mouseClick(self.window.tabs.tabBar(), Qt.MouseButton.LeftButton,
                             pos=self.window.tabs.tabBar().tabRect(1).center())
            self.assertEqual(self.window.tabs.currentIndex(), 1)
            self.window.table.item(0, 2).setText('大家好')
            self.assertEqual(read_json(path)['segments'][0]['target_text'], '大家好')
            picker.close()
            self.assertEqual(len(self.window.player_windows), 0)
            self.window.play_video(video)
            viewer = next(iter(self.window.player_windows.values()))
            self.assertFalse(viewer.isModal())
            self.window.close_output_players(video)
            self.assertFalse(viewer.isVisible())
            self.assertEqual(len(self.window.player_windows), 0)
            self.window.play_video(video)
            viewer = next(iter(self.window.player_windows.values()))
            self.window.close()
            self.assertFalse(viewer.isVisible())
            self.assertEqual(len(self.window.player_windows), 0)

    def test_source_language_switch_keeps_separate_projects_and_persists(self):
        video = Path(self.directory.name) / "sample.mp4"
        video.write_bytes(b"test")
        self.window.video_selected(str(video))
        english_path = self.window.project_path
        self.window.source_language.setCurrentIndex(1)
        self.assertEqual(self.window.cfg["source_lang"], "ja")
        self.assertEqual(load_config(self.window.config_path)["source_lang"], "ja")
        self.assertNotEqual(english_path, self.window.project_path)
        self.assertIsNone(self.window.project)
        self.window.dirty = True
        with patch.object(self.window, "save_translations", return_value=False):
            self.window.source_language.setCurrentIndex(0)
        self.assertEqual(self.window.source_language.currentData(), "ja-zh")
        self.assertTrue(self.window.dirty)

    def test_six_directions_update_table_output_and_restore_saved_project(self):
        from voxlate.languages import DIRECTIONS, LANGUAGES
        video = Path(self.directory.name)/'directions.mp4'
        video.write_bytes(b'synthetic')
        self.window.video_selected(str(video))
        paths, outputs = set(), set()
        for source, target in DIRECTIONS:
            index = self.window.source_language.findData(f'{source}-{target}')
            self.window.source_language.setCurrentIndex(index)
            paths.add(self.window.project_path)
            outputs.add(self.window.output_path)
            self.assertEqual(self.window.cfg['target_lang'], target)
            self.assertEqual(self.window.table.horizontalHeaderItem(2).text(), LANGUAGES[target]['name']+'译文')
            saved = load_config(self.window.config_path)
            self.assertEqual((saved['source_lang'], saved['target_lang']), (source, target))
        self.assertEqual(len(paths), 6)
        self.assertEqual(len(outputs), 6)
        saved_path = self.window.project_path
        write_json(saved_path, dict(schema_version=1, name='voxlate', input=str(video), duration=2,
            source_lang='en', target_lang='ja', segments=[dict(id=1,start=0,end=1,source_text='Hello',target_text='こんにちは')]))
        self.window.source_language.setCurrentIndex(0)
        self.window.source_language.setCurrentIndex(self.window.source_language.findData('en-ja'))
        self.assertEqual(self.window.project_path, saved_path)
        self.assertEqual(self.window.table.item(0,2).text(), 'こんにちは')
        self.assertTrue(self.window.translation_ready())

    def test_edit_translation_preserves_cache_keys(self):
        project_path = Path(self.directory.name) / "input.mp4.voxlate" / "project.json"
        project = {"schema_version": 1, "name": "voxlate", "input": str(Path(self.directory.name) / "input.mp4"),
                   "duration": 2.0, "segments": [{"id": 1, "start": 0.2, "end": 1.5,
                   "source_text": "Hello", "target_text": "你好", "translation_key": "keep", "tts_key": "old"}]}
        write_json(project_path, project)
        self.window.load_project(project_path)
        self.window.table.item(0, 2).setText("大家好")
        self.assertFalse(self.window.dirty)
        updated = read_json(project_path)
        self.assertEqual(updated["segments"][0]["target_text"], "大家好")
        self.assertEqual(updated["segments"][0]["translation_key"], "keep")
        self.assertFalse(self.window.dirty)

    def test_legacy_project_outside_video_directory_cannot_be_written(self):
        path = Path(self.directory.name) / 'legacy/project.json'
        project = dict(schema_version=1, name='voxlate', input=str(Path(self.directory.name)/'source.mp4'),
            duration=2, segments=[dict(id=1, start=0, end=1, source_text='Hello', target_text='你好')])
        write_json(path, project)
        self.window.load_project(path)
        with patch.object(QMessageBox, 'warning') as warning:
            self.window.table.item(0, 2).setText('新译文')
            self.assertFalse(self.window.save_translations())
            self.assertTrue(warning.called)
        self.assertEqual(read_json(path), project)
        self.window.dirty = False

    def test_sentence_selection_is_compact_dimmed_saved_and_locked_while_busy(self):
        path = Path(self.directory.name) / "project.mp4.voxlate" / "project.json"
        write_json(path, dict(schema_version=1, name="voxlate", input=str(Path(self.directory.name) / "project.mp4"),
            duration=2, segments=[dict(id=1, start=0, end=1, source_text="Hello", target_text="你好")]))
        self.window.load_project(path)
        table = self.window.table
        self.assertLessEqual(table.rowHeight(0), 40)
        self.assertIsNone(table.item(0, 0).data(Qt.ItemDataRole.CheckStateRole))
        self.assertEqual(table.item(0, 0).text(), "00:00:00 – 01")
        self.assertEqual(table.item(0, 0).toolTip(), '00:00:00.000 – 00:00:01.000')
        self.window.toggle_sentence(0, 0)
        self.assertTrue(table.item(0, 0).data(Qt.ItemDataRole.UserRole))
        self.window.toggle_sentence(0, 1)
        self.assertFalse(self.window.dirty)
        self.assertEqual(table.item(0, 2).foreground().color().name(), "#a0a8b4")
        self.assertTrue(self.window.save_translations())
        self.assertFalse(read_json(path)["segments"][0]["enabled"])
        self.window.load_project(path)
        self.assertFalse(table.item(0, 0).data(Qt.ItemDataRole.UserRole))
        release = threading.Event()
        self.window.start_task(lambda emit: release.wait(5), lambda result: None)
        try:
            self.assertTrue(table.isEnabled())
            self.window.toggle_sentence(0, 0)
            self.assertFalse(table.item(0, 0).data(Qt.ItemDataRole.UserRole))
            self.window.load_project(path)
            self.window.toggle_sentence(0, 1)
            self.assertFalse(table.item(0, 0).data(Qt.ItemDataRole.UserRole))
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while self.window.task and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.window.toggle_sentence(0, 1)
        self.assertEqual(table.item(0, 2).foreground().color().name(), "#23324a")

    def test_copy_selection_does_not_discard_source_sentence(self):
        from PySide6.QtTest import QTest
        path = Path(self.directory.name) / 'source.mp4.voxlate/project.json'
        project = dict(schema_version=1, name='voxlate', input=str(Path(self.directory.name) / 'source.mp4'),
            duration=3, segments=[dict(id=1, start=0, end=1, source_text='Hello', target_text='你好'),
                                  dict(id=2, start=1, end=2, source_text='Goodbye', target_text='再见')])
        write_json(path, project)
        self.window.load_project(path)
        self.window.show()
        self.app.processEvents()
        table = self.window.table
        before = path.read_bytes()
        for modifier in (Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.ShiftModifier):
            table.clearSelection()
            point = table.visualItemRect(table.item(0, 1)).center()
            QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, modifier, point)
            QTest.keyClick(table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(self.app.clipboard().text(), 'Hello')
            self.assertTrue(table.item(0, 0).data(Qt.ItemDataRole.UserRole))
            self.assertEqual(path.read_bytes(), before)
        table.clearSelection()
        start = table.visualItemRect(table.item(0, 1)).center()
        end = table.visualItemRect(table.item(1, 2)).center()
        QTest.mousePress(table.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(table.viewport(), end)
        QTest.mouseRelease(table.viewport(), Qt.MouseButton.LeftButton, pos=end)
        QTest.keyClick(table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.app.clipboard().text(), 'Hello\t你好\nGoodbye\t再见')
        self.assertEqual(path.read_bytes(), before)

    def test_sentence_numbers_and_timing_survive_reload_but_not_text_edits(self):
        from voxlate.translation_view import TIMING_ROLE, gradient_fraction
        path = Path(self.directory.name) / "project.mp4.voxlate" / "project.json"
        write_json(path, dict(schema_version=1, name="voxlate", input=str(Path(self.directory.name) / "project.mp4"),
            duration=4, segments=[dict(id=7, start=0, end=1, source_text="Hello", target_text="你好", generated_duration=3)]))
        self.window.load_project(path)
        table = self.window.table
        self.assertEqual(table.verticalHeaderItem(0).text(), "7")
        self.assertEqual(table.item(0, 2).data(TIMING_ROLE), 3)
        for ratio, width in ((1, 0), (1.2, 0), (1.4, .125), (1.6, .25), (1.8, .375), (2, .5), (12, .5)):
            self.assertAlmostEqual(gradient_fraction(ratio), width)
        self.window.toggle_sentence(0, 1)
        self.assertIsNone(table.item(0, 2).data(TIMING_ROLE))
        self.window.toggle_sentence(0, 1)
        self.assertEqual(table.item(0, 2).data(TIMING_ROLE), 3)
        table.item(0, 2).setText("嗨")
        self.assertIsNone(table.item(0, 2).data(TIMING_ROLE))
        self.window.load_project(path)
        self.assertIsNone(table.item(0, 2).data(TIMING_ROLE))
        self.window.update_install_progress(dict(stage="sentence_timing", id=7, generated_duration=2, timing_text="嗨"))
        self.assertEqual(table.item(0, 2).data(TIMING_ROLE), 2)
        self.assertIn("2.00×", table.item(0, 2).toolTip())

    def test_gradient_keeps_normal_text_layout(self):
        from PySide6.QtCore import QRect, Qt
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtWidgets import QStyleOptionViewItem, QTableWidgetItem, QStyle
        from voxlate.translation_view import TIMING_ROLE
        from voxlate.gui import STYLE
        table = self.window.table
        table.setStyleSheet(STYLE)
        table.blockSignals(True)
        table.setRowCount(1)
        item = QTableWidgetItem('Identical text layout')
        item.setForeground(QColor('#23324a'))
        table.setItem(0, 2, item)
        index = table.model().index(0, 2)
        def render(ratio):
            item.setData(TIMING_ROLE, ratio)
            image = QImage(550, 100, QImage.Format.Format_ARGB32)
            image.fill(QColor('white'))
            painter = QPainter(image)
            opt = QStyleOptionViewItem()
            opt.rect = QRect(100, 40, 400, 36)
            opt.widget = table
            opt.state = QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Active
            table.itemDelegateForColumn(2).paint(painter, opt, index)
            painter.end()
            return image
        normal, colored = render(None), render(2)
        self.assertEqual(normal.copy(100, 40, 190, 36), colored.copy(100, 40, 190, 36))
        self.assertNotEqual(normal.pixelColor(490, 58), colored.pixelColor(490, 58))
        table.blockSignals(False)


    def test_background_task_returns_to_idle(self):
        results = []
        self.window.start_task(lambda emit: "ok", results.append)
        deadline = time.monotonic() + 5
        while self.window.task and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(self.window.task)
        self.assertEqual(results, ["ok"])
        self.assertFalse(self.window.cancel_button.isEnabled())

    def test_startup_restores_cache_without_checking_resources(self):
        results = check_resources(self.window.cfg, self.window.config_path, quick=True)
        self.window.resources_checked(results)
        with patch("voxlate.gui.check_resources", side_effect=AssertionError("startup recheck")):
            self.window.restore_resources()
        self.assertIsNone(self.window.task)
        self.assertIn("沿用上次", self.window.logs.toPlainText())
        self.assertEqual(self.window.model_buttons, {})
        self.assertNotIn("保存译文", [button.text() for button in self.window.findChildren(QPushButton)])

    def test_model_switch_keeps_ready_states_and_cache_without_scanning(self):
        from voxlate.resource_cache import load_resource_cache
        results = check_resources(self.window.cfg, self.window.config_path, quick=True)
        for item in results:
            item.ready = True
        results.sizes['asr_qwen_model'] = 100
        self.window.resources_checked(results)
        with (patch('voxlate.gui.check_resources', side_effect=AssertionError('resource check')),
              patch('voxlate.gui.tracked_resource_paths', side_effect=AssertionError('presence scan')),
              patch('voxlate.resources.os.walk', side_effect=AssertionError('directory scan')),
              patch.object(self.window, 'start_task', side_effect=AssertionError('background check'))):
            for key in ('asr_qwen_model', 'asr_combined', 'asr_turbo_model'):
                self.window.asr_model_buttons[key].click()
                cached = load_resource_cache(self.window.cfg, self.window.config_path)
                self.assertIsNotNone(cached)
                self.assertTrue(all(item.ready for item in cached))
                self.assertEqual(cached.sizes['asr_qwen_model'], 100)
                self.assertEqual({r.key for r in cached if r.required and r.key in self.window.asr_model_buttons},
                                 {'asr_large_model', 'asr_turbo_model'} if key == 'asr_combined' else {key})

    def test_readonly_tasks_keep_resident_voice_model(self):
        with patch.object(self.window.tts_session, 'close') as close:
            self.window.start_task(lambda emit: 'checked', lambda result: None)
            deadline = time.monotonic()+5
            while self.window.task and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
            self.assertIsNone(self.window.task)
            close.assert_not_called()
            self.window.start_task(lambda emit: 'translated', lambda result: None, keep_tts=False)
            close.assert_called_once_with('开始识别翻译，腾出显存')
            deadline = time.monotonic()+5
            while self.window.task and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)

    def test_release_model_button_follows_session_and_keeps_audio_files(self):
        audio = Path(self.directory.name)/'saved-audio.wav'
        audio.write_bytes(b'saved synthetic audio')
        button = self.window.release_model_button
        self.assertFalse(button.isEnabled())
        self.assertEqual(button.toolTip(), '释放显存，下次配音需重新加载。')
        process = Mock()
        process.poll.return_value = None
        self.window.tts_session.process = process
        self.window.refresh_model_button()
        self.assertTrue(button.isEnabled())
        button.click()
        self.assertFalse(button.isEnabled())
        deadline = time.monotonic()+5
        while self.window.task and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertIsNone(self.window.task)
        self.assertFalse(button.isEnabled())
        process.terminate.assert_called_once()
        self.assertIsNone(self.window.tts_session.process)
        self.assertIn('手动释放', self.window.logs.toPlainText())
        self.assertEqual(audio.read_bytes(), b'saved synthetic audio')

    def test_failed_auto_save_preserves_external_changes_and_blocks_switch(self):
        path = Path(self.directory.name) / "project.mp4.voxlate" / "project.json"
        project = dict(schema_version=1, name="voxlate", input=str(Path(self.directory.name) / "project.mp4"),
            duration=2, segments=[dict(id=1, start=0, end=1, source_text="Hello", target_text="你好")])
        write_json(path, project)
        self.window.load_project(path)
        project["segments"][0]["target_text"] = "其他窗口修改"
        write_json(path, project)
        with patch.object(QMessageBox, "warning"):
            self.window.table.item(0, 2).setText("当前修改")
            self.assertTrue(self.window.dirty)
            self.assertFalse(self.window.confirm_discard())
        self.assertEqual(read_json(path)["segments"][0]["target_text"], "其他窗口修改")

    def test_close_with_invalid_path_keeps_recovery_and_does_not_block(self):
        from PySide6.QtGui import QCloseEvent
        path = Path(self.directory.name)/'moved'/'synthetic.AVI.voxlate'/'en-combined'/'project.json'
        project = dict(schema_version=1, name='voxlate', input=str(Path(self.directory.name)/'synthetic.AVI'),
            duration=2, segments=[dict(id=1, start=0, end=1, source_text='Hello', target_text='你好')])
        write_json(path, project)
        self.window.project = project
        self.window.project_path = path
        self.window.project_hash = digest(project)
        self.window.table.setRowCount(1)
        from PySide6.QtWidgets import QTableWidgetItem
        self.window.table.blockSignals(True)
        self.window.table.setItem(0, 0, QTableWidgetItem('time'))
        self.window.table.item(0, 0).setData(Qt.ItemDataRole.UserRole, False)
        self.window.table.setItem(0, 2, QTableWidgetItem('未保存的修改'))
        self.window.table.blockSignals(False)
        self.window.dirty = True
        before = path.read_bytes()
        with patch.object(QMessageBox, 'warning') as warning, patch.object(self.window.tts_session, 'close') as close:
            event = QCloseEvent()
            self.window.closeEvent(event)
            self.assertTrue(event.isAccepted())
            warning.assert_not_called()
            close.assert_called_once_with('关闭程序')
        self.assertEqual(path.read_bytes(), before)
        recovery = list((path.parent/'.temp').glob('unsaved-edits-*.json'))
        self.assertEqual(len(recovery), 1)
        segment = read_json(recovery[0])['segments'][0]
        self.assertEqual(segment['target_text'], '未保存的修改')
        self.assertFalse(segment['enabled'])

    def test_install_button_runs_missing_stages_and_rechecks(self):
        missing = [ResourceStatus("asr_turbo_model", "英文识别模型", False, "missing", "重新检查")]
        ready = [ResourceStatus("asr_turbo_model", "英文识别模型", True, "ready", "重新检查")]
        self.window.resources = missing
        with patch("voxlate.gui.check_resources", side_effect=[missing, ready]) as check, patch("voxlate.gui.Installer") as installer, patch('voxlate.gui.ResourceTermsDialog') as terms:
            terms.return_value.exec.return_value = 1
            terms.return_value.selected_stages.return_value = ['asr_turbo_model']
            installer.return_value.install.return_value = self.window.cfg
            self.window.install_all_button.click()
            self.assertTrue(self.window.install_cancel_button.isEnabled())
            deadline = time.monotonic() + 5
            while self.window.task and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertIsNone(self.window.task)
            installer.return_value.install.assert_called_once_with(["asr_turbo_model"], accepted_terms=True)
            self.assertEqual(check.call_count, 2)
            self.assertEqual(check.call_args_list[-1].kwargs["keys"], {"asr_turbo_model"})
            self.assertTrue(self.window.resources[0].ready)
            self.assertFalse(self.window.install_cancel_button.isEnabled())

    def test_declining_download_does_not_start_installer_or_network_check(self):
        self.window.resources = [ResourceStatus('tts_model', 'TTS', False, '', '')]
        with patch('voxlate.gui.ResourceTermsDialog') as terms, patch('voxlate.gui.Installer') as installer, patch('voxlate.gui.check_resources') as check:
            terms.return_value.exec.return_value = 0
            self.window.install_resources()
            installer.assert_not_called()
            check.assert_not_called()
            self.assertIsNone(self.window.task)

    def test_unselected_resources_are_not_downloaded_after_recheck(self):
        missing = [ResourceStatus(key, key, False, '', '') for key in ('hy7_model', 'tts_model')]
        self.window.resources = missing
        with patch('voxlate.gui.ResourceTermsDialog') as terms, patch('voxlate.gui.Installer') as installer, patch('voxlate.gui.check_resources', return_value=missing):
            terms.return_value.exec.return_value = 1
            terms.return_value.selected_stages.return_value = ['hy7_model']
            installer.return_value.install.return_value = self.window.cfg
            self.window.install_resources()
            deadline = time.monotonic() + 5
            while self.window.task and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertIsNone(self.window.task)
            installer.return_value.install.assert_called_once_with(['hy7_model'], accepted_terms=True)

    def test_install_failure_restores_idle_and_keeps_diagnostic(self):
        missing = [ResourceStatus("runtime", "运行环境", False, "missing", "重新检查")]
        self.window.resources = missing
        with patch("voxlate.gui.check_resources", return_value=missing), patch("voxlate.gui.Installer") as installer, patch.object(QMessageBox, "warning"), patch('voxlate.gui.ResourceTermsDialog') as terms:
            terms.return_value.exec.return_value = 1
            terms.return_value.selected_stages.return_value = ['runtime']
            installer.return_value.install.side_effect = VoxlateError("测试下载失败，重试")
            self.window.install_all_button.click()
            deadline = time.monotonic() + 5
            while (self.window.task or self.window.installing or self.window.inspect_after_task) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.app.processEvents()
            while self.window.task and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertIsNone(self.window.task)
            self.assertTrue(self.window.install_all_button.isEnabled())
            self.assertIn("测试下载失败", self.window.install_logs.toPlainText())
            self.assertIn("准备未完成 · 用时", self.window.install_logs.toPlainText())
            self.assertNotIn("100%", self.window.install_logs.toPlainText())
            self.assertFalse(self.window.install_timer.isActive())

    def test_install_progress_counts_time_and_finishes_only_after_result(self):
        release = threading.Event()
        def action(emit, progress):
            progress({"percent": 45, "detail": "下载资源"})
            release.wait(4)
            return "done"
        def complete(result):
            self.window.install_percent = 100
            self.window.install_outcome = "完成"
        self.window.start_task(action, complete, with_progress=True)
        try:
            deadline = time.monotonic() + 2
            while self.window.install_percent < 45 and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.window.install_started = time.monotonic() - 65
            self.window.render_status()
            self.assertIn("约 45% · 已用 01:05", self.window.activity.text())
            self.assertEqual(self.window.progress.maximum(), 100)
            self.assertEqual(self.window.progress.value(), 45)
            self.window.update_install_progress({"percent": 20, "detail": "重试"})
            self.assertEqual(self.window.progress.value(), 45)
        finally:
            release.set()
            deadline = time.monotonic() + 3
            while self.window.task and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertIsNone(self.window.task)
        self.assertFalse(self.window.install_timer.isActive())
        self.assertIn("100% · 完成 · 用时", self.window.install_logs.toPlainText())

    def test_resource_groups_match_configuration_and_hide_ready_programs(self):
        results = [ResourceStatus(k, info.title, True, "ready", "重新检查") for k, info in CATALOG.items()]
        results.append(ResourceStatus("ffprobe", "FFprobe", True, "ready", "重新检查"))
        self.window.resources_checked(results)
        tree = self.window.resource_tree
        self.assertEqual([tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())], ["程序", "环境", "模型"])
        self.assertNotIn("ffmpeg", self.window.resource_nodes)
        asr_parent = self.window.resource_nodes["asr_large_model"].parent()
        translation_parent = self.window.resource_nodes["hy7_model"].parent()
        self.assertEqual(asr_parent.text(0), "识别模型")
        self.assertEqual(translation_parent.text(0), "模型")
        self.assertEqual(self.window.resource_nodes["hy7_model"].text(0), "翻译模型 · Hy-MT2 7B")
        self.assertEqual([asr_parent.child(i).data(0, Qt.ItemDataRole.UserRole) for i in range(asr_parent.childCount()) if asr_parent.child(i).data(0, Qt.ItemDataRole.UserRole)],
                         ["asr_qwen_model", "asr_large_model", "asr_turbo_model"])
        self.assertEqual([translation_parent.child(i).text(0) for i in range(translation_parent.childCount())],
                         [CATALOG['separator_model'].title, '识别模型', '翻译模型 · Hy-MT2 7B', CATALOG['tts_model'].title])
        self.assertEqual([tree.headerItem().text(i) for i in range(6)], ["检查项", "空间", "状态", "", "", ""])
        for key, node in self.window.resource_nodes.items():
            if key in self.window.model_buttons:
                self.assertTrue(self.window.model_buttons[key].text())
            elif key in self.window.asr_model_buttons:
                self.assertTrue(self.window.asr_model_buttons[key].text())
            else:
                self.assertEqual(node.text(0), f"翻译模型 · {CATALOG[key].title}" if key == "hy7_model" else CATALOG[key].title)
            self.assertTrue(node.text(1))
            self.assertNotIn("install", self.window.row_buttons[key])
            self.assertEqual("settings" in self.window.row_buttons[key], key in ("runtime", "qwen", "separator", "tts"))
            self.assertNotIn("delete", self.window.row_buttons[key])
        results[0].ready = False
        self.window.resources_checked(results)
        self.assertEqual(tree.topLevelItem(0).text(0), "程序")
        self.assertEqual(set(self.window.row_buttons["ffmpeg"]), {"install"})

    def test_drop_video_sets_same_folder_output_without_export_input(self):
        video = Path(self.directory.name) / "英文 示例.mp4"
        video.write_bytes(b"sample")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(video))])
        enter = QDragEnterEvent(QPoint(30, 30), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.window.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        event = QDropEvent(QPointF(30, 30), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.window.dropEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(self.window.output_path, video.with_name("英文 示例.en-zh.mp4"))
        self.assertEqual(self.window.video.text(), str(video))
        self.assertFalse(hasattr(self.window, "output"))
        self.assertTrue(self.window.video.isReadOnly())
        mime.setUrls([QUrl.fromLocalFile(str(video)), QUrl.fromLocalFile(str(video))])
        self.assertIsNone(self.window.dropped_video(mime))

    def test_drop_respects_unsaved_edits_and_busy_state(self):
        video = Path(self.directory.name) / "new.mp4"
        video.write_bytes(b"sample")
        self.window.selected_video = "old.mp4"
        self.window.video.setText("old.mp4")
        self.window.dirty = True
        with patch.object(self.window, "save_translations", return_value=False):
            self.assertFalse(self.window.video_selected(video))
        self.assertEqual(self.window.video.text(), "old.mp4")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(video))])
        self.window.task = object()
        self.assertIsNone(self.window.dropped_video(mime))
        self.window.task = None

    def test_status_is_single_bottom_right_label(self):
        self.window.notify("正在准备")
        self.assertEqual(self.window.activity.text(), "正在准备")
        self.assertTrue(self.window.activity.alignment() & Qt.AlignmentFlag.AlignRight)
        self.assertFalse(hasattr(self.window, "notice"))
        self.assertFalse(any(label.objectName() == "brand" for label in self.window.findChildren(QLabel)))

    def test_cleanup_review_cancel_and_confirm(self):
        from voxlate.resources import cleanup_inventory, PURPOSES, CATALOG, EXTRAS
        self.assertEqual(set(PURPOSES), set(CATALOG) | set(EXTRAS))
        model = Path(self.window.cfg["asr"]["model_path"])
        model.mkdir(parents=True)
        (model / "model.bin").write_bytes(b"fake-model")
        entries = cleanup_inventory(self.window.cfg, self.window.config_path)
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
        self.window.show_cleanup_dialog(entries)
        self.assertTrue(model.exists())
        def confirm():
            dialog = QApplication.activeModalWidget()
            self.assertIsNone(dialog.findChild(QCheckBox))
            self.assertIn(str(model), [label.text() for label in dialog.findChildren(QLabel)])
            labels = [label.text() for label in dialog.findChildren(QLabel)]
            self.assertLess(labels.index(PURPOSES[entries[0].key]), labels.index(str(model)))
            self.assertIn("确认删除", [button.text() for button in dialog.findChildren(QPushButton)])
            dialog.accept()
        QTimer.singleShot(0, confirm)
        missing = [ResourceStatus("asr_turbo_model", "英文识别模型", False, "missing", "重新检查")]
        with patch("voxlate.gui.check_resources", return_value=missing):
            self.window.show_cleanup_dialog(entries)
            deadline = time.monotonic() + 5
            while self.window.task and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertIsNone(self.window.task)
        self.assertFalse(model.exists())
        self.assertFalse(self.window.resources[0].ready)

    def test_resource_toolbar_is_one_row_in_requested_order(self):
        row = self.window.resource_toolbar
        self.assertEqual(row.count(), 6)
        self.assertIs(row.itemAt(0).widget(), self.window.storage)
        self.assertEqual([row.itemAt(i).widget().text() for i in range(1, 6)],
                         ["选择", "优先镜像站", "一键准备", "检查", "停止"])
        self.assertIs(row.itemAt(2).widget(), self.window.try_mirrors)
        labels = [b.text() for b in self.window.findChildren(QPushButton)]
        self.assertNotIn("资源清理…", labels)
        self.assertNotIn("打开资源目录", labels)

    def test_mirror_choice_defaults_on_and_persists_without_check(self):
        self.assertTrue(self.window.try_mirrors.isChecked())
        self.window.try_mirrors.click()
        self.assertFalse(load_config(self.window.config_path)["try_mirrors"])
        self.window.close()
        self.window.deleteLater()
        self.window = MainWindow(Path(self.directory.name), auto_check=False)
        self.assertFalse(self.window.try_mirrors.isChecked())
        cfg = load_config(self.window.config_path)
        cfg.pop("try_mirrors")
        write_json(self.window.config_path, cfg)
        self.window.close()
        self.window.deleteLater()
        self.window = MainWindow(Path(self.directory.name), auto_check=False)
        self.assertTrue(self.window.try_mirrors.isChecked())

    def test_auxiliary_resources_are_locatable_and_cleanable_in_the_list(self):
        from voxlate.resources import current_root, remember_root
        current = current_root(self.window.cfg)
        cache = current / "setup-cache"
        cache.mkdir(parents=True)
        old = Path(self.directory.name) / "old-resources"
        (old / "prepare-env").mkdir(parents=True)
        remember_root(self.window.config_path, old)
        missing = [ResourceStatus("asr_turbo_model", "英文识别模型", False, "missing", "重新检查")]
        self.window.resources_checked(missing)
        self.assertIn("download_cache", self.window.resource_nodes)
        self.assertIn("prepare_env", self.window.resource_nodes)
        self.assertEqual(self.window.tabs.tabText(1), "资源配置")
        self.assertEqual(set(self.window.row_buttons["download_cache"]), {"delete"})
        self.assertIn(str(cache), self.window.resource_nodes["download_cache"].toolTip(2))
        with patch.object(self.window, "review_cleanup") as clean:
            self.window.row_buttons["download_cache"]["delete"].click()
            clean.assert_called_once_with("download_cache")
        self.assertEqual(set(self.window.row_buttons["asr_turbo_model"]), {"install"})
        self.assertEqual(self.window.row_buttons["asr_turbo_model"]["install"].text(), "下载")

    def test_default_settings_are_scoped_and_contain_no_paths(self):
        cfg = copy.deepcopy(self.window.cfg)
        cfg["tts"].update(device="cpu", use_bf16=False, emotion_reference=True)
        cfg["separator"]["device"] = "cpu"
        resource = ResourceStatus("tts", "音色克隆环境", False, "test", "help")
        dialog = ResourceSettingsDialog("tts", cfg, self.window.defaults, resource, self.window)
        self.assertEqual(dialog.findChildren(QLineEdit), [])
        self.assertEqual(dialog.help_button.text(), "帮助")
        dialog.default_button.click()
        updated = dialog.settings()
        self.assertEqual(updated["tts"]["device"], self.window.defaults["tts"]["device"])
        self.assertEqual(updated["tts"]["use_bf16"], self.window.defaults["tts"]["use_bf16"])
        self.assertEqual(updated["separator"]["device"], "cpu")
        self.assertEqual(updated["tts"]["model_path"], cfg["tts"]["model_path"])
        self.assertEqual(cfg["tts"]["device"], "cpu")
        dialog.close()

    def test_settings_dialog_cancel_and_save(self):
        result = [ResourceStatus("separator", "人声分离环境", True, "ready", "help")]
        self.window.resources_checked(result)
        original = self.window.cfg["separator"]["device"]
        def edit_and_cancel():
            dialog = QApplication.activeModalWidget()
            dialog.fields["separator.device"].setCurrentText("cpu")
            dialog.reject()
        QTimer.singleShot(0, edit_and_cancel)
        self.window.row_buttons["separator"]["settings"].click()
        self.assertEqual(self.window.cfg["separator"]["device"], original)
        def edit_and_save():
            dialog = QApplication.activeModalWidget()
            dialog.fields["separator.device"].setCurrentText("cpu")
            dialog.accept()
        QTimer.singleShot(0, edit_and_save)
        with patch.object(self.window, "inspect") as inspect:
            self.window.row_buttons["separator"]["settings"].click()
            inspect.assert_not_called()
        self.assertEqual(load_config(self.window.config_path)["separator"]["device"], "cpu")

    def test_unchanged_settings_keep_fixed_per_sentence_emotion(self):
        self.window.resources_checked([ResourceStatus("tts", "tts", True, "ready", "")])
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())
        with patch.object(self.window, "inspect") as inspect:
            self.window.edit_resource_settings("tts")
            self.assertTrue(self.window.cfg['tts']['emotion_reference'])
            self.assertFalse(hasattr(self.window, 'emotion'))
            inspect.assert_not_called()

    def test_dubbing_requires_translation_and_uses_selected_voice_mode(self):
        self.assertFalse(self.window.dub_button.isEnabled())
        with patch.object(QMessageBox, "information") as message, patch.object(self.window, "start_task") as start:
            self.window.start_pipeline("dub")
            message.assert_called_once()
            start.assert_not_called()
        path = Path(self.directory.name) / "project.mp4.voxlate" / "project.json"
        project = dict(schema_version=1, name="voxlate", input=str(Path(self.directory.name) / "project.mp4"),
            duration=5, segments=[dict(id=1, start=0, end=1, source_text="Hello", target_text="你好")],
            translation_config_key=digest(dict(self.window.cfg["translator"], source_lang="en")))
        write_json(path, project)
        self.window.load_project(path)
        self.assertTrue(self.window.dub_button.isEnabled())
        self.assertEqual(self.window.reference_sentence.value(), 0)
        self.window.individual_voice.click()
        self.assertTrue(self.window.dub_button.isEnabled())
        self.assertEqual(read_json(path)['voice_mode'], 'individual')
        self.window.table.item(0, 2).setText("")
        self.assertFalse(self.window.dub_button.isEnabled())
        self.window.table.item(0, 2).setText("您好")
        self.assertTrue(self.window.dub_button.isEnabled())
        self.window.cfg["translator"]["beam_size"] += 1
        self.window.refresh_export_state()
        self.assertFalse(self.window.dub_button.isEnabled())

    def test_main_layout_keeps_only_common_controls(self):
        labels = [w.text() for w in self.window.findChildren(QLabel)]
        buttons = [w.text() for w in self.window.findChildren(QPushButton)]
        self.assertNotIn("源语言", labels)
        for text in ("打开已有项目", "新建项目", "查看项目日志", "播放 / 选音色", "自动选最长句"):
            self.assertNotIn(text, buttons)
        self.assertNotIn('选音色', buttons)
        self.assertFalse(hasattr(self.window, 'reference'))
        self.assertEqual(self.window.uniform_voice.text(), '统一音色')
        self.assertEqual(self.window.individual_voice.text(), '逐句音色')
        for text in ('① 分离', '② 识别', '③ 翻译', '④ 生成配音', '⑤ 导出视频', '一键导出'):
            self.assertIn(text, buttons)
        self.assertIn('清空项目', buttons)

    def test_separate_steps_check_only_their_own_resources(self):
        video = Path(self.directory.name)/'source.mp4'
        video.write_bytes(b'fixture')
        path = self.window.default_project_path(video)
        write_json(path, dict(schema_version=1, name='voxlate', input=str(video), duration=2,
            segments=[dict(id=1, start=0, end=1, source_text='Hello', target_text='')]))
        self.window.load_project(path)
        for stage, missing_keys in [('separate', ['asr_large_model', 'qwen', 'runtime', 'hy7_model', 'tts']),
                                    ('translate', ['asr_large_model', 'separator', 'tts']),
                                    ('recognize', ['hy7_model', 'llm_engine', 'tts', 'separator', 'separator_model']),
                                    ('auto', ['asr_large_model', 'separator'])]:
            with self.subTest(stage=stage), patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), patch.object(self.window, 'start_task') as start:
                self.window.start_pipeline(stage)
                results = [ResourceStatus(k, k, False, '', '', required=True) for k in missing_keys]
                with patch('voxlate.gui.check_resources', return_value=results), patch('voxlate.gui.VideoDubPipeline') as pipeline:
                    start.call_args.args[0](lambda text: None)
                    pipeline.return_value.process.assert_called_once()

    def test_overwrite_confirmation_cancels_or_forces_current_step(self):
        from test_pipeline import tone
        from voxlate.dubbing_state import voice_key
        video = Path(self.directory.name)/'source.mp4'
        video.write_bytes(b'fixture')
        path = self.window.default_project_path(video)
        audio = path.parent/'tts.wav'
        tone(audio, .2)
        project = dict(schema_version=1, name='voxlate', input=str(video), duration=2,
            voice_mode='uniform', reference_sentence_id=1,
            segments=[dict(id=1, start=0, end=1, source_text='Hello', target_text='你好',
                           tts_text='你好', tts_audio=str(audio))])
        project['segments'][0]['voice_key'] = voice_key(project, self.window.cfg)
        write_json(path, project)
        self.window.load_project(path)
        before = path.read_bytes()
        for stage, label in [('recognize', '重新识别'), ('translate', '已有译文'), ('dub', '已有统一音色配音')]:
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No) as question, patch.object(self.window, 'start_task') as start:
                self.window.start_pipeline(stage)
                self.assertIn(label, question.call_args.args[2])
                start.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
        for stage in ('recognize', 'translate', 'dub'):
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), patch.object(self.window, 'start_task') as start:
                self.window.start_pipeline(stage)
            with patch('voxlate.gui.check_resources', return_value=[]), patch('voxlate.gui.VideoDubPipeline') as pipeline:
                start.call_args.args[0](lambda text: None)
                kwargs = pipeline.return_value.process.call_args.kwargs
                self.assertEqual(kwargs['force_translation'], stage == 'translate')
                self.assertEqual(kwargs['force_recognition'], stage == 'recognize')
                self.assertEqual(kwargs['recognition_only'], stage == 'recognize')
                self.assertEqual(kwargs['translate_only'], stage == 'translate')
                self.assertEqual(kwargs['force_tts'], stage == 'dub')
        with patch.object(QMessageBox, 'question') as question, patch.object(self.window, 'start_task') as start:
            self.window.start_pipeline('auto')
            question.assert_not_called()
        with patch('voxlate.gui.check_resources', return_value=[]), patch('voxlate.gui.VideoDubPipeline') as pipeline:
            start.call_args.args[0](lambda text: None)
            kwargs = pipeline.return_value.process.call_args.kwargs
            self.assertTrue(kwargs['auto_export'])
            self.assertFalse(kwargs['force_recognition'])
            self.assertFalse(kwargs['force_translation'])
            self.assertFalse(kwargs['force_tts'])
        with patch.object(QMessageBox, 'question') as question, patch.object(self.window, 'start_task') as start:
            self.window.table.cellWidget(0, 3).dub.click()
            question.assert_not_called()
            start.assert_called_once()
        with patch('voxlate.gui.check_resources', return_value=[]), patch('voxlate.gui.VideoDubPipeline') as pipeline:
            start.call_args.args[0](lambda text: None)
            kwargs = pipeline.return_value.process.call_args.kwargs
            self.assertEqual(kwargs['sentence_ids'], [1])
            self.assertTrue(kwargs['force_tts'])
        self.window.individual_voice.click()
        with patch.object(QMessageBox, 'question') as question, patch.object(self.window, 'start_task') as start:
            self.window.start_pipeline('dub')
            question.assert_not_called()
            start.assert_called_once()

    def test_retranslation_confirms_overwriting_discarded_text(self):
        video = Path(self.directory.name)/'source.mp4'
        video.write_bytes(b'video')
        path = self.window.default_project_path(video)
        write_json(path, dict(schema_version=1, name='voxlate', input=str(video), duration=2,
            segments=[dict(id=1, start=0, end=1, source_text='Hello', target_text='你好', enabled=False)]))
        self.window.load_project(path)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No) as question, patch.object(self.window, 'start_task') as start:
            self.window.start_pipeline('translate')
            self.assertIn('包括已舍弃句子', question.call_args.args[2])
            start.assert_not_called()

    def test_clear_project_confirmation_and_reset_preserve_video_and_export(self):
        from voxlate.project_storage import project_root
        video = Path(self.directory.name)/'source.mp4'
        video.write_bytes(b'video')
        output = video.with_name('source.zh.mp4')
        output.write_bytes(b'export')
        path = self.window.default_project_path(video)
        write_json(path, dict(schema_version=1, name='voxlate', input=str(video), duration=2,
            segments=[dict(id=1, start=0, end=1, source_text='Hello', target_text='你好')]))
        write_json(project_root(video)/'other-profile'/'project.json', {})
        self.window.load_project(path)
        self.assertTrue(self.window.clear_project_button.isEnabled())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No), patch.object(self.window, 'start_task') as start:
            self.window.clear_project()
            start.assert_not_called()
            self.assertTrue(path.exists())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), patch.object(self.window, 'start_task') as start:
            self.window.clear_project()
        action, complete = start.call_args.args
        complete(action(lambda text: None))
        self.window.refresh_export_state()
        self.assertFalse(project_root(video).exists())
        self.assertEqual(video.read_bytes(), b'video')
        self.assertEqual(output.read_bytes(), b'export')
        self.assertIsNone(self.window.project)
        self.assertEqual(self.window.table.rowCount(), 0)
        self.assertFalse(self.window.clear_project_button.isEnabled())
        self.assertFalse(self.window.dub_button.isEnabled())

    def test_sentence_actions_red_record_before_play_and_stale_audio_blocks_export(self):
        from test_pipeline import tone
        from voxlate.dubbing_state import voice_key
        from PySide6.QtMultimedia import QMediaPlayer
        video = Path(self.directory.name)/'source.mp4'
        path = self.window.default_project_path(video)
        audio = path.parent/'aligned.wav'
        tone(audio, .2)
        project = dict(schema_version=1, name='voxlate', input=str(video), duration=2,
            segments=[dict(id=4, start=0, end=1, source_text='Hello', target_text='你好',
                tts_text='你好', timing_text='你好', tts_audio=str(audio), aligned_audio=str(audio))])
        project.update(voice_mode='uniform', reference_sentence_id=4)
        project['segments'][0]['voice_key'] = voice_key(project, self.window.cfg)
        write_json(path, project)
        self.window.load_project(path)
        widget = self.window.table.cellWidget(0, 3)
        self.assertEqual(widget.layout().itemAt(0).widget(), widget.dub)
        self.assertEqual(widget.dub.text(), '●')
        self.assertFalse(widget.play.icon().isNull())
        self.assertEqual(widget.play.accessibleName(), '试听本句配音')
        self.assertTrue(self.window.export_button.isEnabled())
        with patch.object(self.window, 'start_pipeline') as start:
            widget.dub.click()
            start.assert_called_once_with('dub', sentence_ids=[4], force_tts=True)
        with patch.object(QMediaPlayer, 'play'):
            widget.play.click()
            self.assertEqual(Path(self.window.sentence_player.source().toLocalFile()), audio.resolve())
        self.window.table.item(0, 2).setText('您好')
        self.assertFalse(self.window.export_button.isEnabled())
        self.assertTrue(self.window.dub_button.isEnabled())
        self.assertTrue(widget.play.isEnabled(), 'The last audio can still be previewed')
        self.assertIn('上次配音', widget.play.toolTip())
        self.window.project['segments'].append(dict(id=5, start=1, end=2, source_text='Next', target_text=''))
        self.assertFalse(self.window.translation_ready())
        self.assertTrue(self.window.translation_ready([4]), 'Other empty sentences must not block this sentence')

    def test_original_voice_preview_before_dubbing_and_switching_to_chinese(self):
        from test_pipeline import tone
        from PySide6.QtMultimedia import QMediaPlayer
        video = Path(self.directory.name)/'source.mp4'
        path = self.window.default_project_path(video)
        vocals = path.parent/'separated'/'htdemucs'/'original'/'vocals.wav'
        tone(vocals, 3)
        project = dict(schema_version=1, name='voxlate', input=str(video), duration=3,
            separator_model='htdemucs', segments=[dict(id=1, start=1, end=2,
                source_text='Hello', target_text='你好', enabled=False)])
        write_json(path, project)
        self.window.load_project(path)
        original = self.window.table.cellWidget(0, 4).play
        translated = self.window.table.cellWidget(0, 3).play
        self.assertEqual(self.window.table.horizontalHeader().visualIndex(4), 3)
        self.assertTrue(original.isEnabled(), 'Original voice is available even without any TTS')
        self.assertFalse(translated.isEnabled())
        with patch.object(QMediaPlayer, 'play'), patch.object(QMediaPlayer, 'setPosition') as seek:
            original.click()
            self.window.begin_sentence_audio(QMediaPlayer.MediaStatus.LoadedMedia)
            seek.assert_called_with(1000)
            self.assertEqual(self.window.sentence_end, 2000)
            self.assertEqual(Path(self.window.sentence_player.source().toLocalFile()), vocals.resolve())
            with patch.object(QMediaPlayer, 'position', return_value=2000):
                self.window.check_sentence_end()
            self.assertIsNone(self.window.playing_sentence)
            self.assertEqual(Path(self.window.sentence_player.source().toLocalFile()), vocals.resolve())
            self.assertIsNone(self.window.table.playback_row)
            clip = path.parent/'source_clip.wav'
            dub = path.parent/'dub.wav'
            tone(clip, 1)
            tone(dub, 1)
            self.window.project['segments'][0].update(source_audio=str(clip), tts_audio=str(dub))
            self.window.refresh_sentence_buttons()
            original.click()
            self.assertEqual(Path(self.window.sentence_player.source().toLocalFile()), clip.resolve())
            translated.click()
            self.assertEqual(Path(self.window.sentence_player.source().toLocalFile()), dub.resolve())
            self.assertEqual(self.window.playing_sentence, (0, False))
            self.assertIsNone(self.window.sentence_end)

    def test_sentence_playback_reuses_source_and_updates_only_active_rows(self):
        from test_pipeline import tone
        from PySide6.QtMultimedia import QMediaPlayer
        video = Path(self.directory.name)/'source.mp4'
        path = self.window.default_project_path(video)
        vocals = path.parent/'separated'/'htdemucs'/'original'/'vocals.wav'
        tone(vocals, 3)
        write_json(path, dict(schema_version=1, name='voxlate', input=str(video), duration=3,
            separator_model='htdemucs', segments=[dict(id=i+1, start=i, end=i+1,
                source_text='Hello', target_text='你好') for i in range(2)]))
        self.window.load_project(path)
        with patch.object(QMediaPlayer, 'play'), patch.object(QMediaPlayer, 'mediaStatus', return_value=QMediaPlayer.MediaStatus.LoadedMedia), \
                patch.object(self.window, 'refresh_sentence_buttons', side_effect=AssertionError('Full table scan during playback')):
            self.window.play_sentence(0, original=True)
            player = self.window.sentence_player
            with patch.object(player, 'setSource', wraps=player.setSource) as load:
                self.window.play_sentence(1, original=True)
                load.assert_not_called()
                self.assertEqual(self.window.playing_sentence, (1, True))
                with patch.object(player, 'position', return_value=1500):
                    self.window.check_sentence_end()
                self.assertEqual(self.window.table.playback_row, 1)
                self.assertAlmostEqual(self.window.table.playback_progress, .5)
                with patch.object(player, 'position', return_value=2000):
                    self.window.check_sentence_end()
                self.assertIsNone(self.window.table.playback_row)
                self.window.play_sentence(0, original=True)
                load.assert_not_called()
                self.window.stop_sentence_audio(release=False)
                tone(vocals, 4)
                self.window.play_sentence(0, original=True)
                self.assertEqual(load.call_count, 2, 'Overwritten audio must unload and reopen')
            self.window.stop_sentence_audio()
            self.assertTrue(player.source().isEmpty(), 'Project changes release file handles')

    def test_dubbing_progress_uses_actual_audio_duration_and_clears_on_end_or_error(self):
        from test_pipeline import tone
        from PySide6.QtMultimedia import QMediaPlayer
        video = Path(self.directory.name)/'source.mp4'
        path = self.window.default_project_path(video)
        audio = path.parent/'dub.wav'
        tone(audio, 2)
        write_json(path, dict(schema_version=1, name='voxlate', input=str(video), duration=4,
            segments=[dict(id=1, start=0, end=4, source_text='Hello', target_text='你好', tts_audio=str(audio))]))
        self.window.load_project(path)
        with patch.object(QMediaPlayer, 'play'), patch.object(QMediaPlayer, 'mediaStatus', return_value=QMediaPlayer.MediaStatus.LoadedMedia):
            self.window.play_sentence(0)
            player = self.window.sentence_player
            with patch.object(player, 'position', return_value=500), patch.object(player, 'duration', return_value=2000):
                self.window.check_sentence_end()
            self.assertAlmostEqual(self.window.table.playback_progress, .25)
            self.window.sentence_playback_state_changed(QMediaPlayer.PlaybackState.StoppedState)
            self.assertIsNone(self.window.table.playback_row)
            self.window.play_sentence(0)
            with patch.object(self.window, 'notify') as notify:
                self.window.sentence_playback_error(None, 'test error')
                notify.assert_called_once()
            self.assertIsNone(self.window.table.playback_row)
            self.assertIsNone(self.window.playing_sentence)

    def test_statistics_emit_completion_log_before_review(self):
        model = Path(self.window.cfg["asr"]["model_path"])
        model.mkdir(parents=True)
        (model / "model.bin").write_bytes(b"data")
        captured = []
        with patch.object(self.window, "show_cleanup_dialog", side_effect=captured.append):
            self.window.review_cleanup("asr_turbo_model")
            deadline = time.monotonic() + 5
            while (self.window.task or not captured) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertTrue(captured)
        log = self.window.install_logs.toPlainText()
        self.assertIn("正在统计", log)
        self.assertIn("统计完成：", log)
        self.assertIn("1 个目录", log)

    def test_install_row_targets_its_resource_and_blocks_while_busy(self):
        self.window.resources_checked([ResourceStatus("asr_turbo_model", "英文识别模型", False, "missing", "help")])
        button = self.window.row_buttons["asr_turbo_model"]["install"]
        with patch.object(self.window, "install_resources") as install:
            button.click()
            install.assert_called_once_with("asr_turbo_model")
        self.window.resource_tree.setEnabled(False)
        self.assertFalse(button.isEnabled())
        self.window.resource_tree.setEnabled(True)

    def test_processing_keeps_lists_scrollable_but_locks_changes(self):
        results = [ResourceStatus(k, info.title, False, "missing", "help") for k, info in CATALOG.items()]
        self.window.resources_checked(results)
        self.window.table.setRowCount(100)
        self.window.resource_tree.setMaximumHeight(200)
        self.window.table.setMaximumHeight(200)
        self.window.show()
        self.app.processEvents()
        release = threading.Event()
        self.window.start_task(lambda emit: release.wait(5), lambda result: None)
        try:
            self.assertTrue(self.window.resource_tree.isEnabled())
            self.assertTrue(self.window.table.isEnabled())
            self.assertEqual(self.window.table.editTriggers(), self.window.table.EditTrigger.NoEditTriggers)
            self.assertEqual(self.window.model_buttons,{})
            self.assertFalse(self.window.row_buttons["hy7_model"]["install"].isEnabled())
            for tab, widget in ((1, self.window.resource_tree), (0, self.window.table)):
                self.window.tabs.setCurrentIndex(tab)
                self.app.processEvents()
                bar = widget.verticalScrollBar()
                self.assertGreater(bar.maximum(), 0)
                bar.setValue(bar.maximum())
                self.assertGreater(bar.value(), 0)
            self.window.resources_checked(results)
            self.assertFalse(self.window.row_buttons["hy7_model"]["install"].isEnabled())
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while self.window.task and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
        self.assertTrue(self.window.row_buttons["hy7_model"]["install"].isEnabled())
        self.assertEqual(self.window.table.editTriggers(), self.window.table_edit_triggers)

    def test_new_log_lines_do_not_interrupt_reading_earlier_progress(self):
        self.window.show()
        for index, widget in ((0, self.window.logs), (1, self.window.install_logs)):
            self.window.tabs.setCurrentIndex(index)
            widget.setPlainText("\n".join(str(i) for i in range(100)))
            self.app.processEvents()
            bar = widget.verticalScrollBar()
            bar.setValue(5)
            widget.appendPlainText("new progress")
            self.assertEqual(bar.value(), 5)
            bar.setValue(bar.maximum())
            widget.appendPlainText("follow latest")
            self.assertEqual(bar.value(), bar.maximum())


if __name__ == "__main__":
    unittest.main()
