"""
流量监控器 - 监控和统计应用程序网络流量使用情况
"""

import time
from typing import Dict, Optional
from threading import Lock
from dataclasses import dataclass


@dataclass
class TrafficSummary:
    """流量汇总信息"""
    total_bytes_sent: int
    total_bytes_received: int
    total_bytes: int
    duration_seconds: float
    avg_upload_rate_mbps: float
    avg_download_rate_mbps: float
    avg_total_rate_mbps: float


class TrafficMonitor:
    """应用程序流量监控器类 - 专门统计应用程序产生的网络流量"""
    
    def __init__(self):
        """初始化流量监控器"""
        self._lock = Lock()
        self._is_monitoring = False
        
        # 流量统计
        self._app_bytes_sent = 0
        self._app_bytes_received = 0
        self._app_start_time: Optional[float] = None
        self._request_count = 0
    
    def start_monitoring(self):
        """开始监控应用程序流量"""
        with self._lock:
            if self._is_monitoring:
                return
            
            self._is_monitoring = True
            self._app_start_time = time.time()
            self._app_bytes_sent = 0
            self._app_bytes_received = 0
            self._request_count = 0
            
            print(f"开始监控应用程序流量: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    def stop_monitoring(self) -> TrafficSummary:
        """停止监控并返回流量汇总"""
        with self._lock:
            if not self._is_monitoring:
                raise RuntimeError("流量监控未启动")
            
            duration = time.time() - self._app_start_time
            total_bytes = self._app_bytes_sent + self._app_bytes_received
            
            # 计算平均速率 (Mbps)
            avg_upload_rate = (self._app_bytes_sent * 8 / duration / 1024 / 1024) if duration > 0 else 0
            avg_download_rate = (self._app_bytes_received * 8 / duration / 1024 / 1024) if duration > 0 else 0
            avg_total_rate = (total_bytes * 8 / duration / 1024 / 1024) if duration > 0 else 0
            
            summary = TrafficSummary(
                total_bytes_sent=self._app_bytes_sent,
                total_bytes_received=self._app_bytes_received,
                total_bytes=total_bytes,
                duration_seconds=duration,
                avg_upload_rate_mbps=avg_upload_rate,
                avg_download_rate_mbps=avg_download_rate,
                avg_total_rate_mbps=avg_total_rate
            )
            
            self._is_monitoring = False
            return summary
    
    def get_current_stats(self) -> Optional[Dict[str, float]]:
        """获取当前的应用程序流量统计信息"""
        with self._lock:
            if not self._is_monitoring or not self._app_start_time:
                return None
            
            duration = time.time() - self._app_start_time
            
            if duration <= 0:
                return None
            
            total_bytes = self._app_bytes_sent + self._app_bytes_received
            
            return {
                "duration_seconds": duration,
                "total_bytes_sent": self._app_bytes_sent,
                "total_bytes_received": self._app_bytes_received,
                "total_bytes": total_bytes,
                "request_count": self._request_count,
                "avg_upload_rate_mbps": (self._app_bytes_sent * 8 / duration / 1024 / 1024),
                "avg_download_rate_mbps": (self._app_bytes_received * 8 / duration / 1024 / 1024),
                "avg_total_rate_mbps": (total_bytes * 8 / duration / 1024 / 1024)
            }
    
    def add_app_traffic(self, bytes_sent: int, bytes_received: int):
        """添加流量统计"""
        with self._lock:
            if self._is_monitoring:
                self._app_bytes_sent += bytes_sent
                self._app_bytes_received += bytes_received
                self._request_count += 1
    
    def get_total_bytes_sent(self) -> int:
        """获取总上传字节数"""
        with self._lock:
            return self._app_bytes_sent
    
    def get_total_bytes_received(self) -> int:
        """获取总下载字节数"""
        with self._lock:
            return self._app_bytes_received
    
    def get_total_bytes(self) -> int:
        """获取总流量字节数"""
        with self._lock:
            return self._app_bytes_sent + self._app_bytes_received
    
    def get_request_count(self) -> int:
        """获取请求总数"""
        with self._lock:
            return self._request_count
    
    def format_bytes(self, bytes_value: int) -> str:
        """格式化字节数为人类可读的格式"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_value < 1024.0:
                return f"{bytes_value:.2f} {unit}"
            bytes_value /= 1024.0
        return f"{bytes_value:.2f} PB"
    
    def print_summary(self, summary: TrafficSummary):
        """打印流量汇总信息"""
        print("\n" + "=" * 60)
        print("流量统计汇总")
        print("=" * 60)
        print(f"监控时长: {summary.duration_seconds:.1f} 秒")
        print(f"上传流量: {self.format_bytes(summary.total_bytes_sent)}")
        print(f"下载流量: {self.format_bytes(summary.total_bytes_received)}")
        print(f"总流量: {self.format_bytes(summary.total_bytes)}")
        print(f"平均上传速率: {summary.avg_upload_rate_mbps:.2f} Mbps")
        print(f"平均下载速率: {summary.avg_download_rate_mbps:.2f} Mbps")
        print(f"平均总速率: {summary.avg_total_rate_mbps:.2f} Mbps")
        print("=" * 60)