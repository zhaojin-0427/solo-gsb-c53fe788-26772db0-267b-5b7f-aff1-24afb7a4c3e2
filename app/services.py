"""初始化默认数据与配置版本管理。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from .database import Base, engine
from .models import ConfigVersion, Line


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        if db.get(ConfigVersion, 1) is None:
            db.add(ConfigVersion(id=1, version=1))
        existing = {row.id for row in db.query(Line.id).all()}
        for line_id, default_name in [(1, "线路 1"), (2, "线路 2"), (3, "线路 3"), (4, "线路 4")]:
            if line_id not in existing:
                db.add(Line(id=line_id, name=default_name, is_active=True))
        db.commit()


def bump_config_version(db: Session) -> int:
    """任何影响编排结果的配置变化后调用，返回新版本号。"""
    db.execute(
        update(ConfigVersion)
        .where(ConfigVersion.id == 1)
        .values(version=ConfigVersion.version + 1, updated_at=datetime.now(timezone.utc))
    )
    return get_config_version(db)


def get_config_version(db: Session) -> int:
    row = db.get(ConfigVersion, 1)
    return row.version if row else 0
