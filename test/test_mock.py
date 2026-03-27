#!/usr/bin/env python3
"""Unit tests for mock.py"""
import json
import sys
import tempfile
import threading
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "..")
from mock import (
    ROUTE_DEFAULTS,
    MockHTTPServer,
    RequestHandler,
    _strip_exec,
    load_api_file,
    parse_cli_routes,
    set_route_defaults,
    start_mock,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def make_server(routes, allow_exec=False, allow_options=False):
    """Start a real MockHTTPServer on a random port in a daemon thread."""
    srv = MockHTTPServer(
        ("127.0.0.1", 0),
        RequestHandler,
        routes,
        allow_exec=allow_exec,
        allow_options=allow_options,
    )
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def get(srv, path, method="GET"):
    port = srv.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode().strip()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode().strip()


def write_tmp(content, suffix=".json"):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    f.write(content)
    f.flush()
    return f.name


# ── set_route_defaults ────────────────────────────────────────────────────────

class TestSetRouteDefaults:
    def test_empty_route_gets_all_defaults(self):
        r = set_route_defaults({})
        assert r == ROUTE_DEFAULTS

    def test_partial_override(self):
        r = set_route_defaults({"endpoint": "/foo", "statuscode": 404})
        assert r["endpoint"] == "/foo"
        assert r["statuscode"] == 404
        assert r["method"] == "GET"

    def test_none_values_not_applied(self):
        r = set_route_defaults({"endpoint": None})
        assert r["endpoint"] == "/"

    def test_falsy_zero_statuscode_preserved(self):
        # 0 is not None — should override default
        r = set_route_defaults({"statuscode": 0})
        assert r["statuscode"] == 0

    def test_unknown_method_prints_warning(self, capsys):
        set_route_defaults({"method": "GETT"})
        assert "WARNING" in capsys.readouterr().out

    def test_wildcard_method_no_warning(self, capsys):
        set_route_defaults({"method": "*"})
        assert capsys.readouterr().out == ""


# ── _strip_exec ───────────────────────────────────────────────────────────────

class TestStripExec:
    def test_strips_exec_and_warns(self, capsys):
        routes = [{"endpoint": "/x", "method": "GET", "statuscode": 200, "reply": "ok", "exec": "echo hi"}]
        result = _strip_exec(routes)
        assert result[0]["exec"] == ""
        assert "WARNING" in capsys.readouterr().out

    def test_empty_exec_unchanged(self, capsys):
        routes = [{"endpoint": "/x", "method": "GET", "statuscode": 200, "reply": "ok", "exec": ""}]
        _strip_exec(routes)
        assert capsys.readouterr().out == ""


# ── load_api_file ─────────────────────────────────────────────────────────────

class TestLoadApiFile:
    def test_json_array(self):
        path = write_tmp(json.dumps([{"endpoint": "/a", "method": "POST"}]))
        routes, overrides = load_api_file(path)
        assert len(routes) == 1
        assert routes[0]["endpoint"] == "/a"
        assert overrides == {}

    def test_json_full_config(self):
        cfg = {
            "hostname": "example.com",
            "port": 443,
            "cert": "/etc/ssl/cert.pem",
            "key": "/etc/ssl/key.pem",
            "routes": [{"endpoint": "/b", "method": "GET"}],
        }
        path = write_tmp(json.dumps(cfg))
        routes, overrides = load_api_file(path)
        assert overrides["host"] == "example.com"
        assert overrides["port"] == 443
        assert overrides["certfile"] == "/etc/ssl/cert.pem"
        assert overrides["keyfile"] == "/etc/ssl/key.pem"
        assert routes[0]["endpoint"] == "/b"

    def test_port_coerced_to_int(self):
        cfg = {"port": "8080", "routes": []}
        path = write_tmp(json.dumps(cfg))
        _, overrides = load_api_file(path)
        assert overrides["port"] == 8080
        assert isinstance(overrides["port"], int)

    def test_csv_file(self):
        content = "endpoint,method,statuscode,reply,exec\n/c,GET,200,hello,\n"
        path = write_tmp(content, suffix=".csv")
        routes, overrides = load_api_file(path)
        assert routes[0]["endpoint"] == "/c"
        assert overrides == {}

    def test_exec_stripped_without_allow_exec(self, capsys):
        path = write_tmp(json.dumps([{"endpoint": "/r", "method": "GET", "exec": "ls"}]))
        routes, _ = load_api_file(path, allow_exec=False)
        assert routes[0]["exec"] == ""
        assert "WARNING" in capsys.readouterr().out

    def test_exec_kept_with_allow_exec(self):
        path = write_tmp(json.dumps([{"endpoint": "/r", "method": "GET", "exec": "ls"}]))
        routes, _ = load_api_file(path, allow_exec=True)
        assert routes[0]["exec"] == "ls"


# ── parse_cli_routes ──────────────────────────────────────────────────────────

class TestParseCliRoutes:
    def test_parses_json_string(self):
        raw = ['{"endpoint": "/x", "method": "DELETE", "statuscode": 204, "reply": ""}']
        routes = parse_cli_routes(raw, allow_exec=True)
        assert routes[0]["method"] == "DELETE"

    def test_strips_exec_by_default(self, capsys):
        raw = ['{"endpoint": "/x", "method": "GET", "exec": "id"}']
        routes = parse_cli_routes(raw)
        assert routes[0]["exec"] == ""
        assert "WARNING" in capsys.readouterr().out


# ── execute_command ───────────────────────────────────────────────────────────

class TestExecuteCommand:
    def test_success(self):
        out, rc = RequestHandler.execute_command("echo hello")
        assert rc == 0
        assert "hello" in out

    def test_nonzero_exit(self):
        _, rc = RequestHandler.execute_command("exit 42")
        assert rc == 42

    def test_stderr_surfaced(self):
        out, _ = RequestHandler.execute_command("echo err >&2")
        assert "stderr" in out

    def test_timeout(self):
        with patch("mock.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("x", 10)):
            out, rc = RequestHandler.execute_command("sleep 99")
        assert rc == -1
        assert "timed out" in out


# ── HTTP integration ──────────────────────────────────────────────────────────

class TestHTTPRouting:
    def setup_method(self):
        routes = [
            set_route_defaults({"endpoint": "/hello", "method": "GET", "reply": "world"}),
            set_route_defaults({"endpoint": "/json",  "method": "GET", "reply": {"key": "val"}}),
            set_route_defaults({"endpoint": "/post",  "method": "POST", "statuscode": 201, "reply": "created"}),
            set_route_defaults({"endpoint": "/health","method": "*",   "reply": "alive"}),
            set_route_defaults({"endpoint": "*",      "method": "GET", "reply": "catch-all"}),
        ]
        self.srv = make_server(routes)

    def teardown_method(self):
        self.srv.shutdown()

    def test_exact_match(self):
        status, body = get(self.srv, "/hello")
        assert status == 200
        assert body == "world"

    def test_json_reply_content(self):
        status, body = get(self.srv, "/json")
        assert status == 200
        assert json.loads(body) == {"key": "val"}

    def test_post_route(self):
        port = self.srv.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/post",
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            assert r.status == 201

    def test_404_for_unknown_path(self):
        status, body = get(self.srv, "/nope")
        # /nope GET hits wildcard endpoint (*,GET)
        assert status == 200
        assert body == "catch-all"

    def test_wildcard_method(self):
        status, body = get(self.srv, "/health", method="GET")
        assert status == 200
        assert body == "alive"

    def test_wildcard_endpoint(self):
        status, body = get(self.srv, "/anything-unknown")
        assert status == 200
        assert body == "catch-all"

    def test_true_404_no_wildcard(self):
        routes = [set_route_defaults({"endpoint": "/only", "method": "GET", "reply": "ok"})]
        srv = make_server(routes)
        try:
            status, _ = get(srv, "/missing")
            assert status == 404
        finally:
            srv.shutdown()

    def test_request_summary_records_actual_path(self):
        get(self.srv, "/hello")
        summary = dict(self.srv.request_summary)
        assert "/hello" in summary
        assert summary["/hello"]["GET"] == 1

    def test_wildcard_summary_records_real_path(self):
        get(self.srv, "/anything-unknown")
        summary = dict(self.srv.request_summary)
        assert "/anything-unknown" in summary


# ── special methods ───────────────────────────────────────────────────────────

class TestSpecialMethods:
    def setup_method(self):
        routes = [set_route_defaults({"endpoint": "/x", "method": "GET", "reply": "ok"})]
        self.srv = make_server(routes, allow_options=True)
        self.port = self.srv.server_address[1]

    def teardown_method(self):
        self.srv.shutdown()

    def test_list_returns_routes(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="LIST")
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        assert isinstance(data, list)
        assert data[0]["endpoint"] == "/x"

    def test_options_enabled(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="OPTIONS")
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        assert "routes" in data
        assert "args" in data

    def test_options_disabled_returns_403(self):
        routes = [set_route_defaults({})]
        srv = make_server(routes, allow_options=False)
        try:
            port = srv.server_address[1]
            req = urllib.request.Request(f"http://127.0.0.1:{port}/", method="OPTIONS")
            try:
                urllib.request.urlopen(req)
                assert False, "expected 403"
            except urllib.error.HTTPError as e:
                assert e.code == 403
        finally:
            srv.shutdown()

    def test_options_redacts_certfile_keyfile(self):
        srv = MockHTTPServer(
            ("127.0.0.1", 0), RequestHandler,
            [set_route_defaults({})],
            allow_options=True,
            cli_args={"certfile": "/secret/cert.pem", "keyfile": "/secret/key.pem", "host": "localhost"},
        )
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            port = srv.server_address[1]
            req = urllib.request.Request(f"http://127.0.0.1:{port}/", method="OPTIONS")
            with urllib.request.urlopen(req) as r:
                data = json.loads(r.read())
            assert data["args"]["certfile"] == "<redacted>"
            assert data["args"]["keyfile"] == "<redacted>"
            assert data["args"]["host"] == "localhost"
        finally:
            srv.shutdown()

    def test_trace_echoes_headers(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="TRACE")
        req.add_header("X-Custom", "testvalue")
        with urllib.request.urlopen(req) as r:
            body = r.read().decode()
        assert "X-Custom" in body
        assert "testvalue" in body


# ── exec handling ─────────────────────────────────────────────────────────────

class TestExecHandling:
    def test_exec_runs_with_allow_exec(self):
        routes = [set_route_defaults({"endpoint": "/run", "method": "GET", "exec": "echo qatest", "reply": ""})]
        srv = make_server(routes, allow_exec=True)
        try:
            status, body = get(srv, "/run")
            assert status == 200
            assert "qatest" in body
            assert "rc=0" in body
        finally:
            srv.shutdown()

    def test_exec_blocked_without_allow_exec(self):
        routes = [set_route_defaults({"endpoint": "/run", "method": "GET", "exec": "echo secret", "reply": ""})]
        # exec gets stripped at load time by _strip_exec, so we inject directly
        routes[0]["exec"] = "echo secret"
        srv = make_server(routes, allow_exec=False)
        try:
            status, body = get(srv, "/run")
            assert status == 200
            assert "--allow-exec" in body
            assert "NOT Exec" in body
        finally:
            srv.shutdown()

    def test_exec_nonzero_rc_shown(self):
        routes = [set_route_defaults({"endpoint": "/fail", "method": "GET", "exec": "exit 1", "reply": ""})]
        srv = make_server(routes, allow_exec=True)
        try:
            _, body = get(srv, "/fail")
            assert "rc=1" in body
        finally:
            srv.shutdown()


# ── SSL validation ────────────────────────────────────────────────────────────

class TestSSLValidation:
    def test_partial_ssl_warns_and_uses_http(self, capsys):
        routes = [set_route_defaults({})]
        # start_mock binds; we cancel immediately via KeyboardInterrupt simulation
        srv = MockHTTPServer(("127.0.0.1", 0), RequestHandler, routes)
        srv.server_close()

        with patch("mock.MockHTTPServer") as MockSrv, \
             patch("mock.ssl.create_default_context"):
            instance = MagicMock()
            instance.server_address = ("127.0.0.1", 9999)
            instance.serve_forever.side_effect = KeyboardInterrupt
            instance.request_summary = {}
            MockSrv.return_value = instance

            with pytest.raises(SystemExit):
                start_mock("127.0.0.1", 9999, routes, certfile="/c.pem", keyfile=None)

        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "keyfile" in out


# ── MockHTTPServer ────────────────────────────────────────────────────────────

class TestMockHTTPServer:
    def test_route_index_built_correctly(self):
        routes = [
            set_route_defaults({"endpoint": "/a", "method": "GET"}),
            set_route_defaults({"endpoint": "/b", "method": "POST"}),
        ]
        srv = MockHTTPServer(("127.0.0.1", 0), RequestHandler, routes)
        srv.server_close()
        assert ("/a", "GET") in srv.route_index
        assert ("/b", "POST") in srv.route_index

    def test_allow_options_default_false(self):
        srv = MockHTTPServer(("127.0.0.1", 0), RequestHandler, [])
        srv.server_close()
        assert srv.allow_options is False

    def test_allow_options_set_via_constructor(self):
        srv = MockHTTPServer(("127.0.0.1", 0), RequestHandler, [], allow_options=True)
        srv.server_close()
        assert srv.allow_options is True
