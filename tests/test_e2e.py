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
    assert r1.optimality == "optimal"


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


def test_16_feasible_incumbent_at_migration_phase_is_unproven():
    """第一阶段限时只拿到 FEASIBLE 时：返回未证最优，且不得固定值继续第二阶段。"""
    from ortools.sat.python import cp_model

    from app import solver as slv

    spec = slv.SolveSpec(
        fixtures=[slv.FixtureSpec(i, f"F{i}", 10, None, None, False) for i in (1, 2, 3)],
        reservations=[],
        lines=[slv.LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )

    real_cls = cp_model.CpSolver

    class Scripted(real_cls):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def Solve(self, model):  # noqa: N802
            st = super().Solve(model)
            self.calls += 1
            # 第一次 Solve = 第一阶段；伪造为限时 FEASIBLE（incumbent 仍可读）
            return cp_model.FEASIBLE if self.calls == 1 else st

    slv.cp_model.CpSolver = Scripted
    try:
        r = slv.solve(spec)
    finally:
        slv.cp_model.CpSolver = real_cls

    assert r.optimality == slv.UNPROVEN
    assert r.unproven_stage == "migration"
    assert r.feasible is True  # 带回了候选，但不可发布
    assert r.has_candidate is True


def test_17_phase_gating_no_fix_without_optimal():
    """用计数假求解器验证：第一阶段非 OPTIMAL 时，第二、三阶段根本不会被调用。"""
    from ortools.sat.python import cp_model

    from app import solver as slv

    spec = slv.SolveSpec(
        fixtures=[slv.FixtureSpec(i, f"F{i}", 10, None, None, False) for i in (1, 2)],
        reservations=[],
        lines=[slv.LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )

    calls = {"n": 0}
    real_cls = cp_model.CpSolver

    class OnlyFirstFeasible(real_cls):
        def Solve(self, model):  # noqa: N802
            super().Solve(model)
            calls["n"] += 1
            return cp_model.FEASIBLE  # 第一阶段即未证最优

    slv.cp_model.CpSolver = OnlyFirstFeasible
    try:
        r = slv.solve(spec)
    finally:
        slv.cp_model.CpSolver = real_cls

    assert calls["n"] == 1  # 只求解了第一阶段，未固定值继续
    assert r.optimality == slv.UNPROVEN
    assert r.unproven_stage == "migration"


def test_18_lexicographic_batch_shrinks_without_fixing():
    """字典序批量未证最优时收缩批量，未证明的一批取值不被固定；最终仍可证明时给出最优。"""
    from ortools.sat.python import cp_model

    from app import solver as slv

    # 4 条线路时一个加权批最多打包 4 台；3 台灯一批完成
    assert slv._max_packed_batch(4) >= 3

    spec = slv.SolveSpec(
        fixtures=[slv.FixtureSpec(i, f"F{i}", 10, None, None, False) for i in (3, 7, 12)],
        reservations=[],
        lines=[slv.LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )

    real_cls = cp_model.CpSolver
    state = {"n": 0}

    class FirstBatchFeasibleThenOptimal(real_cls):
        """第一次字典序批量（3 台一起）返回 FEASIBLE，之后收缩为逐台求解均 OPTIMAL。

        迁移/线路阶段发生在字典序之前：共 2 次 Solve，需让它们正常返回。
        """

        def Solve(self, model):  # noqa: N802
            st = super().Solve(model)
            state["n"] += 1
            if state["n"] == 3:  # 第一次字典序批量求解
                return cp_model.FEASIBLE
            return st  # 收缩后逐台 OPTIMAL

    slv.cp_model.CpSolver = FirstBatchFeasibleThenOptimal
    try:
        r = slv.solve(spec)
    finally:
        slv.cp_model.CpSolver = real_cls

    # 批量为 3 时 FEASIBLE -> 批量 1（ID 3）OPTIMAL 固定；再 ID 7、ID 12 各一次
    assert r.optimality == slv.OPTIMAL
    keys = [(a.line_id, a.start) for a in sorted(r.allocations, key=lambda a: a.fixture_id)]
    assert keys == [(1, 1), (1, 11), (1, 21)]


def test_19_lexicographic_single_fixture_timeout_is_unproven():
    """收缩到单台灯仍无法证明时：返回未证最优，且该台取值未被固定。"""
    from ortools.sat.python import cp_model

    from app import solver as slv

    spec = slv.SolveSpec(
        fixtures=[slv.FixtureSpec(i, f"F{i}", 10, None, None, False) for i in (3, 7)],
        reservations=[],
        lines=[slv.LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )

    real_cls = cp_model.CpSolver
    state = {"n": 0}

    class TimeoutLexTail(real_cls):
        def Solve(self, model):  # noqa: N802
            st = super().Solve(model)
            state["n"] += 1
            if state["n"] == 3:  # 字典序唯一一次批量（2 台）未证明
                return cp_model.FEASIBLE
            if state["n"] >= 4:  # 收缩到单台仍未证明
                return cp_model.FEASIBLE
            return st

    slv.cp_model.CpSolver = TimeoutLexTail
    try:
        r = slv.solve(spec)
    finally:
        slv.cp_model.CpSolver = real_cls

    assert r.optimality == slv.UNPROVEN
    assert r.unproven_stage == "lexicographic"
    assert r.feasible is True  # 有候选
    # 不可被当作最优：不生成计划由 API 层测试覆盖


def test_20_preview_unproven_creates_no_draft(client):
    """HTTP：第一阶段 FEASIBLE -> 200 + optimality_status=unproven，且不生成草稿。"""
    from ortools.sat.python import cp_model

    from app import planner
    from app.database import SessionLocal
    from app.models import Plan

    # 清理前面用例遗留的互斥/撞保留区锁定灯具，使配置恢复可解
    fx = {f["name"]: f for f in client.get("/api/fixtures").json()}
    for name in ("Lk1", "Lk2", "Lk3"):
        if name in fx:
            assert client.delete(f"/api/fixtures/{fx[name]['id']}").status_code == 204

    with SessionLocal() as db:
        drafts_before = db.query(Plan).filter(Plan.status == "draft").count()

    real_cls = cp_model.CpSolver

    class FirstFeasible(real_cls):
        def __init__(self):
            super().__init__()
            self._n = 0

        def Solve(self, model):  # noqa: N802
            super().Solve(model)
            self._n += 1
            return cp_model.FEASIBLE if self._n == 1 else cp_model.OPTIMAL

    planner.solver.cp_model.CpSolver = FirstFeasible
    try:
        r = client.post("/api/plans/preview")
    finally:
        planner.solver.cp_model.CpSolver = real_cls

    assert r.status_code == 200, r.text
    p = r.json()
    assert p["feasible"] is True
    assert p["optimality_status"] == "unproven"
    assert p["unproven_stage"] == "migration"
    assert "plan_id" not in p and "id" not in p  # 未生成草稿
    assert p["conflicts"] and p["conflicts"][0]["type"] == "unproven"

    with SessionLocal() as db:
        drafts_after = db.query(Plan).filter(Plan.status == "draft").count()
    assert drafts_after == drafts_before


def test_21_legacy_draft_without_proof_is_rejected(client):
    """无 optimality_status 证明记录的遗留草稿（或未证最优草稿）一律拒发。"""
    from app.database import SessionLocal
    from app.models import Plan, PlanAllocation
    from app.services import get_config_version

    with SessionLocal() as db:
        plan = Plan(
            config_version=get_config_version(db),
            status="draft",
            optimality_status=None,
            migration_count=0,
            used_lines=1,
        )
        db.add(plan)
        db.flush()
        db.add(
            PlanAllocation(
                plan_id=plan.id, fixture_id=1, line_id=1, start=1, end=10, migrated=False
            )
        )
        db.commit()
        legacy_id = plan.id

    r = client.post(f"/api/plans/{legacy_id}/publish")
    assert r.status_code == 409
    assert "最优" in r.json()["detail"]

    # 未证最优状态同样拒发（即使版本匹配）
    with SessionLocal() as db:
        plan = Plan(
            config_version=get_config_version(db),
            status="draft",
            optimality_status="unproven",
            migration_count=0,
            used_lines=1,
        )
        db.add(plan)
        db.commit()
        unproven_id = plan.id
    r = client.post(f"/api/plans/{unproven_id}/publish")
    assert r.status_code == 409
    assert "最优" in r.json()["detail"]


def test_22_optimal_preview_persists_and_publishes(client):
    """成功预览持久化 optimality_status=optimal，查询与发布均可核对。"""
    from app.database import SessionLocal
    from app.models import Plan

    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal"
    plan_id = p["plan_id"]

    got = client.get(f"/api/plans/{plan_id}").json()
    assert got["optimality_status"] == "optimal"

    with SessionLocal() as db:
        assert db.get(Plan, plan_id).optimality_status == "optimal"

    r = client.post(f"/api/plans/{plan_id}/publish")
    assert r.status_code == 200, r.text
    assert r.json()["optimality_status"] == "optimal"
