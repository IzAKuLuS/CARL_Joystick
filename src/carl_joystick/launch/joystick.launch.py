from pathlib import Path
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    config = str(Path(get_package_share_directory('carl_joystick')) / 'config' / 'joystick.yaml')
    return LaunchDescription([
        DeclareLaunchArgument('config', default_value=config),
        DeclareLaunchArgument('controls_dir', default_value=''),
        DeclareLaunchArgument('web_port', default_value='8001'),
        Node(package='carl_joystick', executable='webui_node', name='carl_joystick',
             parameters=[LaunchConfiguration('config'), {
                 'controls_dir': ParameterValue(LaunchConfiguration('controls_dir'), value_type=str),
                 'web_port': ParameterValue(LaunchConfiguration('web_port'), value_type=int),
             }], output='screen'),
    ])
