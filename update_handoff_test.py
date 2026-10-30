"""Check the real packaged updater and close-to-tray guard without installing anything."""
import base64
import marshal
from pathlib import Path
import tempfile
import threading
import types
import unittest

ROOT = Path(__file__).parent


def find_code(module, name):
    if module.co_name == name:
        return module
    for constant in module.co_consts:
        if isinstance(constant, types.CodeType):
            result = find_code(constant, name)
            if result is not None:
                return result


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class Window:
    def __init__(self):
        self.events = types.SimpleNamespace(closing=Event())
        self.destroyed = False

    def destroy(self):
        if not any(handler() is False for handler in self.events.closing.handlers):
            self.destroyed = True


class UpdateHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.download_error = self.launch_error = None
        self.launched = []
        original = marshal.loads((ROOT / '_extract/s_app').read_bytes())
        source = (ROOT / 'app_wrapper.py').read_text(encoding='utf-8')
        toy = marshal.dumps(compile('class API: pass', 'app.py', 'exec'))
        self.ns = {'__name__': 'update_handoff_test_wrapper'}
        exec(compile(source.replace('@@ORIG_B64@@', base64.b64encode(toy).decode(), 1),
                     'app.py', 'exec'), self.ns)

        def download(*args, **kwargs):
            if self.download_error:
                raise self.download_error

        def launch(*args, **kwargs):
            if self.launch_error:
                raise self.launch_error
            self.launched.append(args)
            return object()

        globals_ = {'DATA_DIR': Path(self.temp.name),
                    'github_updater': types.SimpleNamespace(download_verified_asset=download, UpdateError=RuntimeError),
                    'subprocess': types.SimpleNamespace(Popen=launch)}
        api_class = type('API', (), {name: types.FunctionType(find_code(original, name), globals_)
                                   for name in ('_download_and_install_update', '_quit_from_tray')})
        self.ns['_API_PATCHED'][0] = False
        self.ns['_BootstrapNS']()['API'] = api_class
        self.api = api_class()
        self.api._update_lock = threading.Lock()
        self.api._update_state = {'state': 'downloading'}
        self.api.minimized = 0
        self.api.disposed = 0
        self.api.minimize_to_tray = self.minimize
        self.api._dispose_tray = self.dispose
        self.window = Window()
        webview = types.SimpleNamespace(create_window=lambda **kw: self.window, start=lambda: None)
        self.ns['_instrument_webview'](webview)
        self.api._window = webview.create_window(js_api=self.api)

    def minimize(self):
        self.api.minimized += 1
        return {'ok': True}

    def dispose(self):
        self.api.disposed += 1

    def run_update(self):
        self.api._download_and_install_update({'version': '9.0.0', 'name': 'test-installer.exe'})

    def test_successful_installer_handoff_exits_instead_of_hiding_to_tray(self):
        self.run_update()
        self.assertEqual(len(self.launched), 1)
        self.assertTrue(self.window.destroyed, 'running app must release its files to the verified installer')
        self.assertEqual(self.api.minimized, 0)
        self.assertEqual(self.api.disposed, 1)

    def test_download_failure_keeps_application_open(self):
        self.download_error = RuntimeError('checksum failure')
        self.run_update()
        self.assertEqual(self.api._update_state['state'], 'failed')
        self.assertFalse(self.window.destroyed)
        self.assertEqual(self.launched, [])

    def test_installer_launch_failure_keeps_application_open(self):
        self.launch_error = OSError('launch failed')
        self.run_update()
        self.assertEqual(self.api._update_state['state'], 'failed')
        self.assertFalse(self.window.destroyed)
        self.assertEqual(self.api.disposed, 0)

    def test_normal_close_still_minimizes_to_tray(self):
        self.window.destroy()
        self.assertFalse(self.window.destroyed)
        self.assertEqual(self.api.minimized, 1)

    def test_exit_failure_reports_error_and_does_not_disable_tray_guard(self):
        def fail():
            raise RuntimeError('window exit failed')
        self.api._quit_from_tray = fail
        self.run_update()
        self.assertFalse(self.window.destroyed)
        self.assertEqual(self.api._update_state['state'], 'failed')
        self.assertFalse(getattr(self.api, '_lxultra_exit_requested', False))
        self.assertEqual(len(self.launched), 1)


if __name__ == '__main__':
    unittest.main()
