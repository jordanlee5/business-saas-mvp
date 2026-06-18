import sqlite3

DB_PATH = "saas_mvp.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute("PRAGMA table_info(upload_batches)")
columns = [row[1] for row in cursor.fetchall()]

if "acceptance_status" not in columns:
    cursor.execute(
        "ALTER TABLE upload_batches ADD COLUMN acceptance_status TEXT DEFAULT '已承接'"
    )
    print("已新增字段：acceptance_status")
else:
    print("字段已存在：acceptance_status")

conn.commit()
conn.close()

print("上传批次承接状态字段迁移完成")