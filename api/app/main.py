"""FastAPI HTTP 层：路由、错误映射、应用工厂。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import service
from .db import Database
from .models import InvalidateRequest, RecordCreate
from .service import ApiError


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("DATABASE_PATH", "calibration.db")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = Database(path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        db.close()

    app = FastAPI(title="低温探测器标定记录服务", version="1.0.0", lifespan=lifespan)
    app.state.db = db
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError):
        fields = [
            {
                "loc": [str(p) for p in e.get("loc", [])],
                "msg": e.get("msg", ""),
                "type": e.get("type", ""),
            }
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求参数校验失败",
                    "details": {"fields": fields},
                }
            },
        )

    @app.get("/health")
    def health():
        with db.read() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/api/records")
    def list_records(valid: str | None = None):
        return {"records": service.list_records(db, valid)}

    @app.post("/api/records", status_code=201)
    def create_record(body: RecordCreate):
        return service.create_record(
            db,
            kind=body.kind,
            detector=body.detector,
            summary=body.summary,
            reading_mk=body.reading_mk,
            depends_on=body.depends_on,
        )

    @app.get("/api/records/{record_id}")
    def get_record(record_id: str):
        return service.get_record(db, record_id)

    @app.get("/api/records/{record_id}/lineage")
    def get_lineage(record_id: str):
        return service.get_lineage(db, record_id)

    @app.post("/api/records/{record_id}/invalidate")
    def invalidate(record_id: str, body: InvalidateRequest):
        result, replayed = service.invalidate_record(db, record_id, body.operation_id)
        return JSONResponse(
            content=result,
            headers={"X-Idempotent-Replay": "true" if replayed else "false"},
        )

    @app.get("/api/operations/{operation_id}")
    def get_operation(operation_id: str):
        return service.get_operation(db, operation_id)

    return app
