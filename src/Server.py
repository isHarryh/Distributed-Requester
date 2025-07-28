from typing import Dict, Any

import json
import os
import random
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.Config import ReportConfig, load_config
from src.utils.Logger import Logger


# Custom JSON response class for pretty formatting
class PrettyJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            separators=(",", ": "),
        ).encode("utf-8")


# Request/Response models
class StandardResponse(BaseModel):
    code: int
    msg: str
    data: Any = None


class UploadLiveRequest(BaseModel):
    span: float
    stats: Dict[str, int]
    bytes_down: int


class ClientReport:
    """Client report data."""

    def __init__(self, span: float, stats: Dict[str, int], bytes_down: int):
        self.timestamp = time.time()
        self.span = span
        self.stats = stats
        self.bytes_down = bytes_down


class Server:
    """FastAPI-based distributed stress testing server."""

    def __init__(self, config_path: str):
        # Load initial config to validate and setup server
        self.config_path = config_path
        self.config = load_config(config_path)

        if not self.config.server:
            raise ValueError("Server configuration is required when running in server mode")

        self.server_config = self.config.server
        self.app = FastAPI(
            title="Distributed Requester Server", version=self.config.version, default_response_class=PrettyJSONResponse
        )

        self._stats_lock = Lock()
        self._config_lock = Lock()

        self.total_stats: Dict[str, int] = defaultdict(int)
        self.total_bytes_down = 0
        self.client_reports: deque = deque()

        self._setup_routes()

    def _setup_routes(self):

        @self.app.get("/get_version", response_model=StandardResponse)
        async def get_version():
            """Get configuration version."""
            return StandardResponse(code=0, msg="success", data=self.config.version)

        @self.app.get("/get_config", response_model=StandardResponse)
        async def get_config():
            """Get configuration for client."""
            # Read fresh config from file with lock protection
            with self._config_lock:
                try:
                    fresh_config = load_config(self.config_path)
                except Exception as e:
                    # If reading fails, return 502 error
                    Logger.error(f"Failed to read fresh config: {e}")
                    raise HTTPException(status_code=502, detail=f"Failed to read config: {e}")

            # Random task selection from fresh config
            selected_task = random.choice(fresh_config.tasks) if fresh_config.tasks else None

            # Build response config (exclude server and client)
            response_config = {
                "version": self.config.version,
                "tasks": [selected_task.model_dump()] if selected_task else [],
            }

            return StandardResponse(code=0, msg="success", data=response_config)

        @self.app.post("/upload_live", response_model=StandardResponse)
        async def upload_live(request: UploadLiveRequest):
            """Upload live statistics from client."""
            # Store client report
            report = ClientReport(request.span, request.stats, request.bytes_down)
            interval = (
                self.config.client.report.live_report_interval
                if self.config.client and self.config.client.report
                else ReportConfig().live_report_interval
            )

            with self._stats_lock:
                self.client_reports.append(report)

                # Aggregate stats
                for status, count in request.stats.items():
                    self.total_stats[status] += count

                self.total_bytes_down += request.bytes_down

                # Clean old reports
                current_time = time.time()
                while self.client_reports and current_time - self.client_reports[0].timestamp > interval * 2:
                    self.client_reports.popleft()

            return StandardResponse(code=0, msg="success", data=None)

        @self.app.get("/get_stats", response_model=StandardResponse)
        async def get_stats():
            """Get aggregated statistics."""
            with self._stats_lock:
                # Count active clients (reported within live_report_interval)
                current_time = time.time()
                interval = (
                    self.config.client.report.live_report_interval
                    if self.config.client and self.config.client.report
                    else ReportConfig().live_report_interval
                )

                active_clients = sum(1 for report in self.client_reports if current_time - report.timestamp <= interval)

                stats_data = {
                    "stats": dict(self.total_stats),
                    "bytes_down": self.total_bytes_down,
                    "active_clients": active_clients,
                }

                os.makedirs("temp", exist_ok=True)

                filename = f"temp/stats-{time.strftime("%Y%m%d%H")}.json"
                with open(filename, "w") as f:
                    json.dump(stats_data, f, ensure_ascii=False, indent=4)

            return StandardResponse(code=0, msg="success", data=stats_data)

    def run(self, host: str = "0.0.0.0"):
        """Run the server."""
        import uvicorn

        uvicorn.run(self.app, host=host, port=self.server_config.port, log_level="info")

    async def serve(self, host: str = "0.0.0.0"):
        """Async server start method."""
        import uvicorn

        config = uvicorn.Config(self.app, host=host, port=self.server_config.port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
