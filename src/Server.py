from typing import Dict, Any

import json
import os
import random
import time
from collections import defaultdict
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.Config import load_config
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
    client_id: str
    client_ttl: int
    span: float
    stats: Dict[str, int]
    bytes_down: int


class ClientRecord:
    """Client connection record with timeout tracking."""

    def __init__(self, client_id: str, client_ttl: int):
        self.client_id = client_id
        self.client_ttl = client_ttl
        self.last_seen = time.time()
        self.is_marked_disconnected = False

    def update_last_seen(self):
        self.last_seen = time.time()

    def is_expired(self) -> bool:
        return time.time() - self.last_seen > self.client_ttl

    def should_be_removed(self) -> bool:
        return self.is_marked_disconnected and self.is_expired()


class ClientReport:
    """Client report data."""

    def __init__(self, client_id: str, span: float, stats: Dict[str, int], bytes_down: int):
        self.client_id = client_id
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
            title="Distributed Requester Server",
            version=self.config.version,
            default_response_class=PrettyJSONResponse,
        )
        self.app.add_middleware(GZipMiddleware, compresslevel=1)

        self._stats_lock = Lock()
        self._config_lock = Lock()
        self._clients_lock = Lock()

        self.total_stats: Dict[str, int] = defaultdict(int)
        self.total_bytes_down = 0
        self.client_records: Dict[str, ClientRecord] = {}

        self._setup_routes()
        self._setup_static_files()

    def _cleanup_expired_clients(self):
        with self._clients_lock:
            clients_to_remove = []
            for client_id, record in self.client_records.items():
                if record.should_be_removed():
                    clients_to_remove.append(client_id)
                elif record.is_expired() and not record.is_marked_disconnected:
                    record.is_marked_disconnected = True

            for client_id in clients_to_remove:
                Logger.info(f"Removing disconnected client: {client_id}")
                del self.client_records[client_id]

    def _setup_static_files(self):
        static_dir = os.path.join(os.path.dirname(__file__), "data", "public")

        if os.path.exists(static_dir):
            self.app.mount("/", StaticFiles(directory=static_dir, html=True), name="public")
            Logger.info(f"Static files mounted")
        else:
            Logger.warning(f"Static directory not found: {static_dir}")

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
                    # If reading fails, return 500 error
                    Logger.error(f"Failed to read fresh config: {e}")
                    raise HTTPException(status_code=500, detail=f"Failed to read config: {e}")

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
            # Clean up expired clients first
            self._cleanup_expired_clients()

            # Store client report
            report = ClientReport(request.client_id, request.span, request.stats, request.bytes_down)

            # Update or create client record
            with self._clients_lock:
                if request.client_id in self.client_records:
                    # Update existing client record
                    client_record = self.client_records[request.client_id]
                    client_record.update_last_seen()
                    client_record.is_marked_disconnected = False
                    client_record.client_ttl = request.client_ttl
                else:
                    # Create new client record
                    Logger.info(f"New client connected: {request.client_id}")
                    self.client_records[request.client_id] = ClientRecord(request.client_id, request.client_ttl)

            with self._stats_lock:
                # Aggregate stats
                for status, count in request.stats.items():
                    self.total_stats[status] += count

                self.total_bytes_down += request.bytes_down

            return StandardResponse(code=0, msg="success", data=None)

        @self.app.get("/get_stats", response_model=StandardResponse)
        async def get_stats():
            """Get aggregated statistics."""
            # Clean up expired clients before returning stats
            self._cleanup_expired_clients()

            with self._stats_lock:
                stats_data = {
                    "stats": dict(sorted(self.total_stats.items(), key=lambda x: x[1], reverse=True)),
                    "bytes_down": self.total_bytes_down,
                    "active_clients": len(self.client_records),
                }

                os.makedirs("temp", exist_ok=True)

                filename = f"temp/stats-{time.strftime('%Y%m%d%H')}.json"
                with open(filename, "w") as f:
                    json.dump(stats_data, f, ensure_ascii=False, indent=4)

            return StandardResponse(code=0, msg="success", data=stats_data)

        Logger.info("API endpoints mounted")

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
