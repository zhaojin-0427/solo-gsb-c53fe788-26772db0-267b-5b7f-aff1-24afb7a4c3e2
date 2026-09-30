"""基于 OR-Tools CP-SAT 的 DMX 通道编排求解器。

约束：
- 每台灯具在某条启用线路上占据一段连续通道，不跨线路、不越界；
- 同一线路上灯具之间、灯具与保留区之间不重叠；
- 锁定灯具必须使用既有线路与起始地址。

目标（按优先级）：
1. 已配灯具迁移台数最少；
2. 启用线路数最少；
3. 按灯具 ID 排序，逐台取（线路 ID, 起始地址）字典序最小。

无解时返回可读的冲突约束诊断。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

LINE_SIZE = 512
SOLVE_SECONDS_PHASE = 5.0
SOLVE_SECONDS_TIE = 1.5
# 字典序破局目标中“线路优先于起始地址”的权重（起始地址最大 512）
LINE_PRIORITY_WEIGHT = 1024
_FEASIBLE_STATUSES = (cp_model.OPTIMAL, cp_model.FEASIBLE)


def _params(seconds: float, *, first_solution: bool = False) -> cp_model.sat_parameters_pb2.SatParameters:
    p = cp_model.sat_parameters_pb2.SatParameters()
    p.max_time_in_seconds = seconds
    p.num_search_workers = 8
    p.log_search_progress = False
    if first_solution:
        # 只需要证明可行并拿到一个解时，尽快返回（不追求最优）
        p.stop_after_first_solution = True
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
    feasible: bool
    allocations: list[Allocation] = field(default_factory=list)
    migration_count: int = 0
    used_lines: int = 0
    conflicts: list[dict] = field(default_factory=list)
    occupancy: list[dict] = field(default_factory=list)
    # 第三级字典序目标是否取到了精确最优（规模过大时回退为启发式）
    tie_exact: bool = True


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
    """硬约束模型无解：放宽锁定并最小化放宽数量，定位冲突锁。"""
    model, art = _build_model(spec, relax_locks=True)
    if art.relax:
        model.Minimize(sum(art.relax.values()))
    solver = cp_model.CpSolver()
    solver.parameters.CopyFrom(_params(SOLVE_SECONDS_PHASE))
    status = solver.Solve(model)

    if status in _FEASIBLE_STATUSES:
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
            return SolveResult(feasible=False, conflicts=conflicts)
    elif status == cp_model.UNKNOWN:
        return SolveResult(
            feasible=False,
            conflicts=[
                {
                    "type": "solver_timeout",
                    "message": "冲突诊断阶段超时：约束可能过于紧张，请减少灯具数量或放宽部分锁定后重试。",
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
        return SolveResult(feasible=False, conflicts=conflicts)

    sorted_fixtures = sorted(spec.fixtures, key=lambda f: f.id)
    active_lines = [l for l in spec.lines if l.active]
    line_ids = [l.id for l in active_lines]

    def fresh_hard_model():
        return _build_model(spec, relax_locks=False)

    solver = cp_model.CpSolver()

    # 第一阶段：迁移最少
    model1, art1 = fresh_hard_model()
    model1.Minimize(art1.total_mig)
    solver = cp_model.CpSolver()
    solver.parameters.CopyFrom(_params(SOLVE_SECONDS_PHASE))
    s1 = solver.Solve(model1)
    if s1 == cp_model.INFEASIBLE:
        return _diagnose(spec)
    if s1 not in _FEASIBLE_STATUSES:
        return SolveResult(
            feasible=False,
            conflicts=[
                {
                    "type": "solver_timeout",
                    "message": "求解器在限定时间内未能完成第一阶段求解，请减少灯具数量或放宽约束后重试。",
                }
            ],
        )
    min_mig = solver.Value(art1.total_mig)

    # 第二阶段：迁移数固定，启用线路最少
    model2, art2 = fresh_hard_model()
    model2.Add(art2.total_mig == min_mig)
    model2.Minimize(art2.total_used)
    s2 = solver.Solve(model2)
    if s2 == cp_model.INFEASIBLE:
        return _diagnose(spec)
    if s2 not in _FEASIBLE_STATUSES:
        return SolveResult(
            feasible=False,
            conflicts=[
                {
                    "type": "solver_timeout",
                    "message": "求解器在限定时间内未能完成第二阶段求解，请重试。",
                }
            ],
        )
    min_used = solver.Value(art2.total_used)

    # 第三阶段：按灯具 ID 排序后的 (线路, 起始地址) 字典序最小化。
    # CP-SAT 目标系数须落在 int63 内，因此前 K 台灯（权重不溢出部分）用
    # B 进制大权重一次性精确最小化；K 之后逐台固定前缀精确求解。
    # 规模过大导致精确破局超时时，回退到单次“扁平字典序”启发式（前两级
    # 目标仍然严格最优），保证界面始终拿得到方案。
    chosen, tie_exact = _lexicographic_phase(
        spec, sorted_fixtures, line_ids, min_mig, min_used
    )
    if chosen is None:
        chosen = _lexicographic_fallback(spec, sorted_fixtures, line_ids, min_mig, min_used)
        if chosen is None:
            return SolveResult(
                feasible=False,
                conflicts=[
                    {
                        "type": "solver_timeout",
                        "message": "字典序破局阶段超时，请重试或减少灯具数量。",
                    }
                ],
            )
        tie_exact = False

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
        allocations=allocations,
        migration_count=sum(1 for a in allocations if a.migrated),
        used_lines=used_lines,
        occupancy=occupancy,
        tie_exact=tie_exact,
    )


def _read_chosen(solver: cp_model.CpSolver, art: _ModelArtifacts, fixtures, line_ids):
    chosen = {}
    for f in fixtures:
        lid = next(lid for lid in line_ids if solver.Value(art.present[f.id][lid]) == 1)
        chosen[f.id] = (lid, solver.Value(art.starts[f.id]))
    return chosen


def _lexicographic_phase(spec, sorted_fixtures, line_ids, min_mig, min_used):
    """精确字典序破局：B 进制前缀 + 逐台固定。返回 (chosen|None, exact_bool)。"""
    base = LINE_PRIORITY_WEIGHT
    digit_max = base * len(line_ids)
    cap = 2**62

    packed_k = 0
    max_coeff = base
    n = len(sorted_fixtures)
    while packed_k < n and max_coeff <= cap:
        packed_k += 1
        max_coeff *= digit_max

    head, tail = sorted_fixtures[:packed_k], sorted_fixtures[packed_k:]
    fixed: dict[int, tuple[int, int]] = {}
    solver = cp_model.CpSolver()

    if head:
        model, art = _build_model(spec)
        model.Add(art.total_mig == min_mig)
        model.Add(art.total_used == min_used)
        terms = []
        for pos, f in enumerate(head):
            w = digit_max ** (packed_k - 1 - pos)
            for idx, lid in enumerate(line_ids):
                terms.append(art.present[f.id][lid] * (idx * base) * w)
            terms.append(art.starts[f.id] * w)
        model.Minimize(sum(terms))
        solver.parameters.CopyFrom(_params(SOLVE_SECONDS_PHASE))
        st = solver.Solve(model)
        if st == cp_model.INFEASIBLE:
            return None, False
        if st not in _FEASIBLE_STATUSES:
            return None, False
        for f in head:
            lid = next(l for l in line_ids if solver.Value(art.present[f.id][l]) == 1)
            fixed[f.id] = (lid, solver.Value(art.starts[f.id]))

    for f in tail:
        model, art = _build_model(spec)
        model.Add(art.total_mig == min_mig)
        model.Add(art.total_used == min_used)
        for fid, (lid, stv) in fixed.items():
            model.Add(art.present[fid][lid] == 1)
            model.Add(art.starts[fid] == stv)
        terms = [art.starts[f.id]] + [
            art.present[f.id][lid] * idx * base for idx, lid in enumerate(line_ids)
        ]
        model.Minimize(sum(terms))
        solver.parameters.CopyFrom(_params(SOLVE_SECONDS_TIE))
        st = solver.Solve(model)
        if st not in _FEASIBLE_STATUSES:
            return None, False
        lid = next(l for l in line_ids if solver.Value(art.present[f.id][l]) == 1)
        fixed[f.id] = (lid, solver.Value(art.starts[f.id]))
    return dict(fixed), True


def _lexicographic_fallback(spec, sorted_fixtures, line_ids, min_mig, min_used):
    """扁平字典序启发式：所有台灯等权（线路指数 + 起始地址），单次求解。"""
    model, art = _build_model(spec)
    model.Add(art.total_mig == min_mig)
    model.Add(art.total_used == min_used)
    terms = []
    for f in sorted_fixtures:
        terms.extend(art.present[f.id][lid] * idx for idx, lid in enumerate(line_ids))
        terms.append(art.starts[f.id])
    model.Minimize(sum(terms))
    solver = cp_model.CpSolver()
    solver.parameters.CopyFrom(_params(SOLVE_SECONDS_PHASE))
    st = solver.Solve(model)
    if st not in _FEASIBLE_STATUSES:
        return None
    return _read_chosen(solver, art, sorted_fixtures, line_ids)


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
