from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from .database import Base
from .time_utils import utc8_now

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

    # 是否必须在首次登录后修改初始密码：
    # 历史账号默认 False；
    # 新创建的管理员和上传方显式设为 True。
    must_change_password = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
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

    created_at = Column(DateTime(timezone=True), default=utc8_now)

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

    created_at = Column(DateTime(timezone=True), default=utc8_now)

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
    created_at = Column(DateTime(timezone=True), default=utc8_now)

class Notification(Base):
    __tablename__ = "notifications"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    # 接收通知的管理员账号
    recipient_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    # 通知类型，例如：
    # business_batch_uploaded = 上传方提交了新业务清单
    notification_type = Column(
        String(50),
        nullable=False,
        index=True,
    )

    title = Column(
        String(120),
        nullable=False,
    )

    message = Column(
        String(500),
        nullable=False,
    )

    # 点击通知后跳转的系统内地址
    target_url = Column(
        String(500),
        nullable=True,
    )

    # 当前通知关联的业务上传批次
    related_batch_id = Column(
        Integer,
        ForeignKey("upload_batches.id"),
        nullable=True,
        index=True,
    )

    is_read = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
        index=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        default=utc8_now,
    )

    read_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )


class PromotionPage(Base):
    __tablename__ = "promotion_pages"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    # 公开访问地址：
    # /promo/{slug}
    slug = Column(
        String(80),
        nullable=False,
        unique=True,
        index=True,
    )

    company_name = Column(
        String(120),
        nullable=False,
    )

    page_title = Column(
        String(200),
        nullable=False,
    )

    subtitle = Column(
        String(300),
        nullable=True,
    )

    body_text = Column(
        Text,
        nullable=True,
    )

    cta_text = Column(
        String(80),
        nullable=True,
    )

    cta_url = Column(
        String(500),
        nullable=True,
    )

    primary_color = Column(
        String(20),
        nullable=False,
        default="#2563EB",
        server_default="#2563EB",
    )

    is_published = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
        index=True,
    )

    created_by_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
    )

    updated_by_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
    )

    created_at = Column(
        DateTime(timezone=True),
        default=utc8_now,
    )

    updated_at = Column(
        DateTime(timezone=True),
        default=utc8_now,
        onupdate=utc8_now,
    )


class PromotionPageImage(Base):
    __tablename__ = "promotion_page_images"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    promotion_page_id = Column(
        Integer,
        ForeignKey("promotion_pages.id"),
        nullable=False,
        index=True,
    )

    # logo：机构 Logo
    # hero：宣传页头图
    # content：正文配图
    image_role = Column(
        String(30),
        nullable=False,
        index=True,
    )

    image_path = Column(
        String(500),
        nullable=False,
    )

    alt_text = Column(
        String(200),
        nullable=True,
    )

    caption = Column(
        String(300),
        nullable=True,
    )

    display_order = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    uploaded_by_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
    )

    created_at = Column(
        DateTime(timezone=True),
        default=utc8_now,
    )


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
    created_at = Column(DateTime(timezone=True), default=utc8_now)


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

    created_at = Column(DateTime(timezone=True), default=utc8_now)


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
    allocation_amount = Column(
        Numeric(12, 2),
        nullable=True,
    )
    primary_reviewer_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=True,
    )
    primary_review_result = Column(String, nullable=True)
    primary_review_comment = Column(String, nullable=True)
    primary_reviewed_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    secondary_reviewer_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=True,
    )
    secondary_review_result = Column(String, nullable=True)
    secondary_review_comment = Column(String, nullable=True)
    secondary_reviewed_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), default=utc8_now)


class AdminActionLog(Base):
    __tablename__ = "admin_action_logs"

    id = Column(Integer, primary_key=True, index=True)

    admin_id = Column(Integer, ForeignKey("users.id"))

    action_type = Column(String, nullable=False)

    target_type = Column(String, nullable=True)

    target_id = Column(Integer, nullable=True)

    description = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc8_now)