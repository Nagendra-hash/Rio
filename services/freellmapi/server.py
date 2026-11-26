#!/usr/bin/env python3
"""
freellmapi — a local OpenAI-compatible pool over the free LLM endpoints.

WHY THIS EXISTS
    RIO's `freellm` provider, the Browser Agent's brain chain and the Science
    Agent all point at `http://127.0.0.1:3001/v1` and expect a single
    OpenAI-compatible endpoint that fans out across free providers. The catalog
    at github.com/mnfst/awesome-free-llm-apis is a *list* of such providers, not
    a server, so this is the missing server: it reads a curated slice of that
    catalog and exposes `/v1/models` + `/v1/chat/completions`.

DESIGN
    * stdlib only (http.server + urllib) — no dependency on RIO's venv, so the
      service survives a rebuild of the app environment.
    * binds 127.0.0.1 only; every API request needs the bearer key configured
      in `~/.hermes/.env` (FREELLMAPI_API_KEY). No key configured = 503, never
      an open proxy.
    * `model="auto"` walks the pool in priority order, skipping providers that
      are cooling down after a failure. Any explicit model id is routed to the
      provider(s) that advertise it.
    * failures are classified: 429 parks a provider for its Retry-After (or
      5 min), 401/403 for 10 min, 5xx/timeout for 60 s. One provider being down
      never fails the request while another can answer.
    * an empty `content` is a failure, not an answer: these free endpoints are
      often reasoning models, and a tiny max_tokens is consumed by reasoning
      tokens before any text is emitted. That is why `max_tokens` is raised to
      a floor (default 64) — documented, and visible in the response headers.
    * logs one line per request (model, provider, status, duration). Prompt
      text, keys and headers are never logged.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

SERVICE_DIR = Path(__file__).resolve().parent
PROVIDERS_PATH = SERVICE_DIR / "providers.json"
CATALOG_PATH = SERVICE_DIR / "vendor" / "data.json"
ENV_FILE = Path.home() / ".hermes" / ".env"

HOST = "127.0.0.1"
PORT = int(os.environ.get("FREELLMAPI_PORT", "3001"))
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_INSPECT_BYTES = 4000

COOLDOWN_RATE_LIMIT = 300.0
COOLDOWN_AUTH = 600.0
COOLDOWN_FAILURE = 60.0
DEFAULT_TIMEOUT_S = 45.0
DEFAULT_MAX_TOKENS_FLOOR = 64


# ── configuration ───────────────────────────────────────────────────────────


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    return (_read_env_file(ENV_FILE).get(name) or "").strip()


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    models: tuple[str, ...]
    key_env: Optional[str]
    priority: int
    timeout_s: float = DEFAULT_TIMEOUT_S
    path: str = "/chat/completions"

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + self.path

    def key(self) -> str:
        return _env(self.key_env) if self.key_env else ""

    def configured(self) -> bool:
        return bool(self.key()) if self.key_env else True

    def advertised(self, model: str) -> bool:
        if not model:
            return False
        return any(model == known or model.endswith("/" + known) for known in self.models)


@dataclass
class ProviderState:
    cooldown_until: float = 0.0
    last_status: str = ""
    last_error: str = ""
    last_model: str = ""
    successes: int = 0
    failures: int = 0

    def cooling_for(self) -> float:
        return max(0.0, self.cooldown_until - time.monotonic())

    def public(self) -> dict:
        return {
            "last_status": self.last_status,
            "last_error": self.last_error[:200],
            "last_model": self.last_model,
            "cooling_for_s": round(self.cooling_for()),
            "successes": self.successes,
            "failures": self.failures,
        }


@dataclass
class RouterConfig:
    providers: list[Provider] = field(default_factory=list)
    min_max_tokens: int = DEFAULT_MAX_TOKENS_FLOOR
    catalog: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = PROVIDERS_PATH) -> "RouterConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        providers = [
            Provider(
                name=str(entry["name"]),
                base_url=str(entry["base_url"]),
                models=tuple(str(m) for m in entry.get("models") or ()),
                key_env=(str(entry["key_env"]) if entry.get("key_env") else None),
                priority=int(entry.get("priority", 100)),
                timeout_s=float(entry.get("timeout_s", DEFAULT_TIMEOUT_S)),
                path=str(entry.get("path") or "/chat/completions"),
            )
            for entry in raw.get("providers") or []
        ]
        providers.sort(key=lambda item: item.priority)
        return cls(
            providers=providers,
            min_max_tokens=int(raw.get("min_max_tokens", DEFAULT_MAX_TOKENS_FLOOR)),
            catalog=raw.get("catalog") or {},
        )


# ── upstream call ───────────────────────────────────────────────────────────


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, dict, Optional[str]]:
    """(status, body, retry_after_header). status 0 = transport failure."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            try:
                return int(response.status), json.loads(data or b"{}"), None
            except Exception:
                return int(response.status), {"raw": data[:500].decode("utf-8", "ignore")}, None
    except urllib.error.HTTPError as exc:
        retry_after = exc.headers.get("retry-after") if exc.headers else None
        try:
            payload_body = json.loads(exc.read() or b"{}")
        except Exception:
            payload_body = {"error": f"HTTP {exc.code}"}
        return int(exc.code), payload_body if isinstance(payload_body, dict) else {"error": "bad body"}, retry_after
    except Exception as exc:
        return 0, {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}, None


def _content_of(body: dict) -> tuple[str, bool]:
    """(content, has_tool_calls) — an empty answer is not a success."""
    try:
        choice = (body.get("choices") or [{}])[0]
    except Exception:
        return "", False
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some providers return content parts
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    has_tools = bool(message.get("tool_calls"))
    return str(content or "").strip(), has_tools


def _summarise_error(body: dict) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "error")[:200]
    if error:
        return str(error)[:200]
    return json.dumps(body)[:200] if body else "no response body"


# ── router ──────────────────────────────────────────────────────────────────


class Router:
    def __init__(self, config: Optional[RouterConfig] = None) -> None:
        self.config = config or RouterConfig.load()
        self._states: dict[str, ProviderState] = {p.name: ProviderState() for p in self.config.providers}
        self._lock = threading.RLock()

    # -- selection ---------------------------------------------------------
    def _state(self, provider: Provider) -> ProviderState:
        return self._states.setdefault(provider.name, ProviderState())

    def candidates(self, model: str) -> list[tuple[Provider, str]]:
        """Ordered (provider, model) attempts for a request."""
        wanted = (model or "auto").strip()
        out: list[tuple[Provider, str]] = []
        for provider in self.config.providers:
            if not provider.configured():
                continue
            if wanted == "auto":
                target = provider.models[0] if provider.models else ""
            elif provider.advertised(wanted):
                target = wanted
            else:
                continue
            if not target:
                continue
            out.append((provider, target))
        return out

    def _cooling(self, provider: Provider) -> float:
        return self._state(provider).cooling_for()

    def _park(self, provider: Provider, seconds: float, status: str, error: str) -> None:
        state = self._state(provider)
        state.cooldown_until = time.monotonic() + max(0.0, seconds)
        state.last_status = status
        state.last_error = error

    # -- one attempt -------------------------------------------------------
    def attempt(self, provider: Provider, model: str, payload: dict) -> tuple[bool, int, dict, str]:
        """(ok, http_status, body, note) for one provider."""
        headers = {"User-Agent": "freellmapi/1.0"}
        key = provider.key()
        if provider.key_env:
            if not key:
                return False, 0, {"error": f"{provider.key_env} not set"}, "not configured"
            headers["Authorization"] = f"Bearer {key}"

        started = time.monotonic()
        status, body, retry_after = _post_json(provider.endpoint, payload, headers, provider.timeout_s)
        duration = time.monotonic() - started
        state = self._state(provider)
        state.last_model = str(body.get("model") or model)

        if status == 0:
            self._park(provider, COOLDOWN_FAILURE, "unreachable", _summarise_error(body))
            state.failures += 1
            return False, 0, body, f"unreachable ({duration:.1f}s)"

        if status == 429:
            wait = COOLDOWN_RATE_LIMIT
            try:
                if retry_after:
                    wait = max(5.0, min(3600.0, float(retry_after)))
            except ValueError:
                pass
            self._park(provider, wait, "rate_limited", "429")
            state.failures += 1
            return False, 429, body, f"rate limited (cooling {wait:.0f}s)"

        if status in (401, 403):
            self._park(provider, COOLDOWN_AUTH, "auth", _summarise_error(body))
            state.failures += 1
            return False, status, body, "key rejected"

        if status >= 500:
            self._park(provider, COOLDOWN_FAILURE, "server_error", _summarise_error(body))
            state.failures += 1
            return False, status, body, f"upstream {status}"

        if status >= 400:
            self._park(provider, COOLDOWN_FAILURE, "rejected", _summarise_error(body))
            state.failures += 1
            return False, status, body, f"upstream {status}"

        content, has_tools = _content_of(body)
        if not content and not has_tools:
            self._park(provider, COOLDOWN_FAILURE, "empty", "empty content")
            state.failures += 1
            return False, status, body, "empty content"

        state.cooldown_until = 0.0
        state.last_status = "ok"
        state.last_error = ""
        state.successes += 1
        return True, status, body, "ok"

    # -- the public operation ---------------------------------------------
    def complete(self, payload: dict) -> tuple[int, dict, dict]:
        """(status, body, meta) — walks the pool until something answers."""
        requested = str(payload.get("model") or "auto")
        candidates = self.candidates(requested)
        if not candidates:
            return 404, {
                "error": {
                    "message": f"no configured provider serves model {requested!r}",
                    "type": "invalid_request_error",
                }
            }, {"requested_model": requested}

        body = dict(payload)
        floor = self.config.min_max_tokens
        try:
            current = int(body.get("max_tokens") or 0)
        except (TypeError, ValueError):
            current = 0
        if floor and current < floor:
            # Reasoning models spend the first tokens on hidden reasoning; a
            # tiny budget yields an empty answer on every free endpoint.
            body["max_tokens"] = floor

        reasons: list[str] = []
        for provider, model in candidates:
            cooling = self._cooling(provider)
            if cooling > 0:
                reasons.append(f"{provider.name}: cooling {cooling:.0f}s")
                continue
            attempt = dict(body)
            attempt["model"] = model
            ok, status, upstream, note = self.attempt(provider, model, attempt)
            if ok:
                used = str(upstream.get("model") or model)
                meta = {
                    "provider": provider.name,
                    "model": used,
                    "requested_model": requested,
                    "max_tokens_floor": floor if body.get("max_tokens") != payload.get("max_tokens") else 0,
                }
                return status, upstream, meta
            reasons.append(f"{provider.name}: {note}")

        return 503, {
            "error": {
                "message": "no free provider could answer: " + "; ".join(reasons[:6]),
                "type": "service_unavailable",
            }
        }, {"requested_model": requested, "attempts": reasons[:6]}

    # -- introspection -----------------------------------------------------
    def models(self) -> list[str]:
        seen: list[str] = ["auto"]
        for provider in self.config.providers:
            if not provider.configured():
                continue
            for model in provider.models:
                if model not in seen:
                    seen.append(model)
        return seen

    def health(self) -> dict:
        providers = []
        ready = 0
        for provider in self.config.providers:
            state = self._state(provider)
            configured = provider.configured()
            cooling = state.cooling_for()
            if configured and cooling <= 0:
                ready += 1
            providers.append({
                "name": provider.name,
                "configured": configured,
                "key_env": provider.key_env,
                "priority": provider.priority,
                "models": list(provider.models),
                **state.public(),
            })
        return {
            "status": "ok" if ready else "degraded",
            "ready_providers": ready,
            "providers": providers,
            "catalog": self.config.catalog,
            "min_max_tokens": self.config.min_max_tokens,
        }


# ── HTTP surface ────────────────────────────────────────────────────────────

def _expected_key() -> str:
    return _env("FREELLMAPI_API_KEY")


ROUTER: Optional[Router] = None
ROUTER_LOCK = threading.Lock()


def get_router() -> Router:
    global ROUTER
    with ROUTER_LOCK:
        if ROUTER is None:
            ROUTER = Router()
        return ROUTER


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "freellmapi/1.0"

    # -- helpers -----------------------------------------------------------
    def _json(self, status: int, payload: dict, headers: Optional[dict] = None) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _authorised(self) -> bool:
        expected = _expected_key()
        if not expected:
            self._json(503, {"error": {
                "message": "FREELLMAPI_API_KEY is not configured; refusing to act as an open proxy",
                "type": "service_unavailable"}})
            return False
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(token, expected):
            self._json(401, {"error": {"message": "invalid API key", "type": "auth_error"}})
            return False
        return True

    def _read_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, {"error": {"message": "empty request body", "type": "invalid_request_error"}})
            return None
        if length > MAX_BODY_BYTES:
            # Refuse without draining megabytes of body. Closing the connection
            # is deliberate: the client may see a broken pipe instead of the
            # 413, which is still a refusal (never a half-read request).
            self.close_connection = True
            self._json(413, {"error": {"message": "request body too large", "type": "invalid_request_error"}})
            return None
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except Exception:
            self._json(400, {"error": {"message": "body is not valid JSON", "type": "invalid_request_error"}})
            return None
        if not isinstance(body, dict):
            self._json(400, {"error": {"message": "body must be a JSON object", "type": "invalid_request_error"}})
            return None
        return body

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        return  # replaced by _log below

    def _log(self, text: str) -> None:
        sys.stderr.write(f"[freellmapi] {text}\n")
        sys.stderr.flush()

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/healthz"):
            self._json(200, get_router().health())
            return
        if path in ("/v1/models", "/models"):
            if not self._authorised():
                return
            models = get_router().models()
            self._json(200, {
                "object": "list",
                "data": [{"id": name, "object": "model", "owned_by": "freellmapi"} for name in models],
            })
            return
        if path == "/":
            self._json(200, {"service": "freellmapi", "endpoints": ["/v1/models", "/v1/chat/completions", "/health"]})
            return
        self._json(404, {"error": {"message": f"no route {path}", "type": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": f"no route {path}", "type": "not_found"}})
            return
        if not self._authorised():
            return
        body = self._read_body()
        if body is None:
            return
        started = time.monotonic()
        status, payload, meta = get_router().complete(body)
        duration = time.monotonic() - started
        headers = {
            "X-FreeLLM-Provider": str(meta.get("provider") or ""),
            "X-FreeLLM-Model": str(meta.get("model") or ""),
            "X-FreeLLM-Latency": f"{duration:.2f}",
        }
        if meta.get("max_tokens_floor"):
            headers["X-FreeLLM-MaxTokensFloor"] = str(meta["max_tokens_floor"])
        self._json(status, payload, headers)
        self._log(
            f"model={meta.get('requested_model')!r} -> {status} "
            f"provider={meta.get('provider') or '-'} "
            f"used={meta.get('model') or '-'} {duration:.1f}s"
        )


def main() -> int:
    config = RouterConfig.load()
    global ROUTER
    ROUTER = Router(config)
    if not _expected_key():
        sys.stderr.write(
            "[freellmapi] WARNING: FREELLMAPI_API_KEY is not set; API calls will be refused "
            "(health stays available).\n"
        )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    ready = [p.name for p in config.providers if p.configured()]
    sys.stderr.write(
        f"[freellmapi] listening on http://{HOST}:{PORT} — providers: {', '.join(ready) or 'none'}\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
