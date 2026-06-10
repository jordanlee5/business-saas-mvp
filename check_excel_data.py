# check_excel_data.py
from app.database import SessionLocal
from app.models import BusinessRecord

db = SessionLocal()
records = db.query(BusinessRecord).all()
for r in records:
    print(r.name, r.phone, r.plate_number, r.points_amount, r.bank_card)
db.close()