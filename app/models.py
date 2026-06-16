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
    upstream_cost_rate = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

# 业务数据表
class BusinessRecord(Base):
    __tablename__ = "business_records"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    batch_id = Column(Integer, ForeignKey("upload_batches.id"))
    business_no = Column(String, index=True)
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

class VoucherRecord(Base):
    __tablename__ = "voucher_records"

    id = Column(Integer, primary_key=True, index=True)
    uploader_id = Column(Integer, ForeignKey("users.id"))
    batch_id = Column(Integer, ForeignKey("upload_batches.id"), index=True)
    filename = Column(String)
    file_path = Column(String)
    file_hash = Column(String, index=True)
    voucher_amount = Column(Float, default=0.0)
    ocr_text = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class VoucherUploadBatch(Base):
    __tablename__ = "voucher_upload_batches"

    id = Column(Integer, primary_key=True, index=True)
    uploader_id = Column(Integer, ForeignKey("users.id"))
    partner_id = Column(Integer, default=0)

    total_files = Column(Integer, default=0)
    success_files = Column(Integer, default=0)
    duplicate_files = Column(Integer, default=0)
    failed_files = Column(Integer, default=0)
    total_created_reviews = Column(Integer, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MatchReview(Base):
    __tablename__ = "match_reviews"

    id = Column(Integer, primary_key=True, index=True)
    voucher_id = Column(Integer, ForeignKey("voucher_records.id"))
    business_record_id = Column(Integer, ForeignKey("business_records.id"))
    match_status = Column(String)
    name_match = Column(String)
    bank_match = Column(String)
    amount_match = Column(String)
    score = Column(Integer)
    review_status = Column(String, default="待审核")
    created_at = Column(DateTime(timezone=True), server_default=func.now())