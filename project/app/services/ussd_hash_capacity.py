"""One scrypt operation per process (~128 MiB), shared across Flask apps/threads.

Admission is non-blocking: no waiting queue. Multiple worker processes require
a deployment-wide worker/memory budget and ingress limits in Stage 2C.2B.
"""

from contextlib import contextmanager
from threading import BoundedSemaphore


_slots = BoundedSemaphore(1)


class UssdHashCapacityError(Exception):
    """Safe operational rejection, never a wrong PIN."""


@contextmanager
def scrypt_slot():
    if not _slots.acquire(blocking=False):
        raise UssdHashCapacityError("USSD PIN operation is unavailable.")
    try:
        yield
    finally:
        _slots.release()
