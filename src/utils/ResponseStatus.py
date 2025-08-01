from enum import Enum
from typing import Union

import httpx
import socksio


class ResponseStatus(Enum):
    """Response status categories."""

    INFORMATION = "Information"
    SUCCESS = "Success"
    REDIRECTED = "Redirected"
    HTTP_ERROR = "HTTP Error"

    CONNECT_TIMEOUT = "Connect Timeout"
    READ_TIMEOUT = "Read Timeout"
    WRITE_TIMEOUT = "Write Timeout"
    POOL_TIMEOUT = "Pool Timeout"

    NETWORK_ERROR = "Network Error"
    PROTOCOL_ERROR = "Protocol Error"
    TOO_MANY_REDIRECTS = "Too Many Redirects"
    UNCAUGHT_EXCEPTION = "Uncaught Exception"
    UNKNOWN = "Unknown"

    @staticmethod
    def of(response: Union[httpx.Response, Exception]) -> "ResponseStatus":
        if isinstance(response, httpx.Response):
            if 100 <= response.status_code < 200:
                return ResponseStatus.INFORMATION
            elif 200 <= response.status_code < 300:
                return ResponseStatus.SUCCESS
            elif 300 <= response.status_code < 400:
                return ResponseStatus.REDIRECTED
            return ResponseStatus.HTTP_ERROR
        elif isinstance(response, httpx.ConnectTimeout):
            return ResponseStatus.CONNECT_TIMEOUT
        elif isinstance(response, httpx.ReadTimeout):
            return ResponseStatus.READ_TIMEOUT
        elif isinstance(response, httpx.WriteTimeout):
            return ResponseStatus.WRITE_TIMEOUT
        elif isinstance(response, httpx.PoolTimeout):
            return ResponseStatus.POOL_TIMEOUT
        elif isinstance(response, httpx.NetworkError):
            return ResponseStatus.NETWORK_ERROR
        elif isinstance(response, (httpx.ProtocolError, socksio.ProtocolError)):
            return ResponseStatus.PROTOCOL_ERROR
        elif isinstance(response, httpx.TooManyRedirects):
            return ResponseStatus.TOO_MANY_REDIRECTS
        elif isinstance(response, Exception):
            return ResponseStatus.UNCAUGHT_EXCEPTION
        return ResponseStatus.UNKNOWN

    def is_exception(self):
        return self not in {
            ResponseStatus.INFORMATION,
            ResponseStatus.SUCCESS,
            ResponseStatus.REDIRECTED,
            ResponseStatus.HTTP_ERROR,
        }


class HttpStatus(Enum):
    """
    HTTP status codes.

    Ported from Starlette's HTTP status codes.

    - https://www.iana.org/assignments/http-status-codes/http-status-codes.xhtml

    - https://tools.ietf.org/html/rfc2324
    """

    UNKNOWN = 0

    HTTP_100_CONTINUE = 100
    HTTP_101_SWITCHING_PROTOCOLS = 101
    HTTP_102_PROCESSING = 102
    HTTP_103_EARLY_HINTS = 103

    HTTP_200_OK = 200
    HTTP_201_CREATED = 201
    HTTP_202_ACCEPTED = 202
    HTTP_203_NON_AUTHORITATIVE_INFORMATION = 203
    HTTP_204_NO_CONTENT = 204
    HTTP_205_RESET_CONTENT = 205
    HTTP_206_PARTIAL_CONTENT = 206
    HTTP_207_MULTI_STATUS = 207
    HTTP_208_ALREADY_REPORTED = 208
    HTTP_226_IM_USED = 226

    HTTP_300_MULTIPLE_CHOICES = 300
    HTTP_301_MOVED_PERMANENTLY = 301
    HTTP_302_FOUND = 302
    HTTP_303_SEE_OTHER = 303
    HTTP_304_NOT_MODIFIED = 304
    HTTP_305_USE_PROXY = 305
    HTTP_306_RESERVED = 306
    HTTP_307_TEMPORARY_REDIRECT = 307
    HTTP_308_PERMANENT_REDIRECT = 308

    HTTP_400_BAD_REQUEST = 400
    HTTP_401_UNAUTHORIZED = 401
    HTTP_402_PAYMENT_REQUIRED = 402
    HTTP_403_FORBIDDEN = 403
    HTTP_404_NOT_FOUND = 404
    HTTP_405_METHOD_NOT_ALLOWED = 405
    HTTP_406_NOT_ACCEPTABLE = 406
    HTTP_407_PROXY_AUTHENTICATION_REQUIRED = 407
    HTTP_408_REQUEST_TIMEOUT = 408
    HTTP_409_CONFLICT = 409
    HTTP_410_GONE = 410
    HTTP_411_LENGTH_REQUIRED = 411
    HTTP_412_PRECONDITION_FAILED = 412
    HTTP_413_REQUEST_ENTITY_TOO_LARGE = 413
    HTTP_414_REQUEST_URI_TOO_LONG = 414
    HTTP_415_UNSUPPORTED_MEDIA_TYPE = 415
    HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE = 416
    HTTP_417_EXPECTATION_FAILED = 417
    HTTP_418_IM_A_TEAPOT = 418
    HTTP_421_MISDIRECTED_REQUEST = 421
    HTTP_422_UNPROCESSABLE_ENTITY = 422
    HTTP_423_LOCKED = 423
    HTTP_424_FAILED_DEPENDENCY = 424
    HTTP_425_TOO_EARLY = 425
    HTTP_426_UPGRADE_REQUIRED = 426
    HTTP_428_PRECONDITION_REQUIRED = 428
    HTTP_429_TOO_MANY_REQUESTS = 429
    HTTP_431_REQUEST_HEADER_FIELDS_TOO_LARGE = 431
    HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS = 451

    HTTP_500_INTERNAL_SERVER_ERROR = 500
    HTTP_501_NOT_IMPLEMENTED = 501
    HTTP_502_BAD_GATEWAY = 502
    HTTP_503_SERVICE_UNAVAILABLE = 503
    HTTP_504_GATEWAY_TIMEOUT = 504
    HTTP_505_HTTP_VERSION_NOT_SUPPORTED = 505
    HTTP_506_VARIANT_ALSO_NEGOTIATES = 506
    HTTP_507_INSUFFICIENT_STORAGE = 507
    HTTP_508_LOOP_DETECTED = 508
    HTTP_510_NOT_EXTENDED = 510
    HTTP_511_NETWORK_AUTHENTICATION_REQUIRED = 511

    @staticmethod
    def of(response: httpx.Response) -> "HttpStatus":
        if response and response.status_code in HttpStatus._value2member_map_:
            return HttpStatus(response.status_code)
        return HttpStatus.UNKNOWN
