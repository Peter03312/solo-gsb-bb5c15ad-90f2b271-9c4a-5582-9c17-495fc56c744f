"""两版修订号之间穿越结论差异核对测试。"""
from tests.conftest import DAY_WINDOWS, register_square


def _points(pairs):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in pairs]}


CROSS_POINTS = [
    ("p1", "2026-09-30T10:00:00Z", -50, 50),
    ("p2", "2026-09-30T10:00:40Z", 50, 50),
    ("p3", "2026-09-30T10:01:20Z", 150, 50),
]


def _setup_revisions(client):
    # rev 1：Z-A 宽 100（x 0..100，全天生效）
    register_square(client, zone_id="Z-A")
    # rev 2：Z-B 宽 100
    register_square(client, zone_id="Z-B")
    # rev 3：Z-A 扩到宽 200（x 0..200）
    register_square(
        client,
        zone_id="Z-A",
        exterior=[[0, 0], [200, 0], [200, 100], [0, 100]],
    )
    # rev 4：Z-C 新登记（宽 100）
    register_square(client, zone_id="Z-C")


def test_diff_lists_changed_zone_with_old_and_new(client):
    _setup_revisions(client)
    client.post("/api/v1/trips/T1/points", json=_points(CROSS_POINTS))

    r = client.get("/api/v1/crossing-diffs?old_revision=1&new_revision=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["old_revision"] == 1
    assert body["new_revision"] == 3
    assert body["next_cursor"] is None
    # rev1→rev3：Z-A 由宽100变宽200（40s→60s）；Z-B 在 rev1 未登记（按空结果
    # 比较），故两者都列出；只校验 Z-A 的明细
    changes = body["changes"]
    assert [(c["trip_id"], c["zone_id"]) for c in changes] == [("T1", "Z-A"), ("T1", "Z-B")]
    ch = changes[0]
    assert ch["trip_id"] == "T1" and ch["zone_id"] == "Z-A"
    assert ch["old"]["crossed"] is True
    assert ch["old"]["total_duration_sec"] == 40.0
    assert ch["old"]["segments"] == [
        {"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:01:00Z", "duration_sec": 40.0}
    ]
    assert ch["new"]["total_duration_sec"] == 60.0
    assert ch["new"]["segments"] == [
        {"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:01:20Z", "duration_sec": 60.0}
    ]
    assert ch["duration_delta_sec"] == 20.0


def test_diff_zone_unregistered_at_old_compared_as_empty(client):
    _setup_revisions(client)
    client.post("/api/v1/trips/T2/points", json=_points(CROSS_POINTS))

    # rev 3 → rev 4：Z-C 在 rev3 尚未登记（按空结果比较），rev4 有 40s 穿越
    body = client.get("/api/v1/crossing-diffs?old_revision=3&new_revision=4").json()
    assert [(c["trip_id"], c["zone_id"]) for c in body["changes"]] == [("T2", "Z-C")]
    ch = body["changes"][0]
    assert ch["old"] == {"crossed": False, "total_duration_sec": 0, "segments": []}
    assert ch["new"]["total_duration_sec"] == 40.0
    assert ch["duration_delta_sec"] == 40.0

    # rev 1 → rev 4：Z-A 变化、Z-B 新建（rev2）、Z-C 新建（rev4）；Z-B/Z-C 均与空比较
    body = client.get("/api/v1/crossing-diffs?old_revision=1&new_revision=4").json()
    assert [(c["trip_id"], c["zone_id"]) for c in body["changes"]] == [
        ("T2", "Z-A"),
        ("T2", "Z-B"),
        ("T2", "Z-C"),
    ]


def test_diff_window_change_detected(client):
    register_square(client, zone_id="win")  # rev1 全天
    register_square(  # rev2 仅 10:00:20~10:00:50 生效
        client,
        zone_id="win",
        windows=[{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:50Z"}],
    )
    client.post("/api/v1/trips/TW/points", json=_points(CROSS_POINTS))

    body = client.get("/api/v1/crossing-diffs?old_revision=1&new_revision=2").json()
    (ch,) = body["changes"]
    assert ch["old"]["total_duration_sec"] == 40.0
    assert ch["new"]["total_duration_sec"] == 30.0
    assert ch["duration_delta_sec"] == -10.0


def test_diff_same_duration_but_different_segments_listed(client):
    # 旧配置：宽 100 全天（单段 40s：10:00:20~10:01:00）
    register_square(client, zone_id="geo")
    # 新配置：宽 200（覆盖 10:00:20~10:01:20 共 60s），但中间 10:00:40~10:01:00
    # 不生效，被裁成两段（20s+20s=40s）：累计秒数相同、区间不同
    register_square(
        client,
        zone_id="geo",
        exterior=[[0, 0], [200, 0], [200, 100], [0, 100]],
        windows=[
            {"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:40Z"},
            {"start": "2026-09-30T10:01:00Z", "end": "2026-10-01T00:00:00Z"},
        ],
    )
    client.post("/api/v1/trips/TS/points", json=_points(CROSS_POINTS))

    body = client.get("/api/v1/crossing-diffs?old_revision=1&new_revision=2").json()
    (ch,) = body["changes"]
    assert ch["old"]["total_duration_sec"] == ch["new"]["total_duration_sec"] == 40.0
    assert ch["duration_delta_sec"] == 0.0
    assert ch["old"]["segments"] != ch["new"]["segments"]
    assert ch["new"]["segments"] == [
        {"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:40Z", "duration_sec": 20.0},
        {"start": "2026-09-30T10:01:00Z", "end": "2026-09-30T10:01:20Z", "duration_sec": 20.0},
    ]


def test_diff_no_changes_empty_list(client):
    _setup_revisions(client)
    client.post("/api/v1/trips/T3/points", json=_points(CROSS_POINTS))

    # rev2→rev3 改的是 Z-A，rev1→rev2 只新增 Z-B；取相邻且对该车次结论不变的区间：
    # 这里直接构造一个只注册不改动的场景
    client.post(
        "/api/v1/zones",
        json={
            "zone_id": "far",
            "exterior": [[1000, 1000], [1100, 1000], [1100, 1100], [1000, 1100]],
            "holes": [],
            "windows": DAY_WINDOWS,
        },
    )  # rev 5
    body = client.get("/api/v1/crossing-diffs?old_revision=4&new_revision=5").json()
    assert body["changes"] == []
    assert body["next_cursor"] is None


def test_diff_since_until_halfopen_window(client):
    register_square(client, zone_id="Z-A")  # rev1
    register_square(  # rev2 宽 200
        client,
        zone_id="Z-A",
        exterior=[[0, 0], [200, 0], [200, 100], [0, 100]],
    )
    client.post("/api/v1/trips/TR/points", json=_points(CROSS_POINTS))

    # 只核算 [10:00:30, 10:01:00)：旧(0..100) 在窗内 30s；
    # 新(0..200) 在窗内也是 30s（10:00:30~10:01:00）→ 无变化
    url = (
        "/api/v1/crossing-diffs?old_revision=1&new_revision=2"
        "&since=2026-09-30T10:00:30Z&until=2026-09-30T10:01:00Z"
    )
    body = client.get(url).json()
    assert body["changes"] == []
    assert body["since"] == "2026-09-30T10:00:30Z"
    assert body["until"] == "2026-09-30T10:01:00Z"

    # 核算窗 [10:00:30, 10:01:10)：旧 30s，新 40s → 有变化
    url = (
        "/api/v1/crossing-diffs?old_revision=1&new_revision=2"
        "&since=2026-09-30T10:00:30Z&until=2026-09-30T10:01:10Z"
    )
    body = client.get(url).json()
    (ch,) = body["changes"]
    assert ch["old"]["total_duration_sec"] == 30.0
    assert ch["new"]["total_duration_sec"] == 40.0
    assert ch["duration_delta_sec"] == 10.0


def test_diff_pagination_stable_trip_zone_order(client):
    _setup_revisions(client)
    # 4 个车次，结果按 (trip_id, zone_id) 稳定排序
    for tid in ["T-a", "T-b", "T-c", "T-d"]:
        client.post(f"/api/v1/trips/{tid}/points", json=_points(CROSS_POINTS))

    url = "/api/v1/crossing-diffs?old_revision=1&new_revision=4&page_size=3"
    body1 = client.get(url).json()
    keys1 = [(c["trip_id"], c["zone_id"]) for c in body1["changes"]]
    assert keys1 == [
        ("T-a", "Z-A"),
        ("T-a", "Z-B"),
        ("T-a", "Z-C"),
    ]
    assert body1["next_cursor"]

    body2 = client.get(f"{url}&cursor={body1['next_cursor']}").json()
    keys2 = [(c["trip_id"], c["zone_id"]) for c in body2["changes"]]
    assert keys2 == [
        ("T-b", "Z-A"),
        ("T-b", "Z-B"),
        ("T-b", "Z-C"),
    ]

    # 翻到末尾（每车次 3 条变化 × 4 车次 = 12 条；page_size=3 → 4 页）
    body3 = client.get(f"{url}&cursor={body2['next_cursor']}").json()
    body4 = client.get(f"{url}&cursor={body3['next_cursor']}").json()
    all_keys = keys1 + keys2 + [
        (c["trip_id"], c["zone_id"]) for c in body3["changes"]
    ] + [(c["trip_id"], c["zone_id"]) for c in body4["changes"]]
    assert all_keys == sorted(all_keys)
    assert len(all_keys) == 12
    assert body4["next_cursor"] is None


def test_diff_snapshot_freezes_trips_and_zones_after_first_request(client):
    _setup_revisions(client)  # 当前 rev 4
    client.post("/api/v1/trips/T1/points", json=_points(CROSS_POINTS))

    url = "/api/v1/crossing-diffs?old_revision=1&new_revision=4&page_size=1"
    body1 = client.get(url).json()
    cursor = body1["next_cursor"]
    assert cursor
    assert [(c["trip_id"], c["zone_id"]) for c in body1["changes"]] == [("T1", "Z-A")]

    # 首请求之后：再登记一个区域（rev 5）并上传新车次 —— 后续页不得受影响
    register_square(client, zone_id="Z-LATE")
    client.post("/api/v1/trips/T-LATE/points", json=_points(CROSS_POINTS))

    body2 = client.get(f"{url}&cursor={cursor}").json()
    keys = [(c["trip_id"], c["zone_id"]) for c in body2["changes"]]
    # 仍是首请求快照的内容：T1 的 Z-B；没有 Z-LATE，也没有 T-LATE
    assert keys == [("T1", "Z-B")]
    assert all(c["zone_id"] != "Z-LATE" for c in body2["changes"])

    # 新发起首请求才会看到新数据
    fresh = client.get(
        "/api/v1/crossing-diffs?old_revision=1&new_revision=5&page_size=100"
    ).json()
    fresh_keys = [(c["trip_id"], c["zone_id"]) for c in fresh["changes"]]
    assert ("T-LATE", "Z-LATE") in fresh_keys
    assert any(k[1] == "Z-LATE" for k in fresh_keys)


def test_diff_revision_order_error_400(client):
    register_square(client, zone_id="z")
    client.post("/api/v1/trips/T/points", json=_points(CROSS_POINTS))
    r = client.get("/api/v1/crossing-diffs?old_revision=1&new_revision=1")
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"
    r = client.get("/api/v1/crossing-diffs?old_revision=2&new_revision=1")
    assert r.status_code == 400


def test_diff_revision_not_found_404(client):
    register_square(client, zone_id="z")
    client.post("/api/v1/trips/T/points", json=_points(CROSS_POINTS))
    r = client.get("/api/v1/crossing-diffs?old_revision=1&new_revision=999")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    assert "999" in r.json()["detail"]


def test_diff_revision_non_positive_or_non_integer_422(client):
    for q in ("old_revision=0&new_revision=2", "old_revision=-1&new_revision=2",
              "old_revision=1&new_revision=2.5"):
        r = client.get(f"/api/v1/crossing-diffs?{q}")
        assert r.status_code == 422, q


def test_diff_no_zones_registered_404(client):
    client.post("/api/v1/trips/T/points", json=_points(CROSS_POINTS))
    r = client.get("/api/v1/crossing-diffs?old_revision=1&new_revision=2")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_diff_bad_window_400(client):
    register_square(client, zone_id="z")  # rev1
    register_square(  # rev2
        client,
        zone_id="z",
        exterior=[[0, 0], [200, 0], [200, 100], [0, 100]],
    )
    client.post("/api/v1/trips/T/points", json=_points(CROSS_POINTS))
    r = client.get(
        "/api/v1/crossing-diffs?old_revision=1&new_revision=2"
        "&since=2026-09-30T12:00:00Z&until=2026-09-30T11:00:00Z"
    )
    assert r.status_code == 400
    r = client.get(
        "/api/v1/crossing-diffs?old_revision=1&new_revision=2&since=2026-09-30T12:00:00"
    )
    assert r.status_code == 400
    assert "时区" in r.json()["detail"]


def test_diff_cursor_tampered_or_garbage_400(client):
    _setup_revisions(client)
    client.post("/api/v1/trips/T1/points", json=_points(CROSS_POINTS))
    body1 = client.get(
        "/api/v1/crossing-diffs?old_revision=1&new_revision=4&page_size=1"
    ).json()
    cursor = body1["next_cursor"]

    for bad in ("not-a-cursor", cursor[:-1] + ("0" if cursor[-1] != "0" else "1"),
                "YWJj.sig"):
        r = client.get(
            f"/api/v1/crossing-diffs?old_revision=1&new_revision=4&page_size=1&cursor={bad}"
        )
        assert r.status_code == 400, bad
        assert r.json()["error"] == "validation_error"
        assert "游标" in r.json()["detail"]


def test_diff_cursor_param_mismatch_400(client):
    _setup_revisions(client)
    client.post("/api/v1/trips/T1/points", json=_points(CROSS_POINTS))
    cursor = client.get(
        "/api/v1/crossing-diffs?old_revision=1&new_revision=4&page_size=1"
    ).json()["next_cursor"]

    base = "/api/v1/crossing-diffs"
    # 改 page_size
    r = client.get(f"{base}?old_revision=1&new_revision=4&page_size=2&cursor={cursor}")
    assert r.status_code == 400
    assert "参数不匹配" in r.json()["detail"]
    # 改 new_revision
    r = client.get(f"{base}?old_revision=1&new_revision=3&page_size=1&cursor={cursor}")
    assert r.status_code == 400
    # 缺 old_revision
    r = client.get(f"{base}?new_revision=4&page_size=1&cursor={cursor}")
    assert r.status_code == 422
    # 加了首请求没有的 since
    r = client.get(
        f"{base}?old_revision=1&new_revision=4&page_size=1"
        f"&since=2026-09-30T10:00:00Z&cursor={cursor}"
    )
    assert r.status_code == 400
    assert "since" in r.json()["detail"]


def test_diff_trips_without_enough_points_have_no_changes(client):
    _setup_revisions(client)
    client.post(
        "/api/v1/trips/T0/points",
        json=_points([("only", "2026-09-30T10:00:00Z", 50, 50)]),
    )
    body = client.get("/api/v1/crossing-diffs?old_revision=1&new_revision=4").json()
    assert body["changes"] == []


def test_diff_cursor_survives_process_restart(tmp_path):
    """游标密钥与快照落库，重新打开同一库后后续页仍可继续。"""
    from types import SimpleNamespace

    from app.db import Database
    from app.service import AuditService

    db_path = tmp_path / "diff.db"
    settings = SimpleNamespace(db_path=db_path, min_duration=1e-9)

    db = Database(settings)
    svc = AuditService(db)
    svc.register_zone(
        {"zone_id": "Z-A", "exterior": [[0, 0], [100, 0], [100, 100], [0, 100]],
         "holes": [], "windows": DAY_WINDOWS}
    )
    svc.register_zone(
        {"zone_id": "Z-B", "exterior": [[0, 0], [100, 0], [100, 100], [0, 100]],
         "holes": [], "windows": DAY_WINDOWS}
    )
    svc.register_zone(
        {"zone_id": "Z-A", "exterior": [[0, 0], [200, 0], [200, 100], [0, 100]],
         "holes": [], "windows": DAY_WINDOWS}
    )
    svc.submit_points("T1", {"points": [
        {"point_id": f"p{i}", "ts": ts, "x": x, "y": 50}
        for i, (ts, x) in enumerate([
            ("2026-09-30T10:00:00Z", -50),
            ("2026-09-30T10:00:40Z", 50),
            ("2026-09-30T10:01:20Z", 150),
        ])
    ]})
    # rev1→rev3：Z-A 几何变化、Z-B 新建，共 2 条变化
    page1 = svc.crossing_diffs(old_revision=1, new_revision=3, page_size=1)
    assert len(page1["changes"]) == 1
    assert page1["changes"][0]["zone_id"] == "Z-A"
    cursor = page1["next_cursor"]
    assert cursor
    db.close()

    # 模拟重启：新连接、新服务实例（新内存状态），游标仍有效
    db2 = Database(settings)
    svc2 = AuditService(db2)
    page2 = svc2.crossing_diffs(
        old_revision=1, new_revision=3, page_size=1, cursor=cursor
    )
    assert [(c["trip_id"], c["zone_id"]) for c in page2["changes"]] == [("T1", "Z-B")]
    assert page2["next_cursor"] is None
    db2.close()
