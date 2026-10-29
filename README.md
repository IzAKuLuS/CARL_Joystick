# CARL Joystick

Touch joystick server for CARL, publishing signed per-wheel throttle commands
and e-stop requests through the existing CARL ROS bridge.

The user interface lives in the sibling `CARL_Controls_Application/src` checkout.
Telemetry continues to use rosbridge independently of joystick commands.

```text
Controls app / Joystick -> WebSocket :8001/ws -> carl_joystick
  -> /carl/throttle_cmd + /carl/estop -> existing carl_bridge
  -> STM32 binary framing -> CAN -> Teensy throttle firmware
```

No firmware changes are required. This implements CARL's current skid-steer drive.
Positive wheel values mean forward; firmware already inverts the right motor
wiring. Do not invert it again in the joystick server.

## Build and run

Use the same ROS distribution, domain and middleware for all processes. These
examples use this machine's ROS 2 Lyrical installation and sibling checkout paths.
Install the server dependencies with your OS package manager or into a virtual
environment using the same Python version as ROS:

```bash
sudo apt install python3-fastapi python3-uvicorn python3-websockets
source /opt/ros/lyrical/setup.bash
cd /home/izakulus/Documents/AVL/AVL_CARL/ros2_ws
colcon build --symlink-install --packages-select carl_msgs carl_bridge
source install/setup.bash
cd /home/izakulus/Documents/AVL/CARL_Joystick
colcon build --symlink-install --packages-select carl_joystick
source install/setup.bash
```

This workspace contains only `carl_joystick`; a plain `colcon build --symlink-install`
is equivalent to selecting that package explicitly.

Start **one** CARL hardware bridge in a sourced terminal, unless bringup already
runs it. Its serial settings remain in `AVL_CARL`'s `carl_bridge/config/bridge.yaml`:

```bash
ros2 launch carl_bridge bridge.launch.py
```

`ros2 launch carl_bringup carl.launch.py` is the normal top-level alternative;
it includes this same bridge launch. Run either command, never both. Browser
joystick mode publishes wheel commands itself, so do not run keyboard/gamepad
`carl_teleop` at the same time.

In another terminal, source ROS and both workspace overlays, then launch:

```bash
source /opt/ros/lyrical/setup.bash
source /home/izakulus/Documents/AVL/AVL_CARL/ros2_ws/install/setup.bash
source /home/izakulus/Documents/AVL/CARL_Joystick/install/setup.bash
ros2 launch carl_joystick joystick.launch.py \
  controls_dir:=/home/izakulus/Documents/AVL/CARL_Controls_Application/src
```

This serves the controls application at `http://<server-IP>:8001` and joystick
WebSocket at `ws://<server-IP>:8001/ws`. Omit `controls_dir` to run only the API
if the controls app is served separately. Override the port with `web_port:=8001`.
The `/health` endpoint reports command readiness without claiming a controller.

For live telemetry in the page, also run rosbridge in a terminal sourced with
ROS and the CARL firmware workspace overlay:

```bash
ros2 launch rosbridge_server rosbridge_websocket_launch.xml
```

Open the controls app, sign in, choose **Joystick**, then press **Connect joystick**.
The page remembers the joystick endpoint separately from rosbridge. On a phone,
use the server's LAN IP for both connections. The default joystick port is 8001
and telemetry port is 9090. After a custom port change, update the joystick
address in the page. The existing `http.server` workflow on port 8000 also works.

## Driving behavior

- Touch and hold the joystick to drive. Up/down requests forward/reverse;
  right requests a clockwise turn (`left = forward + turn`,
  `right = forward - turn`). Front/rear outputs on each side match.
- A 5% axis deadzone is rescaled over the remaining travel. Both sides are
  normalized together so combining forward and turn never exceeds the wheel
  limit or changes the requested ratio. Default maximum is **0.4 per wheel**,
  a normalized motor output, not a speed in m/s.
- Healthy heartbeat and throttle status must both have arrived within one
  second. Firmware e-stop, throttle fault, startup or lost feedback prevents
  drive and requires releasing the joystick before another attempt.
- Commands refresh at 20 Hz. The server uses a monotonic 250 ms input watchdog,
  checked every 50 ms independently of the WebSocket loop and ROS simulation
  clock. It commands zero on timeout, release, malformed input, lost readiness,
  disconnect, and shutdown. Timeout detection can take up to one timer period
  after the threshold. The STM32's existing 300 ms watchdog remains in effect
  if this process or the ROS bridge stops.
- The browser allows one unacknowledged packet. After 200 ms without a reply it
  cancels held movement and returns to neutral when replies recover, retaining
  the connection. A three-second reply loss triggers automatic reconnection
  with 0.5–5 second backoff. Each reconnect begins neutral and requires a fresh
  touch. Idle sessions remain connected. Hiding the page disconnects; returning
  restores a previously requested connection at neutral. Explicit Disconnect or
  sign-out stops reconnection. Losing focus or cancelling a touch releases input.
- Only one WebSocket controller is admitted at once. Other connections receive
  `code=controller_busy` and retry without stopping or replacing its session.
  Silent receive or blocked reply operations expire after three seconds, freeing
  a half-open phone session; healthy idle clients continue neutral keepalives.
  There is no arbitration
  with other ROS publishers: stop keyboard/gamepad teleop and other command
  sources before using this mode.
- **E-STOP** sends `CarlEstop.reason=1`. **Reset e-stop** sends zero wheel outputs
  and then `reason=0`, matching the current master's clear-latch behavior. Reset
  never restores the previous joystick position. These controls require the
  joystick connection; the physical e-stop remains independent.
- Zero motor output coasts CARL; this is not active braking.

### Weak-link recovery (October 25, 2026 field report)

The phone's reported “Input paused” followed by “another client” matches a
half-open session retaining ownership during browser recovery. The updated
client retries occupied sessions at neutral; the server expires silent sessions.
Motion still expires after 250 ms (plus a timer tick); connection cleanup does
not extend that deadline. Sessions log connect/close reasons, and motion stops
log their reason. `/health` adds `blocked_reason`, `last_stop_reason`, and
`stop_count`. Phone diagnostics show round-trip time, pauses and retries.

Protocol v2 replies include `protocol=2` and a fresh single-use `token`. Every
drive and reset request must echo the latest token before `input_timeout`
elapses on the **server's monotonic clock**. Rejected requests command zero,
return `rejection=expired_command`, and require neutral before another drive.
Neutral and e-stop always work without a lease. Tokens prevent buffered old
requests from becoming fresh input merely because TCP delivers them later;
they are not authentication. Update the server and controls scripts together,
restart the server, and reload the phone page. Older browsers cannot drive this
server; the new browser refuses older servers. No firmware flash is required.

The server explicitly uses the websockets backend, a one-message receive queue,
no per-message compression, and 5 s ping / 10 s pong deadlines. The independent
3 s application timeout reclaims ownership sooner. The controls app requests
10 Hz telemetry with a one-sample rosbridge queue; this reduces Wi-Fi traffic
without reducing ROS publishers or joystick input frequency. The hardware bridge
keeps only one queued throttle command. Firmware watchdogs are unchanged.

The 40% nominal joystick wheel limit remains. No velocity topic or 5 mph limiter
was created: wheel-distance/timing calibration and a validated IMU fallback are
missing, and coasting cannot enforce downhill speed. See the sibling firmware
repository's `docs/field-test-2026-10-25.md` for the assessment and rollout checks.

Limits and TLS paths are in `src/carl_joystick/config/joystick.yaml`. To use a
custom YAML file, pass `config:=/absolute/path/joystick.yaml`. Keep the node key
`carl_joystick`. `input_timeout` must stay between 0.05 and 0.25 seconds; wheel
limits must be in `(0, 1]`. HTTPS controls pages require a trusted `wss://`
endpoint, using `ssl_certfile` and `ssl_keyfile` on this server (and TLS for
rosbridge). This service assumes a trusted vehicle network. The controls app's
existing browser passcode is not server-side WebSocket authentication.

## Verify without motors

```bash
cd /home/izakulus/Documents/AVL/CARL_Joystick
PYTHONPATH=src/carl_joystick python3 -m unittest discover -s src/carl_joystick/test -v
cd /home/izakulus/Documents/AVL/CARL_Controls_Application
node tests/joystick-client.test.js
node tests/joystick-ui.test.js
node tests/ros-client.test.js
```

Controller tests cover direction, per-wheel limits, deadman release, monotonic
watchdog, telemetry gating, malformed input, exclusive ownership, and e-stop
reset. Browser tests cover acknowledgement pacing, timeouts, neutral startup,
release, reconnect and rejected sessions. Hardware direction, output and stopping
behavior still require bench verification with wheels lifted before driving.

`abs(horizontal) <= 0.05` requests equal wheel commands; full vertical requests
+/-0.4 on every wheel at the default limit. Tests cover the inclusive +/-0.05
boundary and values just outside it. Each accepted input is now published
immediately, and release publishes zero without first replaying the previous
wheel command. The firmware's rear encoder fallback must not zero an individual
wheel on entry to this center band; see the firmware clone's joystick diagnostics.

FastAPI endpoint tests also run in the Python suite when FastAPI and `httpx`
are installed; otherwise they are reported as skipped. Install the test client
with `sudo apt install python3-httpx` or install the package's test extra with
`python3 -m pip install './src/carl_joystick[test]'` in your ROS-compatible virtual
environment. The runtime dependencies are declared in `setup.py` and
`package.xml`; there is no separate legacy requirements file.

With ROS, the CARL overlay and those test dependencies sourced, verify the
complete command path. This check also requires Node.js 22 or newer for its
native WebSocket client and the sibling controls application checkout:

```bash
cd /home/izakulus/Documents/AVL/CARL_Joystick
ROS_DOMAIN_ID=213 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST \
  PYTHONPATH=src/carl_joystick:$PYTHONPATH \
  python3 src/carl_joystick/test/integration_ros.py -v
```

Set `CARL_SOAK_SECONDS=65` on that command for a held-input soak exceeding the
reported one-minute failure interval. Integration now also injects a delayed
reply, delayed outgoing drive, and a half-open old connection to verify busy
retries, watchdog zero and neutral recovery with the actual browser client.

The integration suite runs actual joystick and bridge nodes with an in-memory
transport on isolated domain 213. It verifies signed per-mille wheel ordering,
the independent input watchdog, disconnect cleanup, and firmware e-stop/reset
wire frames. A second test starts a localhost WebSocket server and exercises
the actual `CARL_Controls_Application/src/joystick-client.js` against it, covering
forward/reverse, both turns, combined forward/turn limits, release, competing
controllers, e-stop/reset, unexpected disconnect and neutral reconnect. Override
the controls source directory with `CARL_CONTROLS_DIR=/absolute/path/to/src` if
the checkout is elsewhere. No serial or UDP hardware transports are opened.

Cleanup validation on October 23, 2026: a clean workspace build discovered only
`carl_joystick`; all 14 controller/API tests, 15 browser transport tests and both
end-to-end integration tests passed without skips. CARL runtime Python semantics
and browser source/test hashes were checked against the pre-cleanup baseline.
This is software validation with simulated firmware traffic; physical motor
direction and stopping behavior remain to be verified on CARL.

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

Every retained source file supports the CARL runtime, its build/install process,
validation or operation. Generated build/install output is ignored. The firmware,
ROS message definitions and hardware bridge are supplied by `AVL_CARL`; browser
pages and client tests are supplied by `CARL_Controls_Application`. They are not
copied into this repository.

The joystick server was derived from AV Lab / Parsa Ghasemi's original
WebSocket application. Its license is retained in the CARL package. Previous
source remains available in Git history.
