"""编排生成、发布与发布快照构建。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import solver
from .models import (
    Fixture,
    FixtureMode,
    FixtureType,
    Line,
    Plan,
    PlanAllocation,
    PublishedSnapshot,
    Reservation,
)
from .services import bump_config_version, get_config_version


def build_spec(db: Session) -> solver.SolveSpec:
    lines = [
        solver.LineSpec(id=l.id, name=l.name, active=l.is_active)
        for l in db.query(Line).order_by(Line.id).all()
    ]
    reservations = [
        solver.ReservationSpec(id=r.id, line_id=r.line_id, start=r.start, end=r.end, reason=r.reason)
        for r in db.query(Reservation).order_by(Reservation.id).all()
    ]
    rows = (
        db.query(Fixture, FixtureMode, FixtureType)
        .join(FixtureMode, Fixture.mode_id == FixtureMode.id)
        .join(FixtureType, FixtureMode.type_id == FixtureType.id)
        .order_by(Fixture.id)
        .all()
    )
    fixtures = [
        solver.FixtureSpec(
            id=fx.id,
            name=fx.name,
            channels=mode.channels,
            prev_line=fx.previous_line,
            prev_start=fx.previous_start,
            locked=fx.locked,
        )
        for fx, mode, _t in rows
    ]
    return solver.SolveSpec(fixtures=fixtures, reservations=reservations, lines=lines)


def create_preview(db: Session) -> dict:
    spec = build_spec(db)
    result = solver.solve(spec)
    version = get_config_version(db)
    if result.optimality == solver.INFEASIBLE:
        return {
            "feasible": False,
            "optimality_status": solver.INFEASIBLE,
            "config_version": version,
            "conflicts": result.conflicts,
            "occupancy": [],
        }

    if not result.is_optimal:
        # 存在可行候选但未能逐级证明最优：可展示，绝不生成可发布草稿。
        return _candidate_payload(db, result, version)

    plan = Plan(
        config_version=version,
        status="draft",
        optimality_status=solver.OPTIMAL,
        migration_count=result.migration_count,
        used_lines=result.used_lines,
    )
    db.add(plan)
    db.flush()
    for a in result.allocations:
        db.add(
            PlanAllocation(
                plan_id=plan.id,
                fixture_id=a.fixture_id,
                line_id=a.line_id,
                start=a.start,
                end=a.end,
                migrated=a.migrated,
            )
        )
    db.commit()
    db.refresh(plan)
    return _plan_payload(db, plan, feasible=True)


def _candidate_payload(db: Session, result: solver.SolveResult, version: int) -> dict:
    """未证最优时返回的候选预览（无 plan_id，不可发布）。"""
    names = {f.id: f.name for f in db.query(Fixture).all()}
    locked_ids = {f.id for f in db.query(Fixture).filter(Fixture.locked.is_(True)).all()}
    prev = {f.id: (f.previous_line, f.previous_start, f.locked) for f in db.query(Fixture).all()}
    alloc_out = [
        {
            "fixture_id": a.fixture_id,
            "name": names.get(a.fixture_id, f"#{a.fixture_id}"),
            "line_id": a.line_id,
            "start": a.start,
            "end": a.end,
            "channels": a.channels,
            "migrated": a.migrated,
            "locked": a.fixture_id in locked_ids,
        }
        for a in result.allocations
    ]
    migrations = []
    for a in alloc_out:
        old_line, old_start, _locked = prev.get(a["fixture_id"], (None, None, False))
        if old_line is not None and (old_line, old_start) != (a["line_id"], a["start"]):
            migrations.append(
                {
                    "fixture_id": a["fixture_id"],
                    "name": a["name"],
                    "from_line": old_line,
                    "from_start": old_start,
                    "to_line": a["line_id"],
                    "to_start": a["start"],
                    "locked": a["fixture_id"] in locked_ids,
                }
            )
    return {
        "feasible": True,
        "optimality_status": solver.UNPROVEN,
        "unproven_stage": result.unproven_stage,
        "message": result.unproven_message,
        "config_version": version,
        "migration_count": result.migration_count,
        "used_lines": result.used_lines,
        "allocations": alloc_out,
        "migrations": migrations,
        "occupancy": result.occupancy,
        "conflicts": result.conflicts,
    }


def _plan_payload(db: Session, plan: Plan, *, feasible: bool = True) -> dict:
    allocs = (
        db.query(PlanAllocation)
        .filter(PlanAllocation.plan_id == plan.id)
        .order_by(PlanAllocation.fixture_id)
        .all()
    )
    names = {f.id: f.name for f in db.query(Fixture).all()}
    locked_ids = {f.id for f in db.query(Fixture).filter(Fixture.locked.is_(True)).all()}
    alloc_out = [
        {
            "fixture_id": a.fixture_id,
            "name": names.get(a.fixture_id, f"#{a.fixture_id}"),
            "line_id": a.line_id,
            "start": a.start,
            "end": a.end,
            "channels": a.end - a.start + 1,
            "migrated": a.migrated,
            "locked": a.fixture_id in locked_ids,
        }
        for a in allocs
    ]
    return {
        "feasible": feasible,
        "id": plan.id,
        "plan_id": plan.id,
        "config_version": plan.config_version,
        "status": plan.status,
        "optimality_status": plan.optimality_status,
        "migration_count": plan.migration_count,
        "used_lines": plan.used_lines,
        "created_at": plan.created_at,
        "allocations": alloc_out,
        "migrations": _migrations_from_alloc(db, alloc_out),
        "occupancy": _occupancy_for(db, allocs),
        "conflicts": [],
    }


def _migrations_from_alloc(db: Session, alloc_out: list[dict]) -> list[dict]:
    prev = {f.id: (f.previous_line, f.previous_start, f.locked) for f in db.query(Fixture).all()}
    items = []
    for a in alloc_out:
        old_line, old_start, locked = prev.get(a["fixture_id"], (None, None, False))
        # 从未配置过的新灯具不算迁移
        if old_line is not None and (old_line, old_start) != (a["line_id"], a["start"]):
            items.append(
                {
                    "fixture_id": a["fixture_id"],
                    "name": a["name"],
                    "from_line": old_line,
                    "from_start": old_start,
                    "to_line": a["line_id"],
                    "to_start": a["start"],
                    "locked": locked,
                }
            )
    return items


def _occupancy_for(db: Session, allocs: list[PlanAllocation]) -> list[dict]:
    lines = db.query(Line).order_by(Line.id).all()
    reservations = db.query(Reservation).all()
    names = {f.id: f.name for f in db.query(Fixture).all()}
    locked_ids = {f.id for f in db.query(Fixture).filter(Fixture.locked.is_(True)).all()}
    blocks: dict[int, list[dict]] = {l.id: [] for l in lines}
    for a in allocs:
        blocks[a.line_id].append(
            {
                "fixture_id": a.fixture_id,
                "name": names.get(a.fixture_id, f"#{a.fixture_id}"),
                "start": a.start,
                "end": a.end,
                "channels": a.end - a.start + 1,
                "migrated": a.migrated,
                "locked": a.fixture_id in locked_ids,
            }
        )
    out = []
    for line in lines:
        res = [
            {"id": r.id, "start": r.start, "end": r.end, "reason": r.reason}
            for r in sorted((r for r in reservations if r.line_id == line.id), key=lambda r: r.start)
        ]
        occupied: set[int] = set()
        for b in blocks[line.id]:
            occupied.update(range(b["start"], b["end"] + 1))
        for r in res:
            occupied.update(range(r["start"], r["end"] + 1))
        out.append(
            {
                "line_id": line.id,
                "name": line.name,
                "is_active": line.is_active,
                "size": solver.LINE_SIZE,
                "used_channels": len(occupied),
                "free_channels": solver.LINE_SIZE - len(occupied),
                "fixtures": sorted(blocks[line.id], key=lambda b: b["start"]),
                "reservations": res,
            }
        )
    return out


def get_plan_payload(db: Session, plan_id: int) -> dict:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="预览不存在（可能已被新配置覆盖）")
    return _plan_payload(db, plan, feasible=True)


def latest_published(db: Session) -> Plan | None:
    return (
        db.query(Plan)
        .filter(Plan.status == "published")
        .order_by(Plan.published_at.desc(), Plan.id.desc())
        .first()
    )


def publish_plan(db: Session, plan_id: int) -> dict:
    """原子发布：版本必须与当前配置一致，旧预览一律拒绝。"""
    current_version = get_config_version(db)
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="预览不存在")
    if plan.status != "draft":
        raise HTTPException(status_code=409, detail=f"该预览已为 {plan.status} 状态，不能重复发布")
    if plan.optimality_status != solver.OPTIMAL:
        if plan.optimality_status is None:
            raise HTTPException(
                status_code=409,
                detail="该预览由旧版本生成、没有最优性证明记录，不能发布，请重新生成预览。",
            )
        raise HTTPException(
            status_code=409,
            detail=f"该预览的最优性状态为 {plan.optimality_status}，未逐级证明最优，不能发布，请重新生成预览。",
        )
    if plan.config_version != current_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"配置已变化（预览版本 {plan.config_version}，当前版本 {current_version}），"
                "请基于最新配置重新生成预览后再发布。"
            ),
        )

    payload = _plan_payload(db, plan, feasible=True)
    now = datetime.now(timezone.utc)

    # 同一事务内完成：发布本预览、作废其余草稿、回写既有地址、保存快照
    db.query(Plan).filter(Plan.status == "draft", Plan.id != plan.id).update(
        {Plan.status: "superseded"}, synchronize_session=False
    )
    plan.status = "published"
    plan.published_at = now
    db.add(
        PublishedSnapshot(
            plan_id=plan.id,
            config_version=current_version,
            published_at=now,
            payload=json.dumps(payload, ensure_ascii=False, default=str),
        )
    )
    for a in payload["allocations"]:
        fx = db.get(Fixture, a["fixture_id"])
        if fx is not None:
            fx.previous_line = a["line_id"]
            fx.previous_start = a["start"]
    # 发布改变了灯具的“既有地址”，配置版本再推进一步，使旧草稿全部失效
    new_version = bump_config_version(db)
    db.commit()
    return {
        "ok": True,
        "plan_id": plan.id,
        "config_version": new_version,
        "optimality_status": solver.OPTIMAL,
        "published_at": now,
    }
