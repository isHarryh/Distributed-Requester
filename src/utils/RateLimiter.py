from typing import Optional

import asyncio
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_rps: float, max_rpm: float, stop_event: Optional[asyncio.Event] = None):
        self._rps = max_rps
        self._rpm = max_rpm
        self._stop_event = stop_event

        self._hits = deque(maxlen=int(max_rpm) + 1)
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            if self._stop_event and self._stop_event.is_set():
                return
            if not self._rps > 0:
                raise ValueError("RPS must be greater than 0")
            if not self._rpm > 0:
                raise ValueError("RPM must be greater than 0")

            current_time = time.time()
            this_minute_count = 0
            this_second_count = 0
            this_minute_last_hit = 0
            this_second_last_hit = 0

            if len(self._hits):
                while len(self._hits) and self._hits[0] < current_time - 60:
                    self._hits.popleft()
                this_minute_count = len(self._hits)
                this_minute_last_hit = self._hits[0] if self._hits else 0

                this_second_count = 0
                for i in range(len(self._hits) - 1, -1, -1):
                    if self._hits[i] < current_time - 1:
                        break
                    this_second_count += 1
                    this_second_last_hit = self._hits[i]

            wait_time = 0
            if this_minute_last_hit and this_minute_count and this_minute_count >= self._rpm:
                wait_time = 60 - (current_time - this_minute_last_hit)
            if this_second_last_hit and this_minute_count and this_second_count >= self._rps:
                wait_time = max(wait_time, 1 - (current_time - this_second_last_hit))
            if wait_time > 0:
                await asyncio.sleep(wait_time)

            self._hits.append(current_time)
