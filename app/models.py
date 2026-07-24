from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.sql import func
from .database import Base

# 用户表
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)
    role = Column(String)  # admin / partner

    # 管理员级别：
    # super_admin = 超级管理员
    # primary_reviewer = 初审管理员
    # secondary_reviewer = 复核管理员
    # operator = 运营管理员
    # partner 账号保持为空
    admin_level = Column(
        String(30),
        nullable=True,
        default=None,
    )

    # 账号是否启用：
    # True = 启用，可以登录和使用系统
    # False = 停用，保留历史数据但禁止继续使用账号
    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
    )
    service_rate = Column(Float, default=0.0)
    upstream_cost_rate = Column(Float, default=0.0)

    # 下游服务费率计算方式：
    # external = 外扣
    # internal = 内扣
    service_rate_mode = Column(
        String(20),
        nullable=False,
        default="external",
        server_default="external",
    )

    # 上游成本费率计算方式：
    # external = 外扣
    # internal = 内扣
    upstream_cost_rate_mode = Column(
        String(20),
        nullable=False,
        default="external",
        server_default="external",
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())

# 业务数据表
class BusinessRecord(Base):
    __tablename__ = "business_records"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    batch_id = Column(Integer, ForeignKey("upload_batches.id"))
    
    # 旧业务单号：暂时保留，用于历史兼容。
    business_no = Column(String, index=True)
    
    # 新公开业务单号：后续用于页面、导出和凭证文件命名。
    public_business_no = Column(
        String(32),
        unique=True,
        index=True,
        nullable=True,
    )
    name = Column(String)
    phone = Column(String, index=True)
    plate_number = Column(String, index=True)
    points_amount = Column(Float)
    bank_card = Column(String, index=True)

    record_service_rate = Column(Float, default=0.0)
    record_upstream_cost_rate = Column(Float, default=0.0)

    # 上传业务时保存下游服务费率计算方式快照：
    # external = 外扣
    # internal = 内扣
    record_service_rate_mode = Column(
        String(20),
        nullable=False,
        default="external",
        server_default="external",
    )

    # 上传业务时保存上游成本费率计算方式快照：
    # external = 外扣
    # internal = 内扣
    record_upstream_cost_rate_mode = Column(
        String(20),
        nullable=False,
        default="external",
        server_default="external",
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    @property
    def display_business_no(self) -> str:
        """
        对外展示使用公开业务单号。

        如果个别历史异常数据缺少公开编号，
        则临时回退到旧业务单号，避免页面和导出报错。
        """
        if self.public_business_no:
            return self.public_business_no

        if self.business_no:
            return self.business_no

        if self.id is not None:
            return f"BR-{self.id}"

        return "未生成业务单号"

class UploadBatch(Base):
    __tablename__ = "upload_batches"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    filename = Column(String)
    total_rows = Column(Integer, default=0)
    success_rows = Column(Integer, default=0)
    failed_rows = Column(Integer, default=0)
    acceptance_status = Column(String, default="待承接")
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


class AdminActionLog(Base):
    __tablename__ = "admin_action_logs"

    id = Column(Integer, primary_key=True, index=True)

    admin_id = Column(Integer, ForeignKey("users.id"))

    action_type = Column(String, nullable=False)

    target_type = Column(String, nullable=True)

    target_id = Column(Integer, nullable=True)

    description = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())