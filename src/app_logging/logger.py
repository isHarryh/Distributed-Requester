"""
日志记录器 - 提供结构化日志记录功能
"""

import os
import json
import time
from enum import Enum
from typing import Optional, Dict, Any, Union
from datetime import datetime, timezone
from threading import Lock


class LogLevel(Enum):
    """日志级别枚举"""
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50


class Logger:
    """日志记录器类"""
    
    def __init__(self, name: str, log_dir: str = "logs", log_level: LogLevel = LogLevel.INFO):
        self.name = name
        self.log_level = log_level
        self.log_dir = log_dir
        self._lock = Lock()
        
        # 创建日志目录
        os.makedirs(log_dir, exist_ok=True)
        
        # 生成日志文件名（按日期）
        today = datetime.now().strftime("%Y%m%d")
        self.log_file = os.path.join(log_dir, f"{name}_{today}.log")
        
        # 初始化日志文件
        self._init_log_file()
    
    def _init_log_file(self):
        """初始化日志文件"""
        if not os.path.exists(self.log_file):
            with open(self.log_file, 'w', encoding='utf-8') as f:
                f.write(f"# Log file created at {datetime.now().isoformat()}\n")
    
    def _should_log(self, level: LogLevel) -> bool:
        """检查是否应该记录此级别的日志"""
        return level.value >= self.log_level.value
    
    def _format_message(self, level: LogLevel, message: str, extra: Optional[Dict[str, Any]] = None) -> str:
        """格式化日志消息"""
        timestamp = datetime.now(timezone.utc).isoformat()
        
        log_entry = {
            "timestamp": timestamp,
            "level": level.name,
            "logger": self.name,
            "message": message
        }
        
        if extra:
            log_entry["extra"] = extra
        
        return json.dumps(log_entry, ensure_ascii=False)
    
    def _write_log(self, level: LogLevel, message: str, extra: Optional[Dict[str, Any]] = None):
        """写入日志到文件"""
        if not self._should_log(level):
            return
        
        formatted_message = self._format_message(level, message, extra)
        
        with self._lock:
            try:
                with open(self.log_file, 'a', encoding='utf-8') as f:
                    f.write(formatted_message + '\n')
                    f.flush()
            except Exception as e:
                # 如果写入失败，输出到控制台
                print(f"Failed to write log: {e}")
                print(f"Original message: {formatted_message}")
    
    def debug(self, message: str, **kwargs):
        """记录DEBUG级别日志"""
        self._write_log(LogLevel.DEBUG, message, kwargs if kwargs else None)
    
    def info(self, message: str, **kwargs):
        """记录INFO级别日志"""
        self._write_log(LogLevel.INFO, message, kwargs if kwargs else None)
    
    def warning(self, message: str, **kwargs):
        """记录WARNING级别日志"""
        self._write_log(LogLevel.WARNING, message, kwargs if kwargs else None)
    
    def error(self, message: str, **kwargs):
        """记录ERROR级别日志"""
        self._write_log(LogLevel.ERROR, message, kwargs if kwargs else None)
    
    def critical(self, message: str, **kwargs):
        """记录CRITICAL级别日志"""
        self._write_log(LogLevel.CRITICAL, message, kwargs if kwargs else None)
    
    def log_request(self, method: str, url: str, status_code: Optional[int] = None, 
                   response_time: Optional[float] = None, error: Optional[str] = None):
        """记录HTTP请求日志"""
        extra = {
            "method": method,
            "url": url,
            "status_code": status_code,
            "response_time_ms": round(response_time * 1000, 2) if response_time else None,
            "error": error
        }
        
        if error:
            self.error(f"Request failed: {method} {url}", **extra)
        elif status_code and status_code >= 400:
            self.warning(f"Request error: {method} {url} -> {status_code}", **extra)
        else:
            self.info(f"Request: {method} {url} -> {status_code}", **extra)
    
    def log_stats(self, stats_type: str, stats_data: Dict[str, Any]):
        """记录统计信息"""
        self.info(f"Stats: {stats_type}", stats=stats_data)
    
    def log_traffic(self, bytes_sent: int, bytes_received: int, duration: float):
        """记录流量信息"""
        extra = {
            "bytes_sent": bytes_sent,
            "bytes_received": bytes_received,
            "duration_seconds": round(duration, 2),
            "upload_rate_mbps": round(bytes_sent * 8 / duration / 1024 / 1024, 2) if duration > 0 else 0,
            "download_rate_mbps": round(bytes_received * 8 / duration / 1024 / 1024, 2) if duration > 0 else 0
        }
        self.info("Traffic statistics", **extra)