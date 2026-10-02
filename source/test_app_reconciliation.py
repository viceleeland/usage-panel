"""Verify that diagnostic export only serializes the allowlisted aggregate report."""
import ast
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from usage_reconciliation import build_reconciliation


class AppReconciliationTests(unittest.TestCase):
    def make_panel(self, directory, snapshot, builder=build_reconciliation):
        source = Path(__file__).with_name('app.py')
        tree = ast.parse(source.read_text(encoding='utf-8'))
        panel = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'UsagePanel')
        panel.body = [node for node in panel.body if isinstance(node, ast.FunctionDef)
                      and node.name == 'save_reconciliation']
        namespace = {'DATA': Path(directory)/'data', 'json': json,
                     'build_reconciliation': builder}
        exec(compile(ast.Module(body=[panel], type_ignores=[]), str(source), 'exec'), namespace)
        app = namespace['UsagePanel']()
        app.analytics_snapshot = snapshot
        app.result = {'codex': {'daily_usage': {'ok': True, 'buckets': []}}}
        return app

    def test_unloaded_snapshot_does_not_replace_previous_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'data'
            output.mkdir()
            target = output/'usage-reconciliation.json'
            target.write_text('{"previous":true}', encoding='utf-8')
            self.make_panel(directory, {}).save_reconciliation()
            self.assertEqual(json.loads(target.read_text()), {'previous': True})

    def test_atomic_report_omits_private_metadata_and_creates_no_temp_remnant(self):
        with tempfile.TemporaryDirectory() as directory:
            stamp = dt.datetime.now(dt.timezone.utc).timestamp()
            snapshot = {'available': True, 'partial': False, 'updated': stamp,
                        'events': [{'timestamp': stamp, 'input': 8, 'cached': 4, 'output': 2,
                                    'total': 10, 'session_id': 'PRIVATE_SESSION',
                                    'title': 'PRIVATE_TITLE', 'message': 'PRIVATE_MESSAGE'}],
                        'sessions': {'PRIVATE_SESSION': {'title': 'PRIVATE_TITLE'}},
                        'notes': ['PRIVATE_NOTE'], 'issues': []}
            self.make_panel(directory, snapshot).save_reconciliation()
            target = Path(directory)/'data'/'usage-reconciliation.json'
            content = target.read_text(encoding='utf-8')
            self.assertNotIn('PRIVATE_', content)
            self.assertIsInstance(json.loads(content), dict)
            self.assertFalse(target.with_suffix('.tmp').exists())

    def test_diagnostic_failure_preserves_previous_file_and_does_not_stop_polling(self):
        def failed_builder(*args):
            raise RuntimeError('private exception detail must not escape')
        for builder in (failed_builder, lambda *args: {'invalid': {1, 2}}):
            with self.subTest(builder=builder), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)/'data'
                output.mkdir()
                target = output/'usage-reconciliation.json'
                target.write_text('{"previous":true}', encoding='utf-8')
                app = self.make_panel(directory, {'updated': 1}, builder=builder)
                app.save_reconciliation()
                self.assertEqual(json.loads(target.read_text()), {'previous': True})


if __name__ == '__main__':
    unittest.main()
