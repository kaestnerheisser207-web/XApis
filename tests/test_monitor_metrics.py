"""监听指标回归测试，仅使用本地临时 SQLite 数据库。"""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from x_apis import monitor_metrics as metrics


class MonitorMetricsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        metrics.ensure_schema(self.db)

    def test_empty_snapshot_and_idempotent_schema(self):
        metrics.ensure_schema(self.db)
        self.assertEqual(metrics.snapshot(self.db), {
            'counters': {}, 'samples': {}, 'sample_limit_per_kind': 1000,
        })

    def test_counters_and_samples_survive_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'monitor.sqlite3'
            with sqlite3.connect(path) as db:
                metrics.ensure_schema(db)
                metrics.increment(db, 'requests')
                metrics.increment(db, 'requests', 2)
                metrics.record(db, 'request_ms', 123.5, 100)
                expected = metrics.snapshot(db)
            db.close()
            with sqlite3.connect(path) as restored:
                metrics.ensure_schema(restored)
                self.assertEqual(metrics.snapshot(restored), expected)
                self.assertEqual(expected['counters']['requests'], 3)
                self.assertEqual(expected['samples']['request_ms']['p95'], 123.5)
            restored.close()

    def test_samples_are_bounded_independently_by_insertion_order(self):
        for index in range(1105):
            metrics.record(self.db, 'request_ms', index, index)
            metrics.record(self.db, 'publish_to_detect_ms', 10000 + index, index)
        # 时钟回拨时仍保留新写入样本，并记录真实传入的采样时间。
        metrics.record(self.db, 'request_ms', 0, 1)
        report = metrics.snapshot(self.db)['samples']
        self.assertEqual(report['request_ms']['count'], 1000)
        self.assertEqual(report['publish_to_detect_ms']['count'], 1000)
        self.assertEqual(report['request_ms']['last_at'], 1)
        request_values = [row[0] for row in self.db.execute(
            "SELECT value FROM metrics_samples WHERE kind = 'request_ms' ORDER BY id")]
        self.assertEqual(request_values, list(range(106, 1105)) + [0])
        self.assertEqual(report['publish_to_detect_ms']['max'], 11104)

    def test_percentiles_use_linear_interpolation_on_sorted_values(self):
        for value in range(100, 0, -1):
            metrics.record(self.db, 'request_ms', value, 100 - value)
        report = metrics.snapshot(self.db)['samples']['request_ms']
        self.assertEqual(report['count'], 100)
        self.assertEqual(report['p50'], 50.5)
        self.assertAlmostEqual(report['p95'], 95.05)
        self.assertEqual(report['max'], 100)
        self.assertEqual(report['last_at'], 99)

    def test_single_sample_zero_and_large_finite_values(self):
        metrics.record(self.db, 'detect_to_delivery_ms', 0, 0)
        self.assertEqual(metrics.snapshot(self.db)['samples']['detect_to_delivery_ms'], {
            'count': 1, 'p50': 0, 'p95': 0, 'max': 0, 'last_at': 0,
        })
        metrics.record(self.db, 'large', 1e308, 1)
        metrics.record(self.db, 'large', 1.7e308, 2)
        self.assertAlmostEqual(metrics.snapshot(self.db)['samples']['large']['p50'] / 1e308, 1.35)

    def test_invalid_samples_and_times_do_not_write(self):
        invalid = [-1, float('nan'), float('inf'), -float('inf'), '1', None, True, 10 ** 1000]
        for value in invalid:
            with self.subTest(value_type=type(value), field='value'):
                with self.assertRaises(ValueError):
                    metrics.record(self.db, 'request_ms', value, 1)
            with self.subTest(value_type=type(value), field='now'):
                with self.assertRaises(ValueError):
                    metrics.record(self.db, 'request_ms', 1, value)
        self.assertEqual(metrics.snapshot(self.db)['samples'], {})

    def test_invalid_names_and_increments_do_not_write(self):
        for name in ['', None, 12]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    metrics.increment(self.db, name)
                with self.assertRaises(ValueError):
                    metrics.record(self.db, name, 1, 1)
        for count in [-1, 1.5, True, None]:
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    metrics.increment(self.db, 'requests', count)
        self.assertEqual(metrics.snapshot(self.db)['counters'], {})

    def test_caller_rollback_includes_metrics_and_schema_ensure(self):
        self.db.execute('CREATE TABLE caller_data(value INTEGER)')
        self.db.execute('INSERT INTO caller_data VALUES (1)')
        metrics.ensure_schema(self.db)
        metrics.increment(self.db, 'requests')
        metrics.record(self.db, 'request_ms', 10, 1)
        metrics.snapshot(self.db)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT * FROM caller_data').fetchall(), [])
        self.assertEqual(metrics.snapshot(self.db)['counters'], {})
        self.assertEqual(metrics.snapshot(self.db)['samples'], {})

    def test_retention_deletes_roll_back_with_new_sample(self):
        for index in range(1000):
            metrics.record(self.db, 'request_ms', index, index)
        self.db.commit()
        before = metrics.snapshot(self.db)
        metrics.record(self.db, 'request_ms', 2000, 2000)
        self.db.rollback()
        self.assertEqual(metrics.snapshot(self.db), before)


if __name__ == '__main__':
    unittest.main()
