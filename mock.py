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
from typing import Any, Dict, List, Optional
from textwrap import dedent

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


class RequestHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "QA Mock"

    @property
    def _request_summary(self) -> Dict[str, Dict[str, int]]:
        return self.server.request_summary

    def _send_response(self, code: int, content: str) -> None:
        self.send_response(code)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(f"{content}\n".encode())

    def _handle_route(self, route: Dict[str, Any]) -> None:
        response = route["reply"]
        if route["exec"]:
            exec_output = self.execute_command(route["exec"])
            response += f"\nExec: [{route['exec']}]\n{exec_output}"
        self._send_response(route["statuscode"], response)
        self._increment_request_count(route)

    def _increment_request_count(self, route: Dict[str, Any]) -> None:
        self._request_summary[route["endpoint"]][route["method"]] += 1

    def _read_payload(self) -> str:
        content_length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""

    def handle_request(self) -> None:
        route = next(
            (r for r in self.server.routes if r["endpoint"] == self.path and r["method"] == self.command),
            None,
        )
        if route:
            self._handle_route(route)
        else:
            self._send_response(404, "Not Found")
            self._increment_request_count({"endpoint": self.path, "method": self.command})

    def log_message(self, format: str, *args) -> None:
        message = format % args
        payload = self._read_payload()
        print(f"{time.strftime('%F %T')} | {self.address_string()} | {message} | {payload}")

    def do_GET(self) -> None:
        self.handle_request()

    def do_LIST(self) -> None:
        self._send_response(200, json.dumps(self.server.routes, indent=2))

    def do_KILL(self) -> None:
        self._send_response(666, json.dumps(dict(self._request_summary), indent=2))
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
    def execute_command(command: str) -> str:
        """
        Execute a shell command and return its stdout.

        WARNING: Commands are executed via shell=True. Only enable route exec
        via --allow-exec when the routes file is fully trusted. Untrusted input
        can lead to arbitrary code execution.
        """
        try:
            result = subprocess.run(
                command,
                shell=True,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            return f"Error executing command: {e.stderr}"
        except subprocess.TimeoutExpired:
            return "Command execution timed out after 10 seconds"


class MockHTTPServer(http.server.HTTPServer):
    def __init__(self, server_address, RequestHandlerClass, routes: List[Dict[str, Any]]) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.routes = routes
        self.request_summary: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))


def load_routes(file_path: str, allow_exec: bool = False) -> List[Dict[str, Any]]:
    with open(file_path, "r") as file:
        data = csv.DictReader(file) if file_path.endswith(".csv") else json.load(file)
        routes = [set_route_defaults(route) for route in data]

    if not allow_exec:
        for route in routes:
            if route.get("exec"):
                print(f"WARNING: exec ignored for {route['method']} {route['endpoint']} (use --allow-exec to enable)")
                route["exec"] = ""

    return routes


def set_route_defaults(route: Dict[str, Any]) -> Dict[str, Any]:
    return ROUTE_DEFAULTS | {k: v for k, v in route.items() if v is not None}


def start_mock(
    host: str,
    port: int,
    routes: List[Dict[str, Any]],
    certfile: Optional[str] = None,
    keyfile: Optional[str] = None,
) -> None:
    mock = MockHTTPServer((host, port), RequestHandler, routes)

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

    if not allow_exec:
        for route in routes:
            if route.get("exec"):
                print(f"WARNING: exec ignored for {route['method']} {route['endpoint']} (use --allow-exec to enable)")
                route["exec"] = ""

    return routes


def main():
    parser = argparse.ArgumentParser(
        description="Start an HTTP server based on a CSV or JSON specification or command-line arguments.",
        epilog=dedent(f"""\
        examples:
            Default route: --default
            {ROUTE_DEFAULTS=} (this will be overridden by --route)

            JSON route file: --routesfile routes.json
            [
                {{"endpoint": "/test", "method": "GET", "statuscode": 200, "reply": "OK", "exec": "echo 'Hello World'"}}
            ]

            CSV route file: --routesfile routes.csv
            endpoint,method,statuscode,reply,exec
            /test,GET,200,OK,"echo 'Hello World'"
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default="localhost", help="The host to listen on.")
    parser.add_argument("--port", type=int, default=4443, help="The port to listen on.")
    parser.add_argument("--default", action="store_true", help="Add a default 'GET /' route.")
    parser.add_argument("--routesfile", help="The CSV or JSON file specifying the API endpoints.")
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
        help="Allow routes to execute shell commands via 'exec'. "
             "Only use with trusted route files. Enables arbitrary code execution.",
    )

    args = parser.parse_args()

    routes = (
        ([set_route_defaults({})] if args.default else [])
        + (load_routes(args.routesfile, args.allow_exec) if args.routesfile else [])
        + (parse_cli_routes(args.route, args.allow_exec) if args.route else [])
    )

    if not routes:
        routes = [set_route_defaults({})]

    print(json.dumps(routes, indent=2))
    start_mock(args.host, args.port, routes, args.certfile, args.keyfile)


if __name__ == "__main__":
    main()
