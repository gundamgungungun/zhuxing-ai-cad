"""FastAPI server for 铸形 FormForge Studio.

Run with::

    python -m multi_agent_cad.web

Listens on ``0.0.0.0:8000`` by default (override with ``MAC_WEB_HOST`` /
``MAC_WEB_PORT``). Single-user, trusted-network only — the pipeline
executes generated Python server-side; treat the server as your own dev
environment.

Architecture::

    browser  ──HTTP──▶  FastAPI (this module)
                         ├─ POST /api/run           → spawn web_runner subprocess
                         ├─ GET  /api/jobs/{id}/events  → SSE progress stream
                         ├─ GET  /api/jobs/{id}/files/{name} → serve GLB/STEP/...
                         └─ static/  → index.html, app.js, styles.css
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from multi_agent_cad.execution_security import sanitized_subprocess_env

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError as exc:  # pragma: no cover
    print(
        "Web extras not installed. Run:  pip install -e '.[web]'",
        file=sys.stderr,
    )
    raise

_STATIC_DIR = Path(__file__).resolve().parent / "static"
# Repo root, so the web_runner subprocess (whose cwd is a per-job tempdir)
# can still `import multi_agent_cad` without a pip-installed package.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_DEPLOYMENT_MODE = os.environ.get("MAC_DEPLOYMENT_MODE", "local").strip().lower()
_ALLOW_CLIENT_API_KEY = _env_flag(
    "MAC_ALLOW_CLIENT_API_KEY", default=_DEPLOYMENT_MODE != "production"
)
_TRUST_GATEWAY_AUTH = _env_flag("MAC_TRUST_GATEWAY_AUTH", default=False)
_BASIC_AUTH_USER = os.environ.get("MAC_BASIC_AUTH_USER", "")
_BASIC_AUTH_PASSWORD = os.environ.get("MAC_BASIC_AUTH_PASSWORD", "")
_MAX_ACTIVE_JOBS = max(
    1,
    int(os.environ.get("MAC_MAX_ACTIVE_JOBS", "1" if _DEPLOYMENT_MODE == "production" else "2")),
)
_MAX_PROMPT_CHARS = max(100, int(os.environ.get("MAC_MAX_PROMPT_CHARS", "10000")))
_MAX_API_KEY_CHARS = 512

# In-memory job registry (single-user, process-lifetime). A dict is fine
# because only one user is expected and jobs rarely exceed a handful.
_JOBS: dict[str, dict[str, Any]] = {}

# Auto-cleanup: delete job tempdirs 6h after the job started (and exited),
# and sweep orphaned macjob_* dirs from previous server runs every 30 min.
_CLEANUP_AFTER_SECONDS = 6 * 3600
_CLEANUP_INTERVAL_SECONDS = 30 * 60


async def _cleanup_old_jobs() -> None:
    """Delete finished-job tempdirs older than 6h, plus orphaned macjob_* dirs.

    Two sources:
      1. Known jobs in _JOBS whose subprocess has exited and whose
         ``started_at`` (wall-clock) is older than the threshold.
      2. Orphaned ``macjob_*`` dirs in the system tempdir that aren't in
         _JOBS (left over from previous server runs / crashes). Protected
         from the create-race by the mtime check — a fresh tempdir's mtime
         is ~now, well above the threshold.
    """
    now = time.time()
    threshold = now - _CLEANUP_AFTER_SECONDS
    known_tempdirs: set[str] = set()
    finished: list[str] = []

    for job_id, job in list(_JOBS.items()):
        td = str(job.get("tempdir") or "")
        known_tempdirs.add(td)
        started = job.get("started_at", now)
        proc = job.get("proc")
        proc_done = proc is not None and proc.returncode is not None
        if proc_done and started < threshold:
            shutil.rmtree(job["tempdir"], ignore_errors=True)
            finished.append(job_id)
    for jid in finished:
        _JOBS.pop(jid, None)

    temp_root = tempfile.gettempdir()
    n_orphans = 0
    for p in Path(temp_root).glob("macjob_*"):
        if str(p) in known_tempdirs:
            continue
        try:
            if p.stat().st_mtime < threshold:
                shutil.rmtree(p, ignore_errors=True)
                n_orphans += 1
        except OSError:
            continue
    if finished or n_orphans:
        print(f"[web] cleanup: removed {len(finished)} finished job(s), "
              f"{n_orphans} orphan(s)", file=sys.stderr)


async def _cleanup_loop() -> None:
    """Periodic background sweep; started at app startup, cancelled on shutdown."""
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        try:
            await _cleanup_old_jobs()
        except Exception as exc:  # pragma: no cover
            print(f"[web] cleanup pass failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Provider presets & config schema
# ---------------------------------------------------------------------------

_PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "qwen": {
        "ds_base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "model_hint": "qwen3.7-max",
    },
    "openai": {
        "ds_base_url": "https://api.openai.com/v1",
        "model_hint": "gpt-4o",
    },
    "deepseek": {
        "ds_base_url": "https://api.deepseek.com/v1",
        "model_hint": "deepseek-chat",
    },
    "gemini": {
        "ds_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model_hint": "gemini-2.5-flash",
    },
    "ollama": {
        "ds_base_url": "http://localhost:11434/v1",
        "model_hint": "qwen2.5-coder:32b",
    },
}

_WEB_CONFIG_FIELDS = {
    "DS_BASE_URL",
    "MAX_RETRIES",
    "MAX_EXEC_RETRIES",
    "SPEC_PLANNER_MODEL",
    "SPEC_PLANNER_TEMPERATURE",
    "SPEC_PLANNER_MAX_TOKENS",
    "ARCHITECT_MODEL",
    "ARCHITECT_TEMPERATURE",
    "ARCHITECT_MAX_TOKENS",
    "CODER_MODEL",
    "CODER_TEMPERATURE",
    "CODER_MAX_TOKENS",
    "REPAIR_MODEL",
    "REPAIR_TEMPERATURE",
    "REPAIR_MAX_TOKENS",
}
_RETRY_FIELDS = {"MAX_RETRIES", "MAX_EXEC_RETRIES"}
_TEMPERATURE_FIELDS = {name for name in _WEB_CONFIG_FIELDS if name.endswith("_TEMPERATURE")}
_TOKEN_FIELDS = {name for name in _WEB_CONFIG_FIELDS if name.endswith("_MAX_TOKENS")}
_MODEL_FIELDS = {name for name in _WEB_CONFIG_FIELDS if name.endswith("_MODEL")}
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
_SECRET_TEXT_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_.-]{8,}\b"),
    re.compile(r"\bAKLT[A-Za-z0-9]{8,}\b"),
    re.compile(
        r"(?i)((?:api[_ -]?key|access[_ -]?key|secret|token|password)\s*[:=]\s*)"
        r"([^\s,;]+)"
    ),
)


def _redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_TEXT_PATTERNS:
        if pattern.groups:
            result = pattern.sub(r"\1[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


def _redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


def _allowed_model_base_urls() -> set[str]:
    allowed = {
        preset["ds_base_url"].rstrip("/")
        for name, preset in _PROVIDER_PRESETS.items()
        if name != "ollama" or _DEPLOYMENT_MODE != "production"
    }
    allowed.update(
        value.strip().rstrip("/")
        for value in os.environ.get("MAC_ALLOWED_MODEL_BASE_URLS", "").split(",")
        if value.strip()
    )
    return allowed


def _validated_web_config(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(400, "config must be an object")

    result: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _WEB_CONFIG_FIELDS:
            continue
        if key == "DS_BASE_URL":
            if not isinstance(value, str):
                raise HTTPException(400, "DS_BASE_URL must be a string")
            normalized = value.strip().rstrip("/")
            if normalized not in _allowed_model_base_urls():
                raise HTTPException(400, "model service URL is not in the server allowlist")
            result[key] = normalized
        elif key in _RETRY_FIELDS:
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} must be an integer") from None
            upper = 5 if key == "MAX_RETRIES" else 3
            if not 0 <= number <= upper:
                raise HTTPException(400, f"{key} must be between 0 and {upper}")
            result[key] = number
        elif key in _TEMPERATURE_FIELDS:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} must be numeric") from None
            if not 0 <= number <= 2:
                raise HTTPException(400, f"{key} must be between 0 and 2")
            result[key] = number
        elif key in _TOKEN_FIELDS:
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} must be an integer") from None
            if not 256 <= number <= 65536:
                raise HTTPException(400, f"{key} must be between 256 and 65536")
            result[key] = number
        elif key in _MODEL_FIELDS:
            if not isinstance(value, str) or not _MODEL_NAME_RE.fullmatch(value.strip()):
                raise HTTPException(400, f"{key} contains unsupported characters")
            result[key] = value.strip()
    return result


def _validate_startup_configuration() -> None:
    if bool(_BASIC_AUTH_USER) != bool(_BASIC_AUTH_PASSWORD):
        raise RuntimeError("MAC_BASIC_AUTH_USER and MAC_BASIC_AUTH_PASSWORD must be set together")
    if _DEPLOYMENT_MODE != "production":
        return
    if _ALLOW_CLIENT_API_KEY:
        return
    if not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        raise RuntimeError(
            "DASHSCOPE_API_KEY is required when client-provided keys are disabled"
        )
    if not _TRUST_GATEWAY_AUTH and not (_BASIC_AUTH_USER and _BASIC_AUTH_PASSWORD):
        raise RuntimeError(
            "production mode requires Basic Auth credentials or MAC_TRUST_GATEWAY_AUTH=true"
        )


def _resolve_api_key(body: dict[str, Any]) -> str:
    """Resolve a per-request key without silently falling back to an owner key."""

    if _ALLOW_CLIENT_API_KEY:
        raw = body.pop("api_key", None)
        if not isinstance(raw, str) or not raw.strip():
            raise HTTPException(400, "please enter your own model service API key")
        api_key = raw.strip()
        if len(api_key) > _MAX_API_KEY_CHARS or any(ord(char) < 32 for char in api_key):
            raise HTTPException(400, "invalid model service API key")
        return api_key

    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(503, "model service credential is not configured")
    return api_key


def _config_schema() -> dict:
    """Return editable config fields with current defaults from config.py."""
    from multi_agent_cad import config as cfg

    fields = [
        "DS_BASE_URL", "API_KEY_ENV_VAR", "API_BASE_ENV_VAR",
        "MAX_RETRIES", "MAX_EXEC_RETRIES",
        "SPEC_PLANNER_MODEL", "SPEC_PLANNER_TEMPERATURE", "SPEC_PLANNER_MAX_TOKENS",
        "ARCHITECT_MODEL", "ARCHITECT_TEMPERATURE", "ARCHITECT_MAX_TOKENS",
        "CODER_MODEL", "CODER_TEMPERATURE", "CODER_MAX_TOKENS",
        "AIDER_MODEL", "AIDER_MAX_TOKENS",
        "REPAIR_MODEL", "REPAIR_TEMPERATURE", "REPAIR_MAX_TOKENS",
        "USER_REQUEST", "WORKFLOW_ID",
    ]
    return {k: getattr(cfg, k, None) for k in fields}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    _validate_startup_configuration()
    cleanup_task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()


app = FastAPI(title="铸形 FormForge Studio", lifespan=_lifespan)


def _basic_auth_matches(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return hmac.compare_digest(username, _BASIC_AUTH_USER) and hmac.compare_digest(
        password, _BASIC_AUTH_PASSWORD
    )


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if request.url.path != "/api/health" and _BASIC_AUTH_USER:
        if not _basic_auth_matches(request.headers.get("authorization", "")):
            return JSONResponse(
                {"detail": "authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="FormForge"'},
            )

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/config/schema")
async def get_schema() -> dict:
    return {
        "config": _config_schema(),
        "providers": _PROVIDER_PRESETS,
        "security": {
            "allow_client_api_key": _ALLOW_CLIENT_API_KEY,
            "server_api_key_configured": bool(os.environ.get("DASHSCOPE_API_KEY", "").strip()),
            "deployment_mode": _DEPLOYMENT_MODE,
        },
    }


@app.post("/api/run")
async def run(req: Request) -> dict:
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be an object")

    config = _validated_web_config(body.get("config", {}))
    prompt = (body.get("prompt") or config.get("USER_REQUEST", "") or "").strip()
    workflow = body.get("workflow") or config.get("WORKFLOW_ID", "original")
    if not prompt:
        raise HTTPException(400, "prompt is required")
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise HTTPException(400, f"prompt exceeds {_MAX_PROMPT_CHARS} characters")
    if workflow not in {"original", "aider"}:
        raise HTTPException(400, "unsupported workflow")
    if body.get("dest_path"):
        raise HTTPException(400, "server-side destination paths are not supported")

    api_key = _resolve_api_key(body)
    active_jobs = sum(
        1
        for job in _JOBS.values()
        if job.get("proc") is not None and job["proc"].returncode is None
    )
    if active_jobs >= _MAX_ACTIVE_JOBS:
        raise HTTPException(429, "the service is at its active-job limit; retry later")

    job_id = uuid.uuid4().hex[:12]
    tempdir = Path(tempfile.mkdtemp(prefix=f"macjob_{job_id}_"))

    # config.json — everything except the API key (key goes via env var).
    cfg_out = dict(config)
    cfg_out["USER_REQUEST"] = prompt
    cfg_out["WORKFLOW_ID"] = workflow
    cfg_out.pop("DS_API_KEY", None)  # never persist the key to disk
    config_path = tempdir / "config.json"
    config_path.write_text(json.dumps(cfg_out, indent=2), encoding="utf-8")

    env = sanitized_subprocess_env(
        os.environ,
        extra={
        "MAC_CONFIG_FILE": str(config_path),
        "PYTHONUNBUFFERED": "1",
        # The subprocess cwd is the per-job tempdir (isolation), not the
        # project root — and the package may not be pip-installed in the
        # venv (editable install). Add the repo root to sys.path so the
        # runner can `import multi_agent_cad` from anywhere.
        "PYTHONPATH": str(_REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        },
    )

    cmd = [
        sys.executable, "-m", "multi_agent_cad.web_runner",
        "--config", str(config_path),
        "--out", str(tempdir),
        "--workflow", workflow,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        cwd=str(tempdir),
    )
    assert proc.stdin is not None
    try:
        proc.stdin.write((json.dumps({"api_key": api_key}) + "\n").encode("utf-8"))
        await proc.stdin.drain()
    finally:
        proc.stdin.close()
        api_key = ""

    queue: asyncio.Queue = asyncio.Queue()
    job: dict[str, Any] = {
        "proc": proc,
        "tempdir": tempdir,
        "queue": queue,
        "result": None,
        "started_at": time.time(),
    }
    _JOBS[job_id] = job

    asyncio.create_task(_reader_task(job_id, job))
    asyncio.create_task(_watcher_task(job_id, job))
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    """Terminate a running job. Whatever artifacts exist on disk become
    downloadable — the reader task will emit a synthetic 'done' with the
    harvested file paths once the subprocess exits."""
    if job_id not in _JOBS:
        raise HTTPException(404, "job not found")
    job = _JOBS[job_id]
    proc = job.get("proc")
    if proc is None or proc.returncode is not None:
        return {"status": "already_done", "rc": proc.returncode if proc else None}
    if job.get("cancelled"):
        return {"status": "already_cancelling"}
    job["cancelled"] = True
    asyncio.create_task(_terminate_job(job))
    return {"status": "cancelling"}


async def _terminate_job(job: dict) -> None:
    """Send SIGTERM, wait up to 5s, escalate to SIGKILL if the runner
    hasn't exited (e.g. stuck inside a subprocess.run for the CAD script).
    The reader task observes the proc exit and emits the synthetic 'done'."""
    proc = job["proc"]
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
    except ProcessLookupError:
        pass


async def _reader_task(job_id: str, job: dict) -> None:
    proc = job["proc"]
    queue: asyncio.Queue = job["queue"]
    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                await queue.put({"log": _redact_text(line)})
                continue
            if not isinstance(msg, dict):
                # The subprocess (web_runner + LangGraph / aider / build123d)
                # sometimes prints lines that happen to parse as valid JSON
                # but aren't objects (quoted strings, numbers, arrays) — e.g.
                # aider echoing a JSON literal mid-repair. Treat them as log
                # lines so the SSE stream keeps flowing instead of crashing
                # the reader on `msg.get("done")`.
                await queue.put({"log": _redact_text(line)})
                continue
            msg = _redact_payload(msg)
            if msg.get("done"):
                job["result"] = msg
                await queue.put(msg)
                break
            await queue.put(msg)

        rc = await proc.wait()
        if job.get("result") is None:
            if job.get("cancelled"):
                # User clicked Stop — emit a synthetic 'done' from whatever's
                # on disk so the UI can offer downloads of half-finished work.
                msg = _harvest_intermediate(job)
                job["result"] = msg
                await queue.put(msg)
            else:
                err = {"error": f"runner exited (rc={rc}) without 'done'", "rc": rc}
                job["result"] = err
                await queue.put(err)
    except Exception as exc:  # pragma: no cover
        err = {"error": f"reader task crashed: {exc}"}
        job["result"] = err
        await queue.put(err)
    finally:
        await queue.put(None)  # sentinel: SSE stream ends


def _stl_to_glb(stl_path: str, glb_path: str) -> bool:
    """Convert an STL mesh to GLB for browser preview. Returns True on success."""
    try:
        import trimesh
        mesh = trimesh.load(stl_path)
        mesh.export(glb_path, file_type="glb")
        return True
    except Exception as exc:
        print(f"[web] STL→GLB conversion failed: {exc}", file=sys.stderr)
        return False


async def _watcher_task(job_id: str, job: dict) -> None:
    """Poll the job's STL; whenever it changes, convert to a live-preview GLB
    and push an event to the SSE stream. Lets the browser show each
    intermediate model as the autonomous loop iterates (Aider repair →
    re-execute → new STL). Stops when the subprocess exits or 'done' is sent.

    The autonomous loop writes a *new* file per iteration rather than
    overwriting in place:
      - first attempt:  temp_output_0.stl
      - aider retry:     temp_output_aider_autonomous_0.stl
      - plain retry:     temp_output_autonomous_0.stl
      - iteration N:     temp_output_N.stl
    So we glob all ``temp_output_*.stl`` and track the newest by mtime —
    a new file appearing OR an existing file's mtime changing both trigger
    a fresh live preview.
    """
    tempdir = job["tempdir"]
    proc = job["proc"]
    queue: asyncio.Queue = job["queue"]
    live_glb = tempdir / "temp_output_0.live.glb"
    last_sig: tuple[str, float] | None = None  # (path, mtime) of last converted STL
    while proc.returncode is None:
        if job.get("result") is not None:
            break  # reader task already emitted 'done'; stop pushing intermediates
        await asyncio.sleep(1.0)
        try:
            stls = sorted(
                tempdir.glob("temp_output_*.stl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not stls:
                continue
            stl_path = stls[0]
            mtime = stl_path.stat().st_mtime
            sig = (str(stl_path), mtime)
            if sig == last_sig:
                continue
            last_sig = sig
        except OSError:
            continue
        # Wait for the write to complete + size to stabilize (avoid half-written reads)
        await asyncio.sleep(1.0)
        try:
            s1 = stl_path.stat().st_size
            await asyncio.sleep(0.3)
            s2 = stl_path.stat().st_size
            if s1 != s2:
                continue
        except OSError:
            continue
        ok = await asyncio.to_thread(_stl_to_glb, str(stl_path), str(live_glb))
        if not ok:
            continue
        ts = int(time.time() * 1000)
        await queue.put({
            "intermediate": True,
            "glb": f"/api/jobs/{job_id}/files/live.glb?t={ts}",
        })


def _harvest_intermediate(job: dict) -> dict:
    """Build a synthetic 'done' message from whatever files exist in tempdir.

    Called when the user cancels mid-run — the runner subprocess didn't emit
    a 'done' record, so we assemble one from disk state so the UI can show
    download buttons for the half-finished artifacts.
    """
    tempdir: Path = job["tempdir"]

    def _find(pattern: str) -> str | None:
        matches = sorted(
            tempdir.rglob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return str(matches[0]) if matches else None

    step = _find("temp_output_*.step")
    stl = _find("temp_output_*.stl")
    py = _find("temp_design_*.py")
    measurements = _find("temp_measurements_*.json")
    missed = _find("temp_missed_*.json")
    glb = _find("*.glb")
    # No GLB yet but STL exists — convert now so the browser preview works
    # for the cancelled-state artifacts.
    if not glb and stl:
        live_glb = tempdir / "temp_output_0.live.glb"
        if _stl_to_glb(stl, str(live_glb)):
            glb = str(live_glb)

    return {
        "done": True,
        "cancelled": True,
        "error_type": "CANCELLED_BY_USER",
        "step": step,
        "stl": stl,
        "glb": glb,
        "py": py,
        "measurements": measurements,
        "missed": missed,
        "tokens": None,
        "api_calls": None,
    }


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    if job_id not in _JOBS:
        raise HTTPException(404, "job not found")
    job = _JOBS[job_id]
    queue: asyncio.Queue = job["queue"]

    async def event_stream():
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"
            if msg.get("done") or msg.get("error"):
                # Drain any remaining (e.g. sentinel) without blocking.
                while not queue.empty():
                    extra = queue.get_nowait()
                    if extra is None:
                        break
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/result")
async def job_result(job_id: str) -> JSONResponse:
    if job_id not in _JOBS:
        raise HTTPException(404, "job not found")
    return JSONResponse(_JOBS[job_id].get("result") or {"status": "running"})


@app.get("/api/jobs/{job_id}/files/{name}")
async def job_file(job_id: str, name: str) -> FileResponse:
    if job_id not in _JOBS:
        raise HTTPException(404, "job not found")
    job = _JOBS[job_id]
    result = job.get("result") or {}
    tempdir = job.get("tempdir")

    def _find_in_tempdir(pattern: str) -> str | None:
        if not tempdir:
            return None
        matches = sorted(
            Path(tempdir).rglob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return str(matches[0]) if matches else None

    name_map = {
        "model.glb": result.get("glb") or _find_in_tempdir("*.glb"),
        "live.glb": str(tempdir / "temp_output_0.live.glb") if tempdir else None,
        "model.step": result.get("step") or _find_in_tempdir("temp_output_*.step"),
        "model.stl": result.get("stl") or _find_in_tempdir("temp_output_*.stl"),
        "source.py": result.get("py") or _find_in_tempdir("temp_design_*.py"),
        "measurements.json": result.get("measurements")
            or _find_in_tempdir("temp_measurements_*.json"),
        "missed.json": result.get("missed")
            or _find_in_tempdir("temp_missed_*.json"),
    }
    path = name_map.get(name)
    if not path or not Path(path).is_file():
        raise HTTPException(404, f"file '{name}' not available (yet)")
    media_types = {
        "model.glb": "model/gltf-binary",
        "live.glb": "model/gltf-binary",
        "model.step": "application/step",
        "model.stl": "model/stl",
        "source.py": "text/x-python",
        "measurements.json": "application/json",
        "missed.json": "application/json",
    }
    # `live.glb` is overwritten in-place by the watcher task as the autonomous
    # loop iterates. Without no-cache, browsers may serve a stale copy from
    # heuristic caching even when the URL's `?t=<ts>` query changes — the
    # model-viewer then renders the old scene, not the new one. Setting this
    # on every file is harmless (each job has a unique job_id path segment).
    headers = {"Cache-Control": "no-cache, must-revalidate"}
    return FileResponse(
        path,
        media_type=media_types.get(name, "application/octet-stream"),
        headers=headers,
    )


# Static frontend — mounted LAST so /api/* routes take precedence.
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main() -> None:
    host = os.environ.get("MAC_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("MAC_WEB_PORT", "8000"))
    # 0.0.0.0 listens on all interfaces but is not a valid browser URL on
    # Windows (ERR_ADDRESS_INVALID). Show 127.0.0.1 as the clickable URL when
    # host is 0.0.0.0, while still binding to all interfaces for LAN access.
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    print(f"\n  MAC Web UI — http://{display_host}:{port}")
    print("  Single-user, trusted-network only. Generated .py runs server-side.")
    print(f"  Auto-cleanup: job tempdirs removed {_CLEANUP_AFTER_SECONDS // 3600}h after completion.\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
