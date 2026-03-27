#!/usr/bin/env python3
import argparse
import csv
import http.server
import json
import ssl
import subprocess
import sys
import time
from collections import defaultdict
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from textwrap import dedent
from typing import Any, Dict, List, Optional

ROUTE_DEFAULTS = {
    "endpoint": "/",
    "method": "GET",
    "statuscode": 200,
    "reply": "OK",
    "exec": "",
}


class HTTPMethod(Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    OPTIONS = "OPTIONS"
    HEAD = "HEAD"
    LIST = "LIST"
    KILL = "KILL"
    TRACE = "TRACE"
    ANY = "*"


class RequestHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "QA Mock"

    _exec_log: str = ""  # reset per-request in handle_request; default for special methods

    @property
    def _request_summary(self) -> Dict[str, Dict[str, int]]:
        return self.server.request_summary

    def _send_response(self, code: int, content: str, content_type: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(f"{content}\n".encode())

    def _handle_route(self, route: Dict[str, Any]) -> None:
        reply = route["reply"]
        is_json = isinstance(reply, (dict, list))
        response = json.dumps(reply) if is_json else reply
        if route["exec"]:
            if self.server.allow_exec:
                exec_output, rc = self.execute_command(route["exec"])
                self._exec_log = f"Exec: [{route['exec']}] (rc={rc})"
                response += f"\n{self._exec_log}\n{exec_output}"
            else:
                self._exec_log = f"*!* exec commands require explicit --allow-exec | NOT Exec: [{route['exec']}]"
                response += f"\n{self._exec_log}"
        self._send_response(route["statuscode"], response, content_type="application/json" if is_json else "text/plain")
        self._increment_request_count(route)

    def _increment_request_count(self, route: Dict[str, Any]) -> None:
        # Record actual requested path/method, not the (possibly wildcard) route pattern.
        self._request_summary[self.path][self.command] += 1

    def _read_payload(self) -> str:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""
        except AttributeError:
            return ""

    def handle_request(self) -> None:
        self._exec_log = ""
        try:
            method = HTTPMethod(self.command)
        except ValueError:
            self._send_response(405, f"Method Not Allowed: {self.command}")
            return

        idx = self.server.route_index
        route = (
            idx.get((self.path, method.value))  # exact
            or idx.get((self.path, "*"))  # wildcard method
            or idx.get(("*", method.value))  # wildcard endpoint
            or idx.get(("*", "*"))  # both wildcards
        )
        if route:
            self._handle_route(route)
        else:
            self._send_response(404, "Not Found")
            self._request_summary[self.path][self.command] += 1

    def log_message(self, format: str, *args) -> None:
        message = format % args
        payload = self._read_payload()
        parts = [time.strftime('%F %T'), self.address_string(), message]
        if payload:
            parts.append(payload)
        if self._exec_log:
            parts.append(self._exec_log)
        print(" | ".join(parts))

    def do_GET(self) -> None:
        self.handle_request()

    def do_OPTIONS(self) -> None:
        if not self.server.allow_options:
            self._send_response(403, "OPTIONS endpoint disabled. Use --allow-options to enable.")
            return
        _REDACTED = ("certfile", "keyfile")
        safe_args = {k: "<redacted>" if k in _REDACTED else v for k, v in self.server.cli_args.items()}
        info = {
            "api_file": self.server.api_file,
            "args": safe_args,
            "routes": [{k: v for k, v in r.items() if v != ""} for r in self.server.routes],
        }
        self._send_response(200, json.dumps(info, indent=2), content_type="application/json")

    def do_LIST(self) -> None:
        self._send_response(200, json.dumps(self.server.routes, indent=2), content_type="application/json")

    def do_KILL(self) -> None:
        self._send_response(666, json.dumps(dict(self._request_summary), indent=2), content_type="application/json")
        self.wfile.flush()
        sys.exit(666)

    def do_TRACE(self) -> None:
        self.send_response(200)
        self.send_header("Content-type", "message/http")
        self.end_headers()
        response_body = f"{self.requestline}\r\n"
        response_body += "".join(f"{key}: {value}\r\n" for key, value in self.headers.items())
        response_body += "\r\n"
        self.wfile.write(response_body.encode())

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):
            return self.handle_request
        raise AttributeError(f"{self.__class__.__name__} object has no attribute {name}")

    @staticmethod
    def execute_command(command: str) -> tuple:
        """
        Execute a shell command and return (stdout, returncode).

        WARNING: Commands are executed via shell=True. Only enable route exec
        via --allow-exec when the routes file is fully trusted. Untrusted input
        can lead to arbitrary code execution.

        NOTE: _read_payload consumes rfile in log_message, which runs after the
        response is sent. If body-based route matching is ever added, payload
        must be read in handle_request before routing, not in log_message.
        """
        try:
            result = subprocess.run(
                command,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout
            if result.stderr:
                output += f"\nstderr: {result.stderr}"
            return output, result.returncode
        except subprocess.TimeoutExpired:
            return "Command execution timed out after 10 seconds", -1


class MockHTTPServer(http.server.HTTPServer):
    def __init__(
        self,
        server_address,
        RequestHandlerClass,
        routes: List[Dict[str, Any]],
        allow_exec: bool = False,
        allow_options: bool = False,
        api_file: Optional[str] = None,
        cli_args: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.routes = routes
        # Exact matches keyed by (endpoint, method); wildcards resolved at request time.
        self.route_index: Dict[tuple, Dict[str, Any]] = {(r["endpoint"], r["method"]): r for r in routes}
        self.request_summary: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.allow_exec = allow_exec
        self.allow_options = allow_options
        self.api_file = api_file
        self.cli_args = cli_args or {}


def _strip_exec(routes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for route in routes:
        if route.get("exec"):
            print(f"WARNING: exec ignored for {route['method']} {route['endpoint']} (use --allow-exec to enable)")
            route["exec"] = ""
    return routes


def load_api_file(file_path: str, allow_exec: bool = False) -> tuple:
    """
    Load routes from a CSV, a plain JSON routes array, or a full JSON config.

    Full config keys (all optional except routes):
        hostname, port, cert, key, routes (list)

    Returns (routes, overrides) where overrides is a dict with any of
    host/port/certfile/keyfile found in the config.
    """
    overrides: Dict[str, Any] = {}

    with open(file_path, "r") as file:
        if file_path.endswith(".csv"):
            raw = list(csv.DictReader(file))
        else:
            data = json.load(file)
            if isinstance(data, list):
                raw = data
            else:
                overrides = {
                    k: v
                    for k, v in {
                        "host": data.get("hostname"),
                        "port": int(data["port"]) if "port" in data else None,
                        "certfile": data.get("cert"),
                        "keyfile": data.get("key"),
                    }.items()
                    if v is not None
                }
                raw = data.get("routes", [])

    routes = [set_route_defaults(r) for r in raw]
    return (routes if allow_exec else _strip_exec(routes)), overrides


def set_route_defaults(route: Dict[str, Any]) -> Dict[str, Any]:
    merged = ROUTE_DEFAULTS | {k: v for k, v in route.items() if v is not None}
    method = merged["method"]
    valid_methods = {m.value for m in HTTPMethod}
    if method not in valid_methods:
        print(f"WARNING: unrecognised method '{method}' for endpoint '{merged['endpoint']}' — route will never match")
    return merged


def start_mock(
    host: str,
    port: int,
    routes: List[Dict[str, Any]],
    certfile: Optional[str] = None,
    keyfile: Optional[str] = None,
    allow_exec: bool = False,
    allow_options: bool = False,
    api_file: Optional[str] = None,
    cli_args: Optional[Dict[str, Any]] = None,
) -> None:
    mock = MockHTTPServer((host, port), RequestHandler, routes, allow_exec=allow_exec, allow_options=allow_options, api_file=api_file, cli_args=cli_args)

    if bool(certfile) ^ bool(keyfile):
        missing = "keyfile" if certfile else "certfile"
        print(f"WARNING: --{missing} not provided — ignoring SSL config, serving plain HTTP")
        certfile = keyfile = None

    if certfile and keyfile:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        mock.socket = context.wrap_socket(mock.socket, server_side=True)
        protocol = "https"
    else:
        protocol = "http"

    print(f"Serving on {protocol}://{host}:{port}")

    try:
        mock.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        print(f"Requests summary: {json.dumps(mock.request_summary, indent=2)}")
        mock.server_close()
        sys.exit(0)


def parse_cli_routes(cli_routes: List[str], allow_exec: bool = False) -> List[Dict[str, Any]]:
    routes = [set_route_defaults(json.loads(route)) for route in cli_routes]
    return routes if allow_exec else _strip_exec(routes)


def main():
    parser = argparse.ArgumentParser(
        description="Start an HTTP server based on a CSV or JSON specification or command-line arguments.",
        epilog=dedent("""        route defaults:
            endpoint=/  method=GET  statuscode=200  reply=OK  exec=

        route fields:
            endpoint    URL path to match. Use * for catch-all.
            method      HTTP method to match. Use * for any method.
            statuscode  HTTP status code to return (default: 200).
            reply       Response body: string, JSON object, or JSON array.
            exec        Shell command to run (requires --allow-exec).

        special methods (built-in, not configurable via routes):
            LIST        Returns all configured routes as JSON.
            KILL        Returns request summary as JSON and exits with code 666.
            TRACE       Echoes back the raw request headers (RFC 7231 diagnostic).
            OPTIONS     Returns runtime config and routes (requires --allow-options).

        wildcard examples:
            {"endpoint": "/health", "method": "*",   "statuscode": 200, "reply": "OK"}
            {"endpoint": "*",       "method": "GET", "statuscode": 200, "reply": "OK"}
            {"endpoint": "*",       "method": "*",   "statuscode": 200, "reply": "OK"}

        match priority (highest to lowest):
            1. exact endpoint + exact method
            2. exact endpoint + wildcard method (*)
            3. wildcard endpoint (*) + exact method
            4. wildcard endpoint (*) + wildcard method (*)

        --api-file formats:

            1. JSON routes array:
            [
                {"endpoint": "/test",  "method": "GET",  "statuscode": 200, "reply": "OK"},
                {"endpoint": "/token", "method": "POST", "reply": {"token": "abc123"}},
                {"endpoint": "/data",  "method": "GET",  "reply": [1, 2, 3]},
                {"endpoint": "/run",   "method": "GET",  "exec": "uptime"}
            ]

            2. JSON full config (overrides --host/--port/--certfile/--keyfile):
            {
                "hostname": "example.com",
                "port": 443,
                "cert": "/etc/ssl/cert.pem",
                "key":  "/etc/ssl/key.pem",
                "routes": [
                    {"endpoint": "/test", "method": "GET", "statuscode": 200, "reply": "OK"}
                ]
            }

            3. CSV routes file:
            endpoint,method,statuscode,reply,exec
            /test,GET,200,OK,
            /run,GET,200,,uptime

        --route inline (repeatable):
            --route '{"endpoint": "/ping", "method": "GET", "reply": "pong"}'
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    try:
        __version__ = version("qamock")
    except PackageNotFoundError:
        __version__ = "dev"
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--host", default="localhost", help="The host to listen on.")
    parser.add_argument("--port", type=int, default=4443, help="The port to listen on.")
    parser.add_argument("--default", action="store_true", help="Add a default 'GET /' route.")
    parser.add_argument("--api-file", help="CSV, JSON routes file, or JSON config with host/port/cert/key/routes.")
    parser.add_argument(
        "--route",
        action="append",
        help="JSON string specifying a single route. Can be used multiple times.",
    )
    parser.add_argument("--certfile", help="The SSL certificate file.")
    parser.add_argument("--keyfile", help="The SSL key file.")
    parser.add_argument(
        "--allow-exec",
        action="store_true",
        help="Allow routes to execute shell commands via 'exec'. Only use with trusted route files. Enables arbitrary code execution.",
    )
    parser.add_argument(
        "--allow-options",
        action="store_true",
        help="Enable OPTIONS endpoint, which exposes routes and runtime args.",
    )

    args = parser.parse_args()

    if not any([args.default, args.api_file, args.route]):
        parser.print_help()
        sys.exit(0)

    overrides = {}
    file_routes = []
    if args.api_file:
        file_routes, overrides = load_api_file(args.api_file, args.allow_exec)

    routes = ([set_route_defaults({})] if args.default else []) + file_routes + (parse_cli_routes(args.route, args.allow_exec) if args.route else [])

    host = overrides.get("host", args.host)
    port = overrides.get("port", args.port)
    certfile = overrides.get("certfile", args.certfile)
    keyfile = overrides.get("keyfile", args.keyfile)

    printable = [{k: v for k, v in r.items() if v != ""} for r in routes]
    print(json.dumps(printable, indent=2))
    cli_args = {k: v for k, v in vars(args).items() if v is not None and v is not False}
    start_mock(host, port, routes, certfile, keyfile, allow_exec=args.allow_exec, allow_options=args.allow_options, api_file=args.api_file, cli_args=cli_args)


if __name__ == "__main__":
    main()
