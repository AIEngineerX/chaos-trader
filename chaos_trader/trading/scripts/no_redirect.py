#!/usr/bin/env python3
"""Open a request that carries a Bearer token without following any redirect.

urllib's default opener re-sends every request header, Authorization included, to wherever a 3xx
points, even another host or plain http. The opener here refuses every redirect instead, so a 3xx
raises urllib.error.HTTPError with that status and the token is never sent to the new location.
The caller closes the HTTPError it catches, as with any other HTTP error.
"""
from __future__ import annotations

import http.client
import urllib.request


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(NoRedirectHandler)


def open_no_redirect(request: urllib.request.Request, timeout: float) -> http.client.HTTPResponse:
    """`urlopen` with redirects refused: a 3xx raises urllib.error.HTTPError carrying the 3xx code."""
    return _OPENER.open(request, timeout=timeout)
