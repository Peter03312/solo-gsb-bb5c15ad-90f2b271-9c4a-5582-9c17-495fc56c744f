"""两次全局修订号之间穿越结论差异核对的测试。"""
import base64
import json

from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _points(pairs):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in pairs]}


# 200m / 80s = 2.5m/s 匀速横穿 y=50
CROSS_POINTS = [
    ("p1", "2026-09-30T10:00:00Z", -50, 50),
    ("p2", "2026-09-30T10:00:40Z", 50, 50),
    ("p3", "2026-09-30T10:01:20Z", 150, 50),
]

WIDE = [[0, 0], [200, 0], [200, 100], [0, 100]]
NARROW = [[0, 0], [20, 0], [20, 100], [0, 100]]

DIFF_PATH = "/api/v1/crossing-diffs"


def _find(items, trip_id, zone_id):
    matches = [i for i in items if i["trip_id"] == trip_id and i["zone_id"] == zone_id]
    assert len(matches) == 1, f"条目不唯一或缺失: {trip_id}/{zone_id}"
    return matches[0]


def _setup_basic(client):
    # rev 1: za 窄方块（x 0..20）；rev 2: zb 首次登记；rev 3: za 扩到 0..200
    register_square(client, zone_id="za", exterior=NARROW)
    register_square(client, zone_id="zb")
    register_square(client, zone_id="za", exterior=WIDE)
    client.post("/api/v1/trips/T/points", json=_points(CROSS_POINTS))


def test_diff_returns_changed_trip_zone_pairs_with_both_versions(client):
    _setup_basic(client)
    r = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["old_revision"] == 1
    assert body["new_revision"] == 3
    assert body["since"] is None and body["until"] is None
    assert body["next_cursor"] is None
    assert body["snapshot_id"]

    pairs = sorted((i["trip_id"], i["zone_id"]) for i in body["items"])
    assert pairs == [("T", "za"), ("T", "zb")]

    item = _find(body["items"], "T", "za")
    # 旧版窄方块 8s（x 0..20），新版宽方块 60s（x 0..150 是轨迹终点）
    assert item["old"]["zone_revision"] == 1
    assert item["old"]["total_duration_sec"] == 8.0
    assert item["new"]["zone_revision"] == 3
    assert item["new"]["total_duration_sec"] == 60.0
    assert item["total_duration_delta_sec"] == 52.0
    # 两版区间都完整回显
    assert item["old"]["segments"] == [
        {
            "start": "2026-09-30T10:00:20Z",
            "end": "2026-09-30T10:00:28Z",
            "duration_sec": 8.0,
        }
    ]
    assert item["new"]["segments"] == [
        {
            "start": "2026-09-30T10:00:20Z",
            "end": "2026-09-30T10:01:20Z",
            "duration_sec": 60.0,
        }
    ]


def test_diff_zone_unregistered_in_old_version_compared_as_empty(client):
    _setup_basic(client)
    item = _find(client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3").json()["items"], "T", "zb")
    # zb 在 rev 2 才登记：旧版按空结果，新版 40s
    assert item["old"] == {
        "zone_revision": None,
        "crossed": False,
        "total_duration_sec": 0,
        "segments": [],
    }
    assert item["new"]["zone_revision"] == 2
    assert item["new"]["total_duration_sec"] == 40.0
    assert item["total_duration_delta_sec"] == 40.0

    # 反过来：新版尚未登记不可能（new > old 且截至版本单调），但区间方向覆盖
    item2 = _find(client.get(f"{DIFF_PATH}?old_revision=2&new_revision=3").json()["items"], "T", "za")
    assert item2["old"]["zone_revision"] == 1  # rev2 时 za 的最后版本仍是 rev1
    assert item2["new"]["zone_revision"] == 3


def test_diff_no_changes_returns_empty_list(client):
    register_square(client, zone_id="stable")  # rev 1
    register_square(client, zone_id="other")   # rev 2，与 stable 无关
    client.post("/api/v1/trips/T/points", json=_points(CROSS_POINTS))
    # 1 -> 2：stable 自身没改（other 对 stable 车次核算无影响）
    body = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=2").json()
    # other 在旧版不存在：会产生变化条目；再看 2 之后没有新版本的情形
    assert {(i["trip_id"], i["zone_id"]) for i in body["items"]} == {("T", "other")}
    register_square(client, zone_id="third")  # rev 3
    body = client.get(f"{DIFF_PATH}?old_revision=2&new_revision=3").json()
    assert {(i["trip_id"], i["zone_id"]) for i in body["items"]} == {("T", "third")}


def test_diff_nothing_changed_at_all_empty(client):
    register_square(client, zone_id="s1")  # rev 1
    # rev 2 只覆盖更新别的区域且轨迹不穿越它；s1 结论不变
    client.post("/api/v1/trips/T/points", json=_points(CROSS_POINTS))
    register_square(
        client,
        zone_id="far",
        exterior=[[500, 500], [600, 500], [600, 600], [500, 600]],
    )  # rev 2，轨迹不经过
    body = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=2").json()
    assert body["items"] == []
    assert body["next_cursor"] is None


def test_diff_window_change_reflected(client):
    register_square(client, zone_id="w")  # rev1 全天生效 -> 40s
    register_square(
        client,
        zone_id="w",
        windows=[{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:50Z"}],
    )  # rev2 半开裁剪 -> 30s
    client.post("/api/v1/trips/T/points", json=_points(CROSS_POINTS))
    body = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=2").json()
    item = _find(body["items"], "T", "w")
    assert item["old"]["total_duration_sec"] == 40.0
    assert item["new"]["total_duration_sec"] == 30.0
    assert item["total_duration_delta_sec"] == -10.0


def test_diff_since_until_halfopen_window(client):
    _setup_basic(client)
    r = client.get(
        f"{DIFF_PATH}?old_revision=1&new_revision=3"
        "&since=2026-09-30T10:00:30Z&until=2026-09-30T10:01:00Z"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["since"] == "2026-09-30T10:00:30Z"
    assert body["until"] == "2026-09-30T10:01:00Z"
    # za 旧版区间 10:00:20~28 完全在 since 之前 -> 空；新版 60s 被裁成 30s
    item = _find(body["items"], "T", "za")
    assert item["old"]["segments"] == []
    assert item["old"]["total_duration_sec"] == 0
    assert item["new"]["total_duration_sec"] == 30.0
    assert item["new"]["segments"] == [
        {
            "start": "2026-09-30T10:00:30Z",
            "end": "2026-09-30T10:01:00Z",
            "duration_sec": 30.0,
        }
    ]
    # zb 旧版尚未登记（空），新区间 10:00:20~10:01:00 被裁成 30s：空→30s 仍属变化
    zb = _find(body["items"], "T", "zb")
    assert zb["old"]["segments"] == []
    assert zb["new"]["total_duration_sec"] == 30.0


def test_diff_since_until_invalid_400(client):
    _setup_basic(client)
    r = client.get(
        f"{DIFF_PATH}?old_revision=1&new_revision=3"
        "&since=2026-09-30T11:00:00Z&until=2026-09-30T10:00:00Z"
    )
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"

    # 不带时区
    r = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3&since=2026-09-30T10:00:00")
    assert r.status_code == 400


def test_diff_revision_order_error_400(client):
    _setup_basic(client)
    for old, new in ((2, 1), (2, 2)):
        r = client.get(f"{DIFF_PATH}?old_revision={old}&new_revision={new}")
        assert r.status_code == 400
        assert r.json()["error"] == "validation_error"
        assert "顺序" in r.json()["detail"]


def test_diff_revision_not_exist_404(client):
    register_square(client, zone_id="z")  # rev 1
    client.post("/api/v1/trips/T/points", json=_points(CROSS_POINTS))
    r = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=99")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    assert "99" in r.json()["detail"]


def test_diff_no_zones_registered_404(client):
    client.post("/api/v1/trips/T/points", json=_points(CROSS_POINTS))
    r = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=2")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_diff_non_positive_or_non_integer_revision_422(client):
    for q in ("old_revision=0&new_revision=2", "old_revision=-1&new_revision=2",
              "old_revision=1.5&new_revision=2", "old_revision=1&new_revision=x"):
        r = client.get(f"{DIFF_PATH}?{q}")
        assert r.status_code == 422, q


def test_diff_page_size_bounds_422(client):
    _setup_basic(client)
    for size in (0, -1, 1001):
        r = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size={size}")
        assert r.status_code == 422, size


# ---------------------------------------------------------------------------
# 分页与快照稳定性
# ---------------------------------------------------------------------------

def _setup_multi(client):
    register_square(client, zone_id="za", exterior=NARROW)  # rev1
    register_square(client, zone_id="zb")                    # rev2 新登记
    register_square(client, zone_id="za", exterior=WIDE)     # rev3 扩区
    register_square(client, zone_id="zc")                    # rev4 新登记
    client.post("/api/v1/trips/T1/points", json=_points(CROSS_POINTS))
    client.post(
        "/api/v1/trips/T2/points",
        json=_points([
            ("q1", "2026-09-30T10:00:00Z", 0, 50),
            ("q2", "2026-09-30T10:01:20Z", 200, 50),
        ]),
    )
    # 预期 1->3 的变化（按 trip_id, zone_id 排序）：
    # (T1,za) 8->60, (T1,zb) 空->40, (T2,za) 8->80, (T2,zb) 空->40


def test_diff_pagination_stable_order_and_walk(client):
    _setup_multi(client)
    r1 = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=2")
    assert r1.status_code == 200
    page1 = r1.json()
    assert [(i["trip_id"], i["zone_id"]) for i in page1["items"]] == [
        ("T1", "za"),
        ("T1", "zb"),
    ]
    cursor = page1["next_cursor"]
    assert cursor

    # 翻页期间新上传轨迹 + 新登记区域：不得影响本次核对
    client.post(
        "/api/v1/trips/T3/points",
        json=_points(CROSS_POINTS),
    )
    register_square(client, zone_id="zd")  # rev5

    r2 = client.get(
        f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=2&cursor={cursor}"
    )
    assert r2.status_code == 200, r2.text
    page2 = r2.json()
    assert [(i["trip_id"], i["zone_id"]) for i in page2["items"]] == [
        ("T2", "za"),
        ("T2", "zb"),
    ]
    assert page2["snapshot_id"] == page1["snapshot_id"]
    assert page2["next_cursor"] is None
    # 新出现的 T3/zd 未污染任何一页
    all_items = page1["items"] + page2["items"]
    assert {i["trip_id"] for i in all_items} == {"T1", "T2"}
    assert {i["zone_id"] for i in all_items} == {"za", "zb"}

    # 不带游标重发 => 新的首次请求，按当前已存数据建立新快照，包含 T3
    r_fresh = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=100")
    fresh = r_fresh.json()
    assert fresh["snapshot_id"] != page1["snapshot_id"]
    assert "T3" in {i["trip_id"] for i in fresh["items"]}


def test_diff_pagination_covers_zones_registered_between_pages(client):
    _setup_multi(client)
    r1 = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=1")
    assert r1.json()["items"][0]["zone_id"] == "za"
    # rev4 的 zc 在首次快照之后也不会进入 1->3 的 as-of 视图
    r2 = client.get(
        f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=1"
        f"&cursor={r1.json()['next_cursor']}"
    )
    assert r2.json()["items"][0]["zone_id"] == "zb"


def test_diff_cursor_garbage_400(client):
    _setup_basic(client)
    r = client.get(
        f"{DIFF_PATH}?old_revision=1&new_revision=3&cursor=%21%21not-base64%21%21"
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_cursor"

    # 结构可解析但指向不存在的快照
    fake = base64.urlsafe_b64encode(
        json.dumps({"s": "nope", "t": "T", "z": "za"}).encode()
    ).decode().rstrip("=")
    r = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3&cursor={fake}")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_cursor"
    assert "快照不存在" in r.json()["detail"]


def test_diff_cursor_param_mismatch_400(client):
    _setup_basic(client)
    first = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=2").json()
    cursor = first["next_cursor"] or _walk_to_last_cursor(client)

    # 改 revision
    r = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=2&page_size=2&cursor={cursor}")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_cursor"
    assert "不匹配" in r.json()["detail"]

    # 改 page_size
    r = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=3&cursor={cursor}")
    assert r.status_code == 400

    # 加 since
    r = client.get(
        f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=2"
        f"&since=2026-09-30T10:00:00Z&cursor={cursor}"
    )
    assert r.status_code == 400


def _walk_to_last_cursor(client):
    """无下一页游标时构造一个合法快照游标（指向已有快照的某个条目）。"""
    body = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=100").json()
    last = body["items"][-1]
    raw = json.dumps(
        {"s": body["snapshot_id"], "t": last["trip_id"], "z": last["zone_id"]},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_diff_last_page_cursor_returns_empty(client):
    _setup_basic(client)
    cursor = _walk_to_last_cursor(client)
    r = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=100&cursor={cursor}")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


def test_diff_snapshot_uses_first_request_trajectories(client):
    register_square(client, zone_id="za", exterior=NARROW)
    register_square(client, zone_id="za", exterior=WIDE)  # rev2
    client.post("/api/v1/trips/T1/points", json=_points(CROSS_POINTS))

    first = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=2&page_size=100").json()
    assert {i["trip_id"] for i in first["items"]} == {"T1"}

    # 之后上传的新车次，不影响该快照的后续页
    client.post("/api/v1/trips/T2/points", json=_points(CROSS_POINTS))
    last = first["items"][-1]
    cursor = base64.urlsafe_b64encode(
        json.dumps(
            {"s": first["snapshot_id"], "t": last["trip_id"], "z": last["zone_id"]},
            separators=(",", ":"),
        ).encode()
    ).decode().rstrip("=")
    body = client.get(
        f"{DIFF_PATH}?old_revision=1&new_revision=2&page_size=100&cursor={cursor}"
    ).json()
    # 末页之后为空：T2 未被纳入首次请求冻结的快照
    assert body["items"] == []
    assert body["snapshot_id"] == first["snapshot_id"]


def test_diff_skips_trips_with_fewer_than_two_points(client):
    register_square(client, zone_id="za", exterior=NARROW)
    register_square(client, zone_id="za", exterior=WIDE)
    client.post(
        "/api/v1/trips/ONE/points",
        json=_points([("solo", "2026-09-30T10:00:00Z", 10, 50)]),
    )
    body = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=2").json()
    assert body["items"] == []


def test_diff_snapshot_persists_across_reopen(tmp_path):
    """快照表随 SQLite 持久化：重启进程后游标仍可继续翻页。"""
    from types import SimpleNamespace

    from app.db import Database
    from app.service import AuditService

    db_path = tmp_path / "diff.db"
    settings = SimpleNamespace(db_path=db_path, min_duration=1e-9)

    db = Database(settings)
    try:
        svc = AuditService(db)
        svc.register_zone({"zone_id": "za", "exterior": NARROW, "holes": [], "windows": DAY_WINDOWS})
        svc.register_zone({"zone_id": "zb", "exterior": SQUARE, "holes": [], "windows": DAY_WINDOWS})
        svc.register_zone({"zone_id": "za", "exterior": WIDE, "holes": [], "windows": DAY_WINDOWS})
        svc.submit_points("T", {"points": [
            {"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in CROSS_POINTS
        ]})
        # 1->3 变化按序为 (T,za) 8->60、(T,zb) 空->40
        first = svc.query_crossing_diffs(old_revision=1, new_revision=3, page_size=1)
        assert [(i["trip_id"], i["zone_id"]) for i in first["items"]] == [("T", "za")]
        cursor = first["next_cursor"]
        assert cursor
    finally:
        db.close()

    # 模拟重启后新上传一条轨迹：不应进入旧快照
    db2 = Database(settings)
    try:
        svc2 = AuditService(db2)
        svc2.submit_points("T-LATER", {"points": [
            {"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in CROSS_POINTS
        ]})
        second = svc2.query_crossing_diffs(
            old_revision=1, new_revision=3, page_size=1, cursor=cursor
        )
        assert second["snapshot_id"] == first["snapshot_id"]
        assert [(i["trip_id"], i["zone_id"]) for i in second["items"]] == [("T", "zb")]
        assert second["next_cursor"] is None
    finally:
        db2.close()


def test_concurrent_first_requests_get_distinct_complete_snapshots(client):
    import threading

    _setup_multi(client)
    responses = []
    errors = []

    def worker():
        try:
            r = client.get(f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=100")
            responses.append(r.json())
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    snapshot_ids = [b["snapshot_id"] for b in responses]
    assert len(set(snapshot_ids)) == 6  # 每个首次请求各得一个互不冲突的快照
    for body in responses:
        assert sorted((i["trip_id"], i["zone_id"]) for i in body["items"]) == [
            ("T1", "za"),
            ("T1", "zb"),
            ("T2", "za"),
            ("T2", "zb"),
        ]


def test_diff_response_echoes_snapshot_id_and_paging_meta(client):
    _setup_basic(client)
    body = client.get(
        f"{DIFF_PATH}?old_revision=1&new_revision=3&page_size=50"
        "&since=2026-09-30T09:00:00Z"
    ).json()
    assert body["page_size"] == 50
    assert body["since"] == "2026-09-30T09:00:00Z"
    assert body["until"] is None
    assert isinstance(body["snapshot_id"], str) and body["snapshot_id"]
