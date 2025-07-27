from typing import Dict

from httpx import Request, Response, HTTPTransport, AsyncHTTPTransport


class NameSolver:
    def __init__(self, name_ip_map: Dict[str, str]) -> None:
        self._map = name_ip_map

    def resolve(self, request: Request) -> Request:
        host = request.url.host
        ip = self._map.get(host, "")

        if ip:
            request.extensions["sni_hostname"] = host
            request.url = request.url.copy_with(host=ip)

        return request


class CustomHost(HTTPTransport):
    def __init__(self, solver: NameSolver, *args, **kwargs) -> None:
        self.solver = solver
        super().__init__(*args, **kwargs)

    def handle_request(self, request: Request) -> Response:
        request = self.solver.resolve(request)
        return super().handle_request(request)


class AsyncCustomHost(AsyncHTTPTransport):
    def __init__(self, solver: NameSolver, *args, **kwargs) -> None:
        self.solver = solver
        super().__init__(*args, **kwargs)

    async def handle_async_request(self, request: Request) -> Response:
        request = self.solver.resolve(request)
        return await super().handle_async_request(request)
