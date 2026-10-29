import unittest

import cpu_telemetry


class CpuTelemetryTest(unittest.TestCase):
    def test_actual_frequency_splits_p_and_e_core_threads(self):
        readings = {i: 5000 + i for i in range(16)}
        readings.update({i: 4200 for i in range(16, 32)})
        result = cpu_telemetry.summarize_frequencies(readings, 8, 16)
        self.assertEqual(result["p_average"], 5008)
        self.assertEqual(result["p_peak"], 5015)
        self.assertEqual(result["e_average"], 4200)

    def test_mismatched_topology_does_not_guess(self):
        self.assertIsNone(cpu_telemetry.summarize_frequencies({0: 5000}, 8, 16))

    def test_voltage_is_named_vid_not_vcore(self):
        readings = [{"key": "cpu", "name": "P-Core #1 VID", "type": "Voltage", "value": 1.348}]
        self.assertEqual(cpu_telemetry.classify_voltage(readings)["vid"], 1.348)
        self.assertIsNone(cpu_telemetry.classify_voltage(readings)["vcore"])


if __name__ == "__main__":
    unittest.main()
