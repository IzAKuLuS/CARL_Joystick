import unittest

from carl_joystick.session import CommandLease


class CommandLeaseTest(unittest.TestCase):
    def test_expiry_boundary_single_use_rotation_and_clock_reversal(self):
        clock = [0.0]
        lease = CommandLease(0.25, lambda: clock[0])
        self.assertFalse(lease.consume(None))
        token = lease.issue()
        clock[0] = 0.249
        self.assertTrue(lease.consume(token))
        self.assertFalse(lease.consume(token))
        token = lease.issue()
        clock[0] += 0.25
        self.assertFalse(lease.consume(token))
        token = lease.issue()
        self.assertNotEqual(token, lease.issue())
        self.assertFalse(lease.consume(token))
        token = lease.issue()
        clock[0] -= 0.001
        self.assertFalse(lease.consume(token))
        for invalid in (None, False, 3, [], {}, 'é', 'wrong'):
            lease.issue()
            self.assertFalse(lease.consume(invalid))


if __name__ == '__main__':
    unittest.main()
