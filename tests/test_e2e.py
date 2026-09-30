"""端到端测试：型录导入、编排、版本失效发布、CSV 导出与冲突诊断。"""
from __future__ import annotations

import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DMX_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import init_db  # noqa: E402


@pytest.fixture(scope="session")
def client():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app) as c:
        yield c


CATALOG_CSV = (
    "manufacturer,name,mode,channels\n"
    "ACME,Solar,Basic,10\n"
    "ACME,Solar,Full,20\n"
    "BrightCo,Par,3CH,3\n"
)


def test_01_catalog_import_csv(client):
    r = client.post(
        "/api/catalog/import",
        files={"file": ("catalog.csv", CATALOG_CSV, "text/csv")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["created_types"] == 2
    assert data["created_modes"] == 3

    listing = client.get("/api/catalog").json()
    assert len(listing) == 2
    return listing


def test_02_catalog_import_duplicate_updates(client):
    csv = "manufacturer,name,mode,channels\nACME,Solar,Basic,11\n"
    r = client.post("/api/catalog/import", files={"file": ("c.csv", csv, "text/csv")})
    assert r.status_code == 200
    assert r.json()["updated_modes"] == 1


def test_03_bad_csv_rejected(client):
    r = client.post(
        "/api/catalog/import",
        files={"file": ("c.csv", "foo,bar\n1,2", "text/csv")},
    )
    assert r.status_code == 422


def test_04_create_fixtures(client):
    types = client.get("/api/catalog").json()
    mode_basic = next(m for t in types if t["name"] == "Solar" for m in t["modes"] if m["name"] == "Basic")
    mode_full = next(m for t in types if t["name"] == "Solar" for m in t["modes"] if m["name"] == "Full")
    mode_par = next(m for t in types if t["name"] == "Par" for m in t["modes"])
    # 两台已配灯具，一台新灯
    client.post("/api/fixtures", json={"name": "Spot1", "mode_id": mode_basic["id"],
                                       "previous_line": 1, "previous_start": 10, "locked": False})
    client.post("/api/fixtures", json={"name": "Spot2", "mode_id": mode_full["id"],
                                       "previous_line": 1, "previous_start": 100, "locked": True})
    client.post("/api/fixtures", json={"name": "Par1", "mode_id": mode_par["id"]})
    fx = client.get("/api/fixtures").json()
    assert len(fx) == 3
    return {f["name"]: f for f in fx}


def test_05_lock_requires_address(client):
    modes = client.get("/api/catalog").json()
    mode_id = modes[0]["modes"][0]["id"]
    r = client.post("/api/fixtures", json={"name": "Bad", "mode_id": mode_id, "locked": True})
    assert r.status_code == 422


def test_06_preview_feasible(client):
    r = client.post("/api/plans/preview")
    assert r.status_code == 200
    p = r.json()
    assert p["feasible"] is True, p
    # 两台已配灯具应留在原位 -> 迁移 0；新灯不计迁移
    assert p["migration_count"] == 0
    assert p["used_lines"] == 1
    by_name = {a["name"]: a for a in p["allocations"]}
    assert (by_name["Spot1"]["line_id"], by_name["Spot1"]["start"]) == (1, 10)
    assert (by_name["Spot2"]["line_id"], by_name["Spot2"]["start"]) == (1, 100)
    assert by_name["Spot2"]["locked"] is True
    # 无重叠且不越界
    for line in p["occupancy"]:
        assert line["used_channels"] + line["free_channels"] == 512
    return p


def test_07_publish_then_stale_preview_rejected(client):
    p = client.post("/api/plans/preview").json()
    plan_id = p["plan_id"]
    pv = p["config_version"]

    # 配置变化：增加一个保留区
    r = client.post("/api/reservations", json={"line_id": 2, "start": 1, "end": 10, "reason": "desk"})
    assert r.status_code == 201
    assert client.get("/api/plans/version").json()["config_version"] > pv

    r = client.post(f"/api/plans/{plan_id}/publish")
    assert r.status_code == 409
    assert "重新生成" in r.json()["detail"]


def test_08_fresh_preview_publish(client):
    p = client.post("/api/plans/preview").json()
    assert p["feasible"] is True
    r = client.post(f"/api/plans/{p['plan_id']}/publish")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # 重复发布被拒绝
    r2 = client.post(f"/api/plans/{p['plan_id']}/publish")
    assert r2.status_code == 409

    # 发布后既有地址已回写
    fx = {f["name"]: f for f in client.get("/api/fixtures").json()}
    assert (fx["Par1"]["previous_line"], fx["Par1"]["previous_start"]) == (1, 1)

    # 已发布表与 CSV
    latest = client.get("/api/plans/published/latest").json()
    assert latest["has_published"] is True
    csv_resp = client.get(f"/api/plans/published/{p['plan_id']}/csv")
    assert csv_resp.status_code == 200
    assert "Spot1".encode() in csv_resp.content
    assert csv_resp.content.startswith(b"\xef\xbb\xbf")
    return p


def test_09_rerequest_no_migrations_after_publish(client):
    p = client.post("/api/plans/preview").json()
    assert p["feasible"] is True
    assert p["migration_count"] == 0


def test_10_capacity_conflict(client):
    modes = client.get("/api/catalog").json()
    big_mode = next(m for t in modes for m in t["modes"] if m["channels"] == 20)
    # 再加 30 台 20ch 灯，加上保留区/已有灯（约 34ch），将超出一条线路容量
    for i in range(30):
        client.post("/api/fixtures", json={"name": f"Big{i}", "mode_id": big_mode["id"]})
    # 仅保留线路 1 启用
    for lid in (2, 3, 4):
        r = client.patch(f"/api/lines/{lid}", json={"is_active": False})
        assert r.status_code == 200, r.text
    p = client.post("/api/plans/preview").json()
    assert p["feasible"] is False
    assert any(c["type"] == "capacity" for c in p["conflicts"])


def test_11_lock_overlap_conflict(client):
    # 恢复四条线路
    for lid in (2, 3, 4):
        client.patch(f"/api/lines/{lid}", json={"is_active": True})
    modes = client.get("/api/catalog").json()
    mode3 = next(m for t in modes if t["name"] == "Par" for m in t["modes"])
    client.post("/api/fixtures", json={"name": "Lk1", "mode_id": mode3["id"],
                                       "previous_line": 3, "previous_start": 50, "locked": True})
    client.post("/api/fixtures", json={"name": "Lk2", "mode_id": mode3["id"],
                                       "previous_line": 3, "previous_start": 51, "locked": True})
    p = client.post("/api/plans/preview").json()
    assert p["feasible"] is False
    assert any(c["type"] == "lock_overlap" for c in p["conflicts"]), p["conflicts"]


def test_12_lock_reservation_conflict(client):
    client.post("/api/reservations", json={"line_id": 4, "start": 1, "end": 5, "reason": "desk"})
    modes = client.get("/api/catalog").json()
    mode3 = next(m for t in modes if t["name"] == "Par" for m in t["modes"])
    client.post("/api/fixtures", json={"name": "Lk3", "mode_id": mode3["id"],
                                       "previous_line": 4, "previous_start": 3, "locked": True})
    p = client.post("/api/plans/preview").json()
    assert p["feasible"] is False
    assert any(c["type"] == "lock_reservation" for c in p["conflicts"]), p["conflicts"]


def test_13_deterministic_lexicographic_tiebreak():
    """同样迁移数/线路数下，字典序方案确定且最小。"""
    from app.solver import FixtureSpec, LineSpec, SolveSpec, solve

    def run(ids):
        spec = SolveSpec(
            fixtures=[FixtureSpec(i, f"F{i}", 10, None, None, False) for i in ids],
            reservations=[],
            lines=[LineSpec(i, f"L{i}", True) for i in range(1, 5)],
        )
        return solve(spec)

    r1 = run([7, 3, 12])
    r2 = run([7, 3, 12])
    keys1 = [(a.line_id, a.start) for a in sorted(r1.allocations, key=lambda a: a.fixture_id)]
    keys2 = [(a.line_id, a.start) for a in sorted(r2.allocations, key=lambda a: a.fixture_id)]
    assert keys1 == keys2  # 确定性
    # 三台 10ch 都能放线路 1，字典序最小 => 全部 line 1，起始 1/11/21
    assert keys1 == [(1, 1), (1, 11), (1, 21)]
    assert r1.tie_exact is True


def test_14_lexicographic_requires_second_line():
    """60 台灯各 10ch：线路 1 放 51 台后，ID 52 起必须进入线路 2 且从地址 1 开始。"""
    from app.solver import FixtureSpec, LineSpec, SolveSpec, solve

    spec = SolveSpec(
        fixtures=[FixtureSpec(i, f"F{i}", 10, None, None, False) for i in range(1, 61)],
        reservations=[],
        lines=[LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )
    r = solve(spec)
    assert r.feasible
    by_id = {a.fixture_id: a for a in r.allocations}
    # 前 51 台排满线路 1（1..510），第 52 台起线路 2
    assert (by_id[51].line_id, by_id[51].start, by_id[51].end) == (1, 501, 510)
    assert (by_id[52].line_id, by_id[52].start) == (2, 1)
    assert by_id[60].line_id == 2
    assert r.migration_count == 0
    assert r.used_lines == 2


def test_15_unsatisfiable_lock_diagnostic():
    """容量足够但锁定块把线路夹碎时，诊断指出必须放松的锁，而非笼统报容量。"""
    from app.solver import FixtureSpec, LineSpec, ReservationSpec, SolveSpec, solve

    spec = SolveSpec(
        fixtures=[
            FixtureSpec(1, "LockA", 100, 1, 1, True),
            FixtureSpec(2, "LockB", 100, 1, 413, True),
            FixtureSpec(3, "NewBig", 101, None, None, False),
        ],
        reservations=[ReservationSpec(1, 1, 201, 312, "wall")],
        lines=[LineSpec(1, "L1", True)] + [LineSpec(i, f"L{i}", False) for i in range(2, 5)],
    )
    r = solve(spec)
    assert r.feasible is False
    assert any(c["type"] == "unsatisfiable_lock" for c in r.conflicts), r.conflicts
