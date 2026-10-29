"""Publish CARL wheel commands and e-stop requests from the joystick server."""
import threading

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSProfile
from rclpy.signals import SignalHandlerOptions
import uvicorn

from carl_msgs.msg import CarlEstop, CarlHeartbeat, CarlThrottleCmd, CarlThrottleStatus
from carl_joystick.controller import Controller
from carl_joystick.server import create_app


class WebUINode(Node):
    def __init__(self):
        super().__init__('carl_joystick')
        for name, value in [('web_port', 8001), ('controls_dir', ''),
                            ('ssl_certfile', ''), ('ssl_keyfile', ''),
                            ('max_throttle', 0.4), ('max_turn', 0.4),
                            ('input_timeout', 0.25)]:
            self.declare_parameter(name, value)
        self.lock = threading.RLock()
        # Volatile, depth one: late subscribers never get a latched drive command.
        self.command_pub = self.create_publisher(CarlThrottleCmd, '/carl/throttle_cmd', QoSProfile(depth=1))
        self.estop_pub = self.create_publisher(CarlEstop, '/carl/estop', QoSProfile(depth=1))
        self.controller = Controller(
            self.publish_command, self.publish_estop,
            on_event=self.get_logger().info,
            **{name: self.get_parameter(name).value
               for name in ('max_throttle', 'max_turn', 'input_timeout')})
        self.create_subscription(CarlHeartbeat, '/carl/heartbeat', self.on_heartbeat, 1)
        self.create_subscription(CarlThrottleStatus, '/carl/throttle_status', self.on_status, 1)
        # A simulation clock pause must never pause the input watchdog.
        self.create_timer(0.05, self.tick, clock=Clock(clock_type=ClockType.STEADY_TIME))

    def publish_command(self, left, right):
        msg = CarlThrottleCmd()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.front_left = msg.rear_left = float(left)
        msg.front_right = msg.rear_right = float(right)
        self.command_pub.publish(msg)

    def publish_estop(self, reason):
        msg = CarlEstop()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.reason = reason
        self.estop_pub.publish(msg)

    def on_heartbeat(self, msg):
        with self.lock:
            self.controller.heartbeat = (self.controller.now(), bool(msg.flags & 2 or msg.mode == 2))
            if not self.controller.ready() and self.controller.active:
                self.controller.stop(self.controller.blocked_reason())

    def on_status(self, msg):
        with self.lock:
            self.controller.throttle_status = (self.controller.now(), bool(msg.faults or msg.state not in (1, 2)))
            if not self.controller.ready() and self.controller.active:
                self.controller.stop(self.controller.blocked_reason())

    def tick(self):
        with self.lock:
            self.controller.tick()



def main(args=None):
    # Uvicorn owns process signals, so ROS remains available for final zero.
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = WebUINode()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        app = create_app(node, node.get_parameter('controls_dir').value)
        ssl = {name: node.get_parameter(name).value
               for name in ('ssl_certfile', 'ssl_keyfile') if node.get_parameter(name).value}
        uvicorn.run(app, host='0.0.0.0', port=node.get_parameter('web_port').value,
                    ws='websockets', ws_max_size=1024, ws_max_queue=1,
                    ws_per_message_deflate=False, ws_ping_interval=5.0,
                    ws_ping_timeout=10.0, **ssl)
    finally:
        with node.lock:
            node.controller.stop()
        executor.shutdown(timeout_sec=2)
        thread.join(timeout=2)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
