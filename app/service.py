"""业务服务：危险区登记、轨迹点批量提交、穿越审计计算。"""
from __future__ import annotations

import base64
import binascii
import json
import math
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shapely.geometry import Polygon

from .db import Database
from .geometry import GeometryError, build_polygon, merge_intervals, segment_inside_params
from .timeutils import TimeError, clip_intervals_halfopen, merge_windows, parse_utc, validate_window

# 分页大小上界，避免单次请求核算过多（车次 × 区域）
MAX_DIFF_PAGE_SIZE = 1000


class ConflictError(Exception):
    """内容冲突（HTTP 409）。"""


class ValidationError(Exception):
    """语义校验失败（HTTP 400）。"""


class NotFoundError(Exception):
    """资源/版本不存在（HTTP 404）。"""


class CursorError(Exception):
    """差异核对分页游标失效或与请求参数不匹配（HTTP 400）。"""


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.min_duration = db.settings.min_duration

    # ------------------------------------------------------------------
    # 危险区登记
    # ------------------------------------------------------------------

    def register_zone(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        zone_id = payload.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise ValidationError("zone_id 必须是非空字符串")
        zone_id = zone_id.strip()

        exterior = payload.get("exterior")
        holes = payload.get("holes") or []
        if not isinstance(holes, list):
            raise ValidationError("holes 必须是环列表")
        try:
            polygon = build_polygon(exterior, holes)
        except GeometryError as exc:
            raise ValidationError(str(exc)) from exc

        raw_windows = payload.get("windows")
        if not isinstance(raw_windows, list) or not raw_windows:
            raise ValidationError("windows 必须是非空左闭右开时段列表 [{start, end}]")
        windows: List[Dict[str, Any]] = []
        for idx, w in enumerate(raw_windows):
            if not isinstance(w, dict):
                raise ValidationError(f"windows[{idx}] 必须是对象")
            try:
                start = parse_utc(w.get("start"), field=f"windows[{idx}].start")
                end = parse_utc(w.get("end"), field=f"windows[{idx}].end")
                validate_window(start, end)
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc
            windows.append(
                {
                    "start_epoch": start.timestamp(),
                    "end_epoch": end.timestamp(),
                    "start_iso": _iso_z(start),
                    "end_iso": _iso_z(end),
                }
            )

        now_iso = _iso_z(datetime.now(timezone.utc))
        with self.db.transaction() as conn:
            # 存在性判断与写入在同一立即事务内完成，并发登记不会读到半次提交
            existing = conn.execute(
                "SELECT * FROM zones WHERE zone_id = ?", (zone_id,)
            ).fetchone()
            created = existing is None
            created_at = existing["created_at"] if existing else now_iso
            revision = self.db.insert_revision(conn, zone_id, now_iso)
            exterior_coords = [list(c) for c in polygon.exterior.coords]
            hole_coords = [[list(c) for c in ring.coords] for ring in polygon.interiors]
            # 不可变历史版本（该修订号下的区域配置快照）
            self.db.insert_zone_version(
                conn, revision, zone_id, exterior_coords, hole_coords, windows, created_at
            )
            # 最新配置快照：未指定修订号的现有查询路径继续使用
            if existing:
                conn.execute("DELETE FROM zone_windows WHERE zone_id = ?", (zone_id,))
                conn.execute("DELETE FROM zones WHERE zone_id = ?", (zone_id,))
            self.db.insert_zone(
                conn, zone_id, exterior_coords, hole_coords, windows, created_at
            )

        return {
            "zone_id": zone_id,
            "status": "created" if created else "updated",
            "revision": revision,
            "num_holes": len(polygon.interiors),
            "num_windows": len(windows),
        }

    def get_zone(self, zone_id: str) -> Optional[Dict[str, Any]]:
        # 区域行与生效窗口在同一快照事务内读取，避免读到覆盖更新的中间态
        with self.db.read_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM zones WHERE zone_id = ?", (zone_id,)
            ).fetchone()
            if row is None:
                return None
            rev_row = conn.execute(
                "SELECT MAX(revision) AS r FROM zone_versions WHERE zone_id = ?",
                (zone_id,),
            ).fetchone()
            wrows = conn.execute(
                "SELECT start_iso, end_iso FROM zone_windows WHERE zone_id = ? ORDER BY start_epoch",
                (zone_id,),
            ).fetchall()
        return {
            "zone_id": row["zone_id"],
            "revision": None if rev_row["r"] is None else int(rev_row["r"]),
            "exterior": json.loads(row["exterior"]),
            "holes": json.loads(row["holes"]),
            "windows": [
                {"start": w["start_iso"], "end": w["end_iso"]} for w in wrows
            ],
            "created_at": row["created_at"],
        }

    def list_zones(self) -> List[Dict[str, Any]]:
        # 单次快照读：要么看到某次登记之前、要么之后，绝看不到半次提交
        with self.db.read_transaction() as conn:
            rows = list(conn.execute("SELECT * FROM zones ORDER BY zone_id"))
            result = []
            for row in rows:
                rev_row = conn.execute(
                    "SELECT MAX(revision) AS r FROM zone_versions WHERE zone_id = ?",
                    (row["zone_id"],),
                ).fetchone()
                wrows = conn.execute(
                    "SELECT start_iso, end_iso FROM zone_windows "
                    "WHERE zone_id = ? ORDER BY start_epoch",
                    (row["zone_id"],),
                ).fetchall()
                result.append(
                    {
                        "zone_id": row["zone_id"],
                        "revision": None if rev_row["r"] is None else int(rev_row["r"]),
                        "exterior": json.loads(row["exterior"]),
                        "holes": json.loads(row["holes"]),
                        "windows": [
                            {"start": w["start_iso"], "end": w["end_iso"]} for w in wrows
                        ],
                        "created_at": row["created_at"],
                    }
                )
        return result

    def get_zone_history(self, zone_id: str) -> Optional[List[Dict[str, Any]]]:
        """返回某区域全部不可变历史版本（按修订号升序）。"""
        with self.db.read_transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM zone_versions WHERE zone_id = ? LIMIT 1", (zone_id,)
            ).fetchone()
            if exists is None:
                return None
            rows = list(
                conn.execute(
                    "SELECT v.revision AS revision, v.zone_id AS zone_id, "
                    "v.exterior AS exterior, v.holes AS holes, v.created_at AS created_at, "
                    "r.created_at AS registered_at "
                    "FROM zone_versions v JOIN revisions r ON r.revision = v.revision "
                    "WHERE v.zone_id = ? ORDER BY v.revision",
                    (zone_id,),
                )
            )
            window_rows = {
                int(rev): conn.execute(
                    "SELECT start_iso, end_iso FROM zone_version_windows "
                    "WHERE revision = ? ORDER BY start_epoch",
                    (rev,),
                ).fetchall()
                for rev in [row["revision"] for row in rows]
            }

        result: List[Dict[str, Any]] = []
        for row in rows:
            revision = int(row["revision"])
            result.append(
                {
                    "zone_id": row["zone_id"],
                    "revision": revision,
                    "registered_at": row["registered_at"],
                    "created_at": row["created_at"],
                    "exterior": json.loads(row["exterior"]),
                    "holes": json.loads(row["holes"]),
                    "windows": [
                        {"start": w["start_iso"], "end": w["end_iso"]}
                        for w in window_rows[revision]
                    ],
                }
            )
        return result

    # ------------------------------------------------------------------
    # 轨迹点批量提交（幂等 / 冲突）
    # ------------------------------------------------------------------

    def submit_points(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        points = payload.get("points")
        if not isinstance(points, list) or not points:
            raise ValidationError("points 必须是非空轨迹点列表")

        # ---- 批内解析与校验 ----
        parsed: List[Tuple[str, float, float, float, str]] = []
        # point_id -> (ts,x,y,ts_iso)；ts -> point_id，用于批内一致性检查
        seen_ids: Dict[str, Tuple[float, float, float, str]] = {}
        seen_ts: Dict[float, str] = {}

        for idx, item in enumerate(points):
            if not isinstance(item, dict):
                raise ValidationError(f"points[{idx}] 必须是对象")
            point_id = item.get("point_id")
            if not isinstance(point_id, str) or not point_id.strip():
                raise ValidationError(f"points[{idx}].point_id 必须是非空字符串")
            point_id = point_id.strip()

            try:
                dt = parse_utc(item.get("ts"), field=f"points[{idx}].ts")
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc

            x_raw, y_raw = item.get("x"), item.get("y")
            if isinstance(x_raw, bool) or isinstance(y_raw, bool):
                raise ValidationError(f"points[{idx}] 坐标必须是数值")
            try:
                x, y = float(x_raw), float(y_raw)
            except (TypeError, ValueError):
                raise ValidationError(
                    f"points[{idx}] 坐标必须是数值: x={x_raw!r}, y={y_raw!r}"
                )

            if not (math.isfinite(x) and math.isfinite(y)):
                raise ValidationError(
                    f"points[{idx}] 坐标必须有限，禁止 NaN/Infinity: x={x_raw!r}, y={y_raw!r}"
                )

            ts_epoch = dt.timestamp()
            sig = (ts_epoch, x, y)

            # 同 point_id 批内重复：内容一致幂等，不一致冲突
            if point_id in seen_ids:
                if seen_ids[point_id][:3] != sig:
                    raise ConflictError(
                        f"批内冲突: point_id={point_id!r} 出现了不同内容（时刻或坐标不一致）"
                    )
                continue

            # 同车次同刻不同点
            if ts_epoch in seen_ts:
                raise ConflictError(
                    f"批内冲突: 时刻 {_iso_z(dt)} 同时关联了 point_id="
                    f"{seen_ts[ts_epoch]!r} 与 {point_id!r}"
                )

            seen_ids[point_id] = (ts_epoch, x, y, _iso_z(dt))
            seen_ts[ts_epoch] = point_id
            parsed.append((point_id, ts_epoch, x, y, _iso_z(dt)))

        # ---- 与已存数据比对（只读阶段，先全部收集错误） ----
        stored_ids: Dict[str, Tuple[float, float, float]] = {}
        for point_id, ts_epoch, x, y, _ in parsed:
            row = self.db.get_point(trip_id, point_id)
            if row is not None:
                stored = (row["ts_epoch"], row["x"], row["y"])
                incoming = (ts_epoch, x, y)
                if stored != incoming:
                    raise ConflictError(
                        f"point_id={point_id!r} 已存在且内容不同："
                        f"已存 (ts_epoch={stored[0]}, x={stored[1]}, y={stored[2]})，"
                        f"新传 (ts_epoch={ts_epoch}, x={x}, y={y})"
                    )
                stored_ids[point_id] = incoming
                continue
            # 新点：检查是否与已有同刻点撞车
            other = self.db.find_point_at_ts(trip_id, ts_epoch)
            if other is not None:
                raise ConflictError(
                    f"时刻冲突: point_id={point_id!r} 的时刻与已存 point_id="
                    f"{other['point_id']!r} 相同（同车次同刻只能有一个点）"
                )

        # ---- 单事务写入新点 ----
        new_rows = [
            (trip_id, point_id, ts_epoch, x, y)
            for point_id, ts_epoch, x, y, _ in parsed
            if point_id not in stored_ids
        ]
        with self.db.transaction() as conn:
            self.db.insert_points(conn, new_rows)

        return {
            "trip_id": trip_id,
            "received": len(points),
            "accepted_new": len(new_rows),
            "idempotent_retransmit": len(points) - len(new_rows),
        }

    # ------------------------------------------------------------------
    # 穿越审计
    # ------------------------------------------------------------------

    def query_crossings(
        self,
        trip_id: str,
        zone_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        try:
            lo = parse_utc(since, field="since").timestamp() if since else None
            hi = parse_utc(until, field="until").timestamp() if until else None
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo is not None and hi is not None and lo >= hi:
            raise ValidationError("since 必须早于 until（查询范围为半开 [since, until)）")

        if revision is not None:
            if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
                raise ValidationError("revision 必须是正整数")

        # 一次只读快照事务内取齐轨迹与区域配置，保证读不到并发写入的半次提交
        with self.db.read_transaction() as conn:
            if revision is not None:
                current = self.db.current_revision(conn)
                if current == 0:
                    raise NotFoundError("尚无任何区域登记，修订号不存在: 0")
                if revision > current:
                    raise NotFoundError(
                        f"修订号超出当前范围: revision={revision}，当前最大修订号={current}"
                    )

            points = self.db.list_trip_points(trip_id, conn)
            if len(points) < 2:
                response = {
                    "trip_id": trip_id,
                    "point_count": len(points),
                    "zones": [],
                }
                if revision is not None:
                    response["revision"] = revision
                return response

            if revision is None:
                # 现有最新配置路径（请求字段与响应语义不变）
                zone_rows = self.db.list_zone_rows(conn)
                if zone_id is not None:
                    zone_rows = [r for r in zone_rows if r["zone_id"] == zone_id]
                    if not zone_rows:
                        raise ValidationError(f"危险区不存在: {zone_id}")
                snapshots = [
                    {
                        "zone_id": zrow["zone_id"],
                        "exterior": json.loads(zrow["exterior"]),
                        "holes": json.loads(zrow["holes"]),
                        "windows": self.db.get_windows(zrow["zone_id"], conn),
                    }
                    for zrow in zone_rows
                ]
            else:
                # 指定修订号：取各区截至该号的最后版本；该版本下未登记的区域不参与
                snapshots = self.db.zone_snapshots_at_revision(conn, revision, zone_id)

        # points 已在 SQL 层按时刻排序，乱序上传在此完成重建

        zone_results: List[Dict[str, Any]] = []
        for snap in snapshots:
            result = self._compute_crossing(points, snap, lo, hi)
            result["zone_id"] = snap["zone_id"]
            zone_results.append(result)

        response = {
            "trip_id": trip_id,
            "point_count": len(points),
            "zones": zone_results,
        }
        if revision is not None:
            response["revision"] = revision
        return response

    @staticmethod
    def _empty_crossing() -> Dict[str, Any]:
        return {"crossed": False, "total_duration_sec": 0, "segments": []}

    def _compute_crossing(
        self,
        points: List[Any],
        snapshot: Dict[str, Any],
        lo: Optional[float],
        hi: Optional[float],
    ) -> Dict[str, Any]:
        """按一份区域快照（几何 + 生效时段）核算单区穿越结论。

        供单车次穿越查询与跨修订号差异核对共用，保证两者契约完全一致。
        """
        polygon = Polygon(snapshot["exterior"], snapshot["holes"])
        windows = merge_windows(snapshot["windows"])
        return self._cross_one_zone(points, polygon, windows, lo, hi)

    def _cross_one_zone(
        self,
        points: List[Any],
        polygon: Polygon,
        windows: List[Tuple[float, float]],
        lo: Optional[float],
        hi: Optional[float],
    ) -> Dict[str, Any]:
        # 覆盖参数（时间）区间：遍历相邻点，乱序已在 SQL 层按时刻排序重建
        cover: List[Tuple[float, float]] = []
        for a, b in zip(points, points[1:]):
            t0, t1 = a["ts_epoch"], b["ts_epoch"]
            if t1 <= t0:
                # 同刻点不应出现（唯一约束），稳妥跳过
                continue
            params = segment_inside_params(
                polygon, (a["x"], a["y"]), (b["x"], b["y"])
            )
            for pa, pb in params:
                # 参数 0/1 直接取端点时刻，保证相邻段在公共采样点精确相接
                s = t0 if pa == 0.0 else t0 + pa * (t1 - t0)
                e = t1 if pb == 1.0 else t0 + pb * (t1 - t0)
                if e - s > self.min_duration:
                    cover.append((s, e))

        # 时间域合并留 1e-6 秒容差，吸收不同时刻解析路径的浮点误差
        cover = merge_intervals(cover, eps=1e-6)
        intervals = clip_intervals_halfopen(cover, windows)
        if lo is not None or hi is not None:
            bounds = [(-float("inf") if lo is None else lo, float("inf") if hi is None else hi)]
            intervals = clip_intervals_halfopen(intervals, bounds)
        intervals = merge_intervals(intervals, eps=1e-6)
        intervals = [(s, e) for s, e in intervals if e - s > self.min_duration]

        segments = [
            {
                "start": _iso_z(datetime.fromtimestamp(s, timezone.utc)),
                "end": _iso_z(datetime.fromtimestamp(e, timezone.utc)),
                "duration_sec": round(e - s, 6),
            }
            for s, e in intervals
        ]
        total = round(sum(e - s for s, e in intervals), 6)

        return {
            "crossed": bool(intervals),
            "total_duration_sec": total,
            "segments": segments,
        }

    # ------------------------------------------------------------------
    # 两次全局修订号之间的穿越结论差异核对
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_cursor(snapshot_id: str, last_trip_id: str, last_zone_id: str) -> str:
        raw = json.dumps(
            {"s": snapshot_id, "t": last_trip_id, "z": last_zone_id},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> Tuple[str, str, str]:
        if not isinstance(cursor, str) or not cursor.strip():
            raise CursorError("游标失效：游标必须是非空字符串")
        cursor = cursor.strip()
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            data = json.loads(raw.decode("utf-8"))
            snapshot_id = data["s"]
            last_trip_id = data["t"]
            last_zone_id = data["z"]
        except (binascii.Error, ValueError, KeyError, UnicodeDecodeError) as exc:
            raise CursorError("游标失效：无法解析（可能被截断或篡改）") from exc
        if not all(isinstance(v, str) for v in (snapshot_id, last_trip_id, last_zone_id)):
            raise CursorError("游标失效：游标内容格式不正确")
        return snapshot_id, last_trip_id, last_zone_id

    def query_crossing_diffs(
        self,
        old_revision: int,
        new_revision: int,
        since: Optional[str] = None,
        until: Optional[str] = None,
        page_size: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """核对两个全局修订号之间已存车次穿越结论的变化。

        首次请求（无 cursor）时冻结当时已存的全部轨迹点作为两版共同输入；
        后续页凭游标复用同一快照，新上传轨迹或新登记区域不改变结果。
        区域按各自截至 old/new 修订号的最后版本核算，旧版尚未登记的区域按空结果比较。
        """
        # ---- 参数解析与校验 ----
        for name, value in (("old_revision", old_revision), ("new_revision", new_revision)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValidationError(f"{name} 必须是正整数")
        if old_revision >= new_revision:
            raise ValidationError(
                f"修订号顺序错误：old_revision（{old_revision}）必须小于 "
                f"new_revision（{new_revision}）"
            )
        try:
            lo = parse_utc(since, field="since").timestamp() if since else None
            hi = parse_utc(until, field="until").timestamp() if until else None
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo is not None and hi is not None and lo >= hi:
            raise ValidationError("since 必须早于 until（核算时段为半开 [since, until)）")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not (
            1 <= page_size <= MAX_DIFF_PAGE_SIZE
        ):
            raise ValidationError(f"page_size 必须是 1..{MAX_DIFF_PAGE_SIZE} 之间的整数")
        since_iso = _iso_z(datetime.fromtimestamp(lo, timezone.utc)) if lo is not None else None
        until_iso = _iso_z(datetime.fromtimestamp(hi, timezone.utc)) if hi is not None else None

        if cursor is not None:
            snap_id, last_trip, last_zone = self._decode_cursor(cursor)
            with self.db.read_transaction() as conn:
                snap = self.db.get_crossing_diff_snapshot(conn, snap_id)
                if snap is None:
                    raise CursorError("游标失效：快照不存在或已过期，请不带游标重新发起首次请求")
                # 后续页参数必须与首次请求逐一匹配
                if not (
                    snap["old_revision"] == old_revision
                    and snap["new_revision"] == new_revision
                    and snap["page_size"] == page_size
                    and _epoch_equal(snap["since_epoch"], lo)
                    and _epoch_equal(snap["until_epoch"], hi)
                ):
                    raise CursorError(
                        "游标与请求参数不匹配：后续页必须沿用首次请求的 old_revision/"
                        "new_revision/since/until/page_size"
                    )
                # 修订号存在性已在首次请求时校验；区域 as-of 视图由不可变历史版本冻结，
                # 快照存在即可安全翻页，无需再比对当前最大修订号
                items, next_cursor = self._scan_diff_page(
                    conn,
                    snap_id=snap_id,
                    old_revision=old_revision,
                    new_revision=new_revision,
                    lo=lo,
                    hi=hi,
                    page_size=page_size,
                    start_after=(last_trip, last_zone),
                )
            return {
                "old_revision": old_revision,
                "new_revision": new_revision,
                "since": snap["since_iso"],
                "until": snap["until_iso"],
                "page_size": page_size,
                "snapshot_id": snap_id,
                "items": items,
                "next_cursor": next_cursor,
            }

        # ---- 首次请求：校验修订号存在性并冻结轨迹快照 ----
        created_iso = _iso_z(datetime.now(timezone.utc))
        with self.db.transaction() as conn:
            current = self.db.current_revision(conn)
            if current == 0:
                raise NotFoundError("尚无任何区域登记，修订号不存在: 0")
            if new_revision > current:
                raise NotFoundError(
                    f"修订号超出当前范围: new_revision={new_revision}，当前最大修订号={current}"
                )
            # 24 字节随机 ID；主键约束兜底（碰撞时整个事务回滚，调用方重试即可）
            snap_id = secrets.token_urlsafe(18)
            self.db.insert_crossing_diff_snapshot(
                conn,
                snapshot_id=snap_id,
                old_revision=old_revision,
                new_revision=new_revision,
                since_epoch=lo,
                until_epoch=hi,
                since_iso=since_iso,
                until_iso=until_iso,
                page_size=page_size,
                created_iso=created_iso,
            )
            items, next_cursor = self._scan_diff_page(
                conn,
                snap_id=snap_id,
                old_revision=old_revision,
                new_revision=new_revision,
                lo=lo,
                hi=hi,
                page_size=page_size,
                start_after=None,
            )

        return {
            "old_revision": old_revision,
            "new_revision": new_revision,
            "since": since_iso,
            "until": until_iso,
            "page_size": page_size,
            "snapshot_id": snap_id,
            "items": items,
            "next_cursor": next_cursor,
        }

    def _iter_diff_items(
        self,
        conn: Any,
        snap_id: str,
        old_revision: int,
        new_revision: int,
        old_snaps: Dict[str, Dict[str, Any]],
        new_snaps: Dict[str, Dict[str, Any]],
        zone_ids: List[str],
        lo: Optional[float],
        hi: Optional[float],
        start_after: Optional[Tuple[str, str]],
    ):
        """按 (trip_id, zone_id) 稳定顺序逐个产出发生变化的条目。"""
        for trip_id in self.db.list_snapshot_trip_ids(conn, snap_id):
            points = self.db.list_snapshot_trip_points(conn, snap_id, trip_id)
            if len(points) < 2:
                # 与单车次穿越查询契约一致：不足两点无穿越可言
                continue

            zone_cache: Dict[Tuple[str, int], Optional[Dict[str, Any]]] = {}

            def crossing(zone_id: str, revision: int) -> Dict[str, Any]:
                # 同一 (trip, zone, 版本) 的核算只做一次
                key = (zone_id, revision)
                if key not in zone_cache:
                    snaps = old_snaps if revision == old_revision else new_snaps
                    snap = snaps.get(zone_id)
                    zone_cache[key] = (
                        None if snap is None else self._compute_crossing(points, snap, lo, hi)
                    )
                cached = zone_cache[key]
                return self._empty_crossing() if cached is None else cached

            for zone_id in zone_ids:
                if start_after is not None and (trip_id, zone_id) <= start_after:
                    continue
                old_result = crossing(zone_id, old_revision)
                new_result = crossing(zone_id, new_revision)
                if not self._crossing_changed(old_result, new_result):
                    continue
                yield self._build_diff_item(trip_id, zone_id, old_snaps, new_snaps, old_result, new_result)

    @staticmethod
    def _build_diff_item(
        trip_id: str,
        zone_id: str,
        old_snaps: Dict[str, Dict[str, Any]],
        new_snaps: Dict[str, Dict[str, Any]],
        old_result: Dict[str, Any],
        new_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "trip_id": trip_id,
            "zone_id": zone_id,
            "old": {
                "zone_revision": (
                    old_snaps[zone_id]["revision"] if zone_id in old_snaps else None
                ),
                **old_result,
            },
            "new": {
                "zone_revision": (
                    new_snaps[zone_id]["revision"] if zone_id in new_snaps else None
                ),
                **new_result,
            },
            "total_duration_delta_sec": round(
                new_result["total_duration_sec"] - old_result["total_duration_sec"], 6
            ),
        }

    def _scan_diff_page(
        self,
        conn: Any,
        snap_id: str,
        old_revision: int,
        new_revision: int,
        lo: Optional[float],
        hi: Optional[float],
        page_size: int,
        start_after: Optional[Tuple[str, str]],
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """按 (trip_id, zone_id) 稳定顺序扫描快照，收集本页发生变化的条目。

        返回 (items, next_cursor)：本页之后仍有变化条目时才给出游标，
        末页（含恰好收满的末页）游标为 None。
        """
        # 两版区域配置：截至相应修订号的最后版本（区域在同一快照事务内取齐）
        old_snaps = {s["zone_id"]: s for s in self.db.zone_snapshots_at_revision(conn, old_revision)}
        new_snaps = {s["zone_id"]: s for s in self.db.zone_snapshots_at_revision(conn, new_revision)}
        zone_ids = sorted(set(old_snaps) | set(new_snaps))

        iterator = self._iter_diff_items(
            conn, snap_id, old_revision, new_revision,
            old_snaps, new_snaps, zone_ids, lo, hi, start_after
        )
        items = [next(iterator, None) for _ in range(page_size + 1)]
        has_next = items[-1] is not None
        items = [item for item in items if item is not None]
        if has_next:
            # 多取的一条不属于本页；游标指向本页最后一条
            items = items[:page_size]
            last = items[-1]
            next_cursor = self._encode_cursor(snap_id, last["trip_id"], last["zone_id"])
        else:
            next_cursor = None
        return items, next_cursor

    @staticmethod
    def _crossing_changed(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
        """穿越区间发生变化即视为结论变化（累计秒数由区间决定，一并返回差值）。"""
        return old["segments"] != new["segments"]


def _epoch_equal(a: Optional[float], b: Optional[float]) -> bool:
    """两个可选 epoch 是否一致（None 视为同一"未提供时间过滤"）。"""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1e-6
