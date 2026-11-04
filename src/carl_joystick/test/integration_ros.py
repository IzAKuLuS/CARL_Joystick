"""Exercise real ROS nodes and CARL wire frames with an in-memory transport.

Run on an isolated ROS domain with the CARL overlay and FastAPI/httpx available.
Never opens a hardware transport or uses the operator's normal ROS domain.
"""
import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
import unittest
import tempfile
import signal
import yaml
from unittest.mock import patch

if os.environ.get('ROS_DOMAIN_ID') != '213':
    raise RuntimeError('Run with ROS_DOMAIN_ID=213 to isolate this test from CARL')

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.parameter import Parameter
from fastapi.testclient import TestClient
import uvicorn
from carl_bridge import protocol
from carl_bridge.bridge_node import BridgeNode
from carl_joystick.server import create_app
from carl_joystick.webui_node import WebUINode
from carl_joystick.webui_node import main as joystick_main


class MainLifecycleTest(unittest.TestCase):
    def test_ros_parameter_override_controls_throttle_curve_and_run_manifest(self):
        from carl_joystick.review_run import records
        rclpy.init()
        try:
            with tempfile.TemporaryDirectory(prefix='carl-curve-parameter-') as directory:
                node = WebUINode(parameter_overrides=[Parameter('log_dir', value=directory),
                                                     Parameter('throttle_exponent', value=1.0)])
                try:
                    controller = node.controller
                    owner = object()
                    controller.claim(owner)
                    controller.heartbeat = controller.throttle_status = (controller.now(), False)
                    controller.handle(owner, dict(type='control', x=0, y=0, deadman=False))
                    state = controller.handle(owner, dict(type='control', x=0, y=0.5, deadman=True))
                    self.assertEqual(state['throttle_exponent'], 1.0)
                    self.assertAlmostEqual(state['left'], 0.35526315789473684)
                    self.assertEqual(state['left'], state['right'])
                    controller.stop()
                finally:
                    node.host_monitor.close()
                    node.recorder.close()
                    node.destroy_node()
                manifest = next(item for item in records(node.recorder.directory)
                                if item['event'] == 'run_start')
                self.assertEqual(manifest['data']['config']['throttle_exponent'], 1.0)
        finally:
            rclpy.shutdown()

    def test_process_cleanup_and_server_failure_are_persisted(self):
        from carl_joystick.review_run import records, review
        for failure in (None, RuntimeError('simulated bind failure')):
            with tempfile.TemporaryDirectory(prefix='carl-lifecycle-') as directory:
                with patch('carl_joystick.webui_node.uvicorn.run', side_effect=failure):
                    args = ['--ros-args', '-p', 'log_dir:=' + directory]
                    if failure:
                        with self.assertRaises(RuntimeError):
                            joystick_main(args)
                    else:
                        joystick_main(args)
                run = next(Path(directory).iterdir())
                saved = list(records(run))
                self.assertEqual(saved[-1]['event'], 'run_end')
                self.assertEqual(review(run)['clean_shutdown'], failure is None)
                if failure:
                    self.assertTrue(any(item['event'] == 'process_error' for item in saved))


class Transport:
    def __init__(self):
        self.sent = []
        self.last_feedback = 0

    def send(self, data):
        self.sent.append(data)

    def poll(self):
        if time.monotonic() - self.last_feedback < 0.05:
            return b''
        self.last_feedback = time.monotonic()
        return (protocol.frame(protocol.MSG_HEARTBEAT, bytes([1, 1, 0]))
                + protocol.frame(protocol.MSG_THROTTLE_STATUS, struct.pack('<BBhh', 1, 0, 0, 0)))


class ROSIntegrationTest(unittest.TestCase):
    def setUp(self):
        rclpy.init()
        self.transport = Transport()
        with patch('carl_bridge.bridge_node.make_transport', return_value=self.transport):
            self.bridge = BridgeNode()
        self.log_directory = tempfile.TemporaryDirectory(prefix='carl-recording-integration-')
        self.node = WebUINode(parameter_overrides=[Parameter('log_dir', value=self.log_directory.name)])
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.bridge)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()
        self.wait_for(self.node.controller.ready)

    def tearDown(self):
        self.executor.shutdown(timeout_sec=2)
        self.thread.join(timeout=2)
        self.node.host_monitor.close()
        self.node.recorder.close()
        self.node.destroy_node()
        self.log_directory.cleanup()
        self.bridge.destroy_node()
        rclpy.shutdown()

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail('Timed out waiting for ROS delivery')

    def frames(self):
        return [frame for data in list(self.transport.sent)
                for frame in protocol.Parser().feed(data)]

    def has_wheels(self, values):
        return any(mid == protocol.MSG_THROTTLE_CMD
                   and struct.unpack('<hhhh', payload) == values
                   for mid, payload in self.frames())

    def send(self, ws, data):
        ws.send_json({**data, 'token': getattr(ws, 'token', None)})
        state = ws.receive_json()
        ws.token = state['token']
        return state

    def test_websocket_to_real_bridge_to_firmware_wire_protocol(self):
        with TestClient(create_app(self.node)) as client:
            with client.websocket_connect('/ws') as ws:
                self.assertTrue(self.send(ws, dict(type='control', x=0, y=0, deadman=False))['ready'])
                self.assertTrue(self.send(ws, dict(type='control', x=1, y=0, deadman=True))['active'])
                self.wait_for(lambda: self.has_wheels((400, -400, 400, -400)))
                # Keep the socket open without sending: the ROS steady timer
                # must command zero independently of the WebSocket loop.
                self.transport.sent.clear()
                self.wait_for(lambda: not self.node.controller.active, timeout=0.5)
                self.wait_for(lambda: self.has_wheels((0, 0, 0, 0)))
                self.assertTrue(self.send(ws, {'type': 'estop'})['estop'])
                self.wait_for(lambda: (protocol.MSG_ESTOP, b'\x01') in self.frames())
                self.assertTrue(self.send(ws, {'type': 'reset_estop'})['neutral_required'])
                self.wait_for(lambda: (protocol.MSG_ESTOP, b'\x00') in self.frames())
            self.wait_for(lambda: self.node.controller.owner is None)
        self.node.recorder.close()
        from carl_joystick.review_run import records
        saved = list(records(self.node.recorder.directory))
        self.assertTrue(any(item['event'] == 'command_published' for item in saved))
        self.assertTrue(any(item['event'] == 'estop_published' and item['data']['reason'] == 1 for item in saved))
        self.assertTrue(any(item['event'] == 'motion_stop' and item['data']['reason'] == 'input_timeout' for item in saved))
        self.assertTrue(any(item['event'] == 'telemetry' and item['data']['topic'] == 'bridge_diagnostics'
                            and 'transport_tx' in item['data']['message']['data'] for item in saved))
        self.assertIsNone(self.node.recorder.error)
        observed = [item for item in saved if item['event'] == 'telemetry'
                    and item['data']['topic'] == 'observed_throttle_cmd']
        self.assertTrue(observed)
        self.assertTrue(observed[0]['data']['message_info']['publisher_gid']['data'])

    def test_optional_native_rosbag_records_command_and_feedback(self):
        bag = Path(self.log_directory.name, 'native-bag')
        process = subprocess.Popen(
            ['ros2', 'bag', 'record', '--disable-keyboard-controls', '-o', str(bag), '--topics',
             '/carl/throttle_cmd', '/carl/heartbeat', '/carl/throttle_status', '/carl/bridge_diagnostics'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            # Bridge + JSON recorder + bag subscriber must all be discovered.
            self.wait_for(lambda: self.node.command_pub.get_subscription_count() >= 3, timeout=8)
            with TestClient(create_app(self.node)) as client:
                with client.websocket_connect('/ws') as ws:
                    self.send(ws, dict(type='control', x=0, y=0, deadman=False))
                    self.send(ws, dict(type='control', x=0, y=1, deadman=True))
                    self.send(ws, dict(type='control', x=0, y=0, deadman=False))
                    time.sleep(0.3)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                output, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
                self.fail('Bag failed to stop: ' + output)
        self.assertEqual(process.returncode, 0, output)
        metadata = yaml.safe_load((bag / 'metadata.yaml').read_text())['rosbag2_bagfile_information']
        counts = {item['topic_metadata']['name']: item['message_count']
                  for item in metadata['topics_with_message_count']}
        for topic in ('/carl/throttle_cmd', '/carl/heartbeat', '/carl/throttle_status', '/carl/bridge_diagnostics'):
            self.assertGreater(counts.get(topic, 0), 0, output)

    def test_live_controls_client_through_network_ros_and_firmware_frames(self):
        # Resolve the actual sibling application instead of keeping a second
        # copy of its browser client in this repository.
        repo = Path(__file__).resolve().parents[3]
        controls = Path(os.environ.get(
            'CARL_CONTROLS_DIR', str(repo.parent / 'CARL_Controls_Application' / 'src')))
        client_file = controls / 'joystick-client.js'
        self.assertTrue(client_file.is_file(), f'Controls client missing: {client_file}')
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            create_app(self.node, str(controls)), log_level='warning',
            ws='websockets', ws_max_size=1024, ws_max_queue=1,
            ws_per_message_deflate=False, ws_ping_interval=5.0, ws_ping_timeout=10.0))
        server_thread = threading.Thread(
            target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        server_thread.start()
        try:
            self.wait_for(lambda: server.started)
            result = subprocess.run(
                ['node', str(Path(__file__).with_name('integration_browser.js')),
                 f'ws://127.0.0.1:{port}/ws', str(client_file)],
                capture_output=True, text=True, timeout=20 + float(os.environ.get('CARL_SOAK_SECONDS', '0')))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stdout, end='')
            self.wait_for(lambda: self.node.controller.owner is None)
            self.wait_for(lambda: self.has_wheels((0, 0, 0, 0)))
            for wheels in [(750, 750, 750, 750), (-750, -750, -750, -750),
                           (-400, 400, -400, 400), (400, -400, 400, -400),
                           (750, 749, 750, 749), (749, 750, 749, 750),
                           (358, -21, 358, -21), (-21, 358, -21, 358),
                           (21, -358, 21, -358), (699, 236, 699, 236),
                           (750, 228, 750, 228)]:
                self.assertTrue(self.has_wheels(wheels), f'Missing wire output: {wheels}')
            for magnitude in (2, 19, 20, 33, 168, 407, 673):
                for sign in (-1, 1):
                    wheels = (sign * magnitude,) * 4
                    self.assertTrue(self.has_wheels(wheels), f'Missing quadratic UI output: {wheels}')
            self.assertIn((protocol.MSG_ESTOP, b'\x01'), self.frames())
            self.assertIn((protocol.MSG_ESTOP, b'\x00'), self.frames())
            self.node.recorder.close()
            from carl_joystick.review_run import records
            saved = list(records(self.node.recorder.directory))
            self.assertTrue(any(item['event'] == 'browser_event'
                                and item['data']['browser']['event'] == 'reply_received' for item in saved))
            self.assertEqual(self.node.recorder.dropped, 0)
        finally:
            server.should_exit = True
            server_thread.join(timeout=3)
            listener.close()


if __name__ == '__main__':
    unittest.main()
