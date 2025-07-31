from enum import Enum
from typing import Callable, Optional, Union
import asyncio
import json
import random
import time

import httpx

from src.Config import TaskConfig, RequestConfig
from src.utils.CustomTransport import AsyncCustomHost, NameSolver
from src.utils.RateLimiter import RateLimiter
from src.utils.ResponseStatus import ResponseStatus, HttpStatus


class RequestState(Enum):
    """Request worker state."""

    WAITING = "Waiting"
    WORKING = "Working"
    STOPPED = "Stopped"


class RequestRecord:
    def __init__(self):
        self._request: Optional[httpx.Request] = None
        self._result: Optional[Union[httpx.Response, Exception]] = None
        self._start_time: Optional[float] = None
        self._end_time: Optional[float] = None

    def timing_start(self):
        self._start_time = time.time()

    def timing_end(self):
        if self._start_time is None:
            raise ValueError("Start time not set")
        self._end_time = time.time()

    def put_request(self, request: httpx.Request):
        self._request = request

    def put_result(self, result: Union[httpx.Response, Exception]):
        self._result = result

    def get_method(self) -> str:
        if self._request is None:
            raise ValueError("Request not set")
        return self._request.method

    def get_url(self) -> str:
        if self._request is None:
            raise ValueError("Request not set")
        return str(self._request.url)

    def get_status(self) -> ResponseStatus:
        if self._result is None:
            raise ValueError("Result not set")
        return ResponseStatus.of(self._result)

    def get_response_content(self) -> bytes:
        if self._result is None or not isinstance(self._result, httpx.Response):
            raise ValueError("Result not set or not a valid response")
        return self._result.content

    def get_exception(self) -> Optional[Exception]:
        if self._result is None:
            raise ValueError("Result not set")
        if isinstance(self._result, Exception):
            return self._result
        return None

    def get_http_status(self) -> HttpStatus:
        if self._result is None:
            raise ValueError("Result not set")
        if not isinstance(self._result, httpx.Response):
            raise ValueError("Result is not a valid HTTP response")
        return HttpStatus.of(self._result)

    def get_duration(self) -> float:
        return self._end_time - self._start_time if self._end_time and self._start_time else 0.0

    def get_request_size(self) -> int:
        if self._request is None:
            raise ValueError("Request not set")
        headers_size = sum(len(k) + len(v) + 4 for k, v in self._request.headers.items())
        body_size = len(self._request.content) if self._request.content else 0
        return headers_size + body_size

    def get_response_size(self) -> int:
        if self._result is None or not isinstance(self._result, httpx.Response):
            raise ValueError("Result not set or not a valid response")
        headers_size = sum(len(k) + len(v) + 4 for k, v in self._result.headers.items())
        body_size = len(self._result.content) if self._result.content else 0
        return headers_size + body_size


class RequestWorker:
    """Individual request worker for concurrent execution."""

    def __init__(
        self,
        task_config: TaskConfig,
        rate_limiter: Optional[RateLimiter] = None,
        response_callback: Optional[Callable[[RequestRecord], None]] = None,
        proxy_provider: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.task_config = task_config
        self.rate_limiter = rate_limiter
        self.response_callback = response_callback
        self.proxy_provider = proxy_provider

        # HTTP client configuration
        self.limits = httpx.Limits(
            max_keepalive_connections=(
                self.task_config.policy.limits.coroutines if self.task_config.policy.reuse_connections else 1
            )
        )
        self.timeouts = self.task_config.policy.timeouts.build()

        # Client management
        self._current_proxy: Optional[str] = None
        self._session: Optional[httpx.AsyncClient] = None

        # Task management
        self._task: Optional[asyncio.Task] = None
        self._state = RequestState.WAITING
        self._stop_event = asyncio.Event()

    async def _ensure_client(self):
        """Ensure we have a valid HTTP client for the current proxy"""
        current_proxy = self.proxy_provider() if self.proxy_provider else None

        # Check if we need to recreate the client
        need_new_client = (
            self._session is None
            or self._session.is_closed
            or (not self.task_config.policy.reuse_connections)
            or (current_proxy != self._current_proxy)
        )

        if need_new_client:
            # Close existing client if any
            if self._session and not self._session.is_closed:
                await self._session.aclose()

            limits = self.limits
            timeout = self.timeouts
            follow_redirects = True
            proxy = current_proxy if current_proxy else None
            transport = None

            # Check if custom host resolution is needed
            if self.task_config.prefabs.override_hosts:
                name_solver = NameSolver(self.task_config.prefabs.override_hosts)
                transport = AsyncCustomHost(name_solver)

            self._session = httpx.AsyncClient(
                limits=limits,
                timeout=timeout,
                follow_redirects=follow_redirects,
                transport=transport,
                proxy=proxy,
            )
            self._current_proxy = current_proxy

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

    async def _execute_request(self) -> RequestRecord:
        """Execute a single request and return response record"""
        # Select request for this iteration
        request_config = self._select_request()
        post_data = self._prepare_request_data(request_config)

        # Merge default headers with request-specific headers
        merged_headers = {}
        merged_headers.update(self.task_config.prefabs.default_headers)
        merged_headers.update(request_config.headers)

        # Ensure we have a valid client for current proxy
        await self._ensure_client()
        assert self._session is not None

        if post_data is not None:
            request = self._session.build_request("POST", request_config.url, content=post_data, headers=merged_headers)
        else:
            request = self._session.build_request("GET", request_config.url, headers=merged_headers)

        record = RequestRecord()
        record.put_request(request)
        record.timing_start()

        try:
            self._state = RequestState.WORKING
            response = await self._session.send(request)
        except Exception as e:
            response = e
        finally:
            self._state = RequestState.WAITING

        record.timing_end()
        record.put_result(response)
        return record

    async def _run(self):
        """Internal run method"""
        try:
            while not self._stop_event.is_set():
                if self.rate_limiter:
                    await self.rate_limiter.acquire()

                if self._stop_event.is_set():
                    break

                record = await self._execute_request()

                # Report statistics if callback is set
                if self.response_callback:
                    self.response_callback(record)

                if self._stop_event.is_set():
                    break

        finally:
            # Clean up HTTP client
            if self._session and not self._session.is_closed:
                await self._session.aclose()
                self._session = None

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

        # Clean up HTTP client
        if self._session and not self._session.is_closed:
            await self._session.aclose()
            self._session = None

        self._state = RequestState.STOPPED

    def __del__(self):
        """Cleanup when worker is destroyed"""
        if self._task and not self._task.done():
            self._task.cancel()
