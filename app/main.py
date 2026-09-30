"""FastAPI 入口：工地作业车危险区穿越审计服务。"""
from __future__ import annotations

from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import Database
from .schemas import PointBatch, ZoneCreate
from .service import AuditService, ConflictError, NotFoundError, ValidationError

settings = get_settings()
db = Database(settings)
service = AuditService(db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    db.close()


app = FastAPI(
    title="工地作业车危险区穿越审计服务",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "登记带洞米制多边形危险区及其 UTC 半开生效时段；批量提交车辆轨迹点，"
        "查询各危险区的穿越区间与累计时长。"
    ),
)


@app.exception_handler(ValidationError)
async def _validation_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": "validation_error", "detail": str(exc)})


@app.exception_handler(ConflictError)
async def _conflict_handler(_: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(NotFoundError)
async def _not_found_handler(_: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> dict:
    return {
        "service": "danger-zone-crossing-audit",
        "docs": "/docs",
        "health": "/health",
        "endpoints": [
            "POST /api/v1/zones",
            "GET  /api/v1/zones",
            "GET  /api/v1/zones/{zone_id}",
            "GET  /api/v1/zones/{zone_id}/history",
            "POST /api/v1/trips/{trip_id}/points",
            "GET  /api/v1/trips/{trip_id}",
            "GET  /api/v1/trips/{trip_id}/crossings",
            "GET  /api/v1/crossing-diffs",
        ],
    }


@app.post("/api/v1/zones", status_code=201)
def create_zone(body: ZoneCreate) -> dict:
    return service.register_zone(body.model_dump())


@app.get("/api/v1/zones")
def list_zones() -> dict:
    return {"zones": service.list_zones()}


@app.get("/api/v1/zones/{zone_id}")
def get_zone(zone_id: str) -> JSONResponse:
    detail = service.get_zone(zone_id)
    if detail is None:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": f"危险区不存在: {zone_id}"}
        )
    return JSONResponse(content=detail)


@app.get("/api/v1/zones/{zone_id}/history")
def get_zone_history(zone_id: str) -> JSONResponse:
    history = service.get_zone_history(zone_id)
    if history is None:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": f"危险区不存在: {zone_id}"}
        )
    return JSONResponse(
        content={"zone_id": zone_id, "count": len(history), "versions": history}
    )


@app.post("/api/v1/trips/{trip_id}/points")
def submit_points(trip_id: str, body: PointBatch) -> dict:
    result = service.submit_points(trip_id, body.model_dump())
    return result


@app.get("/api/v1/trips/{trip_id}")
def get_trip(trip_id: str) -> JSONResponse:
    rows = db.list_trip_points(trip_id)
    if not rows:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": f"车次无轨迹数据: {trip_id}"}
        )
    from datetime import datetime, timezone

    return JSONResponse(
        content={
            "trip_id": trip_id,
            "point_count": len(rows),
            "points": [
                {
                    "point_id": r["point_id"],
                    "ts": datetime.fromtimestamp(r["ts_epoch"], timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "x": r["x"],
                    "y": r["y"],
                }
                for r in rows
            ],
        }
    )


@app.get("/api/v1/trips/{trip_id}/crossings")
def get_crossings(
    trip_id: str,
    zone_id: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None, description="只统计该 UTC 时刻（含）之后"),
    until: Optional[str] = Query(default=None, description="只统计该 UTC 时刻（不含）之前"),
    revision: Optional[int] = Query(
        default=None,
        ge=1,
        description="指定全局修订号：按各区截至该号的最后版本核算；不填则使用最新配置",
    ),
) -> dict:
    return service.query_crossings(
        trip_id, zone_id=zone_id, since=since, until=until, revision=revision
    )


@app.get("/api/v1/crossing-diffs")
def get_crossing_diffs(
    old_revision: int = Query(
        ..., ge=1, description="较小（较旧）的全局修订号"
    ),
    new_revision: int = Query(
        ..., ge=1, description="较大（较新）的全局修订号，必须大于 old_revision"
    ),
    since: Optional[str] = Query(default=None, description="UTC 半开核算时段起点（含）"),
    until: Optional[str] = Query(default=None, description="UTC 半开核算时段终点（不含）"),
    page_size: int = Query(default=100, ge=1, le=1000, description="每页条数（1..1000）"),
    cursor: Optional[str] = Query(
        default=None, description="上一页返回的 next_cursor；后续页须沿用首请求全部参数"
    ),
) -> dict:
    return service.crossing_diffs(
        old_revision=old_revision,
        new_revision=new_revision,
        since=since,
        until=until,
        page_size=page_size,
        cursor=cursor,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
