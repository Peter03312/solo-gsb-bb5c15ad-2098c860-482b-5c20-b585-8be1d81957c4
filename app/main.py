"""HTTP API（FastAPI）。

端点：
  GET  /healthz                       健康检查
  POST /api/v1/procedures             首次发布规程（编号、正文、UTC [起,止)）
  POST /api/v1/corrections            提交更正（单条或数组；数组=并发更正，整体成败）
  GET  /api/v1/procedures             列出全部规程
  GET  /api/v1/procedures/{no}/versions           查看某规程已发布的全部版本
  GET  /api/v1/procedures/{no}/effective          按作业时刻+知悉时刻回溯
  GET  /api/v1/effective                         跨规程回溯（无匹配返回空列表）

查询参数 work_at（作业时刻）与 known_at（知悉时刻）均为带时区 ISO 8601；
known_at 省略时取服务端当前 UTC 时刻。
"""
from __future__ import annotations

import json
import logging
import sqlite3

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import service
from .config import APP_HOST, APP_PORT
from .db import connect, init_db
from .errors import ApiError
from .timeutil import canonical, now_canonical, parse_utc

logger = logging.getLogger("sop")

app = FastAPI(title="安全作业规程历史回溯服务", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()  # 非 Docker 方式直接启动时同样保证库已初始化


@app.middleware("http")
async def _db_session(request: Request, call_next):
    # 每个请求一条连接；写服务的事务在 service 层以 BEGIN IMMEDIATE 开启
    request.state.db = connect()
    try:
        return await call_next(request)
    finally:
        request.state.db.close()


@app.exception_handler(ApiError)
async def _api_error_handler(_request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"code": exc.code, "message": exc.message}})


@app.exception_handler(sqlite3.OperationalError)
async def _sqlite_locked_handler(_request: Request, exc: sqlite3.OperationalError):
    # 与并发更正冲突语义对齐：拿不到写锁时拒绝整个提交，历史不变
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "CONCURRENT_CORRECTION",
                               "message": "存在并发更正冲突，请重试（本次提交已整体拒绝）"}})
    logger.exception("SQLite operational error")
    return JSONResponse(status_code=500,
                        content={"error": {"code": "INTERNAL", "message": str(exc)}})


@app.exception_handler(Exception)
async def _unexpected_handler(_request: Request, exc: Exception):
    logger.exception("unexpected error")
    return JSONResponse(status_code=500,
                        content={"error": {"code": "INTERNAL", "message": "服务内部错误"}})


async def _json_body(request: Request):
    raw = await request.body()
    if not raw:
        raise ApiError(400, "INVALID_REQUEST", "请求体必须是 JSON")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(400, "INVALID_REQUEST", "请求体不是合法 JSON")


def _ok(data) -> JSONResponse:
    return JSONResponse({"data": data})


# ---------------------------------------------------------------- 写入

@app.post("/api/v1/procedures", status_code=201)
async def create_procedure(request: Request):
    """首次发布。发布时间由服务端记录，忽略客户端任何 published_at。"""
    payload = await _json_body(request)
    version = service.publish_initial(request.state.db, payload)
    return JSONResponse(status_code=201,
                        content={"data": {"procedure": {"procedure_no": version.procedure_no},
                                          "version": version.to_json()}})


@app.post("/api/v1/corrections", status_code=201)
async def create_correction(request: Request):
    """更正：单对象返回单版本；数组按并发更正处理，全部成功或全部拒绝。"""
    payload = await _json_body(request)
    result = service.publish_corrections(request.state.db, payload)
    if isinstance(result, list):
        return JSONResponse(status_code=201,
                            content={"data": {"corrections": [v.to_json() for v in result]}})
    return JSONResponse(status_code=201, content={"data": {"correction": result.to_json()}})


# ---------------------------------------------------------------- 查询

@app.get("/healthz")
def healthz():
    return {"status": "ok", "now": now_canonical()}


@app.get("/api/v1/procedures")
def get_procedures(request: Request):
    return _ok({"procedures": service.list_procedures(request.state.db)})


@app.get("/api/v1/procedures/{procedure_no}/versions")
def get_versions(request: Request, procedure_no: str):
    versions = service.list_versions(request.state.db, procedure_no)
    if not versions:
        raise ApiError(404, "PROCEDURE_NOT_FOUND", f"规程编号不存在: {procedure_no}")
    return _ok({"procedure_no": procedure_no, "versions": [v.to_json() for v in versions]})


def _parse_moment(value: str, field: str) -> str:
    return canonical(parse_utc(value, field))


@app.get("/api/v1/procedures/{procedure_no}/effective")
def get_effective(request: Request, procedure_no: str,
                  work_at: str = Query(..., description="作业时刻（ISO 8601，带时区）"),
                  known_at: str | None = Query(None, description="知悉时刻（默认服务端当前 UTC）")):
    work = _parse_moment(work_at, "work_at")
    known = _parse_moment(known_at, "known_at") if known_at else now_canonical()
    if service.list_versions(request.state.db, procedure_no) == []:
        raise ApiError(404, "PROCEDURE_NOT_FOUND", f"规程编号不存在: {procedure_no}")
    version = service.find_effective(request.state.db, procedure_no, work, known)
    # 无匹配：返回空（data 为 null），历史与结果均明确
    return _ok({"procedure_no": procedure_no, "work_at": work, "known_at": known,
                "effective": version.to_json() if version else None})


@app.get("/api/v1/effective")
def get_effective_all(request: Request,
                      work_at: str = Query(..., description="作业时刻（ISO 8601，带时区）"),
                      known_at: str | None = Query(None, description="知悉时刻（默认服务端当前 UTC）")):
    work = _parse_moment(work_at, "work_at")
    known = _parse_moment(known_at, "known_at") if known_at else now_canonical()
    matches = service.list_effective_all(request.state.db, work, known)
    # 无匹配：空列表
    return _ok({"work_at": work, "known_at": known, "results": [v.to_json() for v in matches]})


def main() -> None:  # 本地 python -m app.main 直跑入口
    import uvicorn
    uvicorn.run(app, host=APP_HOST, port=APP_PORT)


if __name__ == "__main__":
    main()
