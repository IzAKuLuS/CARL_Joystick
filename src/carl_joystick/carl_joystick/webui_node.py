"""Publish CARL wheel commands and e-stop requests from the joystick server."""
import threading
import hashlib
import os
from pathlib import Path
import platform
import time
import traceback

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rclpy.signals import SignalHandlerOptions
import uvicorn

from carl_msgs.msg import CarlEstop, CarlHeartbeat, CarlThrottleCmd, CarlThrottleStatus, CarlEncoderFeedback, CarlSteeringAngle
from sensor_msgs.msg import Imu, Temperature
from rcl_interfaces.msg import Log
from std_msgs.msg import String
from rosidl_runtime_py.convert import message_to_ordereddict
from carl_joystick.run_log import RunLog
from carl_joystick.host_monitor import HostMonitor
from carl_joystick.controller import Controller
from carl_joystick.server import create_app


class WebUINode(Node):
    def __init__(self, **kwargs):
        super().__init__('carl_joystick', **kwargs)
        for name, value in [('web_port', 8001), ('controls_dir', ''),
                            ('ssl_certfile', ''), ('ssl_keyfile', ''),
                            ('max_throttle', 0.75), ('max_turn', 0.4),
                            ('throttle_exponent', 2.0),
                            ('input_timeout', 0.25),
                            ('log_dir', '~/.ros/carl_test_runs'), ('test_label', ''),
                            ('log_max_mb', 32), ('log_max_files', 32),
                            ('record_rosbag', False), ('bag_dir', ''),
                            ('internet_probe_url', '')]:
            self.declare_parameter(name, value)
        self.recorder = RunLog(self.get_parameter('log_dir').value,
                               max_bytes=self.get_parameter('log_max_mb').value * 1024 * 1024,
                               max_files=self.get_parameter('log_max_files').value,
                               on_error=self.get_logger().error)
        self.recorder.emit('run_start', hostname=platform.node(), python=platform.python_version(),
                           pid=os.getpid(),
                           environment={k: os.environ.get(k) for k in
                                        ('ROS_DISTRO', 'ROS_DOMAIN_ID', 'RMW_IMPLEMENTATION',
                                         'ROS_AUTOMATIC_DISCOVERY_RANGE')},
                           config={k: self.get_parameter(k).value for k in
                                   ('web_port', 'controls_dir', 'max_throttle', 'max_turn', 'record_rosbag', 'bag_dir',
                                    'input_timeout', 'throttle_exponent', 'test_label', 'log_max_mb', 'log_max_files', 'internet_probe_url')},
                           source_hashes={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in Path(__file__).parent.glob('*.py')},
                           controls_hashes={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                            for p in Path(self.get_parameter('controls_dir').value).glob('joystick*.js')})
        self.get_logger().info('CARL test run: ' + self.recorder.directory)
        self.host_monitor = HostMonitor(self.recorder, self.get_parameter('internet_probe_url').value)
        self._last_tick = time.monotonic()
        self._last_snapshot = 0
        self._last_blocked = 'startup'
        self.telemetry_times = {}
        self.lock = threading.RLock()
        # Volatile, depth one: late subscribers never get a latched drive command.
        self.command_pub = self.create_publisher(CarlThrottleCmd, '/carl/throttle_cmd', QoSProfile(depth=1))
        self.estop_pub = self.create_publisher(CarlEstop, '/carl/estop', QoSProfile(depth=1))
        self.controller = Controller(
            self.publish_command, self.publish_estop,
            on_event=self.get_logger().info, on_record=self.recorder.emit,
            **{name: self.get_parameter(name).value
               for name in ('max_throttle', 'max_turn', 'input_timeout', 'throttle_exponent')})
        self.create_subscription(CarlHeartbeat, '/carl/heartbeat', self.on_heartbeat, 1)
        self.create_subscription(CarlThrottleStatus, '/carl/throttle_status', self.on_status, 1)
        self.create_subscription(CarlEncoderFeedback, '/carl/encoders',
                                 lambda msg: self.record_telemetry('encoders', msg), 10)
        self.create_subscription(CarlSteeringAngle, '/carl/steering_angle',
                                 lambda msg: self.record_telemetry('steering_angle', msg), 10)
        self.create_subscription(Temperature, '/carl/imu/temperature',
                                 lambda msg: self.record_telemetry('imu_temperature', msg), 10)
        self.create_subscription(Log, '/rosout',
                                 lambda msg: self.record_telemetry('rosout', msg), 100)
        self.create_subscription(Imu, '/carl/imu',
                                 lambda msg: self.record_telemetry('imu', msg), 10)
        self.create_subscription(String, '/carl/bridge_diagnostics',
                                 lambda msg: self.record_telemetry('bridge_diagnostics', msg),
                                 QoSProfile(depth=100, reliability=ReliabilityPolicy.BEST_EFFORT))
        self.create_subscription(String, '/carl/imu/diagnostics',
                                 lambda msg: self.record_telemetry('imu_diagnostics', msg), 10)
        self.create_subscription(String, '/carl/imu/spi_diagnostics',
                                 lambda msg: self.record_telemetry('imu_spi_diagnostics', msg), 10)
        self.create_subscription(CarlThrottleCmd, '/carl/throttle_cmd',
                                 lambda msg, info: self.record_telemetry('observed_throttle_cmd', msg, info), 10)
        self.create_subscription(CarlEstop, '/carl/estop',
                                 lambda msg, info: self.record_telemetry('observed_estop', msg, info), 10)
        # A simulation clock pause must never pause the input watchdog.
        self.create_timer(0.05, self.tick, clock=Clock(clock_type=ClockType.STEADY_TIME))

    def publish_command(self, left, right):
        msg = CarlThrottleCmd()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.front_left = msg.rear_left = float(left)
        msg.front_right = msg.rear_right = float(right)
        context = dict(session_id=self.controller.session_id, packet_seq=self.controller.packet_seq,
                       source=self.controller.publish_source, message=message_to_ordereddict(msg))
        self.recorder.emit('command_publish_attempt', **context)
        try:
            self.command_pub.publish(msg)
        except Exception as error:
            self.recorder.emit('command_publish_error', detail=str(error), **context)
            raise
        self.recorder.emit('command_published', **context)

    def publish_estop(self, reason):
        msg = CarlEstop()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.reason = reason
        context = dict(session_id=self.controller.session_id, packet_seq=self.controller.packet_seq,
                       reason=reason, message=message_to_ordereddict(msg))
        self.recorder.emit('estop_publish_attempt', **context)
        try:
            self.estop_pub.publish(msg)
        except Exception as error:
            self.recorder.emit('estop_publish_error', detail=str(error), **context)
            raise
        self.recorder.emit('estop_published', **context)

    def record_telemetry(self, topic, msg, info=None):
        with self.lock:
            now = time.monotonic()
            previous = self.telemetry_times.get(topic)
            self.telemetry_times[topic] = now
            metadata = {}
            if info:
                metadata = dict(info)
                gid = info.get('publisher_gid')
                if gid:
                    metadata['publisher_gid'] = dict(gid, data=list(gid['data']))
            self.recorder.emit('telemetry', topic=topic, message=message_to_ordereddict(msg),
                               interval_s=None if previous is None else now - previous,
                               **({'message_info': metadata} if metadata else {}))

    def on_heartbeat(self, msg):
        self.record_telemetry('heartbeat', msg)
        with self.lock:
            self.controller.heartbeat = (self.controller.now(), bool(msg.flags & 2 or msg.mode == 2))
            if not self.controller.ready() and self.controller.active:
                self.controller.stop(self.controller.blocked_reason())

    def on_status(self, msg):
        self.record_telemetry('throttle_status', msg)
        with self.lock:
            self.controller.throttle_status = (self.controller.now(), bool(msg.faults or msg.state not in (1, 2)))
            if not self.controller.ready() and self.controller.active:
                self.controller.stop(self.controller.blocked_reason())

    def tick(self):
        with self.lock:
            now = time.monotonic()
            interval = now - self._last_tick
            self._last_tick = now
            if interval > 0.1:
                self.recorder.emit('control_timer_late', interval_s=interval)
            self.controller.tick()
            blocked = self.controller.blocked_reason()
            if blocked != self._last_blocked:
                self.recorder.emit('readiness_changed', previous=self._last_blocked,
                                   blocked_reason=blocked, state=self.controller.state())
                self._last_blocked = blocked
            if now - self._last_snapshot >= 1:
                self._last_snapshot = now
                self.recorder.emit('runtime_snapshot', state=self.controller.state(),
                                   telemetry_age_s={k: now - v for k, v in self.telemetry_times.items()},
                                   throttle_subscribers=self.command_pub.get_subscription_count(),
                                   command_receivers=[dict(node=info.node_name, namespace=info.node_namespace)
                                                      for info in self.get_subscriptions_info_by_topic('/carl/throttle_cmd')],
                                   throttle_publishers=self.count_publishers('/carl/throttle_cmd'),
                                   command_sources=[dict(node=info.node_name, namespace=info.node_namespace,
                                                         gid=list(info.endpoint_gid))
                                                    for info in self.get_publishers_info_by_topic('/carl/throttle_cmd')],
                                   heartbeat_publishers=self.count_publishers('/carl/heartbeat'),
                                   status_publishers=self.count_publishers('/carl/throttle_status'),
                                   logging=self.recorder.status())



def main(args=None):
    # Uvicorn owns process signals, so ROS remains available for final zero.
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = WebUINode()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    def spin():
        try:
            executor.spin()
        except Exception as error:
            node.recorder.emit('executor_error', error_type=type(error).__name__, detail=str(error),
                               traceback=traceback.format_exc())
            node.get_logger().error('ROS executor failed: ' + str(error))
            # Preserve a final zero attempt; firmware watchdog remains independent.
            with node.lock:
                node.controller.stop('executor_error')
    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        app = create_app(node, node.get_parameter('controls_dir').value)
        ssl = {name: node.get_parameter(name).value
               for name in ('ssl_certfile', 'ssl_keyfile') if node.get_parameter(name).value}
        uvicorn.run(app, host='0.0.0.0', port=node.get_parameter('web_port').value,
                    ws='websockets', ws_max_size=1024, ws_max_queue=1,
                    ws_per_message_deflate=False, ws_ping_interval=5.0,
                    ws_ping_timeout=10.0, **ssl)
    except BaseException as error:
        node.recorder.emit('process_error', error_type=type(error).__name__, detail=str(error),
                           traceback=traceback.format_exc())
        raise
    finally:
        try:
            with node.lock:
                node.controller.stop('process_shutdown')
        except Exception as error:
            node.recorder.emit('shutdown_zero_error', detail=str(error), traceback=traceback.format_exc())
        finally:
            executor.shutdown(timeout_sec=2)
            thread.join(timeout=2)
            node.host_monitor.close()
            node.recorder.emit('run_end', state=node.controller.state(), logging=node.recorder.status())
            node.recorder.close()
            node.destroy_node()
            rclpy.shutdown()


if __name__ == '__main__':
    main()
