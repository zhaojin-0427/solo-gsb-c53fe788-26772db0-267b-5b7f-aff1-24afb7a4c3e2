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
    assert p["optimality_status"] == "proven_optimal", p
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
    assert p["optimality_status"] == "proven_optimal"
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

    # 同样迁移数/线路数下，字典序方案确定且最小。
    r1 = run([7, 3, 12])
    r2 = run([7, 3, 12])
    keys1 = [(a.line_id, a.start) for a in sorted(r1.allocations, key=lambda a: a.fixture_id)]
    keys2 = [(a.line_id, a.start) for a in sorted(r2.allocations, key=lambda a: a.fixture_id)]
    assert keys1 == keys2  # 确定性
    # 三台 10ch 都能放线路 1，字典序最小 => 全部 line 1，起始 1/11/21
    assert keys1 == [(1, 1), (1, 11), (1, 21)]
    assert r1.optimality_status == "proven_optimal"


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
    assert r.optimality_status == "infeasible"
    assert any(c["type"] == "unsatisfiable_lock" for c in r.conflicts), r.conflicts


# ---------------------------------------------------------------------------
# 最优性证明回归：FEASIBLE 不得被当成阶段最优；未证最优必须区别于无解且不可发布
# ---------------------------------------------------------------------------


def _install_feasible_fake(monkeypatch, statuses):
    """把 CP-SAT 求解器替换为按调用次序返回指定状态的桩。

    statuses 中每项为 int 状态码，或 {"status": ..., "values": {...}} 字典，
    values 以变量名为键提供取值（total_mig/total_used/p_*/start_*）。
    """
    import app.solver as solver_mod

    calls = {"n": 0}

    class FakeSolver:
        def __init__(self, *a, **kw):
            self._params = None
            self._values: dict = {}

        @property
        def parameters(self):  # noqa: D401
            return self._params

        @parameters.setter
        def parameters(self, value):
            self._params = value

        def Solve(self, model):
            entry = statuses[min(calls["n"], len(statuses) - 1)]
            calls["n"] += 1
            if isinstance(entry, dict):
                self._values = entry.get("values", {})
                return entry["status"]
            self._values = {}
            return entry

        def Value(self, var):
            name = getattr(var, "Name", lambda: "")()
            if name in self._values:
                return self._values[name]
            # present 布尔默认 0、start 默认 1、总计默认 0
            return 1 if name.startswith("start_") else 0

    monkeypatch.setattr(solver_mod.cp_model, "CpSolver", FakeSolver)
    return calls


def _values_for(total_mig, total_used, chosen):
    """构造桩变量取值：chosen 为 {fixture_id: (line_id, start)}。"""
    values = {"total_mig": total_mig, "total_used": total_used}
    for fid, (lid, st) in chosen.items():
        values[f"start_{fid}"] = st
        for candidate in range(1, 5):
            values[f"p_{fid}_{candidate}"] = 1 if candidate == lid else 0
    return values


def test_16_phase1_feasible_is_unproven_not_optimal(monkeypatch):
    """第一阶段只返回 FEASIBLE 时：不得固定迁移数继续求解，必须报 unproven。"""
    import app.solver as solver_mod
    from ortools.sat.python import cp_model

    _install_feasible_fake(monkeypatch, [cp_model.FEASIBLE])

    spec = solver_mod.SolveSpec(
        fixtures=[solver_mod.FixtureSpec(1, "F1", 10, None, None, False)],
        reservations=[],
        lines=[solver_mod.LineSpec(1, "L1", True)],
    )
    r = solver_mod.solve(spec)
    assert r.optimality_status == solver_mod.STATUS_UNPROVEN
    assert r.feasible is None  # 无现任解
    assert r.phase == solver_mod.PHASE_MIGRATION
    assert not r.allocations


def test_17_phase2_feasible_does_not_pin_migration(monkeypatch):
    """第二阶段 FEASIBLE：迁移数虽已证最优，也不得把未证的启用线路数固定下来。"""
    import app.solver as solver_mod
    from ortools.sat.python import cp_model

    _install_feasible_fake(monkeypatch, [
        {"status": cp_model.OPTIMAL, "values": _values_for(0, 1, {1: (1, 1)})},
        cp_model.FEASIBLE,
    ])

    spec = solver_mod.SolveSpec(
        fixtures=[solver_mod.FixtureSpec(1, "F1", 10, None, None, False)],
        reservations=[],
        lines=[solver_mod.LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )
    r = solver_mod.solve(spec)
    assert r.optimality_status == solver_mod.STATUS_UNPROVEN
    assert r.phase == solver_mod.PHASE_USED_LINES
    assert not r.allocations


def test_18_lexicographic_first_fixture_unproven_blocks_rest(monkeypatch):
    """字典序阶段第一台灯未证最优时必须立即停止，不能固定其值继续后面的灯具。"""
    import app.solver as solver_mod
    from ortools.sat.python import cp_model

    chosen = {1: (1, 1), 2: (1, 11)}
    calls = _install_feasible_fake(
        monkeypatch,
        [
            {"status": cp_model.OPTIMAL, "values": _values_for(0, 1, chosen)},
            {"status": cp_model.OPTIMAL, "values": _values_for(0, 1, chosen)},
            cp_model.FEASIBLE,
        ],
    )

    spec = solver_mod.SolveSpec(
        fixtures=[
            solver_mod.FixtureSpec(1, "F1", 10, None, None, False),
            solver_mod.FixtureSpec(2, "F2", 10, None, None, False),
        ],
        reservations=[],
        lines=[solver_mod.LineSpec(1, "L1", True)],
    )
    r = solver_mod.solve(spec)
    assert r.optimality_status == solver_mod.STATUS_UNPROVEN
    assert r.phase == solver_mod.PHASE_LEXICOGRAPHIC
    assert r.fixture_id == 1
    # 停在第一台灯：绝不能进入第二台灯的求解
    # （共三次 Solve：迁移、启用线路、字典序第 1 台）
    assert calls["n"] == 3


def test_19_unproven_preview_creates_no_draft_and_is_distinguishable(client, monkeypatch):
    """HTTP 预览在未证最优时：200 + unproven 状态、无 plan_id、不写任何草稿；与无解区分。"""
    from app import planner
    from app.models import Plan
    from ortools.sat.python import cp_model

    class IncumbentSolver:
        def __init__(self, *a, **kw):
            self._params = None

        @property
        def parameters(self):
            return self._params

        @parameters.setter
        def parameters(self, value):
            self._params = value

        def Solve(self, model):
            # 只在迁移阶段调用一次：返回 FEASIBLE 但给不出完整变量取值
            return cp_model.FEASIBLE

        def Value(self, var):
            raise RuntimeError("no incumbent")

    monkeypatch.setattr(planner.solver.cp_model, "CpSolver", IncumbentSolver)

    from app.database import SessionLocal

    with SessionLocal() as db:
        before = db.query(Plan).count()

    r = client.post("/api/plans/preview")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["optimality_status"] == "unproven"
    assert data["feasible"] is False
    assert data.get("plan_id") is None and data.get("id") is None
    assert data.get("message")
    assert data["unproven_phase"] == "migration"

    with SessionLocal() as db:
        after = db.query(Plan).count()
    assert after == before, "未证最优预览不得生成任何草稿"

    # 真正无解仍返回 infeasible（与 unproven 明确区分）
    monkeypatch.undo()
    for lid in (1, 2, 3, 4):
        client.patch(f"/api/lines/{lid}", json={"is_active": False})
    bad = client.post("/api/plans/preview").json()
    assert bad["feasible"] is False
    assert bad["optimality_status"] == "infeasible"
    assert any(c["type"] == "no_active_line" for c in bad["conflicts"])
    for lid in (1, 2, 3, 4):
        client.patch(f"/api/lines/{lid}", json={"is_active": True})


def test_20_legacy_unproven_draft_is_rejected(client):
    """无最优性证明状态的旧草稿即使版本未过期，也必须拒发（409）。"""
    from app.database import SessionLocal
    from app.models import Plan, PlanAllocation
    from app.services import get_config_version

    with SessionLocal() as db:
        plan = Plan(
            config_version=get_config_version(db),
            status="draft",
            optimality_status="unproven",  # 模拟旧库/旧代码留下的无证明草稿
            migration_count=0,
            used_lines=1,
        )
        db.add(plan)
        db.flush()
        db.add(
            PlanAllocation(plan_id=plan.id, fixture_id=1, line_id=1, start=1, end=11, migrated=False)
        )
        db.commit()
        legacy_id = plan.id

    r = client.post(f"/api/plans/{legacy_id}/publish")
    assert r.status_code == 409
    assert "最优性证明" in r.json()["detail"]

    # 查询时仍可核对该草稿的最优性状态
    got = client.get(f"/api/plans/{legacy_id}").json()
    assert got["optimality_status"] == "unproven"


def test_21_stale_proven_preview_still_rejected(client):
    """已证最优但配置版本过期的预览仍拒发（版本门禁不被削弱）。"""
    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "proven_optimal"
    plan_id = p["plan_id"]

    r = client.post("/api/reservations", json={"line_id": 3, "start": 1, "end": 8, "reason": "x"})
    assert r.status_code == 201

    r = client.post(f"/api/plans/{plan_id}/publish")
    assert r.status_code == 409
    assert "重新生成" in r.json()["detail"]


def test_22_lexicographic_line_priority_domination():
    """线路 ID 优先于起始地址：宁可 line1 地址 500，也不要 line2 地址 1。"""
    import app.solver as solver_mod

    # 一台占 13ch 的已配灯锁定，把线路 1 的前部全部占满到 499，
    # 新灯可放 line1@500 或 line2@1；字典序最小应为 (1, 500)。
    spec = solver_mod.SolveSpec(
        fixtures=[
            solver_mod.FixtureSpec(1, "Anchor", 499, 1, 1, True),
            solver_mod.FixtureSpec(2, "Small", 10, None, None, False),
        ],
        reservations=[],
        lines=[solver_mod.LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )
    r = solver_mod.solve(spec)
    assert r.optimality_status == solver_mod.STATUS_PROVEN_OPTIMAL, r.message
    by_id = {a.fixture_id: a for a in r.allocations}
    assert (by_id[2].line_id, by_id[2].start) == (1, 500)
