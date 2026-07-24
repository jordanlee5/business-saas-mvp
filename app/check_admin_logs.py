from .database import SessionLocal
from .models import AdminActionLog


def main():
    db = SessionLocal()

    try:
        logs = (
            db.query(AdminActionLog)
            .order_by(AdminActionLog.created_at.desc())
            .limit(10)
            .all()
        )

        print("\n最近管理员操作记录:\n")

        if not logs:
            print("暂无日志记录")
            return

        for log in logs:
            print("----------------------------")
            print(f"ID: {log.id}")
            print(f"管理员ID: {log.admin_id}")
            print(f"操作类型: {log.action_type}")
            print(f"目标类型: {log.target_type}")
            print(f"目标ID: {log.target_id}")
            print(f"说明: {log.description}")
            print(f"时间: {log.created_at}")

    finally:
        db.close()


if __name__ == "__main__":
    main()