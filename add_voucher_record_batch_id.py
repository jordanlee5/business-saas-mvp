import sqlite3

conn = sqlite3.connect("saas_mvp.db")
cursor = conn.cursor()

columns = cursor.execute("PRAGMA table_info(voucher_records)").fetchall()
column_names = [col[1] for col in columns]

if "batch_id" not in column_names:
    cursor.execute("ALTER TABLE voucher_records ADD COLUMN batch_id INTEGER")
    conn.commit()
    print("已新增 voucher_records.batch_id 字段")
else:
    print("voucher_records.batch_id 字段已存在，无需重复新增")

conn.close()