"""Random failure injection for the mock clients.

Every public method of MockEntraClient and MockHrisClient is wrapped with
@injects_faults. Before the call, a failure_rate roll can make it fail with a
transient 429 or 503. After the call, a
lost_response_rate roll can make it fail.

Both rates default to 0.0, so the mocks behave normally unless asked. Pass a
seeded random.Random to make a run repeatable.
"""

import functools
import random
from typing import Iterable, Optional

from hris_sync.errors import TransientError


class FaultInjection:
    """Mixin: holds the rates and the random source the decorator reads."""

    def _init_faults(self, failure_rate: float, lost_response_rate: float,
                     rng: Optional[random.Random], fault_methods: Optional[Iterable[str]]) -> None:
        for name, rate in (("failure_rate", failure_rate), ("lost_response_rate", lost_response_rate)):
            if not 0.0 <= rate <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1, got {rate}")
        self.failure_rate = failure_rate
        self.lost_response_rate = lost_response_rate
        self.rng = rng or random.Random()
        self.fault_methods = None if fault_methods is None else frozenset(fault_methods)
        self.injected_failures = 0  # how many calls were made to fail, for tests and demos

    def _injected_error(self, when: str, method: str) -> TransientError:
        self.injected_failures += 1
        status = self.rng.choice([429, 503])
        # Real 429s carry Retry-After; keep it short so demos don't crawl.
        retry_after = 1.0 if status == 429 else None
        return TransientError(status, retry_after=retry_after,
                              message=f"injected {when} failure in {method}")


def injects_faults(method):
    @functools.wraps(method)
    def wrapper(self: FaultInjection, *args, **kwargs):
        if self.fault_methods is not None and method.__name__ not in self.fault_methods:
            return method(self, *args, **kwargs)
        if self.failure_rate and self.rng.random() < self.failure_rate:
            raise self._injected_error("transient", method.__name__)
        result = method(self, *args, **kwargs)
        if self.lost_response_rate and self.rng.random() < self.lost_response_rate:
            raise self._injected_error("lost-response", method.__name__)
        return result

    return wrapper
