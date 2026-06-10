from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.sql import func
from .database import Base

# 用户表
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)
    role = Column(String)  # admin / partner
    service_rate = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

# 业务数据表
class BusinessRecord(Base):
    __tablename__ = "business_records"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    batch_id = Column(Integer, ForeignKey("upload_batches.id"))
    name = Column(String)
    phone = Column(String, index=True)
    plate_number = Column(String, index=True)
    points_amount = Column(Float)
    bank_card = Column(String, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class UploadBatch(Base):
    __tablename__ = "upload_batches"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    filename = Column(String)
    total_rows = Column(Integer, default=0)
    success_rows = Column(Integer, default=0)
    failed_rows = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())