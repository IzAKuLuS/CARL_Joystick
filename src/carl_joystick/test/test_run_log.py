import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from carl_joystick.run_log import RunLog
from carl_joystick.review_run import records, review


class RunLogTest(unittest.TestCase):
    def test_concurrent_events_are_ordered_redacted_and_flush_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunLog(directory)
            def emit():
                for _ in range(30):
                    recorder.emit('sample', token='private', nested={'authorization': 'secret'}, value=float('nan'))
            threads = [threading.Thread(target=emit) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            recorder.close()
            saved = list(records(recorder.directory))
            self.assertEqual([r['seq'] for r in saved], list(range(1, 121)))
            self.assertEqual(sorted(r['mono_ns'] for r in saved), [r['mono_ns'] for r in saved])
            self.assertNotIn('private', json.dumps(saved))
            self.assertNotIn('secret', json.dumps(saved))
            self.assertIsNone(saved[0]['data']['value'])
            self.assertIsNone(recorder.error)

    def test_rotation_retains_manifest_bounded_tail_and_reports_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunLog(directory, max_bytes=1024, max_files=3)
            recorder.emit('run_start')
            for _ in range(40):
                recorder.emit('payload', message='x' * 500)
            recorder.emit('run_end')
            recorder.close()
            self.assertLessEqual(len(list(Path(recorder.directory).glob('*.jsonl'))), 3)
            summary = review(recorder.directory)
            self.assertGreater(summary['missing_sequences'], 0)
            self.assertEqual(summary['events']['run_start'], 1)
            self.assertTrue(summary['clean_shutdown'])

    def test_queue_overflow_is_visible_and_never_waits_for_disk(self):
        entered, release = threading.Event(), threading.Event()
        original = Path.open
        def slow_open(path, *args, **kwargs):
            entered.set()
            release.wait(2)
            return original(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, 'open', slow_open):
            recorder = RunLog(directory, queue_size=2)
            recorder.emit('first')
            self.assertTrue(entered.wait(1))
            for _ in range(20):
                recorder.emit('overflow')
            self.assertEqual(recorder.dropped, 18)
            release.set()
            recorder.close()
            self.assertEqual(recorder.written, 3)

    def test_storage_failure_is_exposed_without_raising_in_control_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory, 'file')
            blocked.write_text('occupied')
            errors = []
            recorder = RunLog(blocked, on_error=errors.append)
            recorder.emit('sample')
            recorder.close()
            self.assertTrue(recorder.status()['error'])
            self.assertTrue(errors)

    def test_review_separates_active_control_from_measured_motion_and_deduplicates_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunLog(directory)
            recorder.emit('run_start')
            recorder.emit('motion_stop', was_active=False, reason='released')
            recorder.emit('motion_stop', was_active=True, reason='input_timeout')
            for _ in range(2):
                recorder.emit('browser_event', browser={'id': 'a:1', 'event': 'offline'})
            recorder.close()
            timeline = io.StringIO()
            summary = review(recorder.directory, timeline)
            self.assertEqual(summary['active_motion_stops'], {'input_timeout': 1})
            self.assertEqual(summary['browser_events'], {'offline': 1})
            self.assertFalse(summary['clean_shutdown'])
            self.assertIn('input_timeout', timeline.getvalue())

    def test_downloaded_browser_log_merges_without_double_counting_or_old_run_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunLog(directory)
            current = {'id': 'page:1', 'event': 'offline', 'data': {'run_id': recorder.run_id}}
            old = {'id': 'page:2', 'event': 'offline', 'data': {'run_id': 'old-run'}}
            recorder.emit('run_start')
            recorder.emit('browser_event', browser=current)
            recorder.close()
            download = Path(directory, 'browser.jsonl')
            download.write_text(json.dumps(current) + '\n' + json.dumps(old) + '\n')
            summary = review(recorder.directory, browser_logs=[download])
            self.assertEqual(summary['browser_events'], {'offline': 1})
            self.assertEqual(summary['historical_browser_events'], 1)

    def test_corrupt_or_truncated_tail_does_not_hide_prior_events(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'events-000001.jsonl').write_text(
                '{"event":"run_start","data":{}}\n{}\n123\n{"event":')
            summary = review(directory)
            self.assertEqual(summary['events']['run_start'], 1)
            self.assertEqual(summary['events']['unreadable_record'], 3)
            self.assertFalse(summary['clean_shutdown'])


if __name__ == '__main__':
    unittest.main()
