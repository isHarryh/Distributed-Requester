from pathlib import Path
from typing import Dict, Iterable

from src.Config import FilePartConfig


class FilePool:
    """Manages upload file validation and optional in-memory caching."""

    def __init__(self, files: Iterable[FilePartConfig]):
        self._paths: Dict[str, Path] = {}
        self._cache: Dict[str, bytes] = {}
        self.prepare(files)

    @classmethod
    def from_task(cls, task_config):
        """Builds a file pool from all upload files in a task."""
        return cls(file for request in task_config.requests for file in request.files)

    @staticmethod
    def _key(path: str) -> str:
        return str(Path(path).expanduser().resolve())

    def prepare(self, files: Iterable[FilePartConfig]):
        """Checks declared files and preloads cached entries."""
        for file in files:
            path = Path(file.path).expanduser()
            key = self._key(file.path)

            if not path.is_file():
                raise FileNotFoundError(f"Upload file does not exist: {file.path}")

            self._paths[key] = path
            if file.cache and key not in self._cache:
                self._cache[key] = path.read_bytes()

    def get(self, path: str) -> bytes:
        """Returns file bytes by path, using cache when configured."""
        key = self._key(path)
        if key in self._cache:
            return self._cache[key]

        if key not in self._paths:
            file_path = Path(path).expanduser()
            if not file_path.is_file():
                raise FileNotFoundError(f"Upload file does not exist: {path}")
            self._paths[key] = file_path

        return self._paths[key].read_bytes()
