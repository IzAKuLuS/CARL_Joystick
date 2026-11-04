import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from carl_joystick.host_monitor import HostMonitor


class HostMonitorTest(unittest.TestCase):
    def test_independent_probe_success_and_failure_and_default_no_external_requests(self):
        for mode in ('disabled', 'success', 'failure'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                arrived = threading.Event()
                events = []
                def emit(event, **data):
                    events.append((event, data))
                    if event == ('host_resources' if mode == 'disabled' else 'internet_probe'):
                        arrived.set()
                response = MagicMock()
                response.__enter__.return_value.status = 204
                with patch('carl_joystick.host_monitor.urllib.request.urlopen', return_value=response,
                           side_effect=TimeoutError('probe timeout') if mode == 'failure' else None) as request:
                    monitor = HostMonitor(SimpleNamespace(directory=directory + '/run', emit=emit),
                                          '' if mode == 'disabled' else 'https://example.test/check')
                    try:
                        self.assertTrue(arrived.wait(2))
                    finally:
                        monitor.close()
                    if mode == 'disabled':
                        request.assert_not_called()
                    else:
                        probe = next(data for event, data in events if event == 'internet_probe')
                        self.assertEqual(probe['ok'], mode == 'success')
                        if mode == 'failure':
                            self.assertEqual(probe['error_type'], 'TimeoutError')
                    self.assertTrue(any(event == 'host_network' for event, _ in events))
