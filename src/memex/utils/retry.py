"""Retry helper with exponential backoff and jitter."""

import random
import time


def call_with_retry(fn, max_retries=3, base_delay=1.0):
    """Call fn() with exponential backoff + jitter.

    Retries up to ``max_retries`` times with delay = base_delay * (2 ** attempt)
    plus uniform jitter of ±50%. Raises the last exception if all retries fail.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                jitter = delay * random.uniform(-0.5, 0.5)
                time.sleep(delay + jitter)
    raise last_exc
