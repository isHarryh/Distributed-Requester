from typing import Optional, Dict, Any, Union, List, Literal

import json
from datetime import datetime, timezone, timedelta

import httpx
from pydantic import BaseModel, Field, field_validator


VERSION = "0.6"
COMPATIBLE_VERSIONS = ["0.1", "0.2", "0.3", "0.4", "0.5", "0.6"]


class ConfigError(Exception):
    """Configuration error exception"""


class ScheduleConfig(BaseModel):
    """Schedule configuration"""

    start: Optional[Union[str, int, float]] = None
    end: Optional[Union[str, int, float]] = None

    def get_start_time(self) -> datetime:
        return parse_datetime(self.start) if self.start is not None else datetime.now().astimezone()

    def get_end_time(self) -> Optional[datetime]:
        if self.end is not None:
            return (
                self.get_start_time() + timedelta(seconds=self.end)
                if isinstance(self.end, (int, float))
                else parse_datetime(self.end)
            )
        return None


class LimitsConfig(BaseModel):
    """Limits configuration"""

    rps: float = float("inf")
    rpm: float = float("inf")
    coroutines: int = 64


class TimeoutsConfig(BaseModel):
    """Timeouts configuration (all values in seconds)"""

    connect: float = 5.0
    read: float = 10.0
    write: float = 10.0

    def build(self) -> httpx.Timeout:
        return httpx.Timeout(connect=self.connect, read=self.read, write=self.write, pool=None)


class ProxyConfig(BaseModel):
    """Proxy policy configuration"""

    order: Literal["random", "sequential", "switchByRule"] = "random"
    preflight: bool = True
    preflight_url: Optional[str] = None
    preflight_timeout: float = 5.0
    preflight_max_switches: int = 3

    @field_validator("preflight_url")
    @classmethod
    def validate_preflight_url(cls, v):
        if v and not v.startswith(("http://", "https://")):
            raise ConfigError("Preflight URL must start with http:// or https://")
        return v


class PolicyConfig(BaseModel):
    """Policy configuration"""

    reuse_connections: bool = True
    order: Literal["random"] = "random"
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    timeouts: TimeoutsConfig = Field(default_factory=TimeoutsConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)


class RuleConfigBase(BaseModel):
    """Base class for rule configurations"""

    event: Literal["onConsecutiveStatus"]
    action: Literal[
        "switchToNextProxy",
        "switchToPrevProxy",
        "switchToRandomProxy",
        "stopCurrentTask",
        "stopProgram",
    ]


class ConsecutiveStatusRuleConfig(RuleConfigBase):
    """Rule configuration for consecutive status events"""

    event: Literal["onConsecutiveStatus"]
    status: List[str]
    count: int

    @field_validator("count")
    @classmethod
    def validate_count(cls, v):
        if v <= 0:
            raise ConfigError("Count must be positive")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if not v or len(v) == 0:
            raise ConfigError("Status list cannot be empty")
        # Note: Actual status validation should be done against ResponseStatus enum
        return v


class PrefabsConfig(BaseModel):
    """Prefabs configuration for task-level settings"""

    override_hosts: Dict[str, str] = Field(default_factory=dict)
    default_headers: Dict[str, str] = Field(default_factory=dict)


class RequestConfig(BaseModel):
    """Request configuration"""

    url: str
    method: str = "GET"
    data: Optional[Union[str, Dict[str, Any]]] = None
    headers: Dict[str, str] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v):
        if not v.startswith(("http://", "https://")):
            raise ConfigError("URL must start with http:// or https://")
        return v

    @field_validator("method")
    @classmethod
    def validate_method(cls, v):
        return v.upper()


class TaskConfig(BaseModel):
    """Task configuration"""

    name: str
    requests: List[RequestConfig]
    rules: List[ConsecutiveStatusRuleConfig] = Field(default_factory=list)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    prefabs: PrefabsConfig = Field(default_factory=PrefabsConfig)
    proxies: List[Optional[str]] = Field(default_factory=list)

    @field_validator("requests")
    @classmethod
    def validate_requests(cls, v):
        if not v or len(v) == 0:
            raise ConfigError("requests field is required and cannot be empty")
        return v

    @field_validator("proxies")
    @classmethod
    def validate_proxies(cls, v):
        for proxy in v:
            if proxy is not None and not isinstance(proxy, str):
                raise ConfigError("Proxy must be a string or null")
            if proxy is not None and not (proxy.startswith(("http://", "https://", "socks5://", "socks4://"))):
                raise ConfigError("Proxy URL must start with http://, https://, socks4://, or socks5://")
        return v


class DistributingConfig(BaseModel):
    """Distributing configuration for server"""

    task_order: Literal["random"] = "random"


class ServerConfig(BaseModel):
    """Server configuration for distributed mode"""

    port: int
    distributing: DistributingConfig = Field(default_factory=DistributingConfig)

    @field_validator("port")
    @classmethod
    def validate_port(cls, v):
        if not (1 <= v <= 65535):
            raise ConfigError(f"Port must be between 1 and 65535, got {v}")
        return v


class ReportConfig(BaseModel):
    """Report configuration for client"""

    live_report_interval: int = 30

    @field_validator("live_report_interval")
    @classmethod
    def validate_interval(cls, v):
        if v < 0:
            raise ConfigError(f"Report interval must be non-negative, got {v}")
        return v


class ClientConfig(BaseModel):
    """Client configuration for distributed mode"""

    server_url: str
    report: ReportConfig = Field(default_factory=ReportConfig)

    @field_validator("server_url")
    @classmethod
    def validate_server_url(cls, v):
        if not v.startswith(("http://", "https://")):
            raise ConfigError("Server URL must start with http:// or https://")
        return v


class Config(BaseModel):
    """Main configuration"""

    version: str
    tasks: List[TaskConfig]
    server: Optional[ServerConfig] = None
    client: Optional[ClientConfig] = None

    @field_validator("version")
    @classmethod
    def validate_version(cls, v):
        if not v:
            raise ConfigError("Version is required")
        return v


def parse_datetime(value: Union[str, int, float]) -> datetime:
    """Parse datetime string or numeric offset"""
    if isinstance(value, (int, float)):
        # Number represents seconds offset from now
        return (datetime.now(timezone.utc) + timedelta(seconds=value)).astimezone()

    if isinstance(value, str):
        try:
            # Try to parse ISO format datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ConfigError(f"Unable to parse datetime '{value}'")

    raise ConfigError(f"Invalid datetime value type: {type(value)}")


def load_config(config_path: str) -> Config:
    """Load configuration file and return Pydantic config object"""
    import os
    import sys

    try:
        if os.path.exists(config_path):
            path = config_path
        elif getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            # Try to get config file from frozen package
            base_path = getattr(sys, "_MEIPASS")
            path = os.path.join(base_path, config_path)
            if not os.path.exists(path):
                raise FileNotFoundError(config_path)
        else:
            raise FileNotFoundError(config_path)

        with open(path, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
            return Config(**data)

    except FileNotFoundError:
        raise ConfigError(f"Configuration file '{config_path}' not found")
    except json.JSONDecodeError as e:
        raise ConfigError(f"Configuration file format error: {e}")
    except ValueError as e:
        raise ConfigError(f"Configuration validation failed: {e}")
    except Exception as e:
        raise ConfigError(f"Failed to read configuration file: {e}")
