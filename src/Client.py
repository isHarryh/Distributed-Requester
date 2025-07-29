from typing import Optional, Union, List, Dict, Callable
import asyncio
import time
import uuid
import random
from collections import deque, defaultdict
from datetime import datetime, timezone
from threading import Lock

import httpx

from src.Config import TaskConfig, Config, ConsecutiveStatusRuleConfig
from src.Request import RequestWorker, RateLimiter
from src.utils.StringFormatter import format_delta_time
from src.utils.ResponseStatus import HttpStatus, ResponseStatus
from src.utils.Logger import Logger


class ProxyPool:
    """Thread-safe proxy pool for managing proxy rotation."""

    def __init__(self, proxies: List[Optional[str]], proxy_order: str):
        self._lock = Lock()
        self._current_index = 0
        self._proxies = proxies if proxies else [None]  # Default to direct connection
        self._order = proxy_order

        Logger.info(f"ProxyPool initialized with {len(self._proxies)} proxies ({proxy_order})")

    def get_proxy(self) -> Optional[str]:
        with self._lock:
            if self._order == "random":
                return random.choice(self._proxies)
            elif self._order == "sequential":
                proxy = self._proxies[self._current_index]
                self._current_index = (self._current_index + 1) % len(self._proxies)
                return proxy
            elif self._order == "switchByRule":
                return self._proxies[self._current_index]
            else:
                return random.choice(self._proxies)

    def switch_to_next(self) -> Optional[str]:
        with self._lock:
            self._current_index = (self._current_index + 1) % len(self._proxies)
            proxy = self._proxies[self._current_index]
            Logger.info(f"Switched to next proxy: {proxy or 'direct connection'}")
            return proxy

    def switch_to_prev(self) -> Optional[str]:
        with self._lock:
            self._current_index = (self._current_index - 1) % len(self._proxies)
            proxy = self._proxies[self._current_index]
            Logger.info(f"Switched to previous proxy: {proxy or 'direct connection'}")
            return proxy

    def switch_to_random(self) -> Optional[str]:
        with self._lock:
            new_index = random.randint(0, len(self._proxies) - 1)
            self._current_index = new_index
            proxy = self._proxies[self._current_index]
            Logger.info(f"Switched to random proxy: {proxy or 'direct connection'}")
            return proxy

    def get_current_proxy(self) -> Optional[str]:
        with self._lock:
            return self._proxies[self._current_index]

    def get_proxy_count(self) -> int:
        return len(self._proxies)


class RuleExecutor:
    """Execute rules based on response status and other conditions."""

    def __init__(self, rules: List[ConsecutiveStatusRuleConfig], proxy_pool: ProxyPool):
        self.rules = rules
        self.proxy_pool = proxy_pool
        self._lock = Lock()

        # Track consecutive status counts per rule
        self._consecutive_counts: Dict[str, int] = defaultdict(int)
        self._last_status_per_rule: Dict[str, str] = {}

        Logger.info(f"RuleExecutor initialized with {len(rules)} rules")

    def process(self, status: ResponseStatus) -> Optional[str]:
        if not self.rules:
            return None

        with self._lock:
            status_str = status.value

            for i, rule in enumerate(self.rules):
                rule_id = f"rule_{i}"

                if isinstance(rule, ConsecutiveStatusRuleConfig):
                    # Check if this is one of the consecutive statuses we're tracking
                    if status_str in rule.status:
                        # Same status as last time, increment counter
                        if self._last_status_per_rule.get(rule_id) == status_str:
                            self._consecutive_counts[rule_id] += 1
                        else:
                            # Different status, reset counter
                            self._consecutive_counts[rule_id] = 1
                    else:
                        # Different status, reset counter
                        self._consecutive_counts[rule_id] = 0

                    self._last_status_per_rule[rule_id] = status_str

                    # Check if rule condition is met
                    if status_str in rule.status and self._consecutive_counts[rule_id] >= rule.count:
                        # Execute the action and reset counter after rule execution
                        Logger.warning(f"Rule triggered: {rule.event}, {rule.status} x{rule.count}")
                        action_result = self._execute_action(rule.action)
                        self._consecutive_counts[rule_id] = 0
                        return action_result

            return None

    def _execute_action(self, action: str) -> str:
        try:
            if action == "switchToNextProxy":
                self.proxy_pool.switch_to_next()
                return "proxy_switched"
            elif action == "switchToPrevProxy":
                self.proxy_pool.switch_to_prev()
                return "proxy_switched"
            elif action == "switchToRandomProxy":
                self.proxy_pool.switch_to_random()
                return "proxy_switched"
            elif action == "stopCurrentTask":
                Logger.warning("Rule action: Stopping current task")
                return "stop_task"
            elif action == "stopProgram":
                Logger.warning("Rule action: Stopping program")
                return "stop_program"
            else:
                Logger.error(f"Unknown action: {action}")
                return "unknown_action"
        except Exception as e:
            Logger.error(f"Error executing action {action}: {e}")
            return "action_error"

    def reset_counters(self):
        with self._lock:
            self._consecutive_counts.clear()
            self._last_status_per_rule.clear()
            Logger.info("Rule counters reset")


class OverallStats:

    def __init__(self, partial_span: int = 60):
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

        # Per-second statistics: deque of (timestamp, {status: [response_times], bytes_down: int})
        self.partial_span = partial_span
        self.second_stats = deque(maxlen=partial_span)

        # Status type statistics
        self.status_counts = {status: 0 for status in ResponseStatus}

        # Callback for rule processing
        self.rule_callback: Optional[Callable[[ResponseStatus], Optional[str]]] = None

    def set_rule_callback(self, callback: Callable[[ResponseStatus], Optional[str]]):
        """Set callback function for rule processing"""
        self.rule_callback = callback

    @staticmethod
    def _estimate_request_size(response: httpx.Response) -> int:
        request = response.request
        request_headers_len = len("\r\n".join(f"{k}: {v}" for k, v in request.headers.items()))
        request_body_len = len(request.content) if request.content else 0
        response_headers_len = len("\r\n".join(f"{k}: {v}" for k, v in response.headers.items()))
        response_body_len = len(response.content) if response.content else 0
        return request_headers_len + request_body_len + response_headers_len + response_body_len

    def add_result(self, request: httpx.Request, response: Union[httpx.Response, Exception], start_time: float):
        with self._lock:
            response_time = time.time() - start_time

            response_status = ResponseStatus.of(response)

            if not response_status.is_exception():
                assert isinstance(response, httpx.Response)
                bytes_down = OverallStats._estimate_request_size(response)
                self.bytes_down += bytes_down

                http_status = HttpStatus.of(response)
                Logger.debug(
                    f"{request.method} {response_status.name} {http_status.value} {request.url} "
                    + f"in {response_time*1000:.0f}ms, "
                    + f"preview: `{response.content[:64] if len(response.content) > 64 else response.content}` "
                    + f"(full {len(response.content)}B)"
                )

                if response_status == ResponseStatus.SUCCESS:
                    self.success_requests += 1
                else:
                    self.failure_requests += 1
            else:
                Logger.debug(
                    f"{request.method} {response_status.name} {request.url} "
                    + f"in {response_time*1000:.0f}ms, "
                    + f"{type(response).__name__} {response}"
                )

                bytes_down = 0
                self.failure_requests += 1

            self.status_counts[response_status] += 1

            self.total_requests += 1
            self.total_response_time += response_time
            self.min_response_time = min(self.min_response_time, response_time)
            self.max_response_time = max(self.max_response_time, response_time)

            # Update per-second statistics
            now_sec = int(time.time())
            if not self.second_stats or self.second_stats[-1][0] != now_sec:
                # Create new second entry: (timestamp, {status: [response_times], bytes_down: int})
                new_second_data = {"status_times": {status: [] for status in ResponseStatus}, "bytes_down": 0}
                self.second_stats.append((now_sec, new_second_data))

            # Add data to current second
            current_second = self.second_stats[-1][1]
            current_second["status_times"][response_status].append(response_time)
            current_second["bytes_down"] += bytes_down

            # Process rules if callback is set
            if self.rule_callback:
                try:
                    self.rule_callback(response_status)
                except Exception as e:
                    Logger.error(f"Error in rule callback: {e}")

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

    def get_partial_total_count(self, span: Optional[int] = None) -> int:
        if span is None:
            span = self.partial_span
        with self._lock:
            if not self.second_stats:
                return 0

            current_time = int(time.time())
            total_count = 0

            for timestamp, second_data in self.second_stats:
                if current_time - timestamp < span:
                    for times_list in second_data["status_times"].values():
                        total_count += len(times_list)

            return total_count

    def get_partial_success_count(self, span: Optional[int] = None) -> int:
        if span is None:
            span = self.partial_span
        with self._lock:
            if not self.second_stats:
                return 0

            current_time = int(time.time())
            success_count = 0

            for timestamp, second_data in self.second_stats:
                if current_time - timestamp < span:
                    success_count += len(second_data["status_times"][ResponseStatus.SUCCESS])

            return success_count

    def get_partial_failure_count(self, span: Optional[int] = None) -> int:
        if span is None:
            span = self.partial_span
        return self.get_partial_total_count(span) - self.get_partial_success_count(span)

    def get_partial_bytes_down(self, span: Optional[int] = None) -> int:
        if span is None:
            span = self.partial_span
        with self._lock:
            if not self.second_stats:
                return 0

            current_time = int(time.time())
            total_bytes = 0

            for timestamp, second_data in self.second_stats:
                if current_time - timestamp < span:
                    total_bytes += second_data["bytes_down"]

            return total_bytes

    def get_partial_rps(self, span: Optional[int] = None) -> float:
        if span is None:
            span = self.partial_span
        total_count = self.get_partial_total_count(span)
        if span <= 0:
            return 0.0
        return total_count / span

    def get_partial_bandwidth_mbps(self, span: Optional[int] = None) -> float:
        if span is None:
            span = self.partial_span
        total_bytes = self.get_partial_bytes_down(span)
        if span <= 0:
            return 0.0
        return total_bytes * 8 / span / 1024 / 1024

    def get_partial_sorted_stats(self, span: Optional[int] = None) -> list:
        if span is None:
            span = self.partial_span
        with self._lock:
            if not self.second_stats:
                return []

            current_time = int(time.time())
            status_aggregated = {status: [] for status in ResponseStatus}

            # Aggregate data from the specified span
            for timestamp, second_data in self.second_stats:
                if current_time - timestamp < span:
                    for status, times_list in second_data["status_times"].items():
                        status_aggregated[status].extend(times_list)

            # Build result list
            result = []
            total_count = sum(len(times) for times in status_aggregated.values())

            for status, times_list in status_aggregated.items():
                count = len(times_list)
                if count > 0:
                    percentage = (count / total_count * 100) if total_count > 0 else 0
                    avg_latency = sum(times_list) / count * 1000 if count > 0 else 0.0
                    result.append((status.value, count, percentage, avg_latency))

            # Sort by count (descending)
            result.sort(key=lambda x: x[1], reverse=True)
            return result

    def print_live_stats(self):
        if self.total_requests == 0:
            return

        content = "\033[2J\033[H"

        content += "| %-15s | %6s | %7s | %7s |\n" % ("Period", "Total", "Success", "Failure")
        content += "+" + "-" * 17 + "+" + "-" * 8 + "+" + "-" * 9 + "+" + "-" * 9 + "+\n"
        content += "| %-15s | %6d | %7d | %7d |\n" % (
            "Last %s" % format_delta_time(self.partial_span),
            self.get_partial_total_count(),
            self.get_partial_success_count(),
            self.get_partial_failure_count(),
        )
        if time.time() - self.start_time > self.partial_span:
            content += "| %-15s | %6d | %7d | %7d |\n" % (
                "All %s" % format_delta_time(time.time() - self.start_time),
                self.total_requests,
                self.success_requests,
                self.failure_requests,
            )
        content += "\n"

        # Print detailed status breakdown - only header border
        sorted_stats = self.get_partial_sorted_stats()
        if sorted_stats:
            content += "| %-15s | %5s | %5s | %8s |\n" % ("Status", "Count", "%", "Latency")
            content += "+" + "-" * 17 + "+" + "-" * 7 + "+" + "-" * 8 + "+" + "-" * 11 + "+\n"

            for status_name, count, percentage, latency in sorted_stats:
                content += "| %-15s | %5d | %5.1f%% | %6.0fms |\n" % (status_name, count, percentage, latency)

        print(content, end="", flush=True)

    def print_final_stats(self):
        elapsed_time = time.time() - self.start_time

        Logger.info("\n" + "=" * 50)
        Logger.info("Final Statistics")
        Logger.info("=" * 50)

        if self.total_requests == 0:
            Logger.info(f"Total Requests: 0 | Duration: {elapsed_time:.1f}s")
            return

        success_rate = self.success_requests / self.total_requests * 100

        Logger.info(
            f"Total Requests: {self.total_requests} | Success: {self.success_requests} | Failed: {self.failure_requests}"
        )
        Logger.info(f"Success Rate: {success_rate:.1f}% | Duration: {elapsed_time:.1f}s")
        Logger.info(f"Average Latency: {self.get_avg_response_ms():.1f}ms | Average QPS: {self.get_rps():.1f}")
        Logger.info(
            f"Total Downloaded: {self.bytes_down/1048576:.1f}MB | Average Bandwidth: {self.get_bandwidth_mbps():.1f}Mbps"
        )
        Logger.info(
            f"Min Latency: {self.min_response_time*1000:.1f}ms | Max Latency: {self.max_response_time*1000:.1f}ms"
        )

        # Print detailed status breakdown
        Logger.info("Detailed Status Breakdown:")
        sorted_statuses = sorted(self.status_counts.items(), key=lambda x: x[1], reverse=True)
        for status, count in sorted_statuses:
            if count > 0:
                percentage = count / self.total_requests * 100
                Logger.info(f"  {status.value}: {count} ({percentage:.1f}%)")


class Client:
    """Main stress test client."""

    def __init__(self, task_config: TaskConfig, server_config: Optional[Config] = None, partial_span: int = 60):
        self.task_config = task_config
        self.stats = OverallStats(partial_span=partial_span)
        self.server_config = server_config
        self.last_report_time = time.time()
        self.client_id = str(uuid.uuid4())

        # Initialize proxy pool
        self.proxy_pool = ProxyPool(proxies=task_config.proxies, proxy_order=task_config.policy.proxy_order)

        # Initialize rule executor
        self.rule_executor = RuleExecutor(rules=task_config.rules, proxy_pool=self.proxy_pool)

        if server_config and server_config.client:
            self.report_interval = server_config.client.report.live_report_interval
            self.server_url = server_config.client.server_url
        else:
            self.report_interval = 0
            self.server_url = None

        # Track previous stats for incremental reporting
        self.last_reported_status_counts = {status: 0 for status in ResponseStatus}
        self.last_reported_bytes_down = 0

        # Control flags for rule actions
        self._stop_task_requested = False
        self._stop_program_requested = False

        # Set up rule processing callback
        self.stats.set_rule_callback(self._process_rule_response)

        Logger.info(f"Client initialized with ID {self.client_id} (report interval {self.report_interval}s)")
        Logger.info(f"Proxy pool: {self.proxy_pool.get_proxy_count()} proxies, Rules: {len(task_config.rules)}")

    def _process_rule_response(self, status: ResponseStatus) -> Optional[str]:
        """Process response status through rule executor"""
        action = self.rule_executor.process(status)
        if action:
            if action == "stop_task":
                self._stop_task_requested = True
            elif action == "stop_program":
                self._stop_program_requested = True
            elif action == "proxy_switched":
                pass
        return action

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

        report_data = {
            "client_id": self.client_id,
            "client_ttl": self.report_interval,
            "span": span,
            "stats": incremental_stats,
            "bytes_down": incremental_bytes,
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(f"{self.server_url}/upload_live", json=report_data, timeout=5.0)
                response.raise_for_status()

            self.last_report_time = current_time
        except Exception as e:
            Logger.warning(f"Failed to report to server: {e}")

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
                Logger.error(f"Report worker error: {e}")
                await asyncio.sleep(1)

    async def _wait_for_start_time(self):
        start_time = self.task_config.policy.schedule.get_start_time()
        if start_time:
            now = datetime.now(timezone.utc)
            if start_time > now:
                wait_seconds = (start_time - now).total_seconds()
                Logger.info(f"Waiting for start time: {start_time.isoformat()} (waiting {wait_seconds:.1f} seconds)")
                await asyncio.sleep(wait_seconds)

    def _print_test_info(self):
        """Print test information"""
        end_time = self.task_config.policy.schedule.get_end_time()

        Logger.info(f"Starting {self.task_config.name}...")
        Logger.info(f"Requests: {len(self.task_config.requests)} configured")
        Logger.info(f"Request order: {self.task_config.policy.order}")

        # Show sample of requests
        for i, request in enumerate(self.task_config.requests[:3]):  # Show first 3 requests
            Logger.debug(f" [{i+1}] {request.method} {request.url}")
        if len(self.task_config.requests) > 3:
            Logger.debug(f" ... and {len(self.task_config.requests) - 3} more")

        Logger.info(f"End Time: {end_time.isoformat() if end_time else 'No end time set'}")
        Logger.info("-" * 80)

    async def _run_with_shared_client(
        self,
        rate_limiter: Optional[RateLimiter],
        end_time: Optional[datetime],
    ):
        # With new worker design, shared vs independent is handled by reuse_connections policy
        await self._run_client_loop(rate_limiter, end_time)

    async def _run_with_independent_clients(
        self,
        rate_limiter: Optional[RateLimiter],
        end_time: Optional[datetime],
    ):
        # With new worker design, shared vs independent is handled by reuse_connections policy
        await self._run_client_loop(rate_limiter, end_time)

    async def _run_client_loop(
        self,
        rate_limiter: Optional[RateLimiter],
        end_time: Optional[datetime],
    ):
        stop_event = asyncio.Event()

        max_connections = self.task_config.policy.limits.coroutines

        # Prepare HTTP client configuration
        limits_config = httpx.Limits(
            max_keepalive_connections=(max_connections if self.task_config.policy.reuse_connections else 1)
        )
        timeout_config = self.task_config.policy.timeouts.build()

        # Create worker instances with proxy provider
        workers = []
        for _ in range(max_connections):
            worker = RequestWorker(
                task_config=self.task_config,
                rate_limiter=rate_limiter,
                response_callback=self.stats.add_result,
                proxy_provider=self.proxy_pool.get_proxy,
                limits_config=limits_config,
                timeout_config=timeout_config,
            )
            workers.append(worker)

        # Start all workers
        for worker in workers:
            worker.start()

        # Add report worker if server reporting is enabled
        report_task = None
        if self.server_url and self.report_interval > 0:
            report_task = asyncio.create_task(self._report_worker(stop_event))

        try:
            try:
                Logger.disable_console()

                # Run client main loop
                while end_time is None or datetime.now().astimezone() < end_time:
                    # Check for rule-based stop conditions
                    if self._stop_task_requested:
                        Logger.warning("Task stopped by rule")
                        break
                    if self._stop_program_requested:
                        Logger.warning("Program stopped by rule")
                        raise KeyboardInterrupt("Program stopped by rule")

                    self.stats.print_live_stats()
                    await asyncio.sleep(0.5)
            finally:
                Logger.enable_console()

        except (asyncio.CancelledError, KeyboardInterrupt):
            Logger.warning("Client main loop cancelled")
        finally:
            stop_event.set()

            # Stop all workers with timeout
            Logger.info("Stopping all workers, please wait...")
            await asyncio.gather(*(worker.stop(timeout=5.0) for worker in workers))

            # Stop report worker if running
            if report_task:
                try:
                    await asyncio.wait_for(report_task, timeout=5.0)
                except asyncio.TimeoutError:
                    report_task.cancel()

            # Send final report to server
            if self.server_url and self.report_interval > 0:
                await self._report_to_server()

    async def run(self):
        # Wait for start time
        await self._wait_for_start_time()

        # Print test information
        self._print_test_info()

        # Extract configuration
        limits = self.task_config.policy.limits
        end_time = self.task_config.policy.schedule.get_end_time()

        # Create rate limiter
        rate_limiter = None
        if limits.rps:
            rate_limiter = RateLimiter(limits.rps)

        try:
            if self.task_config.policy.reuse_connections:
                await self._run_with_shared_client(rate_limiter, end_time)
            else:
                await self._run_with_independent_clients(rate_limiter, end_time)

        except Exception as e:
            Logger.error(f"Error in client: {type(e).__name__} {e}")
            return

        # Print final statistics
        self.stats.print_final_stats()
