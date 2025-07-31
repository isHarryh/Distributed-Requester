from typing import Optional, List, Dict, Callable
import asyncio
import time
import uuid
import random
from collections import deque, defaultdict
from datetime import datetime, timezone
from threading import Lock

import httpx
import socksio

from src.Config import TaskConfig, Config, ConsecutiveStatusRuleConfig, ProxyConfig
from src.Request import RequestWorker, RequestRecord
from src.utils.StringFormatter import format_delta_time
from src.utils.RateLimiter import RateLimiter
from src.utils.ResponseStatus import ResponseStatus
from src.utils.Logger import Logger


class ProxyPool:
    """Thread-safe proxy pool for managing proxy rotation."""

    def __init__(self, proxies: List[Optional[str]], proxy_config: ProxyConfig):
        self._lock = Lock()
        self._current_index = -1
        self._proxies = proxies if proxies else [None]  # Default to direct connection
        self._proxy_config = proxy_config

        Logger.info(f"ProxyPool initialized with {len(self._proxies)} proxies")

    def get_proxy(self) -> Optional[str]:
        if self._proxy_config.order == "random":
            return self.switch_to_random()
        elif self._proxy_config.order == "sequential":
            return self.switch_to(1)
        elif self._proxy_config.order == "switchByRule":
            return self.switch_by_rule()
        else:
            return self.switch_to_random()

    def switch_to(self, offset: int) -> Optional[str]:
        prev_index = self._current_index
        with self._lock:
            if self._current_index != prev_index:
                Logger.debug("Proxy already switched, returning current proxy")
                return self.get_current_proxy()
            i = max(self._proxy_config.preflight_max_switches, 1)
            while i > 0:
                self._current_index = (self._current_index + offset) % len(self._proxies)
                if not self._preflight():
                    i -= 1
                    continue
                if prev_index != self._current_index:
                    Logger.info(f"Switched to proxy: {self.get_current_proxy()}")
                return self.get_current_proxy()

    def switch_to_random(self) -> Optional[str]:
        prev_index = self._current_index
        with self._lock:
            if self._current_index != prev_index:
                Logger.debug("Proxy already switched, returning current proxy")
                return self.get_current_proxy()
            i = max(self._proxy_config.preflight_max_switches, 1)
            while i > 0:
                new_index = random.randint(0, len(self._proxies) - 1)
                self._current_index = new_index
                if not self._preflight():
                    i -= 1
                    continue
                if prev_index != self._current_index:
                    Logger.info(f"Switched to random proxy: {self.get_current_proxy()}")
                return self.get_current_proxy()

    def switch_by_rule(self) -> Optional[str]:
        with self._lock:
            if self._current_index < 0 or self._current_index >= len(self._proxies):
                self._current_index = 0
                Logger.info(f"Switched to first proxy: {self.get_current_proxy()}")
            return self.get_current_proxy()

    def get_current_proxy(self) -> Optional[str]:
        return self._proxies[self._current_index]

    def get_proxy_count(self) -> int:
        return len(self._proxies)

    def _preflight(self) -> bool:
        if not self._proxy_config.preflight or not self._proxy_config.preflight_url:
            return True
        if not self._proxy_config.order == "switchByRule":
            return True

        try:
            Logger.debug(f"Proxy preflight check starting ({self.get_current_proxy()})")
            httpx.get(
                self._proxy_config.preflight_url,
                proxy=self.get_current_proxy(),
                timeout=self._proxy_config.preflight_timeout,
            )
            Logger.info(f"Proxy preflight passed ({self.get_current_proxy()})")
            return True
        except (httpx.RequestError, socksio.exceptions.ProtocolError) as e:
            Logger.warning(f"Proxy preflight failed ({self.get_current_proxy()}): {type(e).__name__} {e}")
            return False


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
                self.proxy_pool.switch_to(1)
                return "proxy_switched"
            elif action == "switchToPrevProxy":
                self.proxy_pool.switch_to(-1)
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

    def __init__(
        self,
        partial_span: int = 60,
        proxy_pool: Optional[ProxyPool] = None,
        rule_callback: Optional[Callable[[ResponseStatus], Optional[str]]] = None,
    ):
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

        self.proxy_pool = proxy_pool
        self.rule_callback = rule_callback

    @staticmethod
    def _estimate_request_size(response: httpx.Response) -> int:
        request = response.request
        request_headers_len = len("\r\n".join(f"{k}: {v}" for k, v in request.headers.items()))
        request_body_len = len(request.content) if request.content else 0
        response_headers_len = len("\r\n".join(f"{k}: {v}" for k, v in response.headers.items()))
        response_body_len = len(response.content) if response.content else 0
        return request_headers_len + request_body_len + response_headers_len + response_body_len

    def add_result(self, record: RequestRecord):
        with self._lock:
            response_time = record.get_duration()
            response_status = record.get_status()

            if not response_status.is_exception():
                bytes_down = record.get_response_size()
                self.bytes_down += bytes_down

                content = record.get_response_content()
                http_status = record.get_http_status()

                Logger.debug(
                    f"{record.get_method()} {response_status.name} {http_status.value} {record.get_url()} "
                    + f"in {response_time*1000:.0f}ms, "
                    + f"preview: `{content[:64] if len(content) > 64 else content}` (full {len(content)}B)"
                )

                if response_status == ResponseStatus.SUCCESS:
                    self.success_requests += 1
                else:
                    self.failure_requests += 1
            else:
                exception = record.get_exception()

                Logger.debug(
                    f"{record.get_method()} {response_status.name} {record.get_url()} "
                    + f"in {response_time*1000:.0f}ms, "
                    + f"{type(exception).__name__} {exception}"
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

        if self.proxy_pool:
            proxy = self.proxy_pool.get_current_proxy()
            content += "> Using proxy %s\n\n" % proxy if proxy else "> Using direct connection\n\n"

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
            content += "| %-18s | %5s | %6s | %8s |\n" % ("Status", "Count", "%", "Latency")
            content += "+" + "-" * 20 + "+" + "-" * 7 + "+" + "-" * 8 + "+" + "-" * 10 + "+\n"

            for status_name, count, percentage, latency in sorted_stats:
                content += "| %-18s | %5d | %5.1f%% | %6.0fms |\n" % (status_name, count, percentage, latency)

        print(content + "\n", end="", flush=True)

    def print_final_stats(self):
        elapsed_time = time.time() - self.start_time

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
        self.server_config = server_config
        self.proxy_pool = ProxyPool(proxies=task_config.proxies, proxy_config=task_config.policy.proxy)
        self.stats = OverallStats(
            partial_span=partial_span, proxy_pool=self.proxy_pool, rule_callback=self._process_rule_response
        )
        self.last_report_time = time.time()
        self.client_id = str(uuid.uuid4())

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
            Logger.info("Reported to server successfully")
        except httpx.HTTPStatusError as e:
            Logger.warning(f"Failed to report to server, response code {e.response.status_code}: {e.response.text}")
        except Exception as e:
            Logger.warning(f"Failed to report to server: {type(e)} - {e}")

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

    async def _run_client_loop(
        self,
        end_time: Optional[datetime],
    ):
        stop_event = asyncio.Event()
        rate_limiter = RateLimiter(
            self.task_config.policy.limits.rps,
            self.task_config.policy.limits.rpm,
            stop_event=stop_event,
        )

        # Create worker instances with proxy provider
        workers = []
        for _ in range(self.task_config.policy.limits.coroutines):
            worker = RequestWorker(
                task_config=self.task_config,
                rate_limiter=rate_limiter,
                response_callback=self.stats.add_result,
                proxy_provider=self.proxy_pool.get_proxy,
            )
            workers.append(worker)

        # Start all workers
        Logger.info("Starting workers")
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
        end_time = self.task_config.policy.schedule.get_end_time()

        try:
            await self._run_client_loop(end_time)
        except Exception as e:
            Logger.error(f"Error in client: {type(e).__name__} {e}")
            return

        # Print final statistics
        self.stats.print_final_stats()
