"""ROS-independent joystick policy. Call under the node's shared lock."""
import math
import time


def axis(value):
    if type(value) not in (float, int) or not math.isfinite(value) or abs(value) > 1:
        raise ValueError('Axes must be finite numbers between -1 and 1')
    return float(value)


def mix(x, y, max_throttle=0.4, max_turn=0.4, deadzone=0.05):
    """Screen right = clockwise; positive output drives each wheel forward.

    Normalize both sides together to preserve curvature and cap *each* wheel
    at max_throttle, including when forward and turn are combined.
    """
    x, y = axis(x), axis(y)
    def dz(value):
        return math.copysign(max(0, abs(value) - deadzone) / (1 - deadzone), value)
    forward, turn = dz(y) * max_throttle, dz(x) * max_turn
    left, right = forward + turn, forward - turn
    scale = max(1.0, abs(left) / max_throttle, abs(right) / max_throttle)
    return left / scale, right / scale


class Controller:
    def __init__(self, publish, estop, *, now=time.monotonic,
                 max_throttle=0.4, max_turn=0.4, input_timeout=0.25,
                 on_event=lambda message: None):
        for value in (max_throttle, max_turn):
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError('Output limits must be in (0, 1]')
        if not math.isfinite(input_timeout) or not 0.05 <= input_timeout <= 0.25:
            raise ValueError('input_timeout must be between 0.05 and 0.25 seconds')
        self.publish, self.estop, self.now = publish, estop, now
        self.on_event = on_event
        self.last_stop_reason = 'startup'
        self.stop_count = 0
        self.max_throttle, self.max_turn = max_throttle, max_turn
        self.input_timeout = input_timeout
        self.owner = None
        self.active = False
        self.neutral_required = True
        self.local_estop = False
        self.heartbeat = self.throttle_status = None
        self.last_input = None
        self.wheels = (0.0, 0.0)

    def ready(self):
        return self.blocked_reason() is None

    def blocked_reason(self):
        now = self.now()
        if self.local_estop:
            return 'estop'
        for name in ('heartbeat', 'throttle_status'):
            value = getattr(self, name)
            if value is None:
                return name + '_missing'
            if not 0 <= now - value[0] <= 1.0:
                return name + '_stale'
            if value[1]:
                return name + '_fault'
        return None

    def stop(self, reason='released'):
        if self.active:
            self.stop_count += 1
            self.last_stop_reason = reason
            self.on_event('Joystick motion stopped: ' + reason)
        elif reason != 'released':
            self.last_stop_reason = reason
        self.active = False
        self.neutral_required = True
        self.wheels = (0.0, 0.0)
        self.publish(0.0, 0.0)

    def claim(self, owner):
        if self.owner is not None:
            return False
        self.owner = owner
        self.stop()
        return True

    def release(self, owner):
        if self.owner is owner:
            self.stop('disconnected')
            self.owner = None

    def handle(self, owner, data):
        if self.owner is not owner:
            raise ValueError('Controller does not own this session')
        try:
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object')
            kind = data.get('type')
            if kind == 'control':
                x, y = axis(data.get('x')), axis(data.get('y'))
                deadman = data.get('deadman')
                if type(deadman) is not bool:
                    raise ValueError('deadman must be a boolean')
                # Check age before accepting a new packet: a stalled client
                # cannot revive an expired command without returning to rest.
                self.check_freshness()
                if not deadman:
                    self.stop()
                    self.neutral_required = x != 0 or y != 0
                elif self.neutral_required or not self.ready():
                    self.stop(self.blocked_reason() or 'neutral_required')
                else:
                    self.wheels = mix(x, y, self.max_throttle, self.max_turn)
                    self.active = True
                    self.last_input = self.now()
                    self.publish(*self.wheels)
            elif kind == 'estop':
                self.stop('estop')
                self.local_estop = True
                self.estop(1)  # CarlEstop.REASON_OPERATOR
            elif kind == 'reset_estop':
                self.stop('reset_estop')  # Clear stored wheels before firmware latch.
                self.estop(0)  # Current master firmware uses reason 0 to reset.
                self.local_estop = False
            else:
                raise ValueError('Unsupported joystick message type')
        except ValueError:
            self.stop('invalid_input')
            raise
        return self.state()

    def check_freshness(self):
        if not self.active:
            return
        reason = self.blocked_reason()
        if self.now() - self.last_input >= self.input_timeout:
            reason = 'input_timeout'
        if reason:
            self.stop(reason)

    def tick(self):
        self.check_freshness()
        if self.active:
            self.publish(*self.wheels)

    def state(self):
        return {'ready': bool(self.ready()), 'active': self.active,
                'neutral_required': self.neutral_required,
                'estop': self.local_estop or bool(self.heartbeat and self.heartbeat[1]),
                'left': self.wheels[0], 'right': self.wheels[1],
                'max_throttle': self.max_throttle,
                'blocked_reason': self.blocked_reason(),
                'last_stop_reason': self.last_stop_reason,
                'stop_count': self.stop_count}
