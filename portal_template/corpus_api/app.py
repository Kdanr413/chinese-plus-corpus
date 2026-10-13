"""FastAPI application factory for the local corpus platform backend."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse

from corpus_registry import (
    CorpusRegistry,
    DatabaseConfigurationError,
    DatabaseDependencyError,
    RegistryNotFoundError,
)

from .models import (
    CorpusListResponse,
    CorpusVersionListResponse,
    ErrorResponse,
    HealthResponse,
    SearchResponse,
)
from .service import (
    CorpusService,
    CorpusServiceUnavailableError,
    SearchAssetUnavailableError,
    SearchEngineCache,
    SearchRequestError,
    SearchTargetError,
)
from .platform import router as platform_router
from .recognition import router as recognition_router
from .workbench import router as workbench_router
from .model_errors import router as model_errors_router
from .topic_workflow import router as topic_workflow_router


API_VERSION = "1.6.0"
SERVICE_NAME = "chinese-plus-corpus-api"
WEB_ROOT = Path(__file__).resolve().parent / "web"
_SEARCH_CACHE = SearchEngineCache()


def get_corpus_service() -> CorpusService:
    """Build the service from the existing environment-configured repository."""
    return CorpusService(CorpusRegistry(), _SEARCH_CACHE)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def create_app(
    service_factory: Callable[[], CorpusService] | None = None,
) -> FastAPI:
    application = FastAPI(
        title="‘中文+’语料库 API",
        description="支持多文件、多模型、提示词版本、人工反馈、Corpus Release 与 LangSC.BCC 检索的一体化服务。",
        version=API_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    dependency = service_factory or get_corpus_service

    @application.get("/", include_in_schema=False)
    def platform_page() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html", media_type="text/html")

    @application.get("/search", include_in_schema=False)
    def search_page() -> FileResponse:
        return FileResponse(WEB_ROOT / "search.html", media_type="text/html")

    @application.get("/files/view", include_in_schema=False)
    def file_viewer_page() -> FileResponse:
        return FileResponse(WEB_ROOT / "file-viewer.html", media_type="text/html")

    @application.get("/topic-workflow", include_in_schema=False)
    def topic_workflow_page() -> FileResponse:
        """Serve the embedded taxonomy construction and annotation workbench."""
        return FileResponse(WEB_ROOT / "topic-workflow.html", media_type="text/html")

    @application.get("/assets/ui-polish.css", include_in_schema=False)
    def shared_ui_styles() -> FileResponse:
        return FileResponse(WEB_ROOT / "ui-polish.css", media_type="text/css")

    @application.get("/docs", include_in_schema=False)
    def api_guide_page() -> FileResponse:
        """Serve the Chinese, task-oriented API guide."""
        return FileResponse(WEB_ROOT / "api-docs.html", media_type="text/html")

    @application.get("/api-reference", include_in_schema=False)
    def interactive_api_reference():
        """Keep Swagger available for developers who need live requests."""
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="“中文+”语料库 · 交互式 API 调试",
        )

    @application.exception_handler(PermissionError)
    async def admin_forbidden(request: Request, exc: PermissionError) -> JSONResponse:
        del request
        return _error(403, "admin_forbidden", str(exc))

    @application.exception_handler(FileNotFoundError)
    async def file_not_found(request: Request, exc: FileNotFoundError) -> JSONResponse:
        del request
        return _error(404, "file_not_found", str(exc))

    @application.exception_handler(RegistryNotFoundError)
    async def registry_not_found(
        request: Request, exc: RegistryNotFoundError
    ) -> JSONResponse:
        del request
        return _error(404, "registry_not_found", str(exc))

    @application.exception_handler(DatabaseConfigurationError)
    async def database_not_configured(
        request: Request, exc: DatabaseConfigurationError
    ) -> JSONResponse:
        del request
        return _error(503, "database_not_configured", str(exc))

    @application.exception_handler(DatabaseDependencyError)
    async def database_dependency_missing(
        request: Request, exc: DatabaseDependencyError
    ) -> JSONResponse:
        del request
        return _error(503, "database_dependency_missing", str(exc))

    @application.exception_handler(CorpusServiceUnavailableError)
    async def registry_unavailable(
        request: Request, exc: CorpusServiceUnavailableError
    ) -> JSONResponse:
        del request
        return _error(503, "registry_unavailable", str(exc))

    @application.exception_handler(SearchTargetError)
    async def search_target_invalid(
        request: Request, exc: SearchTargetError
    ) -> JSONResponse:
        del request
        return _error(409, "search_target_invalid", str(exc))

    @application.exception_handler(SearchRequestError)
    async def search_request_invalid(
        request: Request, exc: SearchRequestError
    ) -> JSONResponse:
        del request
        return _error(400, "search_request_invalid", str(exc))

    @application.exception_handler(SearchAssetUnavailableError)
    async def search_asset_unavailable(
        request: Request, exc: SearchAssetUnavailableError
    ) -> JSONResponse:
        del request
        return _error(503, "search_asset_unavailable", str(exc))

    @application.get(
        "/health",
        response_model=HealthResponse,
        tags=["System"],
        summary="检查 API 服务",
    )
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok", service=SERVICE_NAME, api_version=API_VERSION
        )

    @application.get(
        "/api/v1/corpora",
        response_model=CorpusListResponse,
        responses={503: {"model": ErrorResponse}},
        tags=["Corpus Registry"],
        summary="列出已登记语料库",
    )
    def list_corpora(
        service: CorpusService = Depends(dependency),
    ) -> CorpusListResponse:
        items = service.list_corpora()
        return CorpusListResponse(items=items, count=len(items))

    @application.get(
        "/api/v1/corpora/{corpus_id}/versions",
        response_model=CorpusVersionListResponse,
        responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
        tags=["Corpus Registry"],
        summary="列出一个语料库的版本",
    )
    def list_versions(
        corpus_id: uuid.UUID,
        service: CorpusService = Depends(dependency),
    ) -> CorpusVersionListResponse:
        items = service.list_versions(corpus_id)
        return CorpusVersionListResponse(
            corpus_id=corpus_id, items=items, count=len(items)
        )

    @application.get(
        "/api/v1/search",
        response_model=SearchResponse,
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                   409: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
        tags=["Corpus Search"],
        summary="执行 BCC 频率检索",
    )
    def search(
        q: Annotated[str, Query(min_length=1, max_length=500, description="BCC 检索式")],
        corpus_key: Annotated[str | None, Query(max_length=128)] = None,
        version: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        service: CorpusService = Depends(dependency),
    ) -> SearchResponse:
        return SearchResponse.model_validate(
            service.search(q, corpus_key=corpus_key, version=version, limit=limit)
        )

    @application.get(
        "/api/v1/kwic",
        response_model=SearchResponse,
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                   409: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
        tags=["Corpus Search"],
        summary="执行 BCC 上下文检索",
    )
    def kwic(
        q: Annotated[str, Query(min_length=1, max_length=500, description="BCC 检索式")],
        corpus_key: Annotated[str | None, Query(max_length=128)] = None,
        version: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        page: Annotated[int, Query(ge=0)] = 0,
        window: Annotated[int, Query(ge=1, le=200)] = 50,
        service: CorpusService = Depends(dependency),
    ) -> SearchResponse:
        return SearchResponse.model_validate(
            service.kwic(
                q,
                corpus_key=corpus_key,
                version=version,
                limit=limit,
                page=page,
                window=window,
            )
        )

    application.include_router(workbench_router)
    application.include_router(model_errors_router)
    application.include_router(platform_router)
    application.include_router(recognition_router)
    application.include_router(topic_workflow_router)

    return application


app = create_app()
