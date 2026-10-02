"""基于 OR-Tools CP-SAT 的 DMX 通道编排求解器。

约束：
- 每台灯具在某条启用线路上占据一段连续通道，不跨线路、不越界；
- 同一线路上灯具之间、灯具与保留区之间不重叠；
- 锁定灯具必须使用既有线路与起始地址。

三级目标（严格按优先级逐级“证明”最优，而非仅找到可行解）：
1. 已配灯具迁移台数最少；
2. 在迁移台数已证最少的前提下，启用线路数最少；
3. 前两级都证明后，按灯具 ID 顺序逐台把 (线路 ID, 起始地址)
   字典序最小化——逐台固定前缀，每一台都必须由求解器返回 OPTIMAL
   （即证明该台在已固定前缀下取得最小字典序值）后才固定其值。

任何一级在限时内只拿到 FEASIBLE（可行但未证明最优）或 UNKNOWN，
都返回 ``optimality_status = "unproven"``：不把可行目标值当作阶段
最优值固定下来继续求解；该状态与“真正无解”（"infeasible"）明确区分。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

LINE_SIZE = 512
# 每一级目标求解的时间上限（秒），可用环境变量覆盖
SOLVE_SECONDS_PHASE = float(os.environ.get("DMX_SOLVE_SECONDS_PHASE", "5.0"))
# 字典序阶段单台灯的时间上限
SOLVE_SECONDS_TIE = float(os.environ.get("DMX_SOLVE_SECONDS_TIE", "1.5"))
# 整个 solve() 的总时限兜底（字典序台灯数很多时防止请求无限挂起）
SOLVE_SECONDS_TOTAL = float(os.environ.get("DMX_SOLVE_SECONDS_TOTAL", "60.0"))
# 字典序破局：同一台灯的目标值 = 线路序号 * BASE + 起始地址；
# BASE > 起始地址最大跨度(511)，保证“线路 ID 较小”严格压过“起始地址较小”。
LEX_LINE_BASE = 512
# 剩余预算低于该秒数时不再发起新的字典序求解，直接判未证最优
_MIN_USEFUL_SECONDS = 0.05

# 最优性状态（同时作为 HTTP 预览响应中的 optimality_status）
STATUS_INFEASIBLE = "infeasible"          # 已证明无解（预检冲突 / INFEASIBLE + 诊断）
STATUS_PROVEN_OPTIMAL = "proven_optimal"  # 三级目标全部证明最优，可发布
STATUS_UNPROVEN = "unproven"              # 某级到时只拿到 FEASIBLE/UNKNOWN：未证最优，不可发布

PHASE_MIGRATION = "migration"
PHASE_USED_LINES = "used_lines"
PHASE_LEXICOGRAPHIC = "lexicographic"

_PHASE_LABELS = {
    PHASE_MIGRATION: "第一阶段（已配灯具迁移台数最少）",
    PHASE_USED_LINES: "第二阶段（启用线路数最少）",
    PHASE_LEXICOGRAPHIC: "第三阶段（按灯具 ID 的 (线路, 起始地址) 字典序最小化）",
}


def _params(seconds: float) -> cp_model.sat_parameters_pb2.SatParameters:
    p = cp_model.sat_parameters_pb2.SatParameters()
    p.max_time_in_seconds = max(seconds, 0.0)
    p.num_search_workers = 8
    p.log_search_progress = False
    return p


@dataclass
class FixtureSpec:
    id: int
    name: str
    channels: int
    prev_line: int | None
    prev_start: int | None
    locked: bool


@dataclass
class ReservationSpec:
    id: int
    line_id: int
    start: int
    end: int
    reason: str = ""


@dataclass
class LineSpec:
    id: int
    name: str
    active: bool


@dataclass
class SolveSpec:
    fixtures: list[FixtureSpec]
    reservations: list[ReservationSpec]
    lines: list[LineSpec]


@dataclass
class Allocation:
    fixture_id: int
    name: str
    line_id: int
    start: int
    end: int
    channels: int
    migrated: bool
    locked: bool


@dataclass
class SolveResult:
    # True：找到可行排布（含未证最优的现任解）；False：已证无解；None：未证最优且连可行解都没找到
    feasible: bool | None
    optimality_status: str
    allocations: list[Allocation] = field(default_factory=list)
    migration_count: int = 0
    used_lines: int = 0
    conflicts: list[dict] = field(default_factory=list)
    occupancy: list[dict] = field(default_factory=list)
    # 未证最优时的诊断信息
    phase: str | None = None
    fixture_id: int | None = None
    message: str | None = None


class _ModelArtifacts:
    def __init__(self) -> None:
        self.present: dict[int, dict[int, cp_model.IntVar]] = {}
        self.starts: dict[int, cp_model.IntVar] = {}
        self.mig: dict[int, cp_model.IntVar] = {}
        self.relax: dict[int, cp_model.IntVar] = {}
        self.total_mig: cp_model.IntVar | None = None
        self.used: dict[int, cp_model.IntVar] = {}
        self.total_used: cp_model.IntVar | None = None


def _precheck(spec: SolveSpec) -> list[dict]:
    """在建模前拦截结构性冲突，给出确定、可读的错误。"""
    conflicts: list[dict] = []
    active_lines = [l for l in spec.lines if l.active]
    active_ids = {l.id for l in active_lines}

    if not active_lines:
        conflicts.append({"type": "no_active_line", "message": "没有启用的线路，至少需要启用一条线路。"})
        return conflicts

    # 每条线路保留区占用通道的并集（允许保留区彼此重叠）
    reserved: dict[int, set[int]] = {lid: set() for lid in active_ids}
    for r in spec.reservations:
        if r.line_id in active_ids:
            reserved[r.line_id].update(range(r.start, r.end + 1))

    total_demand = sum(f.channels for f in spec.fixtures)
    total_capacity = sum(LINE_SIZE - len(ch) for ch in reserved.values())
    if total_demand > total_capacity:
        conflicts.append(
            {
                "type": "capacity",
                "message": (
                    f"通道总需求 {total_demand} 超过启用线路的可用容量 {total_capacity}"
                    f"（{len(active_lines)} 条 × {LINE_SIZE}，扣除保留区）。"
                ),
                "total_demand": total_demand,
                "total_capacity": total_capacity,
            }
        )

    res_by_id = {r.id: r for r in spec.reservations}
    locked: list[FixtureSpec] = []
    for f in spec.fixtures:
        if not f.locked:
            continue
        if f.prev_line is None or f.prev_start is None:
            conflicts.append(
                {
                    "type": "invalid_lock",
                    "fixture_id": f.id,
                    "fixture_name": f.name,
                    "message": f"灯具“{f.name}”被锁定，但缺少既有线路或起始地址。",
                }
            )
            continue
        if f.prev_line not in active_ids:
            conflicts.append(
                {
                    "type": "invalid_lock",
                    "fixture_id": f.id,
                    "fixture_name": f.name,
                    "line_id": f.prev_line,
                    "message": f"灯具“{f.name}”锁定在已停用或不存在的线路 {f.prev_line} 上。",
                }
            )
            continue
        if not (1 <= f.prev_start <= LINE_SIZE - f.channels + 1):
            conflicts.append(
                {
                    "type": "invalid_lock",
                    "fixture_id": f.id,
                    "fixture_name": f.name,
                    "line_id": f.prev_line,
                    "start": f.prev_start,
                    "message": (
                        f"灯具“{f.name}”锁定起始地址 {f.prev_start} 越界"
                        f"（占 {f.channels} 通道，线路 {f.prev_line} 允许 1..{LINE_SIZE - f.channels + 1}）。"
                    ),
                }
            )
            continue
        block = set(range(f.prev_start, f.prev_start + f.channels))
        overlap_r = next(
            (
                r
                for r in spec.reservations
                if r.line_id == f.prev_line
                and not block.isdisjoint(range(r.start, r.end + 1))
            ),
            None,
        )
        if overlap_r is not None:
            r = overlap_r
            conflicts.append(
                {
                    "type": "lock_reservation",
                    "fixture_id": f.id,
                    "fixture_name": f.name,
                    "reservation_id": r.id,
                    "line_id": f.prev_line,
                    "message": (
                        f"灯具“{f.name}”锁定块（线路 {f.prev_line}：{f.prev_start}-"
                        f"{f.prev_start + f.channels - 1}）与保留区 #{r.id}"
                        f"（{res_by_id[r.id].start}-{res_by_id[r.id].end}）重叠。"
                    ),
                }
            )
        locked.append(f)

    # 锁定块两两重叠
    by_line: dict[int, list[FixtureSpec]] = {}
    for f in locked:
        by_line.setdefault(f.prev_line, []).append(f)
    for lid, group in by_line.items():
        group.sort(key=lambda x: (x.prev_start, x.id))
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if b.prev_start >= a.prev_start + a.channels:
                    break
                conflicts.append(
                    {
                        "type": "lock_overlap",
                        "fixture_ids": [a.id, b.id],
                        "fixture_names": [a.name, b.name],
                        "line_id": lid,
                        "message": (
                            f"锁定灯具“{a.name}”（{a.prev_start}-{a.prev_start + a.channels - 1}）"
                            f"与“{b.name}”（{b.prev_start}-{b.prev_start + b.channels - 1}）"
                            f"在线路 {lid} 上重叠。"
                        ),
                    }
                )
    return conflicts


def _build_model(spec: SolveSpec, relax_locks: bool = False) -> tuple[cp_model.CpModel, _ModelArtifacts]:
    model = cp_model.CpModel()
    art = _ModelArtifacts()
    active_lines = [l for l in spec.lines if l.active]
    line_ids = [l.id for l in active_lines]
    intervals: dict[int, list] = {lid: [] for lid in line_ids}

    # 保留区：固定区间
    for r in spec.reservations:
        if r.line_id in intervals:
            iv = model.NewIntervalVar(r.start, r.end - r.start + 1, r.end + 1, f"res_{r.id}")
            intervals[r.line_id].append(iv)

    for f in spec.fixtures:
        max_start = LINE_SIZE - f.channels + 1
        start = model.NewIntVar(1, max_start, f"start_{f.id}")
        art.starts[f.id] = start
        present: dict[int, cp_model.IntVar] = {}
        for lid in line_ids:
            p = model.NewBoolVar(f"p_{f.id}_{lid}")
            iv = model.NewOptionalIntervalVar(
                start, f.channels, start + f.channels, p, f"iv_{f.id}_{lid}"
            )
            present[lid] = p
            intervals[lid].append(iv)
        model.Add(sum(present.values()) == 1)
        art.present[f.id] = present

        if f.locked and f.prev_line is not None and f.prev_start is not None and f.prev_line in line_ids:
            if relax_locks:
                # 放宽该锁定需付代价 1；诊断模型据此找出无法满足的锁
                relax = model.NewBoolVar(f"relax_{f.id}")
                art.relax[f.id] = relax
                model.Add(present[f.prev_line] == 1).OnlyEnforceIf(relax.Not())
                model.Add(start == f.prev_start).OnlyEnforceIf(relax.Not())
            else:
                for lid, p in present.items():
                    model.Add(p == 1 if lid == f.prev_line else p == 0)
                model.Add(start == f.prev_start)
            continue  # 锁定灯具要么留在原地，要么由诊断阶段处理，不计迁移

        # 迁移变量：仅“已配灯具”（有既有地址）离开原位才计迁移；新灯具计 0
        if f.prev_line is None or f.prev_start is None:
            mig = model.NewConstant(0)
        elif f.prev_line not in line_ids:
            # 既有线路被停用：必然迁移
            mig = model.NewConstant(1)
        else:
            mig = model.NewBoolVar(f"mig_{f.id}")
            same_start = model.NewBoolVar(f"same_start_{f.id}")
            model.Add(start == f.prev_start).OnlyEnforceIf(same_start)
            model.Add(start != f.prev_start).OnlyEnforceIf(same_start.Not())
            stay = model.NewBoolVar(f"stay_{f.id}")
            prev_p = present[f.prev_line]
            model.Add(stay <= prev_p)
            model.Add(stay <= same_start)
            model.Add(stay >= prev_p + same_start - 1)
            model.Add(mig == 1 - stay)
        art.mig[f.id] = mig

    for lid, ivs in intervals.items():
        model.AddNoOverlap(ivs)
        used = model.NewBoolVar(f"used_{lid}")
        art.used[lid] = used
        if ivs:
            model.AddMaxEquality(used, [art.present[f.id][lid] for f in spec.fixtures])
        else:
            model.Add(used == 0)

    art.total_mig = model.NewIntVar(0, max(len(spec.fixtures), 1), "total_mig")
    model.Add(art.total_mig == sum(art.mig.values()) if art.mig else art.total_mig == 0)
    art.total_used = model.NewIntVar(0, len(line_ids), "total_used")
    model.Add(art.total_used == sum(art.used.values()))
    return model, art


def _diagnose(spec: SolveSpec) -> SolveResult:
    """硬约束模型已被求解器证明 INFEASIBLE：放宽锁定并最小化放宽数量，定位冲突锁。

    只有放宽模型同样取到 OPTIMAL 时，才把“必须放松”的锁作为已证明诊断返回；
    若诊断本身也只到 FEASIBLE/UNKNOWN，则不给未经证明的锁定罪。
    """
    model, art = _build_model(spec, relax_locks=True)
    if art.relax:
        model.Minimize(sum(art.relax.values()))
    solver = cp_model.CpSolver()
    solver.parameters.CopyFrom(_params(SOLVE_SECONDS_PHASE))
    status = solver.Solve(model)

    if status == cp_model.OPTIMAL:
        bad_locks = [fid for fid, v in art.relax.items() if solver.Value(v) == 1]
        if bad_locks:
            fix_by_id = {f.id: f for f in spec.fixtures}
            conflicts = [
                {
                    "type": "unsatisfiable_lock",
                    "fixture_id": fid,
                    "fixture_name": fix_by_id[fid].name,
                    "line_id": fix_by_id[fid].prev_line,
                    "start": fix_by_id[fid].prev_start,
                    "message": (
                        f"锁定灯具“{fix_by_id[fid].name}”无法保持在"
                        f"线路 {fix_by_id[fid].prev_line} 地址 {fix_by_id[fid].prev_start}"
                        "（容量或保留区排布冲突），需解锁或调整保留区。"
                    ),
                }
                for fid in sorted(bad_locks)
            ]
            return SolveResult(feasible=False, optimality_status=STATUS_INFEASIBLE, conflicts=conflicts)
    elif status in (cp_model.FEASIBLE, cp_model.UNKNOWN):
        return SolveResult(
            feasible=False,
            optimality_status=STATUS_INFEASIBLE,
            conflicts=[
                {
                    "type": "solver_timeout",
                    "message": "已判定无可行排布，但冲突诊断阶段超时、未能定位具体冲突锁：请减少灯具数量或放宽部分锁定后重试。",
                }
            ],
        )

    active_lines = [l for l in spec.lines if l.active]
    occupied: dict[int, set[int]] = {l.id: set() for l in active_lines}
    for r in spec.reservations:
        if r.line_id in occupied:
            occupied[r.line_id].update(range(r.start, r.end + 1))
    free = sum(LINE_SIZE - len(s) for s in occupied.values())
    demand = sum(f.channels for f in spec.fixtures)
    return SolveResult(
        feasible=False,
        optimality_status=STATUS_INFEASIBLE,
        conflicts=[
            {
                "type": "infeasible",
                "message": (
                    "即使放宽全部锁定仍无可行排布：保留区可能把线路切割成无法容纳灯具的碎块。"
                    f"总需求 {demand} 通道，名义空闲 {free} 通道，但最大连续空闲段不足。"
                ),
                "total_demand": demand,
                "total_free": free,
            }
        ],
    )


def solve(spec: SolveSpec) -> SolveResult:
    conflicts = _precheck(spec)
    if conflicts:
        return SolveResult(feasible=False, optimality_status=STATUS_INFEASIBLE, conflicts=conflicts)

    sorted_fixtures = sorted(spec.fixtures, key=lambda f: f.id)
    active_lines = [l for l in spec.lines if l.active]
    line_ids = [l.id for l in active_lines]
    deadline = time.monotonic() + SOLVE_SECONDS_TOTAL

    def _budget(cap: float) -> float:
        return min(cap, deadline - time.monotonic())

    def _fresh_hard_model():
        return _build_model(spec, relax_locks=False)

    def _apply_hint(model: cp_model.CpModel, art: _ModelArtifacts, chosen: dict[int, tuple[int, int]]) -> None:
        """把已知完整解作为热启动提示，加速后续证明。"""
        for f in sorted_fixtures:
            if f.id not in chosen:
                continue
            lid, st = chosen[f.id]
            model.AddHint(art.starts[f.id], int(st))
            for candidate in line_ids:
                model.AddHint(art.present[f.id][candidate], 1 if candidate == lid else 0)

    def _run(model: cp_model.CpModel, seconds: float) -> tuple[int, cp_model.CpSolver]:
        solver = cp_model.CpSolver()
        solver.parameters.CopyFrom(_params(seconds))
        return solver.Solve(model), solver

    # 第一阶段：最小化迁移台数——必须由求解器证明 OPTIMAL。
    model1, art1 = _fresh_hard_model()
    model1.Minimize(art1.total_mig)
    s1, solver1 = _run(model1, _budget(SOLVE_SECONDS_PHASE))
    if s1 == cp_model.INFEASIBLE:
        return _diagnose(spec)
    if s1 != cp_model.OPTIMAL:
        return _unproven(
            spec,
            sorted_fixtures,
            PHASE_MIGRATION,
            _read_if_incumbent(solver1, s1, art1, sorted_fixtures, line_ids),
        )
    min_mig = solver1.Value(art1.total_mig)
    incumbent = _read_chosen(solver1, art1, sorted_fixtures, line_ids)

    # 第二阶段：迁移台数固定为已证最优值，最小化启用线路数——同样必须证明 OPTIMAL。
    model2, art2 = _fresh_hard_model()
    model2.Add(art2.total_mig == min_mig)
    model2.Minimize(art2.total_used)
    _apply_hint(model2, art2, incumbent)
    s2, solver2 = _run(model2, _budget(SOLVE_SECONDS_PHASE))
    if s2 == cp_model.INFEASIBLE:
        return _diagnose(spec)
    if s2 != cp_model.OPTIMAL:
        return _unproven(
            spec,
            sorted_fixtures,
            PHASE_USED_LINES,
            _read_if_incumbent(solver2, s2, art2, sorted_fixtures, line_ids),
        )
    min_used = solver2.Value(art2.total_used)
    incumbent = _read_chosen(solver2, art2, sorted_fixtures, line_ids)

    # 第三阶段：按灯具 ID 顺序逐台做字典序最小化。
    # 前一台未被证明取到最小值，就绝不固定其值继续往后求。
    fixed: dict[int, tuple[int, int]] = {}
    for f in sorted_fixtures:
        model3, art3 = _fresh_hard_model()
        model3.Add(art3.total_mig == min_mig)
        model3.Add(art3.total_used == min_used)
        for fid, (lid, stv) in fixed.items():
            model3.Add(art3.present[fid][lid] == 1)
            model3.Add(art3.starts[fid] == stv)
        # 目标 = 线路序号 * BASE + 起始地址；BASE=512 使线路 ID 优先严格成立
        model3.Minimize(
            sum(art3.present[f.id][lid] * idx * LEX_LINE_BASE for idx, lid in enumerate(line_ids))
            + art3.starts[f.id]
        )
        _apply_hint(model3, art3, incumbent)

        budget = _budget(SOLVE_SECONDS_TIE)
        if budget <= _MIN_USEFUL_SECONDS:
            return _unproven(
                spec,
                sorted_fixtures,
                PHASE_LEXICOGRAPHIC,
                incumbent,
                fixture_id=f.id,
                no_time=True,
            )
        s3, solver3 = _run(model3, budget)
        if s3 == cp_model.INFEASIBLE:
            # 与前两级结论矛盾：不应出现，按未证最优保守处理，不发布。
            return _unproven(
                spec,
                sorted_fixtures,
                PHASE_LEXICOGRAPHIC,
                incumbent,
                fixture_id=f.id,
            )
        if s3 != cp_model.OPTIMAL:
            return _unproven(
                spec,
                sorted_fixtures,
                PHASE_LEXICOGRAPHIC,
                _read_if_incumbent(solver3, s3, art3, sorted_fixtures, line_ids) or incumbent,
                fixture_id=f.id,
            )
        lid = next(l for l in line_ids if solver3.Value(art3.present[f.id][l]) == 1)
        fixed[f.id] = (lid, solver3.Value(art3.starts[f.id]))
        incumbent = _read_chosen(solver3, art3, sorted_fixtures, line_ids)

    return _compose_result(spec, sorted_fixtures, fixed, STATUS_PROVEN_OPTIMAL)


def _read_if_incumbent(
    solver: cp_model.CpSolver,
    status: int,
    art: _ModelArtifacts,
    fixtures: list[FixtureSpec],
    line_ids: list[int],
) -> dict[int, tuple[int, int]] | None:
    """FEASIBLE 时读现任可行解；UNKNOWN（无现任解）返回 None。"""
    if status != cp_model.FEASIBLE:
        return None
    try:
        return _read_chosen(solver, art, fixtures, line_ids)
    except Exception:
        return None


def _unproven(
    spec: SolveSpec,
    sorted_fixtures: list[FixtureSpec],
    phase: str,
    incumbent: dict[int, tuple[int, int]] | None,
    *,
    fixture_id: int | None = None,
    no_time: bool = False,
) -> SolveResult:
    """构造“未证最优”结果：区别于无解；现任解（若有）仅供预览展示，不落草稿。"""
    suffix = ""
    if fixture_id is not None:
        suffix = f"当前停在灯具 ID {fixture_id}：其 (线路 ID, 起始地址) 字典序最小值尚未证明。"
    if no_time:
        tail = "总求解预算已用尽，未继续求解。"
    elif incumbent:
        tail = "下列排布仅为求解器找到的现任可行解，未被证明最优，不能发布；请稍后重试，或减少灯具数量/放宽部分约束。"
    else:
        tail = "限时内既未证明最优、也未找到可行解；这不是“无解”结论。请稍后重试，或减少灯具数量/放宽部分约束。"
    message = f"{_PHASE_LABELS[phase]}在限定时间内未能证明最优。{suffix}{tail}"
    return _compose_result(
        spec,
        sorted_fixtures,
        incumbent,
        STATUS_UNPROVEN,
        phase=phase,
        fixture_id=fixture_id,
        message=message,
    )


def _compose_result(
    spec: SolveSpec,
    sorted_fixtures: list[FixtureSpec],
    chosen: dict[int, tuple[int, int]] | None,
    optimality_status: str,
    *,
    phase: str | None = None,
    fixture_id: int | None = None,
    message: str | None = None,
) -> SolveResult:
    if not chosen:
        feasible = None if optimality_status == STATUS_UNPROVEN else False
        return SolveResult(
            feasible=feasible,
            optimality_status=optimality_status,
            phase=phase,
            fixture_id=fixture_id,
            message=message,
        )

    allocations: list[Allocation] = []
    for f in sorted_fixtures:
        lid, st = chosen[f.id]
        migrated = f.prev_line is not None and (f.prev_line, f.prev_start) != (lid, st)
        allocations.append(
            Allocation(
                fixture_id=f.id,
                name=f.name,
                line_id=lid,
                start=st,
                end=st + f.channels - 1,
                channels=f.channels,
                migrated=migrated,
                locked=f.locked,
            )
        )

    occupancy = _build_occupancy(spec, allocations)
    used_lines = sum(1 for line in occupancy if line["used_channels"] > 0)
    return SolveResult(
        feasible=True,
        optimality_status=optimality_status,
        allocations=allocations,
        migration_count=sum(1 for a in allocations if a.migrated),
        used_lines=used_lines,
        occupancy=occupancy,
        phase=phase,
        fixture_id=fixture_id,
        message=message,
    )


def _read_chosen(
    solver: cp_model.CpSolver, art: _ModelArtifacts, fixtures: list[FixtureSpec], line_ids: list[int]
) -> dict[int, tuple[int, int]]:
    chosen = {}
    for f in fixtures:
        lid = next(lid for lid in line_ids if solver.Value(art.present[f.id][lid]) == 1)
        chosen[f.id] = (lid, solver.Value(art.starts[f.id]))
    return chosen


def _build_occupancy(spec: SolveSpec, allocations: list[Allocation]) -> list[dict]:
    all_lines = sorted(spec.lines, key=lambda l: l.id)
    blocks: dict[int, list[dict]] = {l.id: [] for l in all_lines}
    for a in allocations:
        blocks[a.line_id].append(
            {
                "fixture_id": a.fixture_id,
                "name": a.name,
                "start": a.start,
                "end": a.end,
                "channels": a.channels,
                "migrated": a.migrated,
                "locked": a.locked,
            }
        )
    result = []
    for line in all_lines:
        res = [
            {
                "id": r.id,
                "start": r.start,
                "end": r.end,
                "reason": r.reason,
            }
            for r in sorted(
                (r for r in spec.reservations if r.line_id == line.id),
                key=lambda r: r.start,
            )
        ]
        occupied: set[int] = set()
        for b in blocks[line.id]:
            occupied.update(range(b["start"], b["end"] + 1))
        for r in res:
            occupied.update(range(r["start"], r["end"] + 1))
        result.append(
            {
                "line_id": line.id,
                "name": line.name,
                "is_active": line.active,
                "size": LINE_SIZE,
                "used_channels": len(occupied),
                "free_channels": LINE_SIZE - len(occupied),
                "fixtures": sorted(blocks[line.id], key=lambda b: b["start"]),
                "reservations": res,
            }
        )
    return result
