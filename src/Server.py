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


class Server:
    """FastAPI-based distributed stress testing server."""

    SPEED_INTERVAL = 5  # seconds

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
        self._speed_lock = Lock()

        self.total_stats: Dict[str, int] = defaultdict(int)
        self.total_bytes_down = 0
        self.client_records: Dict[str, ClientRecord] = {}

        self.speed_data: Dict[int, float] = {}
        self.last_speed_timestamp = 0
        self.last_total_bytes = 0

        self._setup_routes()
        self._setup_static_files()

    def _cleanup_expired_clients(self):
        with self._clients_lock:
            clients_to_remove = []
            for client_id, client in self.client_records.items():
                if client.is_expired():
                    if client.is_marked_disconnected:
                        clients_to_remove.append(client_id)
                    else:
                        client.is_marked_disconnected = True

            for client_id in clients_to_remove:
                Logger.info(f"Removing disconnected client: {client_id}")
                del self.client_records[client_id]

    def _update_speed_data(self, span: float, bytes_down: int):
        """Update speed tracking data by distributing bytes over the span period."""
        current_time = time.time()
        current_5s_slot = int(current_time // Server.SPEED_INTERVAL) * Server.SPEED_INTERVAL  # Round down

        with self._speed_lock:
            # Calculate the time range to distribute bytes over
            start_time = current_time - span
            start_ks_slot = int(start_time // Server.SPEED_INTERVAL) * Server.SPEED_INTERVAL

            # Ensure we don't go beyond our 10-minute recording limit
            cutoff_time = current_5s_slot - 600
            start_ks_slot = max(start_ks_slot, cutoff_time)

            # Generate all slots in the span
            slots_in_span = []
            slot = start_ks_slot
            while slot <= current_5s_slot:
                slots_in_span.append(slot)
                slot += Server.SPEED_INTERVAL

            if slots_in_span:
                # Distribute bytes evenly across all slots in the span
                bytes_per_slot = bytes_down / len(slots_in_span)

                for slot_time in slots_in_span:
                    if slot_time >= cutoff_time:  # Only record within our time limit
                        if slot_time not in self.speed_data:
                            self.speed_data[slot_time] = 0
                        self.speed_data[slot_time] += bytes_per_slot

            # Clean up data
            keys_to_remove = [ts for ts in self.speed_data.keys() if ts < cutoff_time]
            for key in keys_to_remove:
                del self.speed_data[key]

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

            # Update speed tracking data
            self._update_speed_data(request.span, request.bytes_down)

            return StandardResponse(code=0, msg="success", data=None)

        @self.app.get("/get_stats", response_model=StandardResponse)
        async def get_stats():
            """Get aggregated statistics."""
            # Clean up expired clients before returning stats
            self._cleanup_expired_clients()

            with self._stats_lock:
                # Get speed data
                with self._speed_lock:
                    speed_down = {}

                    for timestamp, bytes_in_slot in self.speed_data.items():
                        speed_down[timestamp] = int(bytes_in_slot / Server.SPEED_INTERVAL)  # cut off float

                # Get client data
                with self._clients_lock:
                    clients = [
                        {"id": client_id, "last_seen": self.client_records[client_id].last_seen}
                        for client_id in self.client_records.keys()
                    ]

                stats_data = {
                    "stats": dict(sorted(self.total_stats.items(), key=lambda x: x[1], reverse=True)),
                    "clients": clients,
                    "bytes_down": self.total_bytes_down,
                    "speed_down": speed_down,
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
