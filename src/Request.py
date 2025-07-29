from enum import Enum
from typing import Callable, Optional, Union
import asyncio
import json
import random
import time

import httpx

from src.Config import TaskConfig, RequestConfig


class RequestState(Enum):
    """Request worker state."""

    WAITING = "Waiting"
    WORKING = "Working"
    STOPPED = "Stopped"


class RateLimiter:
    """Rate limiter to control requests per second."""

    def __init__(self, max_rps: float):
        self._interval = 1.0 / max(1.0, max_rps)
        self._lock = asyncio.Lock()
        self.last_request_time = 0.0

    async def acquire(self):
        async with self._lock:
            current_time = time.time()
            wait_time = self._interval - (current_time - self.last_request_time)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self.last_request_time = time.time()


class RequestWorker:
    """Individual request worker for concurrent execution."""

    def __init__(
        self,
        task_config: TaskConfig,
        session: httpx.AsyncClient,
        rate_limiter: Optional[RateLimiter] = None,
        response_callback: Optional[Callable[[httpx.Request, Union[httpx.Response, Exception], float], None]] = None,
    ):
        self.task_config = task_config
        self.session = session
        self.rate_limiter = rate_limiter
        self.response_callback = response_callback

        self._task: Optional[asyncio.Task] = None
        self._state = RequestState.WAITING
        self._stop_event = asyncio.Event()

    def get_state(self) -> RequestState:
        """Get current worker state"""
        return self._state

    async def join(self, timeout: Optional[float] = None) -> bool:
        """Wait for task completion with optional timeout"""
        if self._task is None:
            return True

        try:
            await asyncio.wait_for(self._task, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _select_request(self) -> RequestConfig:
        """Select a request based on the policy order"""
        if self.task_config.policy.order == "random":
            return random.choice(self.task_config.requests)
        else:
            # Fallback to first request if order is not supported
            return self.task_config.requests[0]

    def _prepare_request_data(self, request_config: RequestConfig) -> Optional[str]:
        """Prepare request data for the selected request"""
        if request_config.method.upper() in ["POST", "PUT", "PATCH"]:
            if request_config.data:
                if isinstance(request_config.data, dict):
                    return json.dumps(request_config.data)
                return str(request_config.data)
        return None

    @staticmethod
    def estimate_response_size(response: httpx.Response) -> int:
        """Estimate the total size of request and response"""
        request = response.request
        request_headers_len = len("\r\n".join(f"{k}: {v}" for k, v in request.headers.items()))
        request_body_len = len(request.content) if request.content else 0
        response_headers_len = len("\r\n".join(f"{k}: {v}" for k, v in response.headers.items()))
        response_body_len = len(response.content) if response.content else 0
        return request_headers_len + request_body_len + response_headers_len + response_body_len

    async def _execute_request(self) -> tuple[httpx.Request, Union[httpx.Response, Exception], float]:
        """Execute a single request and return response, time taken, and bytes count"""
        # Select request for this iteration
        request_config = self._select_request()
        post_data = self._prepare_request_data(request_config)

        # Merge default headers with request-specific headers
        merged_headers = {}
        merged_headers.update(self.task_config.prefabs.default_headers)
        merged_headers.update(request_config.headers)

        if post_data is not None:
            request = self.session.build_request("POST", request_config.url, content=post_data, headers=merged_headers)
        else:
            request = self.session.build_request("GET", request_config.url, headers=merged_headers)

        start_time = time.time()

        try:
            self._state = RequestState.WORKING
            response = await self.session.send(request)
        except Exception as e:
            response = e
        finally:
            self._state = RequestState.WAITING

        response_time = time.time() - start_time
        return request, response, response_time

    async def _run(self):
        """Internal run method"""
        try:
            while not self._stop_event.is_set():
                if self.rate_limiter:
                    await self.rate_limiter.acquire()

                if self._stop_event.is_set():
                    break

                request, response, response_time = await self._execute_request()

                # Report statistics if callback is set
                if self.response_callback:
                    self.response_callback(request, response, time.time() - response_time)

                if self._stop_event.is_set():
                    break

        finally:
            self._state = RequestState.STOPPED

    def start(self):
        """Start the worker task"""
        if self._task is None or self._task.done():
            self._state = RequestState.WAITING
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self, timeout: Optional[float] = 10.0):
        """Stop the worker task with optional timeout"""
        self._stop_event.set()
        if self._task and not self._task.done():
            try:
                if timeout is None:
                    # Wait indefinitely
                    await self._task
                else:
                    # Wait with timeout
                    await asyncio.wait_for(self._task, timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    self._task.cancel()
                    await self._task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
        self._state = RequestState.STOPPED

    def __del__(self):
        """Cleanup when worker is destroyed"""
        if self._task and not self._task.done():
            self._task.cancel()
