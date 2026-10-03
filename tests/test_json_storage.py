import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from test_pipeline import TestDirectory
from voxlate.common import read_json, write_json


class JsonStorageTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_windows_reader_lock_can_clear_during_retry(self):
        import ctypes
        from ctypes import wintypes
        from concurrent.futures import ThreadPoolExecutor
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with TestDirectory() as directory:
            path = Path(directory)/'state.json'
            write_json(path, {'old': True})
            handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
            self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
            first_retry = threading.Event()
            import time
            sleep = time.sleep
            def wait(seconds):
                first_retry.set()
                sleep(seconds)
            with ThreadPoolExecutor(max_workers=1) as executor:
                try:
                    with patch('voxlate.common.time.sleep', side_effect=wait):
                        job = executor.submit(write_json, path, {'new': True})
                        self.assertTrue(first_retry.wait(timeout=5))
                        kernel.CloseHandle(handle)
                        handle = None
                        job.result(timeout=5)
                finally:
                    if handle is not None:
                        kernel.CloseHandle(handle)
            self.assertEqual(read_json(path), {'new': True})

    def test_temporary_lock_retries_without_truncating_previous_json(self):
        with TestDirectory() as directory:
            path = Path(directory)/'state.json'
            write_json(path, {'old': True})
            replace = os.replace
            attempts = []
            def locked(source, target):
                self.assertEqual(read_json(path), {'old': True})
                self.assertEqual(source.parent, path.parent)
                attempts.append(source)
                if len(attempts) < 3:
                    raise PermissionError('sharing violation')
                replace(source, target)
            with patch('voxlate.common.os.replace', side_effect=locked), patch('voxlate.common.time.sleep') as sleep:
                write_json(path, {'new': '中文'})
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(read_json(path), {'new': '中文'})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_persistent_save_failure_remains_visible_and_keeps_old_file(self):
        with TestDirectory() as directory:
            path = Path(directory)/'project.json'
            write_json(path, {'old': True})
            for error, attempts in ((PermissionError('locked'), 6), (OSError('disk full'), 1)):
                with patch('voxlate.common.os.replace', side_effect=error) as replace, patch('voxlate.common.time.sleep'):
                    with self.assertRaises(type(error)):
                        write_json(path, {'new': True})
                self.assertEqual(replace.call_count, attempts)
                self.assertEqual(read_json(path), {'old': True})
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_concurrent_writers_use_separate_temporary_files(self):
        with TestDirectory() as directory:
            path = Path(directory)/'state.json'
            barrier = threading.Barrier(2)
            replace = os.replace
            temporary, errors = [], []
            def publish(source, target):
                if source not in temporary:
                    temporary.append(source)
                    barrier.wait(timeout=5)
                replace(source, target)
            def write(value):
                try:
                    write_json(path, {'value': value})
                except Exception as exc:
                    errors.append(exc)
            with patch('voxlate.common.os.replace', side_effect=publish):
                threads = [threading.Thread(target=write, args=(value,)) for value in (1, 2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(len(set(temporary)), 2)
            self.assertIn(read_json(path), ({'value': 1}, {'value': 2}))
            self.assertEqual(list(path.parent.iterdir()), [path])
