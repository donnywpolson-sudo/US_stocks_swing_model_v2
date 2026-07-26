from __future__ import annotations

from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..errors import NetworkGuardError


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        raise NetworkGuardError(
            f"credentialed provider redirect rejected before retransmission: HTTP {code}"
        )


def open_without_redirects(request: Request, *, timeout_seconds: int):
    """Open one request without installing or mutating a process-global opener."""

    return build_opener(_RejectRedirects()).open(request, timeout=timeout_seconds)
