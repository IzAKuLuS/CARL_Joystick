from pathlib import Path
from datetime import datetime, timezone
import uuid
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    config = str(Path(get_package_share_directory('carl_joystick')) / 'config' / 'joystick.yaml')
    return LaunchDescription([
        DeclareLaunchArgument('config', default_value=config),
        DeclareLaunchArgument('controls_dir', default_value=''),
        DeclareLaunchArgument('web_port', default_value='8001'),
        DeclareLaunchArgument('record_rosbag', default_value='false'),
        DeclareLaunchArgument('bag_dir', default_value=str(
            Path.home() / '.ros' / 'carl_bags' /
            (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]))),
        Node(package='carl_joystick', executable='webui_node', name='carl_joystick',
             parameters=[LaunchConfiguration('config'), {
                 'controls_dir': ParameterValue(LaunchConfiguration('controls_dir'), value_type=str),
                 'web_port': ParameterValue(LaunchConfiguration('web_port'), value_type=int),
                 'record_rosbag': ParameterValue(LaunchConfiguration('record_rosbag'), value_type=bool),
                 'bag_dir': ParameterValue(LaunchConfiguration('bag_dir'), value_type=str),
             }], output='screen'),
        ExecuteProcess(condition=IfCondition(LaunchConfiguration('record_rosbag')),
                       cmd=['ros2', 'bag', 'record', '-o', LaunchConfiguration('bag_dir'), '--topics',
                            '/carl/throttle_cmd', '/carl/estop', '/carl/heartbeat',
                            '/carl/throttle_status', '/carl/encoders', '/carl/imu',
                            '/carl/imu/temperature', '/carl/imu/diagnostics',
                            '/carl/imu/spi_diagnostics', '/carl/steering_angle',
                            '/carl/bridge_diagnostics', '/rosout'], output='screen'),
    ])
