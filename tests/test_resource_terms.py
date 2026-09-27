import argparse
import io
import json
from pathlib import Path
import unittest
import zipfile
from unittest.mock import patch

from test_pipeline import TestDirectory
from voxlate.common import VoxlateError, load_config
from voxlate.app_settings import prepare_settings
from voxlate.installer import Installer
from voxlate.resource_terms import preserve_model_terms, require_cli_consent
from voxlate.export_notices import write_export_notices


class ResourceTermsTests(unittest.TestCase):
    def test_installer_requires_explicit_consent_before_any_stage(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            installer = Installer(load_config(path), path, lambda _: None)
            with patch.object(installer, 'ffmpeg') as download:
                with self.assertRaisesRegex(VoxlateError, '条款'):
                    installer.install(['ffmpeg'])
                download.assert_not_called()

    def test_cli_without_flag_exits_before_download(self):
        with patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                require_cli_consent(argparse.ArgumentParser(), False, ['tts_model'])
        self.assertEqual(error.exception.code, 2)

    def test_notices_are_cached_only_when_origin_and_hash_match(self):
        with TestDirectory() as folder:
            responses = lambda *_args, **_kwargs: io.BytesIO(b'Official license and attribution')
            with patch('urllib.request.urlopen', side_effect=responses) as get:
                preserve_model_terms('hy7_model', folder, lambda _: None)
                self.assertEqual(get.call_count, 2)
            target = Path(folder)/'licenses/hy7_model'
            info = json.loads((target/'SOURCES.json').read_text())
            self.assertIn('ab8472660ac61fac25f1af43fac2599d52a8a775', info['LICENSE.txt']['url'])
            with patch('urllib.request.urlopen', side_effect=AssertionError('must reuse')):
                preserve_model_terms('hy7_model', folder, lambda _: None)
            (target/'LICENSE.txt').write_bytes(b'corrupt')
            with patch('urllib.request.urlopen', side_effect=responses) as get:
                preserve_model_terms('hy7_model', folder, lambda _: None)
                self.assertEqual(get.call_count, 1)

    def test_invalid_license_stops_before_large_weight_download(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            installer = Installer(load_config(path), path, lambda _: None)
            with patch('urllib.request.urlopen', return_value=io.BytesIO(b'<html>Error</html>')), patch('voxlate.installer.download') as weights:
                with self.assertRaisesRegex(VoxlateError, '许可'):
                    installer.native_translation('hy7_model')
                weights.assert_not_called()

    def test_ffmpeg_preserves_archive_notices_beside_tools(self):
        with TestDirectory() as folder:
            path, _ = prepare_settings(folder)
            installer = Installer(load_config(path), path, lambda _: None)
            archive = Path(folder)/'upstream.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                for name, data in {'bin/ffmpeg.exe': b'fake', 'bin/ffprobe.exe': b'fake',
                                   'LICENSE': b'upstream license', 'README.txt': b'build details',
                                   'doc/source.html': b'source instructions'}.items():
                    z.writestr('release/'+name, data)
            with patch('urllib.request.urlopen', return_value=io.BytesIO(b'0'*64)), patch('voxlate.installer.download', return_value=archive):
                installer.ffmpeg()
            target = installer.root/'tools/ffmpeg/upstream'
            self.assertEqual((target/'LICENSE').read_bytes(), b'upstream license')
            self.assertTrue((target/'doc/source.html').is_file())
            self.assertTrue((target/'DOWNLOAD.json').is_file())

    def test_export_notices_are_offline_and_inside_project(self):
        with TestDirectory() as folder:
            project, model = Path(folder)/'movie.mp4.voxlate/en-combined', Path(folder)/'model'
            model.mkdir()
            (model/'LICENSE').write_text('License of a user-supplied model', encoding='utf-8')
            with patch('urllib.request.urlopen', side_effect=AssertionError('offline')):
                target = write_export_notices(project, model)
            self.assertEqual(target, project/'export-notices')
            self.assertIn('bilibili Model Use License Agreement', (target/'IndexTTS-2.5-LICENSE.txt').read_text(encoding='utf-8'))
            self.assertEqual((target/'Installed-model-LICENSE.txt').read_text(), 'License of a user-supplied model')
            self.assertNotIn(str(Path(folder)), (target/'分享前请读.txt').read_text(encoding='utf-8-sig'))
            (model/'LICENSE').unlink()
            write_export_notices(project, model)
            self.assertFalse((target/'Installed-model-LICENSE.txt').exists())

    def test_public_package_rejects_models_and_private_path_without_echoing_it(self):
        from scripts.check_public_package import audit_release_tree
        with TestDirectory() as folder:
            root = Path(folder)
            (root/'model.gguf').write_bytes(b'synthetic weight')
            with self.assertRaisesRegex(ValueError, 'must not ship'):
                audit_release_tree(root, private_roots=['C:/Users/synthetic-owner'])
            (root/'model.gguf').unlink()
            (root/'README.txt').write_text('Build path C:/Users/synthetic-owner/build', encoding='utf-8')
            with self.assertRaises(ValueError) as error:
                audit_release_tree(root, private_roots=['C:/Users/synthetic-owner'])
            self.assertNotIn('synthetic-owner', str(error.exception))
            (root/'README.txt').write_text('A public example', encoding='utf-8')
            audit_release_tree(root, private_roots=['C:/Users/synthetic-owner'])


try:
    from PySide6.QtWidgets import QApplication
    from voxlate.resource_terms_dialog import ResourceTermsDialog
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, 'Qt required')
class DownloadChoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_terms_start_unaccepted_and_empty_selection_cannot_download(self):
        dialog = ResourceTermsDialog(['tts_model', 'hy7_model'])
        try:
            self.assertFalse(dialog.accept_terms.isChecked())
            self.assertFalse(dialog.download.isEnabled())
            dialog.accept_terms.setChecked(True)
            self.assertTrue(dialog.download.isEnabled())
            dialog.choices['tts_model'].setChecked(False)
            self.assertEqual(dialog.selected_stages(), ['hy7_model'])
            dialog.choices['hy7_model'].setChecked(False)
            self.assertFalse(dialog.download.isEnabled())
        finally:
            dialog.close()
            dialog.deleteLater()
