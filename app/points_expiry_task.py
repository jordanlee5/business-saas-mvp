"""积分到期维护入口：默认只读预览，--apply 才提交当前页。"""

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .database import create_database_engine
from .mall.points_expiry_service import (
    expire_points_grant,
    list_due_points_grants,
    list_expiring_points_grants,
)
from .schema_readiness import assert_database_schema_ready


def run_points_expiry_task(
    engine, *, apply=False, upcoming_days=None, account_id=None,
    limit=100, after_grant_id=0, now=None,
):
    """每次在独立事务处理一页；当前页存在阻止项时整页不写入。"""
    if apply and upcoming_days is not None:
        raise ValueError("即将到期查询只支持只读模式")
    sqlite = engine.dialect.name == "sqlite"
    if sqlite and engine.url.database not in (None, "", ":memory:"):
        # 防止在错误工作目录静默创建空库；URI 连接也交由 SQLite mode=ro/rw 检查。
        database_path = engine.url.database
        if not engine.url.query.get("uri") and not Path(database_path).is_file():
            raise ValueError("数据库文件不存在，请在项目根目录核对 DATABASE_URL")
    assert_database_schema_ready(engine)
    with engine.connect() as connection:
        if sqlite:
            # pysqlite 的传统事务模式不会为 SELECT 自动发出 BEGIN。
            # 预览显式建立快照；执行则在查询候选前串行化 SQLite 写入者。
            connection.exec_driver_sql("BEGIN IMMEDIATE" if apply else "BEGIN")
        else:
            connection = connection.execution_options(
                isolation_level="READ COMMITTED" if apply else "REPEATABLE READ",
            )
            connection.begin()
        try:
            with Session(bind=connection, autoflush=False) as db:
                options = dict(
                    now=now, account_id=account_id, limit=limit,
                    after_grant_id=after_grant_id,
                )
                if upcoming_days is None:
                    page = list_due_points_grants(db, **options)
                else:
                    page = list_expiring_points_grants(db, days=upcoming_days, **options)
                blocked = [item for item in page.items if item.block_reason]
                results = []
                if apply and not blocked:
                    from .models import PointsAccount

                    # 批量任务先按固定顺序锁账户，避免跨账户任务交叉持锁。
                    account_ids = sorted({item.account_id for item in page.items})
                    if account_ids:
                        db.query(PointsAccount.id).filter(
                            PointsAccount.id.in_(account_ids),
                        ).order_by(PointsAccount.id).with_for_update().all()
                    for item in page.items:
                        results.append(expire_points_grant(
                            db, grant_id=item.grant_id, now=page.as_of,
                        ))
                report = {
                    "mode": "apply" if apply else "preview",
                    "query": "due" if upcoming_days is None else "upcoming",
                    "timezone": "Asia/Shanghai",
                    "as_of": page.as_of,
                    "upcoming_days": upcoming_days,
                    "page_count": len(page.items),
                    "page_available_points": sum(
                        (item.available_points for item in page.items), Decimal("0.00"),
                    ),
                    "blocked_count": len(blocked),
                    "has_more": page.has_more,
                    "next_after_grant_id": page.next_after_grant_id,
                    "changed_count": sum(result.changed for result in results),
                    "expired_points": sum(
                        (result.expired_points for result in results), Decimal("0.00"),
                    ),
                    "items": [asdict(item) for item in page.items],
                    "results": [asdict(result) for result in results],
                    "committed": False,
                }
                if apply and not blocked:
                    connection.commit()
                    report["committed"] = True
                else:
                    connection.rollback()
                return report
        except Exception:
            connection.rollback()
            raise


def _json_value(value):
    if isinstance(value, Decimal):
        return format(value, ".2f")
    if isinstance(value, datetime):
        return value.isoformat() + "+08:00"
    raise TypeError(f"无法输出 {type(value).__name__}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="提交当前页到期扣减")
    mode.add_argument("--upcoming-days", type=int, help="只读查询未来 1～366 天内到期的积分")
    parser.add_argument("--account-id", type=int, help="仅检查指定积分账户")
    parser.add_argument("--limit", type=int, default=100, help="每页 1～1000 批，默认 100")
    parser.add_argument("--after-grant-id", type=int, default=0, help="从此积分批次 ID 之后继续")
    args = parser.parse_args(argv)
    engine = create_database_engine()
    try:
        report = run_points_expiry_task(engine, **vars(args))
    except (ValueError, RuntimeError, SQLAlchemyError) as exc:
        # 不打印连接串或数据库异常中的 SQL 参数。
        detail = "数据库操作失败，当前页已回滚" if isinstance(exc, SQLAlchemyError) else str(exc)
        print(json.dumps({"status": "error", "message": detail}, ensure_ascii=False))
        return 1
    finally:
        engine.dispose()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=_json_value))
    return 2 if report["blocked_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
