"""Application factory: importing API contracts opens no clients or connections."""

import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from app.analyzers.reporting import render_json, render_markdown
from app.contracts.http import (
    EventPageV2,
    GitHubAcquisitionV1,
    GitHubBindingV1,
    InventoryEntryV1,
    InventoryPageV1,
    JobEventV2,
    RepositoryListItemV1,
    RepositoryPageV1,
    SessionV1,
)
from app.contracts.ingestion import SubmissionRecordV1, UploadReservationV1
from app.contracts.jobs import JobRecordV2, JobRecordV3, RetryResponseV1
from app.contracts.persistence import RepositoryCreate, RepositoryRecord
from app.contracts.structure import (
    AnalysisAcceptedV1,
    AnalysisConfigV1,
    ArchitectureReportV1,
    StructuralAnalysisRequestV1,
    StructuralLimitsV1,
)
from app.db.transaction import transaction
from app.domain.canonical_json import canonical_bytes
from app.domain.errors import (
    CapacityExceeded,
    DuplicateConflict,
    InvalidTransition,
    PersistenceError,
    RetryRejected,
    ScopeViolation,
    StaleAttempt,
    UploadRejected,
)
from app.domain.scope import TenantContext
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships, repositories, source_files, source_snapshots
from app.ports.object_store import ObjectStore
from app.repositories.command_repository import record_command, replay_command
from app.repositories.identity_repository import IdentityRepository
from app.repositories.repository_repository import RepositoryRepository
from app.security.authorization import require_repository
from app.security.cursors import CursorCodec
from app.security.oauth import OAuthRejected, OAuthService
from app.security.sessions import SessionService, token_digest
from app.services.github_acquisition import GitHubAcquisitionService
from app.services.jobs import JobService
from app.services.retries import RetryService
from app.services.structural_analysis import StructuralAnalysisService
from app.services.submissions import SubmissionService
from app.services.webhooks import GitHubWebhookService
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import and_, literal, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware


class ApiSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    environment: Literal["local", "test", "hosted"] = "local"
    auth_mode: Literal["disabled", "local_fixture", "github_app"] = "disabled"
    origin: str = "http://localhost:5173"


@dataclass(frozen=True)
class ApplicationPorts:
    factory: async_sessionmaker[AsyncSession]
    store: ObjectStore
    fixture_context: TenantContext | None = None
    oauth: OAuthService | None = None
    github: GitHubAcquisitionService | None = None
    webhooks: GitHubWebhookService | None = None
    readiness: Callable[[], Awaitable[bool]] | None = None


def create_app(settings: ApiSettings, ports: ApplicationPorts) -> FastAPI:
    parsed = urlsplit(settings.origin)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
    ):
        raise ValueError("exact origin required")
    loopback = {"localhost", "127.0.0.1", "::1"} | (
        {"testserver"} if settings.environment == "test" else set()
    )
    if settings.auth_mode == "local_fixture" and (
        settings.environment == "hosted"
        or parsed.hostname not in loopback
        or ports.fixture_context is None
    ):
        raise ValueError("fixture identity requires trusted local construction")
    if settings.auth_mode == "github_app" and ports.oauth is None:
        raise ValueError("GitHub authentication provider unavailable")
    secure = parsed.scheme == "https"
    if settings.environment == "hosted" and not secure:
        raise ValueError("hosted cookies require HTTPS")
    cookie = "__Host-codespy" if secure else "codespy_dev"
    csrf_cookie = "__Host-codespy-csrf" if secure else "codespy_dev_csrf"
    app = FastAPI(title="CodeSPY inventory API", version="0.2.0", debug=False)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[parsed.hostname])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"],
    )
    sessions = SessionService(ports.factory)
    jobs = JobService(ports.factory)
    retries = RetryService(ports.factory, ports.store)
    submissions = SubmissionService(ports.factory, ports.store)
    structural = StructuralAnalysisService(ports.factory)
    cursors = CursorCodec(secrets.token_bytes(32))

    def error(status: int, code: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={
                "schema_version": 1,
                "code": code,
                "message": code.replace("_", " "),
                "request_id": str(uuid4()),
                "retryable": status in {429, 503},
            },
        )

    @app.middleware("http")
    async def boundary(request: Request, call_next: RequestResponseEndpoint) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin != settings.origin:
            return error(403, "csrf_invalid")
        if (
            request.method in {"POST", "PUT", "DELETE", "PATCH"}
            and request.url.path != "/api/v1/webhooks/github"
        ):
            resolved = await sessions.resolve(request.cookies.get(cookie))
            if resolved is None:
                return error(401, "unauthenticated")
            referer = request.headers.get("referer")
            if origin is None:
                if referer is None:
                    return error(403, "csrf_invalid")
                ref = urlsplit(referer)
                if f"{ref.scheme}://{ref.netloc}" != settings.origin:
                    return error(403, "csrf_invalid")
            token = request.headers.get("x-csrf-token", "")
            if not 32 <= len(token) <= 128 or not secrets.compare_digest(
                token_digest(token), resolved[1]
            ):
                return error(403, "csrf_invalid")
            request.state.principal_id = resolved[0]
        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exception: HTTPException) -> Response:
        codes = {
            401: "unauthenticated",
            403: "csrf_invalid",
            404: "resource_unavailable",
            409: "invalid_state",
            413: "archive_limit_exceeded",
            415: "unsupported_format",
            422: "validation_error",
            503: "provider_unconfigured",
        }
        return error(exception.status_code, codes.get(exception.status_code, "invalid_state"))

    @app.exception_handler(RequestValidationError)
    async def wire_error(request: Request, exception: RequestValidationError) -> Response:
        return error(422, "validation_error")

    @app.exception_handler(PersistenceError)
    async def domain_error(request: Request, exception: PersistenceError) -> Response:
        if isinstance(exception, ScopeViolation):
            return error(404, "resource_unavailable")
        if isinstance(exception, CapacityExceeded):
            return error(429, "quota_exceeded")
        if isinstance(exception, DuplicateConflict):
            return error(409, "idempotency_conflict")
        if isinstance(exception, RetryRejected):
            return error(409, exception.code)
        if isinstance(exception, UploadRejected):
            return error(415 if exception.code == "unsupported_format" else 422, exception.code)
        if isinstance(exception, (InvalidTransition, StaleAttempt)):
            return error(409, "invalid_state")
        return error(503, "storage_unavailable")

    async def principal(request: Request) -> UUID:
        resolved = await sessions.resolve(request.cookies.get(cookie))
        if resolved is None:
            raise HTTPException(401)
        return resolved[0]

    async def repository_context(
        request: Request, id: UUID, *, write: bool = False
    ) -> TenantContext:
        user = await principal(request)
        async with transaction(ports.factory) as s:
            org = await s.scalar(
                select(repositories.c.org_id)
                .join(
                    memberships,
                    and_(
                        memberships.c.org_id == repositories.c.org_id,
                        memberships.c.principal_id == user,
                    ),
                )
                .where(repositories.c.id == id)
                .limit(1)
            )
            if org is None:
                raise ScopeViolation("repository unavailable")
            return await require_repository(s, org, user, id, write=write)

    async def resource_context(
        request: Request, table: Any, id: UUID, *, write: bool = False
    ) -> TenantContext:
        user = await principal(request)
        async with transaction(ports.factory) as s:
            repo = await s.scalar(
                select(table.c.repository_id)
                .join(
                    memberships,
                    and_(
                        memberships.c.org_id == table.c.org_id, memberships.c.principal_id == user
                    ),
                )
                .where(table.c.id == id)
                .limit(1)
            )
        if repo is None:
            raise ScopeViolation("resource unavailable")
        return await repository_context(request, repo, write=write)

    async def json_body(request: Request, model: type[BaseModel]) -> Any:
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 4096:
                raise HTTPException(413)
        try:
            return model.model_validate_json(data)
        except (ValidationError, ValueError):
            raise HTTPException(422) from None

    def key(request: Request) -> str:
        value = request.headers.get("idempotency-key", "")
        if not 1 <= len(value.encode()) <= 128:
            raise HTTPException(422)
        return value

    @app.post("/api/v1/webhooks/github", status_code=204)
    async def github_webhook(request: Request) -> Response:
        if ports.webhooks is None:
            return error(503, "provider_unavailable")
        raw = bytearray()
        async for block in request.stream():
            raw.extend(block)
            if len(raw) > 1048576:
                raise HTTPException(413)
        try:
            await ports.webhooks.receive(
                request.headers.get("x-github-delivery", ""),
                request.headers.get("x-github-event", ""),
                request.headers.get("x-hub-signature-256", ""),
                bytes(raw),
            )
        except ValueError:
            raise HTTPException(400) from None
        return Response(status_code=204)

    @app.get("/api/v1/auth/github/start")
    async def oauth_start() -> Response:
        if settings.auth_mode != "github_app" or ports.oauth is None:
            return error(503, "provider_unavailable")
        begun = await ports.oauth.begin("/")
        response = RedirectResponse(begun.url, status_code=303)
        response.set_cookie(
            "codespy_oauth_browser",
            begun.browser,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/api/v1/auth/github",
            max_age=600,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v1/auth/github/callback")
    async def oauth_callback(request: Request, state: str, code: str) -> Response:
        if settings.auth_mode != "github_app" or ports.oauth is None:
            return error(503, "provider_unavailable")
        try:
            user = await ports.oauth.callback(
                state, request.cookies.get("codespy_oauth_browser", ""), code
            )
        except OAuthRejected:
            return error(401, "oauth_invalid")
        issued = await sessions.issue(user, previous=request.cookies.get(cookie))
        response = RedirectResponse(settings.origin + "/", status_code=303)
        response.set_cookie(
            cookie,
            issued.token,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
            max_age=86400,
        )
        response.set_cookie(
            csrf_cookie, issued.csrf, secure=secure, samesite="lax", path="/", max_age=86400
        )
        response.delete_cookie("codespy_oauth_browser", path="/api/v1/auth/github")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def health_ready() -> Response:
        if ports.readiness is None or not await ports.readiness():
            return error(503, "dependencies_unavailable")
        return JSONResponse({"status": "ready"})

    @app.get("/api/v1/session")
    async def session(request: Request, response: Response) -> SessionV1:
        supplied = request.cookies.get(cookie)
        resolved = await sessions.resolve(supplied)
        csrf = request.cookies.get(csrf_cookie)
        if resolved is None:
            if supplied is not None:
                raise HTTPException(401)
            if settings.auth_mode != "local_fixture":
                raise HTTPException(503)
            assert ports.fixture_context is not None
            issued = await sessions.issue(ports.fixture_context.principal_id)
            response.set_cookie(
                cookie,
                issued.token,
                httponly=True,
                secure=secure,
                samesite="lax",
                path="/",
                max_age=86400,
            )
            csrf = issued.csrf
            user = issued.principal_id
        else:
            user = resolved[0]
            if csrf is None or not secrets.compare_digest(token_digest(csrf), resolved[1]):
                assert supplied is not None
                csrf = await sessions.renew_csrf(supplied)
        response.set_cookie(
            csrf_cookie,
            csrf,
            httponly=False,
            secure=secure,
            samesite="lax",
            path="/",
            max_age=86400,
        )
        response.headers["Cache-Control"] = "no-store"
        return SessionV1(principal_id=user, csrf_token=csrf, auth_backend=settings.auth_mode)

    @app.post("/api/v1/session/logout", status_code=204)
    @app.post("/api/v1/logout", status_code=204)
    async def logout(request: Request) -> Response:
        await sessions.revoke(request.cookies[cookie])
        response = Response(status_code=204)
        response.delete_cookie(cookie, path="/", secure=secure, httponly=True, samesite="lax")
        response.delete_cookie(csrf_cookie, path="/", secure=secure, samesite="lax")
        return response

    @app.get("/api/v1/repositories")
    async def list_repositories(
        request: Request, after: str | None = None, limit: int = 50
    ) -> RepositoryPageV1:
        user = await principal(request)
        grants = PHASE2_TABLES["repository_grants"]
        if not 1 <= limit <= 200:
            raise HTTPException(422)
        scope = {"principal": str(user), "collection": "repositories", "version": 1}
        page_predicate: Any = True
        if after:
            try:
                stamp, org, identity = json.loads(cursors.decode(after, scope))
                page_predicate = tuple_(
                    repositories.c.created_at, repositories.c.org_id, repositories.c.id
                ) > tuple_(
                    literal(datetime.fromisoformat(stamp)),
                    literal(UUID(org)),
                    literal(UUID(identity)),
                )
            except (ValueError, TypeError):
                raise HTTPException(422) from None
        async with transaction(ports.factory) as s:
            rows = (
                (
                    await s.execute(
                        select(repositories)
                        .join(
                            grants,
                            and_(
                                grants.c.org_id == repositories.c.org_id,
                                grants.c.repository_id == repositories.c.id,
                                grants.c.principal_id == user,
                                grants.c.revoked_at.is_(None),
                            ),
                        )
                        .join(
                            memberships,
                            and_(
                                memberships.c.org_id == repositories.c.org_id,
                                memberships.c.principal_id == user,
                            ),
                        )
                        .where(page_predicate)
                        .order_by(
                            repositories.c.created_at, repositories.c.org_id, repositories.c.id
                        )
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            items = []
            for row in rows[:limit]:
                try:
                    await require_repository(s, row["org_id"], user, row["id"])
                except ScopeViolation:
                    continue
                items.append({k: row[k] for k in ["id", "org_id", "display_name", "source_kind"]})
        return RepositoryPageV1(
            items=[RepositoryListItemV1(**item) for item in items],
            next_cursor=(
                cursors.encode(
                    scope,
                    json.dumps(
                        [
                            rows[limit - 1]["created_at"].isoformat(),
                            str(rows[limit - 1]["org_id"]),
                            str(rows[limit - 1]["id"]),
                        ]
                    ),
                )
                if len(rows) > limit
                else None
            ),
        )

    @app.post("/api/v1/repositories", status_code=201)
    async def create_repository(request: Request) -> RepositoryRecord:
        user = await principal(request)
        body = await json_body(request, RepositoryCreate)
        command_key = key(request)
        if body.source_kind not in {"archive", "github"}:
            raise HTTPException(422)
        if body.source_kind == "github" and ports.github is None:
            raise HTTPException(503)
        async with transaction(ports.factory) as s:
            org = await s.scalar(
                select(memberships.c.org_id)
                .where(
                    memberships.c.principal_id == user, memberships.c.role.in_(["owner", "admin"])
                )
                .order_by(memberships.c.created_at, memberships.c.org_id)
                .limit(1)
            )
            if org is None:
                raise ScopeViolation("organization unavailable")
            limits = PHASE2_TABLES["org_work_limits"]
            await s.execute(pg_insert(limits).values(org_id=org).on_conflict_do_nothing())
            await s.execute(select(limits.c.org_id).where(limits.c.org_id == org).with_for_update())
            await s.execute(
                select(memberships.c.role)
                .where(memberships.c.org_id == org, memberships.c.principal_id == user)
                .with_for_update()
            )
            ctx = await IdentityRepository(s).resolve_context(org, user)
            previous = await replay_command(
                s, ctx, command_key, "repository_create", body.model_dump(mode="json")
            )
            if previous is not None:
                return RepositoryRecord.model_validate_json(json.dumps(previous))
            repo = await RepositoryRepository(s).create(ctx, body)
            grants = PHASE2_TABLES["repository_grants"]
            limits = PHASE2_TABLES["org_work_limits"]
            await s.execute(pg_insert(limits).values(org_id=org).on_conflict_do_nothing())
            await s.execute(
                pg_insert(grants)
                .values(org_id=org, repository_id=repo.id, principal_id=user, permission="write")
                .on_conflict_do_nothing()
            )
            await record_command(
                s,
                ctx,
                command_key,
                "repository_create",
                body.model_dump(mode="json"),
                repo.model_dump(mode="json"),
            )
        return repo

    @app.post("/api/v1/repositories/{repository_id}/github-binding")
    async def bind_github(request: Request, repository_id: UUID) -> GitHubBindingV1:
        if ports.github is None:
            raise HTTPException(503)
        user = await principal(request)
        body = await json_body(request, GitHubBindingV1)
        key(request)
        async with transaction(ports.factory) as s:
            org = await s.scalar(
                select(repositories.c.org_id)
                .join(
                    memberships,
                    and_(
                        memberships.c.org_id == repositories.c.org_id,
                        memberships.c.principal_id == user,
                        memberships.c.role.in_(["owner", "admin"]),
                    ),
                )
                .where(repositories.c.id == repository_id)
            )
            if org is None:
                raise ScopeViolation("repository unavailable")
            ctx = await IdentityRepository(s).resolve_context(org, user)
        await ports.github.bind(ctx, repository_id, body.github_repository_id, body.installation_id)
        return GitHubBindingV1.model_validate(body)

    @app.post("/api/v1/repositories/{repository_id}/uploads", status_code=201)
    async def reserve_upload(request: Request, repository_id: UUID) -> SubmissionRecordV1:
        ctx = await repository_context(request, repository_id, write=True)
        body = await json_body(request, UploadReservationV1)
        if body.repository_id != repository_id:
            raise HTTPException(422)
        return await submissions.reserve(ctx, body, key(request))

    @app.put("/api/v1/uploads/{submission_id}/content", status_code=202)
    @app.put("/api/v1/submissions/{submission_id}/content", status_code=202)
    async def upload_content(request: Request, submission_id: UUID) -> SubmissionRecordV1:
        ctx = await resource_context(
            request, PHASE2_TABLES["source_submissions"], submission_id, write=True
        )
        if request.headers.get("content-type", "").split(";")[0] not in {
            "application/zip",
            "application/octet-stream",
        }:
            raise HTTPException(415)
        current = await submissions.get(ctx, submission_id)
        length = request.headers.get("content-length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                raise HTTPException(422) from None
            if declared < 0 or declared != current.declared_size:
                raise HTTPException(422)
        return await submissions.write_content(ctx, submission_id, request.stream())

    @app.post("/api/v1/repositories/{repository_id}/github-acquisitions", status_code=202)
    async def acquire_github(request: Request, repository_id: UUID) -> SubmissionRecordV1:
        ctx = await repository_context(request, repository_id, write=True)
        if ports.github is None:
            raise HTTPException(503)
        body = await json_body(request, GitHubAcquisitionV1)
        return await ports.github.request(ctx, repository_id, body.requested_ref, key(request))

    @app.delete("/api/v1/submissions/{submission_id}")
    async def delete_submission(request: Request, submission_id: UUID) -> SubmissionRecordV1:
        ctx = await resource_context(
            request, PHASE2_TABLES["source_submissions"], submission_id, write=True
        )
        result = await submissions.cancel(ctx, submission_id)
        if result.state == "admitted":
            raise HTTPException(409)
        objects = PHASE2_TABLES["object_records"]
        async with transaction(ports.factory) as s:
            from app.security.authorization import lock_scope
            from sqlalchemy import func, update

            await lock_scope(s, ctx.org_id, ctx.principal_id, result.repository_id)
            selected = await s.scalar(
                select(PHASE2_TABLES["source_submissions"].c.input_object_id).where(
                    PHASE2_TABLES["source_submissions"].c.org_id == ctx.org_id,
                    PHASE2_TABLES["source_submissions"].c.id == submission_id,
                )
            )
            if selected is not None:
                await s.execute(
                    update(objects)
                    .where(
                        objects.c.org_id == ctx.org_id,
                        objects.c.id == selected,
                        objects.c.state != "available",
                    )
                    .values(expires_at=func.clock_timestamp())
                )
        return result

    @app.get("/api/v1/uploads/{submission_id}")
    @app.get("/api/v1/submissions/{submission_id}")
    async def get_submission(request: Request, submission_id: UUID) -> SubmissionRecordV1:
        ctx = await resource_context(request, PHASE2_TABLES["source_submissions"], submission_id)
        return await submissions.get(ctx, submission_id)

    @app.post("/api/v1/submissions/{submission_id}/cancel")
    async def cancel_submission(request: Request, submission_id: UUID) -> SubmissionRecordV1:
        ctx = await resource_context(
            request, PHASE2_TABLES["source_submissions"], submission_id, write=True
        )
        return await submissions.cancel(ctx, submission_id)

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(request: Request, job_id: UUID) -> JobRecordV2 | JobRecordV3:
        ctx = await resource_context(request, PHASE2_TABLES["jobs"], job_id)
        return await jobs.get(ctx, job_id)

    @app.post("/api/v1/jobs/{job_id}/cancel")
    async def cancel_job(
        request: Request, response: Response, job_id: UUID
    ) -> JobRecordV2 | JobRecordV3:
        ctx = await resource_context(request, PHASE2_TABLES["jobs"], job_id, write=True)
        result = await jobs.request_cancel(ctx, job_id)
        response.status_code = 202 if result.state == "cancelling" else 200
        return result

    @app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
    async def retry_job(request: Request, job_id: UUID) -> RetryResponseV1:
        ctx = await resource_context(request, PHASE2_TABLES["jobs"], job_id, write=True)
        return await retries.retry(ctx, job_id, key(request))

    @app.get("/api/v1/jobs/{job_id}/events")
    async def job_events(
        request: Request, job_id: UUID, after: str | None = None, limit: int = 50
    ) -> EventPageV2:
        if not 1 <= limit <= 200:
            raise HTTPException(422)
        ctx = await resource_context(request, PHASE2_TABLES["jobs"], job_id)
        events = PHASE2_TABLES["job_events"]
        scope = {"org": str(ctx.org_id), "job": str(job_id), "version": 2}
        try:
            position = int(cursors.decode(after, scope)) if after else 0
        except ValueError:
            raise HTTPException(422) from None
        async with transaction(ports.factory) as s:
            minimum = await s.scalar(
                select(PHASE2_TABLES["jobs"].c.events_min_sequence).where(
                    PHASE2_TABLES["jobs"].c.org_id == ctx.org_id,
                    PHASE2_TABLES["jobs"].c.id == job_id,
                )
            )
            if position and minimum is not None and position < minimum - 1:
                raise HTTPException(410)
            rows = (
                (
                    await s.execute(
                        select(
                            events.c.sequence,
                            events.c.event_type,
                            events.c.public_payload,
                            events.c.created_at,
                        )
                        .where(
                            events.c.org_id == ctx.org_id,
                            events.c.job_id == job_id,
                            events.c.sequence > position,
                        )
                        .order_by(events.c.sequence)
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
        items: list[JobEventV2] = []
        size = 0
        for row in rows[:limit]:
            item = JobEventV2(job_id=job_id, **dict(row))
            size += len(canonical_bytes(item.model_dump()))
            if size > 240000:
                break
            items.append(item)
        return EventPageV2(
            items=items,
            next_cursor=(
                cursors.encode(scope, str(items[-1].sequence))
                if items and len(rows) > len(items)
                else None
            ),
        )

    @app.get("/api/v1/snapshots/{snapshot_id}/inventory")
    async def inventory(
        request: Request,
        snapshot_id: UUID,
        after: str | None = None,
        limit: int = 50,
        eligible: bool | None = None,
    ) -> InventoryPageV1:
        if not 1 <= limit <= 200:
            raise HTTPException(422)
        ctx = await resource_context(request, source_snapshots, snapshot_id)
        entries = PHASE2_TABLES["inventory_entries"]
        scope = {
            "org": str(ctx.org_id),
            "snapshot": str(snapshot_id),
            "version": 1,
            "eligible": eligible,
        }
        try:
            position_path = cursors.decode(after, scope) if after else None
        except ValueError:
            raise HTTPException(422) from None
        async with transaction(ports.factory) as s:
            query = (
                select(
                    source_files.c.id,
                    source_files.c.path,
                    source_files.c.sha256,
                    source_files.c.size_bytes,
                    source_files.c.language,
                    entries.c.eligible,
                    entries.c.exclusion_reason,
                )
                .join(
                    entries,
                    and_(
                        entries.c.org_id == source_files.c.org_id,
                        entries.c.snapshot_id == source_files.c.snapshot_id,
                        entries.c.file_id == source_files.c.id,
                    ),
                )
                .where(
                    source_files.c.org_id == ctx.org_id, source_files.c.snapshot_id == snapshot_id
                )
                .order_by(source_files.c.path)
                .limit(limit + 1)
            )
            if eligible is not None:
                query = query.where(entries.c.eligible == eligible)
            if position_path is not None:
                query = query.where(source_files.c.path > position_path)
            rows = (await s.execute(query)).mappings().all()
        inventory_items: list[InventoryEntryV1] = []
        size = 0
        for row in rows[:limit]:
            entry = InventoryEntryV1(**dict(row))
            size += len(canonical_bytes(entry.model_dump()))
            if size > 240000:
                break
            inventory_items.append(entry)
        return InventoryPageV1(
            items=inventory_items,
            snapshot_id=snapshot_id,
            next_cursor=(
                cursors.encode(scope, inventory_items[-1].path)
                if inventory_items and len(rows) > len(inventory_items)
                else None
            ),
        )

    @app.post("/api/v1/repositories/{repository_id}/analyses", status_code=202)
    async def create_analysis(request: Request, repository_id: UUID) -> AnalysisAcceptedV1:
        ctx = await repository_context(request, repository_id, write=True)
        body = await json_body(request, StructuralAnalysisRequestV1)
        config = AnalysisConfigV1(
            toolchain_hash="0" * 64,
            include_prefixes=body.include_prefixes,
            limits=StructuralLimitsV1(),
        )
        spec, job = await structural.create_analysis_spec(
            ctx, repository_id, body.snapshot_id, config, key(request)
        )
        return AnalysisAcceptedV1(
            spec_id=spec.id,
            job_id=job.id,
            snapshot_id=body.snapshot_id,
        )

    @app.get("/api/v1/repositories/{repository_id}/analyses/{run_id}/report")
    async def get_analysis_report(
        request: Request, repository_id: UUID, run_id: UUID
    ) -> ArchitectureReportV1:
        ctx = await repository_context(request, repository_id, write=False)
        async with transaction(ports.factory) as s:
            from app.models.phase3_tables import structural_reports

            row = (
                (
                    await s.execute(
                        select(structural_reports).where(
                            structural_reports.c.org_id == ctx.org_id,
                            structural_reports.c.repository_id == repository_id,
                            structural_reports.c.run_id == run_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if not row:
                raise HTTPException(404)
            return ArchitectureReportV1.model_validate(row["report_json"])

    @app.get("/api/v1/repositories/{repository_id}/analyses/{run_id}/export")
    async def export_analysis_report(
        request: Request, repository_id: UUID, run_id: UUID, format: str = "json"
    ) -> Response:
        ctx = await repository_context(request, repository_id, write=False)
        async with transaction(ports.factory) as s:
            from app.models.phase3_tables import structural_reports

            row = (
                (
                    await s.execute(
                        select(structural_reports).where(
                            structural_reports.c.org_id == ctx.org_id,
                            structural_reports.c.repository_id == repository_id,
                            structural_reports.c.run_id == run_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if not row:
                raise HTTPException(404)
            report = ArchitectureReportV1.model_validate(row["report_json"])

        if format == "markdown":
            content = render_markdown(report)
            media_type = "text/markdown; charset=utf-8"
            filename = f"report-{run_id}.md"
        else:
            content = render_json(report)
            media_type = "application/json"
            filename = f"report-{run_id}.json"

        return Response(
            content=content,
            media_type=media_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get("/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/structural-report")
    async def get_snapshot_structural_report(
        request: Request, repository_id: UUID, snapshot_id: UUID
    ) -> ArchitectureReportV1:
        ctx = await repository_context(request, repository_id, write=False)
        report = await structural.get_report_by_snapshot(ctx, repository_id, snapshot_id)
        if not report:
            raise HTTPException(404)
        return report

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            contract_models: tuple[type[BaseModel], ...] = (
                RepositoryCreate,
                UploadReservationV1,
                GitHubAcquisitionV1,
                GitHubBindingV1,
                StructuralAnalysisRequestV1,
                AnalysisAcceptedV1,
            )
            for model in contract_models:
                components[model.__name__] = model.model_json_schema(
                    ref_template="#/components/schemas/{model}"
                )
            body_contracts: tuple[tuple[str, type[BaseModel]], ...] = (
                ("/api/v1/repositories", RepositoryCreate),
                ("/api/v1/repositories/{repository_id}/github-binding", GitHubBindingV1),
                ("/api/v1/repositories/{repository_id}/uploads", UploadReservationV1),
                ("/api/v1/repositories/{repository_id}/github-acquisitions", GitHubAcquisitionV1),
                ("/api/v1/repositories/{repository_id}/analyses", StructuralAnalysisRequestV1),
            )
            for path, model in body_contracts:
                schema["paths"][path]["post"]["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/" + model.__name__}
                        }
                    },
                }
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]  # FastAPI custom schema hook
    return app
