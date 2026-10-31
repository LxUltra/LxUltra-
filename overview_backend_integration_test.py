"""Checks that the packaged wrapper exposes the new APIs to the app."""
import base64
import builtins
import marshal
from pathlib import Path
import unittest

HERE = Path(__file__).parent


class OverviewBackendIntegrationTest(unittest.TestCase):
    def test_injected_api_reports_actual_frequency_and_driver_methods(self):
        toy = '''class API:
    def metrics(self):
        return {"cpu":{"frequency":5578,"peakFrequency":5578,"eCoreFrequency":4383},
                "temperatures":{"cpu":{"pCoreVoltage":1.348,"eCoreVoltage":1.349},
                "sensors":getattr(self,"sensor_rows",[{"key":"cpu","type":"Clock","name":"P-Core #1"},
                           {"key":"cpu","type":"E-Core","name":"E-Core #1"},
                           {"key":"cpu","type":"Clock","name":"E-Core #1"}])}}
import builtins
builtins.LX_TEST_API = API
'''
        source = '\n'.join((HERE / name).read_text(encoding='utf-8') for name in
                           ('migration_engine.py', 'driver_updates.py', 'cpu_telemetry.py', 'app_wrapper.py'))
        source = source.replace('@@ORIG_B64@@', base64.b64encode(marshal.dumps(compile(toy, 'app.py', 'exec'))).decode(), 1)
        namespace = {'__name__': 'overview_backend_integration_test'}
        exec(compile(source, 'app.py', 'exec'), namespace)
        api = builtins.LX_TEST_API()
        del builtins.LX_TEST_API
        for name in ('driver_updates_scan', 'driver_updates_status', 'driver_updates_install'):
            self.assertTrue(callable(getattr(api, name, None)))
        self.assertEqual(api.driver_updates_status()['state'], 'idle')
        namespace['_CPU_FREQ_SAMPLER'].current = lambda: {0: 5100, 1: 5200, 2: 4200}
        result = api.metrics()
        self.assertEqual(result['cpu']['frequency'], 5150)
        self.assertEqual(result['cpu']['eCoreFrequency'], 4200)
        self.assertEqual(result['cpu']['frequencySource'], 'Windows 实际频率')
        self.assertIsNone(result['temperatures']['cpu']['vcore'])
        api.sensor_rows = []
        namespace['_CPU_FREQ_SAMPLER'].topology = lambda: (1, 1)
        result = api.metrics()
        self.assertEqual(result['cpu']['frequency'], 5150)
        self.assertEqual(result['cpu']['eCoreFrequency'], 4200)
        self.assertEqual(result['cpu']['frequencySource'], 'Windows 实际频率')
        namespace['_CPU_FREQ_SAMPLER'].current = lambda: {}
        unavailable = api.metrics()['cpu']
        self.assertIsNone(unavailable['frequency'])
        self.assertIsNone(unavailable['peakFrequency'])
        self.assertIsNone(unavailable['eCoreFrequency'])


if __name__ == '__main__':
    unittest.main()
