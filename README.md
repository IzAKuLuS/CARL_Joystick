# CARL Joystick
> Authored by Isaac Alvarado

Touch joystick server for CARL, publishing signed per-wheel throttle commands
and e-stop requests through the existing CARL ROS bridge.

The user interface lives in the sibling `CARL_Controls_Application/src` checkout.
Telemetry continues to use rosbridge independently of joystick commands.

```text
Controls app / Joystick -> WebSocket :8001/ws -> carl_joystick
  -> /carl/throttle_cmd + /carl/estop -> existing carl_bridge
  -> STM32 binary framing -> CAN -> Teensy throttle firmware
```

## Repository contents

```text
.gitignore                         Generated Python and ROS output exclusions
README.md                          Architecture, setup, behavior and validation
src/carl_joystick/
  carl_joystick/
    __init__.py                    Python package marker
    controller.py                  Mixing, readiness, deadman, ownership, watchdog
    server.py                      WebSocket API and optional controls-page serving
    webui_node.py                  ROS publishers/subscribers and server lifecycle
  config/joystick.yaml             Output limits, input timeout, port and TLS
  launch/joystick.launch.py        ROS launch entry point
  resource/carl_joystick           ROS package-index marker
  package.xml                     ROS dependencies and package metadata
  setup.py                        Python dependencies, installed assets, entry point
  setup.cfg                       ROS executable installation paths
  LICENSE                         Source license
  test/                           CARL controller, API and integration validation
```

The joystick server was derived from Autonomous Systems Laboratory (previously the Autonomous Vehicle Lab) / Parsa Ghasemi's original
WebSocket application. Its license is retained in the CARL package. Previous
source remains available in Git history.
