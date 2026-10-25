"""HTTP surface of the runner (protocol v1).

Order for ``POST /v1/executions``: bearer token → client address → isolation
preflight (including the in-container probe) → request parsing and
validation against the runner's allowlist and ceilings → archive
verification (size, digest) and scan (no extraction on this host) → paths
present in the archive → concurrency slot → idempotent claim → container →
persisted result. A request that fails before the claim leaves no trace
besides the log; one claimed is recorded, so the same request later gets the
same answer.

Uncertain executions (left open by a crash) are never rerun: operators
inspect them through the admin endpoints, close them with evidence taken
from the engine (``/reconcile``) or declare them failed (``/resolve``).
Nobody can declare one successful by hand.
"""

import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import PROTOCOL_VERSION, __version__
from .auth import bearer_matches, client_allowed
from .backend import Backend, ContainerState, IsolationReport, RunSpec
from .config import RunnerSettings
from .errors import RunnerError
from .extract import ExtractionLimits, scan_archive, verify_archive
from .programs import ensure_paths_in_manifest, validate_argv
from .protocol import (
    ExecutionView,
    ResolveRequest,
    ResourceUsage,
    RunnerHealth,
    SandboxLimits,
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
)
from .store import ExecutionRecord, ExecutionStore, request_digest

logger = logging.getLogger("agentforge_runner")
Clock = Callable[[], datetime]
_REQUEST_PART_MAX = 64 * 1024


class RunnerState:
    def __init__(self, settings: RunnerSettings, backend: Backend, store: ExecutionStore) -> None:
        self.settings = settings
        self.backend = backend
        self.store = store
        self.slots = threading.BoundedSemaphore(settings.max_concurrent)
        self.lock = threading.Lock()
        self.containers: dict[UUID, str] = {}
        self._report: IsolationReport | None = None
        self._report_at = 0.0

    def isolation(self, *, force: bool = False) -> IsolationReport:
        now = time.monotonic()
        with self.lock:
            cached = self._report
            fresh = (
                cached is not None and now - self._report_at <= self.settings.probe_cache_seconds
            )
        if fresh and not force and cached is not None:
            return cached
        report = self.backend.preflight(self.settings.images)
        with self.lock:
            self._report, self._report_at = report, now
        return report


def _problem(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse({"code": code, "detail": detail}, status_code=status)


def create_runner_app(
    settings: RunnerSettings,
    backend: Backend,
    store: ExecutionStore,
    *,
    clock: Clock | None = None,
) -> FastAPI:
    state = RunnerState(settings, backend, store)
    now: Clock = clock or (lambda: datetime.now(UTC))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        _reconcile_after_restart()
        yield

    app = FastAPI(
        title="AgentForge Runner",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runner = state

    @app.exception_handler(RunnerError)
    async def _runner_error(_: Request, exc: RunnerError) -> JSONResponse:
        return _problem(exc.status, exc.code, exc.detail)

    def _client_ok(request: Request) -> None:
        host = request.client.host if request.client else None
        if not client_allowed(host, settings.allowed_client_cidrs):
            raise RunnerError("client_not_allowed", "origen fuera de la red permitida", status=403)

    def authenticated(request: Request) -> None:
        if not bearer_matches(request.headers.get("authorization"), settings.token_sha256):
            raise RunnerError("unauthenticated", "credencial ausente o inválida", status=401)
        _client_ok(request)

    def admin_authenticated(request: Request) -> None:
        if settings.admin_token_sha256 is None:
            raise RunnerError("admin_disabled", "sin credencial de administración", status=404)
        if not bearer_matches(request.headers.get("authorization"), settings.admin_token_sha256):
            raise RunnerError("unauthenticated", "credencial ausente o inválida", status=401)
        _client_ok(request)

    Authenticated = Annotated[None, Depends(authenticated)]
    Admin = Annotated[None, Depends(admin_authenticated)]

    # -- protocol v1 ----------------------------------------------------------------------

    @app.get("/v1/health")
    def health(_: Authenticated, refresh: bool = Query(default=False)) -> RunnerHealth:
        report = state.isolation(force=refresh)
        return RunnerHealth(
            runner_id=settings.runner_id,
            version=f"{__version__}+{PROTOCOL_VERSION}",
            backend=report.backend,
            rootless=report.rootless,
            profiles=sorted(settings.images),
            ready=report.ok,
            checks=report.checks,
        )

    @app.get("/v1/executions/{execution_id}")
    def show(execution_id: UUID, _: Authenticated) -> ExecutionView:
        return _record(execution_id).view()

    @app.delete("/v1/executions/{execution_id}", status_code=204)
    def cancel(execution_id: UUID, _: Authenticated) -> Response:
        record = _record(execution_id)
        if record.state in ("finished", "uncertain"):
            raise RunnerError("execution_finished", "la ejecución ya no está en curso", status=409)
        store.request_cancel(execution_id)
        with state.lock:
            container = state.containers.get(execution_id)
        if container is not None:
            backend.kill(container)
        logger.info("Cancelación solicitada: %s (contenedor=%s)", execution_id, container)
        return Response(status_code=204)

    @app.post("/v1/executions")
    def execute(
        _: Authenticated,
        request: Annotated[UploadFile, File()],
        workspace: Annotated[UploadFile, File()],
    ) -> JSONResponse:
        report = state.isolation()
        if not report.ok:
            raise RunnerError(
                "isolation_unavailable",
                "el aislamiento obligatorio no puede aplicarse: " + _failed_checks(report),
                status=503,
            )
        sandbox_request = _parse_request(request)
        paths = validate_argv(sandbox_request.program, sandbox_request.argv)
        _validate_against_ceilings(sandbox_request, settings)
        image = settings.images.get(sandbox_request.profile)
        if image is None:
            raise RunnerError(
                "profile_unknown", f"perfil sin imagen: {sandbox_request.profile}", status=422
            )
        archive = _read_part(workspace, settings.max_archive_bytes)
        verify_archive(
            archive,
            expected_sha256=sandbox_request.workspace_sha256,
            expected_size=sandbox_request.workspace_bytes,
            max_bytes=settings.max_archive_bytes,
        )
        manifest = scan_archive(
            archive,
            ExtractionLimits(
                max_files=settings.max_files,
                max_file_bytes=settings.max_file_bytes,
                max_total_bytes=min(
                    settings.max_storage_bytes, sandbox_request.limits.storage_bytes
                ),
            ),
        )
        ensure_paths_in_manifest(paths, manifest)
        digest = request_digest(sandbox_request)
        if not state.slots.acquire(blocking=False):
            raise RunnerError("runner_busy", "sin capacidad para más ejecuciones", status=503)
        claimed = False
        try:
            claimed, record = store.claim(sandbox_request, digest)
            if not claimed:
                return _answer_existing(record, digest)
            result = _run_claimed(sandbox_request, record, image, archive, manifest.count)
            return JSONResponse(result.model_dump(mode="json"))
        except RunnerError:
            raise
        except Exception:
            if claimed:
                _close_as_failed(sandbox_request, "runner_internal_error")
            logger.exception("Fallo interno ejecutando %s", sandbox_request.execution_id)
            raise
        finally:
            with state.lock:
                state.containers.pop(sandbox_request.execution_id, None)
            state.slots.release()

    # -- administration: uncertain executions ---------------------------------------------

    @app.get("/v1/admin/executions")
    def admin_list(
        _: Admin,
        state_filter: Annotated[
            Literal["pending", "running", "finished", "uncertain"], Query(alias="state")
        ] = "uncertain",
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[ExecutionView]:
        return [r.view() for r in store.list_by_state(state_filter, limit=limit)]

    @app.get("/v1/admin/executions/{execution_id}")
    def admin_show(execution_id: UUID, _: Admin) -> ExecutionView:
        return _record(execution_id).view()

    @app.post("/v1/admin/executions/{execution_id}/reconcile")
    def admin_reconcile(execution_id: UUID, _: Admin) -> ExecutionView:
        """Close an uncertain execution with what the engine itself knows."""
        record = _record(execution_id)
        if record.state != "uncertain":
            raise RunnerError("not_uncertain", f"la ejecución está {record.state}", status=409)
        evidence = backend.inspect(record.container_name) if record.container_name else None
        if evidence is None:
            raise RunnerError(
                "no_evidence",
                "el motor no conserva el contenedor: solo puede resolverse como fallida",
                status=409,
            )
        if evidence.running:
            backend.kill(evidence.name)
            evidence = backend.inspect(evidence.name) or evidence
        _close_with_evidence(record, evidence, error_code="reconciled")
        backend.remove(evidence.name)
        logger.warning(
            "Ejecución %s reconciliada con evidencia del motor: %s", execution_id, evidence
        )
        return _record(execution_id).view()

    @app.post("/v1/admin/executions/{execution_id}/resolve")
    def admin_resolve(execution_id: UUID, body: ResolveRequest, _: Admin) -> ExecutionView:
        record = _record(execution_id)
        if record.state != "uncertain":
            raise RunnerError("not_uncertain", f"la ejecución está {record.state}", status=409)
        result = _synthetic(record, SandboxStatus.FAILED, f"operator: {body.reason}")
        store.resolve(
            execution_id,
            result,
            resolution=f"operator:{body.outcome}",
            error_code="resolved_failed",
        )
        if record.container_name:
            backend.remove(record.container_name)
        logger.warning(
            "Ejecución %s resuelta por un operador como fallida: %s", execution_id, body.reason
        )
        return _record(execution_id).view()

    # -- helpers bound to this app ---------------------------------------------------------

    def _record(execution_id: UUID) -> ExecutionRecord:
        record = store.get(execution_id)
        if record is None:
            raise RunnerError("execution_unknown", "ejecución desconocida", status=404)
        return record

    def _reconcile_after_restart() -> None:
        open_rows = store.open_executions()
        for row in open_rows:
            evidence = backend.inspect(row.container_name) if row.container_name else None
            if evidence is None:
                store.mark_uncertain(row.execution_id, "container_missing")
                continue
            store.mark_uncertain(row.execution_id, "runner_restarted")
            if evidence.running:
                backend.kill(evidence.name)
                evidence = backend.inspect(evidence.name) or evidence
                record = store.get(row.execution_id)
                if record is not None:
                    _close_with_evidence(
                        record,
                        evidence,
                        error_code="runner_restarted",
                        status=SandboxStatus.CANCELLED,
                    )
            else:
                record = store.get(row.execution_id)
                if record is not None:
                    _close_with_evidence(record, evidence, error_code="reconciled")
            backend.remove(evidence.name)
        known = {r.container_name for r in store.open_executions()} | {
            r.container_name for r in open_rows
        }
        orphans = [name for name in backend.list_containers() if name not in known]
        for name in orphans:
            backend.remove(name)
        if open_rows or orphans:
            logger.warning(
                "Arranque: %d ejecuciones abiertas reconciliadas, "
                "%d contenedores huérfanos eliminados",
                len(open_rows),
                len(orphans),
            )

    def _close_with_evidence(
        record: ExecutionRecord,
        evidence: ContainerState,
        *,
        error_code: str,
        status: SandboxStatus | None = None,
    ) -> None:
        derived = status or (
            SandboxStatus.KILLED if evidence.oom_killed else SandboxStatus.COMPLETED
        )
        result = SandboxResult(
            execution_id=record.execution_id,
            status=derived,
            exit_code=evidence.exit_code,
            stdout="",
            stderr=(
                f"reconciled from engine state: exit={evidence.exit_code} oom={evidence.oom_killed}"
            ),
            stdout_truncated=True,
            stderr_truncated=True,
            duration_ms=0,
            started_at=record.started_at,
            finished_at=evidence.finished_at or now(),
            runner_id=settings.runner_id,
            backend=backend.name,
            usage=ResourceUsage(oom_killed=evidence.oom_killed),
        )
        store.resolve(
            record.execution_id,
            result,
            resolution=f"engine:exit={evidence.exit_code},oom={evidence.oom_killed}",
            error_code=None if derived is SandboxStatus.COMPLETED else error_code,
        )

    def _run_claimed(
        sandbox_request: SandboxRequest,
        record: ExecutionRecord,
        image: str,
        archive: bytes,
        entries: int,
    ) -> SandboxResult:
        execution_id = sandbox_request.execution_id
        container = f"agentforge-{execution_id}"
        logger.info(
            "Ejecución %s: run=%s step=%s attempt=%d program=%s actor=%s limits=%s entradas=%d",
            execution_id,
            sandbox_request.run_id,
            sandbox_request.step,
            sandbox_request.attempt,
            sandbox_request.program.value,
            sandbox_request.actor_subject,
            sandbox_request.limits.model_dump(),
            entries,
        )
        current = store.get(execution_id)
        if current is not None and current.cancel_requested:
            result = _synthetic(record, SandboxStatus.CANCELLED, "cancelled before start")
            store.finish(execution_id, result, error_code="cancelled")
            return result
        store.mark_running(execution_id, container)
        with state.lock:
            state.containers[execution_id] = container
        spec = RunSpec(
            container_name=container,
            image=image,
            argv=list(sandbox_request.argv),
            env=dict(sandbox_request.env),
            archive=archive,
            limits=sandbox_request.limits,
            output_cap=min(sandbox_request.limits.max_output_bytes, settings.max_output_bytes),
        )
        outcome = backend.run(spec)
        latest = store.get(execution_id)
        cancelled = latest.cancel_requested if latest is not None else False
        status = outcome.status
        if cancelled and status in (
            SandboxStatus.COMPLETED,
            SandboxStatus.TIMED_OUT,
            SandboxStatus.KILLED,
        ):
            status = SandboxStatus.CANCELLED
        result = SandboxResult(
            execution_id=execution_id,
            status=status,
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr if status is not SandboxStatus.FAILED else outcome.detail,
            stdout_truncated=outcome.stdout_truncated,
            stderr_truncated=outcome.stderr_truncated,
            duration_ms=outcome.duration_ms,
            started_at=outcome.started_at,
            finished_at=outcome.finished_at,
            runner_id=settings.runner_id,
            backend=backend.name,
            usage=ResourceUsage(oom_killed=outcome.oom_killed),
        )
        store.finish(
            execution_id,
            result,
            error_code=None if status is SandboxStatus.COMPLETED else status.value,
        )
        logger.info(
            "Ejecución %s terminada: status=%s exit=%s duración=%dms truncado=%s/%s",
            execution_id,
            status.value,
            outcome.exit_code,
            outcome.duration_ms,
            outcome.stdout_truncated,
            outcome.stderr_truncated,
        )
        return result

    def _answer_existing(record: ExecutionRecord, digest: str) -> JSONResponse:
        if record.request_digest != digest:
            raise RunnerError(
                "execution_conflict", "ese identificador ya se usó con otra solicitud", status=409
            )
        if record.state == "finished" and record.result is not None:
            return JSONResponse(record.result.model_dump(mode="json"))
        if record.state == "uncertain":
            raise RunnerError(
                "execution_uncertain",
                "la ejecución quedó en estado incierto tras una caída; requiere revisión",
                status=409,
            )
        raise RunnerError("execution_in_progress", "la ejecución ya está en curso", status=409)

    def _close_as_failed(sandbox_request: SandboxRequest, code: str) -> None:
        record = store.get(sandbox_request.execution_id)
        if record is None:
            return
        result = _synthetic(record, SandboxStatus.FAILED, "fallo interno del runner")
        store.finish(sandbox_request.execution_id, result, error_code=code)

    def _synthetic(record: ExecutionRecord, status: SandboxStatus, note: str) -> SandboxResult:
        moment = now()
        return SandboxResult(
            execution_id=record.execution_id,
            status=status,
            exit_code=None,
            stdout="",
            stderr=note[:1000],
            duration_ms=0,
            started_at=record.started_at or moment,
            finished_at=moment,
            runner_id=settings.runner_id,
            backend=backend.name,
        )

    return app


def _failed_checks(report: IsolationReport) -> str:
    return ", ".join(f"{c.name}={c.detail}" for c in report.checks if not c.ok) or "sin detalle"


def _read_part(part: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = part.file.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise RunnerError("archive_too_large", f"la parte supera {max_bytes} bytes", status=413)
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_request(part: UploadFile) -> SandboxRequest:
    raw = _read_part(part, _REQUEST_PART_MAX)
    try:
        return SandboxRequest.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors()
        where = ".".join(str(p) for p in errors[0]["loc"]) if errors else "<root>"
        raise RunnerError(
            "request_invalid", f"solicitud inválida en {where or '<root>'}", status=422
        ) from exc


def _validate_against_ceilings(request: SandboxRequest, settings: RunnerSettings) -> None:
    limits: SandboxLimits = request.limits
    over = [
        name
        for name, asked, ceiling in (
            ("cpu_seconds", limits.cpu_seconds, settings.max_cpu_seconds),
            ("memory_bytes", limits.memory_bytes, settings.max_memory_bytes),
            ("pids", limits.pids, settings.max_pids),
            ("wall_seconds", limits.wall_seconds, settings.max_wall_seconds),
            ("storage_bytes", limits.storage_bytes, settings.max_storage_bytes),
            ("max_output_bytes", limits.max_output_bytes, settings.max_output_bytes),
        )
        if asked > ceiling
    ]
    if over:
        raise RunnerError(
            "limits_rejected",
            "límites por encima del máximo del runner: " + ", ".join(over),
            status=422,
        )
    if request.network != "none" or request.untrusted is not True:
        raise RunnerError("request_invalid", "política de red o confianza no admitida", status=422)


__all__ = ["RunnerState", "create_runner_app"]
