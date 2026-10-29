import math
import unittest

from carl_joystick.controller import Controller, mix


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.time = 0.0
        self.commands, self.estops = [], []
        self.c = Controller(lambda *w: self.commands.append(w), self.estops.append,
                            now=lambda: self.time)
        self.owner = object()
        self.c.claim(self.owner)
        self.healthy()

    def healthy(self):
        self.c.heartbeat = (self.time, False)
        self.c.throttle_status = (self.time, False)

    def control(self, x=0, y=0, deadman=False):
        return self.c.handle(self.owner, dict(type='control', x=x, y=y, deadman=deadman))

    def drive(self):
        self.control()
        self.control(y=1, deadman=True)
        self.c.tick()
        self.assertEqual(self.commands[-1], (0.4, 0.4))

    def test_directions_and_limits(self):
        self.assertEqual(mix(0, 1), (0.4, 0.4))
        self.assertEqual(mix(0, -1), (-0.4, -0.4))
        self.assertEqual(mix(1, 0), (0.4, -0.4))
        self.assertEqual(mix(-1, 0), (-0.4, 0.4))
        self.assertEqual(mix(1, 1), (0.4, 0))
        self.assertEqual(mix(0.04, -0.04), (0, 0))
        for x in (-1, -0.5, 0, 0.5, 1):
            for y in (-1, -0.5, 0, 0.5, 1):
                self.assertTrue(all(abs(v) <= 0.4 for v in mix(x, y)))

    def test_straight_center_band_never_zeroes_one_side(self):
        for y in (-1, 1):
            for x in (-0.05, -0.049, 0, 0.049, 0.05):
                self.assertEqual(mix(x, y), (y * 0.4, y * 0.4))
            for x in (-0.051, 0.051):
                left, right = mix(x, y)
                self.assertGreater(left * y, 0.39)
                self.assertGreater(right * y, 0.39)
                self.assertNotEqual(left, right)

    def test_each_input_publishes_new_command_without_replaying_previous_wheels(self):
        self.drive()
        start = len(self.commands)
        self.control(x=0.25, y=1, deadman=True)
        self.assertEqual(self.commands[start:], [mix(0.25, 1)])
        start = len(self.commands)
        self.control()
        self.assertEqual(self.commands[start:], [(0, 0)])

    def test_neutral_required_on_connect_and_release_stops_immediately(self):
        self.control(y=1, deadman=True)
        self.assertFalse(self.c.active)
        self.drive()
        self.control()
        self.assertEqual(self.commands[-1], (0, 0))
        self.assertFalse(self.c.active)

    def test_watchdog_stops_even_when_no_further_messages_arrive(self):
        self.drive()
        self.time = 0.25
        self.c.tick()
        self.assertEqual(self.commands[-1], (0, 0))
        self.control(y=1, deadman=True)
        self.assertFalse(self.c.active)
        self.drive()

    def test_late_packet_cannot_revive_expired_input_before_timer_tick(self):
        self.drive()
        self.time = 0.3
        self.control(y=1, deadman=True)
        self.assertFalse(self.c.active)

    def test_single_owner_and_obsolete_disconnect(self):
        other = object()
        self.drive()
        self.assertFalse(self.c.claim(other))
        self.c.release(other)
        self.assertTrue(self.c.active)
        self.c.release(self.owner)
        self.assertEqual(self.commands[-1], (0, 0))
        self.assertTrue(self.c.claim(other))
        self.c.release(self.owner)
        self.assertIs(self.c.owner, other)

    def test_telemetry_missing_stale_and_faulted(self):
        for attr, value in [('heartbeat', None), ('throttle_status', None),
                            ('heartbeat', (0, True)), ('throttle_status', (0, True))]:
            self.healthy()
            self.drive()
            setattr(self.c, attr, value)
            self.c.tick()
            self.assertFalse(self.c.active)
            self.assertEqual(self.commands[-1], (0, 0))
        self.healthy()
        self.drive()
        self.time = 1.01
        self.control()
        self.control(y=1, deadman=True)
        self.assertFalse(self.c.active)

    def test_estop_and_reset_require_neutral_and_firmware_clear(self):
        self.drive()
        self.c.handle(self.owner, {'type': 'estop'})
        self.assertEqual(self.estops, [1])
        self.assertEqual(self.commands[-1], (0, 0))
        self.control()
        self.control(y=1, deadman=True)
        self.assertFalse(self.c.active)
        self.c.heartbeat = (self.time, True)
        self.c.handle(self.owner, {'type': 'reset_estop'})
        self.assertEqual(self.estops, [1, 0])
        self.assertFalse(self.c.ready())
        self.healthy()
        self.control(y=1, deadman=True)
        self.assertFalse(self.c.active)
        self.drive()

    def test_invalid_input_stops_and_never_publishes_nonfinite_outputs(self):
        for value in (None, '1', True, math.nan, math.inf, -math.inf, 2):
            self.drive()
            with self.assertRaises(ValueError):
                self.control(x=value, deadman=True)
            self.assertEqual(self.commands[-1], (0, 0))
        for packet in ([], None, {'type': 'mode'},
                       {'type': 'control', 'x': 0, 'y': 1, 'deadman': 'false'}):
            self.drive()
            with self.assertRaises(ValueError):
                self.c.handle(self.owner, packet)
            self.assertFalse(self.c.active)

    def test_idle_does_not_keep_republishing_zero(self):
        self.control()
        count = len(self.commands)
        for _ in range(20):
            self.c.tick()
        self.assertEqual(len(self.commands), count)

    def test_invalid_limits(self):
        for kwargs in ({'max_throttle': 0}, {'max_turn': math.nan},
                       {'input_timeout': 0.3}, {'max_throttle': 2}):
            with self.assertRaises(ValueError):
                Controller(lambda *w: None, lambda e: None, **kwargs)


if __name__ == '__main__':
    unittest.main()
