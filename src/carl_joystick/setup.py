from glob import glob
from setuptools import find_packages, setup

setup(
    name='carl_joystick', version='0.1.0', packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/carl_joystick']),
        ('share/carl_joystick', ['package.xml', 'LICENSE']),
        ('share/carl_joystick/launch', glob('launch/*.launch.py')),
        ('share/carl_joystick/config', glob('config/*.yaml')),
    ],
    install_requires=['setuptools', 'fastapi>=0.100', 'uvicorn[standard]>=0.22'],
    extras_require={'test': ['httpx']},
    maintainer='AV Lab', maintainer_email='avlab@cpp.edu', license='MIT',
    description='CARL WebSocket joystick to per-wheel ROS commands',
    entry_points={'console_scripts': ['webui_node = carl_joystick.webui_node:main']},
)
