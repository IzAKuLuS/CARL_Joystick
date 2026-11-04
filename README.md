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
  limit or changes the requested ratio. Default maximum is **0.75 per wheel**
  in forward and reverse. Pure turning retains its **0.4** limit. These are
  normalized wheel commands, not speed in m/s.
- The server applies a signed **quadratic throttle curve** after the 5% deadzone,
  before adding the existing linear steering command. This gives more stick
  travel to low and medium throttle, with maximum straight throttle only at the
  rim. For straight travel `t`, the default request is
  `sign(t) * 0.75 * (max(0, abs(t) - 0.05) / 0.95)^2`.
  At 10%, 25%, 50%, 75%, 95%, and 100% travel, this gives approximately 0.2%,
  3.3%, 16.8%, 40.7%, 67.3%, and 75.0% throttle. Reverse uses the same curve.
  The browser passes nipplejs's normalized displacement directly to the server,
  so the curve is applied once and API clients receive the same response.
  The page displays the server's signed left/right command percentages and its
  configured limit; these are separate from applied-output telemetry.
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
3 s application timeout reclaims ownership sooner. The controls app subscribes only to encoder feedback, throttle status, and
heartbeat. It requests 10 Hz telemetry with a one-sample rosbridge queue; this reduces Wi-Fi traffic
without reducing ROS publishers or joystick input frequency. The hardware bridge
keeps only one queued throttle command. Firmware watchdogs are unchanged.

The October field report used a 40% nominal joystick wheel limit; the default is
now 75%. No velocity topic or 5 mph limiter was created: wheel-distance/timing
calibration is missing, and coasting cannot enforce downhill speed. See the sibling firmware
repository's `docs/field-test-2026-10-25.md` for the assessment and rollout checks.

The 75% limit caps joystick wheel **commands**. Existing firmware rear RPM
balancing can trim/boost actual rear motor PWM (up to 10 percentage points above
the nominal command); monitor throttle-status applied outputs for that feedback.
The firmware's existing 2% output deadband is unchanged, so straight motor PWM
starts at about 20.3% stick travel with the default quadratic curve (including
bridge per-mille rounding). Smaller accepted commands remain visible in the page
but are below the motor deadband. The 5% input deadzone is retained to reject
center jitter; no minimum-output boost or delayed release is added. Mechanical
breakaway, vehicle speed, and hill-climbing ability depend on load and require CARL testing;
this change does not guarantee a particular climb angle.

Rebuild `carl_joystick`, restart its server, and reload the controls page to deploy
these changes. Existing custom YAML files must also set `max_throttle: 0.75` if
they still override it with 0.4. No firmware flash is required for these changes.

Limits, throttle shaping and TLS paths are in `src/carl_joystick/config/joystick.yaml`.
`throttle_exponent` defaults to `2.0`; use a floating-point value from `1.0`
(previous linear response) through `3.0` (cubic). Larger values soften low/mid
throttle and increase stick travel before the firmware deadband is cleared.
Changing the exponent does not change the configured maximum or pure turning.
The configured exponent is included in `/health`, WebSocket state and run logs.
To use a custom YAML file, pass `config:=/absolute/path/joystick.yaml`. Keep the node key
`carl_joystick`. `input_timeout` must stay between 0.05 and 0.25 seconds; wheel
limits must be in `(0, 1]`. HTTPS controls pages require a trusted `wss://`
endpoint, using `ssl_certfile` and `ssl_keyfile` on this server (and TLS for
rosbridge). This service assumes a trusted vehicle network. The controls app's
existing browser passcode is not server-side WebSocket authentication.

## Test-run recording and disconnect investigation

Recording starts automatically with each joystick server process. The startup
message prints the run directory, normally `~/.ros/carl_test_runs/<run-id>/`.
The files are newline-delimited JSON (`events-000001.jsonl`, etc.). The controls
page also records browser diagnostics and has a **Download browser log** button.
Use **Add note to log** to mark a maneuver, observed issue, or flashed firmware
build identifier; the current firmware telemetry does not report its build ID.
Rebuild both ROS packages (`carl_bridge` and `carl_joystick`), restart those
processes, and reload the controls page to enable all layers. Existing firmware
and message formats are unchanged.

### What a run contains

- A startup manifest: host, process, Python/ROS environment, operating limits,
  optional operator `test_label`, rosbag configuration, and SHA-256 hashes of the
  joystick/server and served controls sources. Bridge summaries carry transport
  configuration and bridge source hashes, including when the bridge started
  before the joystick recorder subscribed.
- Every received command's axes, deadman, type and correlation metadata; handling
  result and rejection; every ROS wheel publication attempt and completion;
  every ESTOP/reset publication, including requests made while idle. Lease tokens
  are excluded. Repeated timer refreshes are distinguished from new inputs.
- Stop decisions: previous wheel commands, whether control was active, stop
  reason, input age and blocking reason. These precede the zero-publication
  attempt so a publication failure is distinguishable from a successful call.
- Complete received heartbeat flags/mode/counter, throttle state/fault bits and
  applied outputs; encoder counts, steering feedback, IMU and temperature,
  firmware IMU diagnostic strings, and received `/rosout` messages. Telemetry
  records include receive intervals and original ROS stamps where available.
  ROS queue delivery can drop samples; this is a record of what the recorder
  received, not a guarantee of every firmware sample.
- Observed ROS throttle/ESTOP topics (including other command sources), command
  publisher names/GIDs, per-message publisher GIDs and DDS timestamps/sequence
  metadata, subscriber/publisher identities/counts, and per-topic freshness.
  There is no new command-source arbitration or firmware acknowledgement.
- WebSocket attempts, admission/rejection, close codes, timeout phase
  (`receive` versus blocked `send`), packet intervals, processing/send duration,
  exceptions, and browser/server packet correlation. A completed send means the
  server handed off a reply; the browser's reply event establishes receipt.
- Browser send/reply events and round trips; touch/release and operator buttons;
  reconnects, close/error events, offline/online and network-change signals,
  page visibility/focus/cancellation, and browser timer delays. Browser Network
  Information fields are recorded only when supported. An independent HTTP
  `/health` request every five seconds checks whether the same server is
  reachable even when the command WebSocket is failing.
- Server event-loop and ROS control-timer delays; Linux interface carrier/state,
  byte/error/drop counters, default routes and available Wi-Fi signal data;
  bridge TX frames, RX/TX totals, decoded-frame counts/ages, CRC/noise discard
  counters, last corrupt-frame prefix and transport errors. CPU/load, memory and
  available disk space are sampled too.
  Diagnostics never initiate extra throttle or ESTOP commands.

Every server record has `schema`, `run_id`, increasing `seq`, UTC, `wall_ns`,
`mono_ns`, `event`, `data`, and `dropped_total`. The writer adds
`write_mono_ns` and `write_lag_ms` to expose recorder backlog. Sessions have separate IDs;
commands have a per-session `packet_seq`. Updated clients echo `client_id` and
`client_seq` after diagnostics negotiation. Correlate a browser send, server
receive/result/publication and browser reply by those IDs, **not by subtracting
browser and server monotonic clocks**. Browser clocks are independent; original
browser event times are retained inside `browser_event` even when uploaded much
later. Duplicate browser uploads retain stable event IDs and are deduplicated by
the review tool. Older compatible protocol-v2 clients still work with reduced
browser evidence.

### Distinguishing failures

| Evidence around a stop | Supported interpretation |
| --- | --- |
| Browser offline event, HTTP probe failure, then server input timeout; firmware heartbeat continues | Device/network-path interruption is plausible; browser offline is a hint, not proof of AP or internet failure. |
| WebSocket fails but browser HTTP health succeeds | Server is reachable over HTTP; investigate the WebSocket channel, stale ownership, browser or application behavior. |
| Browser pagehide/visibility or a large browser timer gap precedes input loss | Browser lifecycle/scheduling contributed; inspect whether it intentionally disconnected or stopped sending. |
| Inputs keep arriving but heartbeat/status ages grow; bridge RX or decoded-frame counts stop | Vehicle/bridge/ROS feedback path is failing independently of the browser command path. |
| Bridge RX bytes continue but parsed-frame count stops | Data is arriving but is not producing valid framed messages; inspect transport/framing/firmware. |
| Bridge reports USB/socket errors, or ROS publisher disappears | Direct evidence of a hardware transport error or process/discovery loss. |
| Server event-loop/control timer stalls while other records continue | Companion scheduling/process stall is plausible; this does not establish a Wi-Fi outage. |
| Applied outputs or encoders disagree with accepted wheel commands | Inspect firmware state, faults, balancing and physical behavior; publishing a command does not prove execution. |

The joystick uses the local device-to-server WebSocket; internet access is not
required after its external page assets have loaded. A failed internet connection
and a failed local vehicle link are different observations. For an independent
**server-side** internet check, set `internet_probe_url` in the joystick YAML to
a trusted HTTP(S) connectivity endpoint. It is disabled by default (no external
probe traffic); when enabled, a worker makes a request every five seconds with a
one-second network timeout. Probe failures can also mean DNS, target or filtering
failures. They do not prove that the phone lost internet. Exact AP/router/WAN
attribution can still require access-point/router logs or a packet capture. A
1006 close alone never proves a particular root cause.

### Storage, failure visibility and browser recovery

The server queues records without doing disk writes on control callbacks. A
worker flushes/syncs at approximately one-second intervals under traffic and at
rotation/shutdown. Default capacity is 32 segments of 32 MiB per run: the first
segment (manifest) and the most recent 31 are retained. Rotation removes older
middle segments; sequence gaps expose the omitted interval. Runs are not deleted
automatically, so archive/remove old runs and size storage before long testing.
A hard power loss can lose the unsynced tail. A missing `run_end` indicates an
unclean/incomplete recording, not proof of a specific crash.

A full 8192-record queue drops new records rather than waiting for disk;
`dropped_total` and runtime snapshots expose that loss. Disk/full/permission
failures report a ROS error and appear in `/health` under `logging`, including
writer status. Bridge diagnostic delivery uses best-effort QoS so it does not
require reliable diagnostic acknowledgements on the hardware command path. If logging fails, driving/watchdogs retain their existing behavior;
recording completeness is not guaranteed. Check `/health` before a test.

The browser stores up to 20,000 events in IndexedDB for the controls origin,
keeps a bounded memory copy, and uploads batches separately from commands.
Offline evidence is sent after recovery; retained evidence is replayed after
reload. **Export the browser log after each test**, especially if reconnection
never recovers. The local copy remains after a successful upload. Storage
unavailability/quota errors and upload backlog appear beside the download
button. Private browsing, clearing site data, switching origins, or exceeding
the rolling limit can remove evidence; dropped upload events are counted.
Persistence occurs on the next approximately one-second flush, so abrupt page
termination can lose recent events. Upload acknowledgement means enqueueing,
not a crash-proof disk commit. No log-upload success is a drive acknowledgement.

### Running and reviewing a test

Optionally set `test_label`, `log_dir`, `log_max_mb`, `log_max_files`, and
`internet_probe_url` in `src/carl_joystick/config/joystick.yaml` (or a custom YAML).
For native ROS replay as well as JSONL, the launch file supports a separate bag
recorder process:

```bash
ros2 launch carl_joystick joystick.launch.py \
  controls_dir:=/home/izakulus/Documents/AVL/CARL_Controls_Application/src \
  record_rosbag:=true bag_dir:=/absolute/path/to/new-test-bag
```

The bag covers the commands, ESTOP, feedback, IMU/steering, bridge diagnostics
and `/rosout` topics above. It does not contain browser events. Its configured
path is included in the JSON manifest; verify the bag process started in the
launch output. Bags have their own disk usage and are not rotated by the JSONL
recorder. Stop the launch cleanly so the bag can finish indexing.

After a test, copy the whole run directory, optional bag, and downloaded browser
JSONL together. Generate a summary and CSV timeline without ROS or motors:

```bash
cd /home/izakulus/Documents/AVL/CARL_Joystick
PYTHONPATH=src/carl_joystick python3 -m carl_joystick.review_run \
  /absolute/path/to/run-directory --timeline /absolute/path/to/timeline.csv \
  --browser-log /absolute/path/to/downloaded-browser.jsonl
```

After rebuilding, `ros2 run carl_joystick carl_log_review` accepts the same
arguments. The summary counts active-control stop reasons, connection/browser
events, missing record sequences, reported drops and clean shutdown. The CSV
contains control transitions, stops, exceptions, connection and browser lifecycle
events, heartbeat/throttle changes and encoder-activity transitions in server
record order. `--browser-log` is optional and repeatable; duplicate uploaded
events are removed, and events explicitly tagged with a different run ID are
counted separately as historical evidence. Uploaded browser rows are ordered by **upload receipt**;
standalone downloaded rows have empty server-time columns and follow the server rows;
use their nested original event times and sequence numbers for browser order.
Full-rate telemetry/commands remain in JSONL and in the optional bag. Inspect
encoder changes and applied-output telemetry to assess movement: `active=true`
only means the joystick accepted control, and zero output can still mean coasting.

## Verify without motors

```bash
cd /home/izakulus/Documents/AVL/CARL_Joystick
PYTHONPATH=src/carl_joystick python3 -m unittest discover -s src/carl_joystick/test -v
cd /home/izakulus/Documents/AVL/CARL_Controls_Application
node tests/joystick-client.test.js
node tests/joystick-diagnostics.test.js
node tests/joystick-ui.test.js
node tests/ros-client.test.js
```

Controller tests cover direction, per-wheel limits, deadman release, monotonic
watchdog, telemetry gating, malformed input, exclusive ownership, and e-stop
reset. Browser tests cover acknowledgement pacing, timeouts, neutral startup,
release, reconnect and rejected sessions. Hardware direction, output and stopping
behavior still require bench verification with wheels lifted before driving.

`abs(horizontal) <= 0.05` requests equal wheel commands; full vertical requests
+/-0.75 on every wheel at the default limit. Tests cover the inclusive +/-0.05
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

The integration suite also verifies lifecycle/error persistence, uploaded browser
events and a native ROS bag containing simulated commands and feedback. It runs
actual joystick and bridge nodes with an in-memory
transport on isolated domain 213. It verifies signed per-mille wheel ordering,
the independent input watchdog, disconnect cleanup, and firmware e-stop/reset
wire frames. A second test starts a localhost WebSocket server and exercises
the actual `CARL_Controls_Application/src/joystick-client.js` against it, covering
forward/reverse, both turns, combined forward/turn limits, release, competing
controllers, e-stop/reset, unexpected disconnect and neutral reconnect. Override
the controls source directory with `CARL_CONTROLS_DIR=/absolute/path/to/src` if
the checkout is elsewhere. No serial or UDP hardware transports are opened.

Throttle-curve validation also runs the actual UI script and browser transport
together, injecting the pinned nipplejs displacement contract from center to rim
in both directions. It verifies percentages, the 5% center band, bridge per-mille
outputs for quadratic light/intermediate/full throttle, the 75% cap, release and recovery.
The firmware host suite additionally checks those quantized commands against
actual throttle-node PWM and telemetry, including its unchanged 2% deadband.

Quadratic-curve validation on November 4, 2026 passed without skips: 45 joystick
unit/API tests, all five controls-browser test files, 50 bridge tests, five
keyboard-teleop tests, seven firmware host regressions with undefined-behavior
checks, and five ROS integration tests. The integration checks include the actual
UI/client over localhost, quadratic intermediate commands in both directions,
combined steering, command quantization, release/watchdog/e-stop/reset, ROS
parameter overrides, recording, and a 65-second held-input soak after impaired
link recovery. Clean temporary builds of `carl_msgs`, `carl_bridge`, and
`carl_joystick` passed; the installed YAML includes `throttle_exponent: 2.0`.
All vehicle transports were simulated and no board was flashed. Physical
breakaway and low-speed handling still require lifted-wheel and controlled
ground checks on CARL.

Earlier linear-curve validation on November 3, 2026 passed without skips: 23 joystick
controller/API/lease tests, 37 controls browser tests, 51 bridge/teleop tests
(plus 35 subtests), seven firmware host regressions with undefined-behavior
checks, and both ROS/WebSocket integration tests including a 65-second held-input
soak after impaired-link recovery. The passenger GUI passed 33 data/reliability
checks, type checking and lint, including signed 75% applied-output telemetry.
Clean temporary builds of `carl_msgs`, `carl_bridge`, and `carl_joystick` passed;
the installed joystick YAML and defaults agree on 75% throttle / 40% turning.
No hardware transport was opened and no board was flashed. Lifted-wheel checks
of breakaway, direction, release and e-stop must precede a controlled ground test;
incline performance has not been measured by this software validation.

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
