import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from test_pipeline import TestDirectory
from voxlate.app_settings import prepare_settings
from voxlate.common import load_config
from voxlate.diagnostics import check_resources
from voxlate.installer import Installer, planned_stages, relocate, resource_root
from voxlate.recognition_models import MODELS, selected_key, select_model, missing_files, download_model


class RecognitionModelTests(unittest.TestCase):
    def test_switch_and_relocation_preserve_each_models_settings(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            original = copy.deepcopy(cfg['asr'])
            root = resource_root(cfg)
            select_model(cfg, 'asr_large_model', root)
            cfg['asr'].update(device='cuda', compute_type='float16', beam_size=3)
            cfg = relocate(cfg, root / 'moved')
            self.assertEqual(selected_key(cfg), 'asr_large_model')
            self.assertEqual(cfg['asr']['beam_size'], 3)
            select_model(cfg, 'asr_turbo_model', root)
            self.assertEqual(cfg['asr'], original)
            select_model(cfg, 'asr_large_model', root)
            self.assertEqual(cfg['asr']['device'], 'cuda')
            self.assertEqual(cfg['asr']['beam_size'], 3)

    def test_only_selected_asr_is_required_and_prepared(self):
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            cfg['asr']['combined'] = False
            select_model(cfg, 'asr_large_model', resource_root(cfg))
            results = check_resources(cfg, config, quick=True)
            self.assertEqual([r.key for r in results if r.key in MODELS and r.required], ['asr_large_model'])
            stages = planned_stages(results)
            self.assertIn('asr_large_model', stages)
            self.assertNotIn('asr_model', stages)
            self.assertNotIn('asr_turbo_model', stages)
            installer = Installer(cfg, config, lambda _: None)
            with patch('voxlate.installer.download_asr') as download, patch.object(installer, 'venv', side_effect=AssertionError('no extra environment')):
                installer.recognition_model('asr_turbo_model')
            self.assertEqual(download.call_args.args[0], 'asr_turbo_model')
            self.assertEqual(selected_key(installer.cfg), 'asr_large_model')

    def test_retired_base_settings_migrate_without_returning_to_catalog(self):
        from voxlate.resources import CATALOG
        with TestDirectory() as folder:
            config, _ = prepare_settings(folder)
            cfg = load_config(config)
            root = resource_root(cfg)
            cfg['asr'].update(model_path=str(root / 'models/faster-whisper-base'), beam_size=1)
            cfg['asr_profiles'] = {'asr_model': {'device': 'cpu'}}
            changed = relocate(cfg, root)
            self.assertEqual(selected_key(changed), 'asr_turbo_model')
            self.assertTrue(changed['asr']['model_path'].endswith('faster-whisper-large-v3-turbo'))
            self.assertEqual(changed['asr']['beam_size'], 5)
            self.assertNotIn('asr_model', changed['asr_profiles'])
            self.assertNotIn('asr_model', MODELS)
            self.assertNotIn('asr_model', CATALOG)

    def test_direct_download_falls_back_and_rejects_truncated_weights(self):
        with TestDirectory() as folder:
            item = dict(repo='test/model', revision='fixed-revision', files={'model.bin': (4, 'checksum'), 'config.json': (2, None)})
            calls = []
            def download(url, path, emit, checksum):
                calls.append((url, checksum))
                if 'hf-mirror' in url:
                    raise OSError('mirror unavailable')
                path.write_bytes(b'data' if path.name == 'model.bin' else b'{}')
            with patch.dict(MODELS, asr_turbo_model=item), patch('voxlate.installer.download', side_effect=download):
                download_model('asr_turbo_model', folder, lambda _: None)
                self.assertFalse(missing_files('asr_turbo_model', folder))
                (Path(folder) / 'model.bin').write_bytes(b'x')
                self.assertEqual(missing_files('asr_turbo_model', folder), ['model.bin'])
            self.assertEqual(len(calls), 4)
            self.assertIn('/resolve/fixed-revision/', calls[1][0])
            self.assertEqual(calls[1][1], 'checksum')
