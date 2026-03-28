# qamock

A lightweight QA HTTP mock server. Define routes via JSON, CSV, or inline CLI args — returns configurable status codes, JSON/text bodies, and optionally runs shell commands.

## Install

No runtime dependencies — just Python 3.12+.

**For development:**
```bash
pip install -e ".[dev]"
# or with uv
uv pip install -e ".[dev]"
```

## Usage

```
usage: qamock.py [-h] [--host HOST] [--port PORT] [--default]
                 [--api-file API_FILE] [--route ROUTE]
                 [--certfile CERTFILE] [--keyfile KEYFILE]
                 [--allow-exec] [--allow-options] [--version]
```

### Quickstart

```bash
# Default GET / → 200 OK
qamock --default

# Inline route
qamock --route '{"endpoint": "/ping", "method": "GET", "reply": "pong"}'

# JSON routes file
qamock --api-file routes.json

# HTTPS
qamock --api-file routes.json --certfile cert.pem --keyfile key.pem
```

### API file formats

**JSON array:**
```json
[
    {"endpoint": "/test",  "method": "GET",  "statuscode": 200, "reply": "OK"},
    {"endpoint": "/token", "method": "POST", "reply": {"token": "abc123"}},
    {"endpoint": "/data",  "method": "GET",  "reply": [1, 2, 3]},
    {"endpoint": "/run",   "method": "GET",  "exec": "uptime"}
]
```

**Full config** (overrides `--host`/`--port`/`--certfile`/`--keyfile`):
```json
{
    "hostname": "example.com",
    "port": 443,
    "cert": "/etc/ssl/cert.pem",
    "key":  "/etc/ssl/key.pem",
    "routes": [
        {"endpoint": "/test", "method": "GET", "statuscode": 200, "reply": "OK"}
    ]
}
```

**CSV:**
```csv
endpoint,method,statuscode,reply,exec
/test,GET,200,OK,
/run,GET,200,,uptime
```

### Wildcards

```json
{"endpoint": "/health", "method": "*",   "reply": "alive"}
{"endpoint": "*",       "method": "GET", "reply": "catch-all"}
{"endpoint": "*",       "method": "*",   "reply": "fallback"}
```

Match priority: exact → wildcard method → wildcard endpoint → both wildcards.

### Special methods

| Method    | Description |
|-----------|-------------|
| `LIST`    | Returns all configured routes as JSON |
| `KILL`    | Returns request summary and exits with code 666 |
| `TRACE`   | Echoes back raw request headers (RFC 7231) |
| `OPTIONS` | Returns runtime config and routes (requires `--allow-options`) |

### Shell exec

Routes can run shell commands on each request. Requires `--allow-exec`:

```bash
qamock --allow-exec --route '{"endpoint": "/status", "method": "GET", "exec": "uptime", "reply": ""}'
```

> **Warning:** `--allow-exec` enables arbitrary code execution. Only use with trusted route files.

### SSL

```bash
# Generate self-signed cert
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"

qamock --api-file routes.json --certfile cert.pem --keyfile key.pem
```

## Development

```bash
# Install dev deps
pip install -e ".[dev]"

# Run tests
mise run test

# Run with coverage
mise run coverage

# Lint + format
ruff check --fix .
ruff format .
```

## Release

Tag a version to trigger the release workflow:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The workflow runs tests, generates a changelog via `git-cliff`, builds a wheel, and creates a GitHub Release.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
