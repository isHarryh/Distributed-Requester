from enum import StrEnum
from typing import Optional, Union, List
import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from threading import Lock

import httpx

from src.Config import TaskConfig, Config
from src.Request import ResponseStatus, RequestWorker, RateLimiter, RequestState


class PartialStats:
    """Per-second statistics."""

    def __init__(self):
        # Each status maps to a list of response times
        self.stats = {status: [] for status in ResponseStatus}
        self.bytes_down = 0

    def add_result(self, status: ResponseStatus, response_time: float, bytes_count: int):
        self.stats[status].append(response_time)
        self.bytes_down += bytes_count

    def get_total_count(self) -> int:
        return sum(len(times) for times in self.stats.values())

    def get_success_count(self) -> int:
        return len(self.stats[ResponseStatus.SUCCESS])

    def get_failure_count(self) -> int:
        return self.get_total_count() - self.get_success_count()

    def get_sorted_stats(self) -> list:
        result = []
        for status, times in self.stats.items():
            count = len(times)
            if count > 0:
                total = self.get_total_count()
                percentage = (count / total * 100) if total > 0 else 0
                avg_latency = sum(times) / count * 1000 if count > 0 else 0.0
                result.append((status.value, count, percentage, avg_latency))

        # Sort by count (descending)
        result.sort(key=lambda x: x[1], reverse=True)
        return result


class OverallStats:
    """Overall statistics."""

    def __init__(self, max_seconds: int = 60):
        self._lock = Lock()
        # Overall statistics
        self.total_requests = 0
        self.success_requests = 0
        self.failure_requests = 0
        self.bytes_down = 0
        self.start_time = time.time()

        # Cumulative data for final statistics
        self.total_response_time = 0
        self.min_response_time = float("inf")
        self.max_response_time = 0

        # Per-second statistics (deque of (second, SecondStats))
        self.second_stats = deque(maxlen=max_seconds)

        # Status type statistics
        self.status_counts = {status: 0 for status in ResponseStatus}

    @staticmethod
    def _estimate_request_size(response: httpx.Response) -> int:
        request = response.request
        request_headers_len = len("\r\n".join(f"{k}: {v}" for k, v in request.headers.items()))
        request_body_len = len(request.content) if request.content else 0
        response_headers_len = len("\r\n".join(f"{k}: {v}" for k, v in response.headers.items()))
        response_body_len = len(response.content) if response.content else 0
        return request_headers_len + request_body_len + response_headers_len + response_body_len

    def add_result(self, response: Union[httpx.Response, Exception], start_time: float):
        with self._lock:
            response_time = time.time() - start_time

            if isinstance(response, httpx.Response):
                bytes_down = OverallStats._estimate_request_size(response)
                self.bytes_down += bytes_down

                status = {
                    2: ResponseStatus.SUCCESS,
                    4: ResponseStatus.HTTP_4XX,
                    5: ResponseStatus.HTTP_5XX,
                }.get(response.status_code // 100, ResponseStatus.HTTP_ERROR)

                if status == ResponseStatus.SUCCESS:
                    self.success_requests += 1
                else:
                    self.failure_requests += 1
            elif isinstance(response, httpx.ConnectTimeout):
                bytes_down = 0
                status = ResponseStatus.TIMEOUT_CONNECT
                self.failure_requests += 1
            elif isinstance(response, httpx.TimeoutException):
                bytes_down = 0
                status = ResponseStatus.TIMEOUT
                self.failure_requests += 1
            else:
                bytes_down = 0
                status = ResponseStatus.EXCEPTION
                self.failure_requests += 1

            self.status_counts[status] += 1

            self.total_requests += 1
            self.total_response_time += response_time
            self.min_response_time = min(self.min_response_time, response_time)
            self.max_response_time = max(self.max_response_time, response_time)

            # Update per-second statistics
            now_sec = int(time.time())
            if not self.second_stats or self.second_stats[-1][0] != now_sec:
                self.second_stats.append((now_sec, PartialStats()))
            self.second_stats[-1][1].add_result(status, response_time, bytes_down)

    def get_recent_stats(self, span: int = 1) -> PartialStats:
        with self._lock:
            if not self.second_stats:
                return PartialStats()

            current_time = int(time.time())
            aggregated_stats = PartialStats()

            # Aggregate stats from the last 'span' seconds
            for timestamp, stats in self.second_stats:
                if current_time - timestamp < span:
                    for status, times in stats.stats.items():
                        aggregated_stats.stats[status].extend(times)
                    aggregated_stats.bytes_down += stats.bytes_down

            return aggregated_stats

    def get_avg_response_ms(self) -> float:
        with self._lock:
            if self.total_requests == 0:
                return 0.0
            return self.total_response_time / self.total_requests * 1000

    def get_rps(self) -> float:
        with self._lock:
            elapsed_time = time.time() - self.start_time
            if elapsed_time <= 0:
                return 0.0
            return self.total_requests / elapsed_time

    def get_bandwidth_mbps(self) -> float:
        with self._lock:
            elapsed_time = time.time() - self.start_time
            if elapsed_time <= 0:
                return 0.0
            return self.bytes_down * 8 / elapsed_time / 1024 / 1024

    def print_final_stats(self):
        elapsed_time = time.time() - self.start_time

        print("\n" + "=" * 50)
        print("Final Statistics")
        print("=" * 50)

        if self.total_requests == 0:
            print(f"Total Requests: 0 | Duration: {elapsed_time:.1f}s")
            return

        success_rate = self.success_requests / self.total_requests * 100

        print(
            f"Total Requests: {self.total_requests} | Success: {self.success_requests} | Failed: {self.failure_requests}"
        )
        print(f"Success Rate: {success_rate:.1f}% | Duration: {elapsed_time:.1f}s")
        print(f"Average Latency: {self.get_avg_response_ms():.1f}ms | Average QPS: {self.get_rps():.1f}")
        print(
            f"Total Downloaded: {self.bytes_down/1048576:.1f}MB | Average Bandwidth: {self.get_bandwidth_mbps():.1f}Mbps"
        )
        print(f"Min Latency: {self.min_response_time*1000:.1f}ms | Max Latency: {self.max_response_time*1000:.1f}ms")

        # Print detailed status breakdown
        print("\nDetailed Status Breakdown:")
        sorted_statuses = sorted(self.status_counts.items(), key=lambda x: x[1], reverse=True)
        for status, count in sorted_statuses:
            if count > 0:
                percentage = count / self.total_requests * 100
                print(f"  {status.value}: {count} ({percentage:.1f}%)")

    def print_live_stats(self, span: int = 30):
        data = self.get_recent_stats(span)

        total_count = data.get_total_count()
        if total_count == 0:
            return  # No data to display

        success_count = data.get_success_count()
        failure_count = data.get_failure_count()

        content = "\033[2J\033[H" + f"{'Request in last ' + str(span) + ' seconds':^30}\n\n"

        # Print main summary table - only header border
        content += f"| Total | Success | Failure |\n"
        content += "+" + "-" * 7 + "+" + "-" * 9 + "+" + "-" * 9 + "+\n"
        content += f"| {total_count:^5} | {success_count:^7} | {failure_count:^7} |\n\n"

        # Print detailed status breakdown - only header border
        sorted_stats = data.get_sorted_stats()
        if sorted_stats:
            content += f"| Status          | Count | %      | Latency |\n"
            content += "+" + "-" * 17 + "+" + "-" * 7 + "+" + "-" * 8 + "+" + "-" * 9 + "+\n"

            for status_name, count, percentage, latency in sorted_stats:
                content += f"| {status_name:<15} | {count:^5} | {percentage:>5.1f}% | {latency:>5.0f}ms |\n"

        print(content, end="", flush=True)


class Client:
    """Main stress test client."""

    def __init__(self, task_config: TaskConfig, server_config: Optional[Config] = None):
        self.task_config = task_config
        self.stats = OverallStats()
        self.server_config = server_config
        self.last_report_time = time.time()

        # Server reporting setup
        if server_config and server_config.client:
            self.report_interval = server_config.client.report.live_report_interval
            self.server_url = server_config.client.server_url
        else:
            self.report_interval = 0
            self.server_url = None

        # Track previous stats for incremental reporting
        self.last_reported_status_counts = {status: 0 for status in ResponseStatus}
        self.last_reported_bytes_down = 0

    async def _report_to_server(self):
        """Report incremental statistics to server"""
        if not self.server_url or self.report_interval <= 0:
            return

        current_time = time.time()
        span = current_time - self.last_report_time

        # Calculate incremental stats data
        incremental_stats = {}
        for status, current_count in self.stats.status_counts.items():
            last_count = self.last_reported_status_counts.get(status, 0)
            incremental_count = current_count - last_count
            if incremental_count > 0:
                incremental_stats[status.value] = incremental_count
            self.last_reported_status_counts[status] = current_count

        # Calculate incremental bytes_down
        incremental_bytes = self.stats.bytes_down - self.last_reported_bytes_down
        self.last_reported_bytes_down = self.stats.bytes_down

        report_data = {"span": span, "stats": incremental_stats, "bytes_down": incremental_bytes}

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(f"{self.server_url}/upload_live", json=report_data, timeout=5.0)
                response.raise_for_status()

            self.last_report_time = current_time
        except Exception as e:
            print(f"Warning: Failed to report to server: {e}")

    async def _report_worker(self, stop_event: asyncio.Event):
        """Background worker for periodic server reporting"""
        if not self.server_url or self.report_interval <= 0:
            return

        while not stop_event.is_set():
            try:
                await asyncio.sleep(self.report_interval)
                if not stop_event.is_set():
                    await self._report_to_server()
            except Exception as e:
                print(f"Warning: Report worker error: {e}")
                await asyncio.sleep(1)  # Brief pause before retrying

    async def _wait_for_start_time(self):
        start_time = self.task_config.policy.schedule.get_start_time()
        if start_time:
            now = datetime.now(timezone.utc)
            if start_time > now:
                wait_seconds = (start_time - now).total_seconds()
                print(f"Waiting for start time: {start_time.isoformat()} (waiting {wait_seconds:.1f} seconds)")
                await asyncio.sleep(wait_seconds)

    def _print_test_info(self):
        """Print test information"""
        end_time = self.task_config.policy.schedule.get_end_time()

        print(f"Starting {self.task_config.name}...")
        print(f"Requests: {len(self.task_config.requests)} configured")
        print(f"Request order: {self.task_config.policy.order}")

        # Show sample of requests
        for i, request in enumerate(self.task_config.requests[:3]):  # Show first 3 requests
            print(f"  [{i+1}] {request.method} {request.url}")
        if len(self.task_config.requests) > 3:
            print(f"  ... and {len(self.task_config.requests) - 3} more")

        print(f"End Time: {end_time.isoformat() if end_time else 'No end time set'}")
        print("-" * 80)

    async def _run_with_shared_client(
        self,
        concurrent_connections: int,
        timeout_config: httpx.Timeout,
        rate_limiter: Optional[RateLimiter],
        end_time: Optional[datetime],
    ):
        limits_config = httpx.Limits(
            max_keepalive_connections=concurrent_connections,  # Keep all connections alive
        )

        async with httpx.AsyncClient(
            limits=limits_config, timeout=timeout_config, follow_redirects=True
        ) as shared_client:
            await self._run_client_loop([shared_client] * concurrent_connections, rate_limiter, end_time)

    async def _run_with_independent_clients(
        self,
        concurrent_connections: int,
        timeout_config: httpx.Timeout,
        rate_limiter: Optional[RateLimiter],
        end_time: Optional[datetime],
    ):
        clients = []
        try:
            for _ in range(concurrent_connections):
                client = httpx.AsyncClient(
                    timeout=timeout_config,
                    follow_redirects=True,
                    limits=httpx.Limits(max_keepalive_connections=1),
                )
                clients.append(client)

            await self._run_client_loop(clients, rate_limiter, end_time)
        finally:
            # Ensure all independent clients are closed
            for client in clients:
                await client.aclose()

    async def _run_client_loop(
        self,
        sessions: List[httpx.AsyncClient],
        rate_limiter: Optional[RateLimiter],
        end_time: Optional[datetime],
    ):
        stop_event = asyncio.Event()

        # Create worker instances
        workers = [RequestWorker(self.task_config, session, rate_limiter) for session in sessions]

        # Set stats callback for each worker
        for worker in workers:
            worker.set_stats_callback(self._stats_callback)

        # Start all workers
        for worker in workers:
            worker.start()

        # Add report worker if server reporting is enabled
        report_task = None
        if self.server_url and self.report_interval > 0:
            report_task = asyncio.create_task(self._report_worker(stop_event))

        try:
            # Run test loop
            while end_time is None or datetime.now().astimezone() < end_time:
                self.stats.print_live_stats()
                await asyncio.sleep(0.5)
        finally:
            print("\nStopping all workers, please wait...")
            # Stop all workers with timeout
            await asyncio.gather(*(worker.stop(timeout=10.0) for worker in workers))

            # Stop report worker if running
            if report_task:
                stop_event.set()
                try:
                    await asyncio.wait_for(report_task, timeout=5.0)
                except asyncio.TimeoutError:
                    report_task.cancel()

        # Send final report to server
        if self.server_url and self.report_interval > 0:
            await self._report_to_server()

    def _stats_callback(self, response, start_time, status, response_time, bytes_count):
        """Callback function for workers to report statistics"""
        self.stats.add_result(response, start_time)

    async def run(self):
        # Wait for start time
        await self._wait_for_start_time()

        # Print test information
        self._print_test_info()

        # Extract configuration
        limits = self.task_config.policy.limits
        timeouts = self.task_config.policy.timeouts
        end_time = self.task_config.policy.schedule.get_end_time()
        concurrent_connections = limits.coroutines

        # Configure timeout
        timeout_config = httpx.Timeout(
            connect=timeouts.connect,
            read=timeouts.read,
            write=timeouts.write,
            pool=5,
        )

        # Create rate limiter
        rate_limiter = None
        if limits.rps:
            rate_limiter = RateLimiter(limits.rps)

        try:
            if self.task_config.policy.reuse_connections:
                await self._run_with_shared_client(concurrent_connections, timeout_config, rate_limiter, end_time)
            else:
                await self._run_with_independent_clients(concurrent_connections, timeout_config, rate_limiter, end_time)

        except Exception as e:
            if "Force exit" not in str(e):
                print(f"\nError during testing: {e}")
                return

        # Print final statistics
        self.stats.print_final_stats()
