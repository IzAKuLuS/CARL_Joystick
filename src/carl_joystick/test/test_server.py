import tempfile
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from carl_joystick.controller import Controller

try:
    import httpx  # Starlette raises RuntimeError, rather than ImportError, if absent.
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
except ImportError:
    TestClient = None
else:
    from carl_joystick.server import create_app


@unittest.skipIf(TestClient is None, 'Install FastAPI and httpx to test the server')
class ServerTest(unittest.TestCase):
    def setUp(self):
        self.commands, self.estops = [], []
        self.lock = threading.RLock()
        self.controller = Controller(lambda *w: self.commands.append(w), self.estops.append)
        now = self.controller.now()
        self.controller.heartbeat = self.controller.throttle_status = (now, False)
        self.app = create_app(self)

    def neutral(self, ws):
        return self.send(ws, {'type': 'control', 'x': 0, 'y': 0, 'deadman': False})

    def send(self, ws, data):
        ws.send_json({**data, 'token': getattr(ws, 'token', None)})
        state = ws.receive_json()
        ws.token = state['token']
        return state

    def test_exclusive_session_and_disconnect_zero(self):
        with TestClient(self.app) as client:
            with client.websocket_connect('/ws') as first:
                self.assertTrue(self.neutral(first)['ready'])
                self.assertTrue(self.send(first, {'type': 'control', 'x': 1, 'y': 0, 'deadman': True})['active'])
                with client.websocket_connect('/ws') as other:
                    self.assertIn('Another controller', other.receive_json()['error'])
                self.assertTrue(self.controller.active)
            self.assertIsNone(self.controller.owner)
            self.assertEqual(self.commands[-1], (0, 0))
            with client.websocket_connect('/ws') as replacement:
                self.assertFalse(self.neutral(replacement)['active'])

    def test_idle_socket_keeps_ownership_past_old_one_second_deadline(self):
        with TestClient(self.app) as client:
            with client.websocket_connect('/ws') as ws:
                self.neutral(ws)
                owner = self.controller.owner
                time.sleep(1.15)
                self.assertIs(self.controller.owner, owner)
                self.assertFalse(self.neutral(ws)['active'])

    def test_invalid_packet_closes_session_and_stops(self):
        for packet in ('null', '[]', '{', '{"type":"mode"}', 'x' * 1025):
            with TestClient(self.app) as client:
                with client.websocket_connect('/ws') as ws:
                    self.neutral(ws)
                    ws.send_text(packet)
                    with self.assertRaises(WebSocketDisconnect):
                        ws.receive_json()
                self.assertIsNone(self.controller.owner)
                self.assertEqual(self.commands[-1], (0, 0))

    def test_estop_and_reset_use_current_firmware_reasons(self):
        with TestClient(self.app) as client:
            with client.websocket_connect('/ws') as ws:
                self.assertTrue(self.send(ws, {'type': 'estop'})['estop'])
                self.assertTrue(self.send(ws, {'type': 'reset_estop'})['neutral_required'])
                self.assertEqual(self.estops, [1, 0])

    def test_silent_half_open_session_frees_ownership_without_taking_other_control(self):
        with patch('carl_joystick.server.SESSION_TIMEOUT', 0.1), TestClient(self.app) as client:
            with client.websocket_connect('/ws') as first:
                self.neutral(first)
                self.assertTrue(self.send(first, dict(type='control', x=0, y=1, deadman=True))['active'])
                with self.assertRaises(WebSocketDisconnect):
                    first.receive_json()
                self.assertIsNone(self.controller.owner)
                self.assertEqual(self.commands[-1], (0, 0))
                with client.websocket_connect('/ws') as replacement:
                    self.assertFalse(self.neutral(replacement)['active'])

    def test_delayed_queued_drive_reset_and_replayed_token_are_rejected_at_rest(self):
        clock = [0.0]
        self.controller.now = lambda: clock[0]
        self.controller.heartbeat = self.controller.throttle_status = (0.0, False)
        with TestClient(self.app) as client:
            with client.websocket_connect('/ws') as ws:
                self.neutral(ws)
                for delay in (0.25, 0.3):
                    clock[0] += delay
                    result = self.send(ws, dict(type='control', x=0, y=1, deadman=True))
                    self.assertEqual(result['rejection'], 'expired_command')
                    self.assertFalse(result['active'])
                    self.assertTrue(result['neutral_required'])
                self.neutral(ws)
                token = ws.token
                self.assertTrue(self.send(ws, dict(type='control', x=0, y=1, deadman=True))['active'])
                ws.token = token  # Duplicate/replayed drive cannot renew motion.
                self.assertFalse(self.send(ws, dict(type='control', x=0, y=1, deadman=True))['active'])
                self.send(ws, dict(type='estop'))
                clock[0] += 0.25
                self.assertEqual(self.send(ws, dict(type='reset_estop'))['rejection'], 'expired_command')
                self.assertEqual(self.estops, [1])
                self.assertTrue(self.send(ws, dict(type='reset_estop'))['neutral_required'])
                self.assertEqual(self.estops, [1, 0])

    def test_legacy_clients_cannot_drive_but_neutral_and_estop_always_work(self):
        with TestClient(self.app) as client:
            with client.websocket_connect('/ws') as ws:
                self.neutral(ws)
                ws.send_json(dict(type='control', x=0, y=1, deadman=True))
                self.assertEqual(ws.receive_json()['rejection'], 'expired_command')
                ws.send_json(dict(type='estop'))
                self.assertTrue(ws.receive_json()['estop'])

    def test_health_is_read_only_and_optional_controls_are_served(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'index.html').write_text('CARL modes')
            Path(directory, 'joystick.html').write_text('CARL joystick')
            with TestClient(create_app(self, directory)) as client:
                self.assertEqual(client.get('/').text, 'CARL modes')
                self.assertTrue(client.get('/health').json()['ready'])
                self.assertIsNone(self.controller.owner)
                self.assertEqual(self.commands, [])


if __name__ == '__main__':
    unittest.main()
