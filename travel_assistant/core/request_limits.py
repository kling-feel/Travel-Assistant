"""Small single-process abuse guard for the one-worker cloud demonstration."""

from collections import defaultdict, deque
from contextlib import contextmanager
from threading import Lock
from time import monotonic


class RequestLimits:
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.lock = Lock()
        self.login_attempts = deque()
        self.login_failures = defaultdict(deque)
        self.turn_attempts = defaultdict(deque)
        self.turn_active = defaultdict(int)

    @staticmethod
    def _trim(events, cutoff):
        while events and events[0] <= cutoff:
            events.popleft()

    def allow_login(self, username):
        now = self.clock()
        with self.lock:
            self._trim(self.login_attempts, now - 60)
            if len(self.login_failures) >= 1024:
                for key, old in list(self.login_failures.items()):
                    self._trim(old, now - 900)
                    if not old:
                        del self.login_failures[key]
            failures = self.login_failures[username.casefold()]
            self._trim(failures, now - 900)
            if len(self.login_attempts) >= 80 or len(failures) >= 8:
                return False
            self.login_attempts.append(now)
            return True

    def login_result(self, username, success):
        with self.lock:
            failures = self.login_failures[username.casefold()]
            if success:
                failures.clear()
            else:
                failures.append(self.clock())

    def acquire_turn(self, traveler_id):
        now = self.clock()
        with self.lock:
            attempts = self.turn_attempts[traveler_id]
            self._trim(attempts, now - 60)
            if len(attempts) >= 20 or self.turn_active[traveler_id] >= 2:
                return False
            attempts.append(now)
            self.turn_active[traveler_id] += 1
            return True

    def release_turn(self, traveler_id):
        with self.lock:
            if self.turn_active[traveler_id] <= 1:
                self.turn_active.pop(traveler_id, None)
            else:
                self.turn_active[traveler_id] -= 1

    @contextmanager
    def turn(self, traveler_id):
        allowed = self.acquire_turn(traveler_id)
        try:
            yield allowed
        finally:
            if allowed:
                self.release_turn(traveler_id)


cloud_limits = RequestLimits()
