"""Default local-calendar regression tests at daylight-saving transitions."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import token_usage


class TokenTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.reader = token_usage.DailyTokens(self.home/'codex', self.home/'claude')

    def write(self, stamps, modified):
        codex, deepseek = [], []
        for index, stamp in enumerate(stamps, 1):
            total = dict(input_tokens=10*index, output_tokens=2*index,
                         cached_input_tokens=0, total_tokens=12*index)
            last = dict(input_tokens=10, output_tokens=2, cached_input_tokens=0, total_tokens=12)
            codex.append(dict(timestamp=stamp, payload=dict(type='token_count', info=dict(
                total_token_usage=total, last_token_usage=last))))
            deepseek.append(dict(timestamp=stamp, type='assistant', message=dict(
                id=str(index), model='deepseek-chat', usage=dict(input_tokens=10, output_tokens=2))))
        for relative, records in [('codex/sessions/current.jsonl', codex),
                                  ('claude/projects/current.jsonl', deepseek)]:
            path = self.home/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(''.join(json.dumps(record)+'\n' for record in records), encoding='utf8')
            os.utime(path, (modified, modified))

    def test_explicit_aware_timezone_is_still_authoritative(self):
        now = dt.datetime(2026, 10, 2, 12, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.write(['2026-10-01T15:59:59Z', '2026-10-01T16:00:00Z'], now.timestamp())
        result = self.reader.read(now=now)
        self.assertEqual(result['date'], '2026-10-02')
        for kind in ('codex', 'deepseek'):
            self.assertEqual(result[kind]['total'], 12)
            self.assertEqual(result['history'][-2][kind]['total'], 12)


@unittest.skipUnless(hasattr(time, 'tzset'), 'OS timezone injection requires time.tzset')
class DefaultLocalDSTTests(TokenTimezoneTests):
    def setUp(self):
        super().setUp()
        # POSIX rules avoid a dependency on an installed IANA timezone database.
        self.zone = patch.dict(os.environ, {'TZ': 'PST8PDT,M3.2.0,M11.1.0'})
        self.zone.start()
        time.tzset()
        self.addCleanup(self.restore_timezone)

    def restore_timezone(self):
        self.zone.stop()
        time.tzset()

    def read_at(self, utc):
        instant = dt.datetime.fromisoformat(utc).timestamp()

        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(instant, tz)

        clock = SimpleNamespace(datetime=Clock, timedelta=dt.timedelta)
        with patch.object(token_usage, 'dt', clock):
            return self.reader.read()

    def test_spring_midnight_does_not_use_the_new_afternoon_offset(self):
        noon = '2026-03-08T19:00:00+00:00'
        self.write(['2026-03-08T07:30:00Z', '2026-03-08T08:30:00Z'],
                   dt.datetime.fromisoformat(noon).timestamp())
        result = self.read_at(noon)
        self.assertEqual(result['date'], '2026-03-08')
        for kind in ('codex', 'deepseek'):
            self.assertEqual(result['history'][-2][kind]['total'], 12)
            self.assertEqual(result[kind]['total'], 12)

    def test_fall_midnight_does_not_use_the_new_afternoon_offset(self):
        noon = '2026-11-01T20:00:00+00:00'
        self.write(['2026-11-01T06:30:00Z', '2026-11-01T07:30:00Z'],
                   dt.datetime.fromisoformat(noon).timestamp())
        result = self.read_at(noon)
        self.assertEqual(result['date'], '2026-11-01')
        for kind in ('codex', 'deepseek'):
            self.assertEqual(result['history'][-2][kind]['total'], 12)
            self.assertEqual(result[kind]['total'], 12)

    def test_window_scan_includes_oldest_midnight_with_its_original_offset(self):
        first = dt.datetime.fromisoformat('2026-11-01T07:30:00+00:00').timestamp()
        self.write(['2026-11-01T07:30:00Z'], first)
        result = self.read_at('2026-11-07T20:00:00+00:00')
        self.assertEqual(result['history'][0]['date'], '2026-11-01')
        for kind in ('codex', 'deepseek'):
            self.assertEqual(result['history'][0][kind]['total'], 12)

    def test_repeated_local_hour_preserves_the_clock_instant_and_fold(self):
        for fold, utc in enumerate(('2026-11-01T08:30:00+00:00', '2026-11-01T09:30:00+00:00')):
            with self.subTest(fold=fold):
                instant = dt.datetime.fromisoformat(utc).timestamp()
                self.write(['2026-11-01T07:30:00Z'], instant)
                self.reader.files.clear()
                with patch.object(self.reader, '_record', wraps=self.reader._record) as record:
                    result = self.read_at(utc)
                clock = record.call_args.args[2]
                self.assertEqual(clock.timestamp(), instant)
                self.assertEqual((clock.hour, clock.minute, clock.fold), (1, 30, fold))
                self.assertIsNone(clock.tzinfo)
                self.assertEqual(result['deepseek']['total'], 12)


if __name__ == '__main__':
    unittest.main()
