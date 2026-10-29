"""Bound the age of drive/reset requests without trusting a browser clock."""
import secrets
import time


class CommandLease:
    def __init__(self, timeout, now=time.monotonic):
        self.timeout, self.now = timeout, now
        self.token = None
        self.issued_at = None

    def issue(self):
        self.token = secrets.token_hex(8)
        self.issued_at = self.now()
        return self.token

    def consume(self, token):
        valid = (self.token is not None and isinstance(token, str)
                 and token.isascii()
                 and secrets.compare_digest(token, self.token)
                 and 0 <= self.now() - self.issued_at < self.timeout)
        # A token is single-use, including rejected requests.
        self.token = None
        return valid
