from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from .database import Base
from .mall.domain import (
    ActivationCredentialStatus,
    ActivationSecurityMethod,
    BusinessChannel,
    PointsGrantStatus,
)
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
        server_default=true(),
    )

    # 是否必须在首次登录后修改初始密码：
    # 历史账号默认 False；
    # 新创建的管理员和上传方显式设为 True。
    must_change_password = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
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
    __table_args__ = (
        CheckConstraint(
            "redemption_mode IN "
            "('CASH_REBATE', 'MALL_REDEMPTION')",
            name="ck_business_records_redemption_mode",
        ),
        CheckConstraint(
            "(redemption_mode = 'CASH_REBATE' "
            "AND claim_status IS NULL) OR "
            "(redemption_mode = 'MALL_REDEMPTION' "
            "AND claim_status IS NOT NULL "
            "AND claim_status IN "
            "('PENDING_ACTIVATION', 'ACTIVATED', "
            "'EXPIRED', 'FROZEN'))",
            name="ck_business_records_claim_status",
        ),
    )
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

    # 一条业务只能进入现金返现或积分商城渠道。
    # 历史数据及未显式选择的新数据继续默认现金返现。
    redemption_mode = Column(
        String(30),
        nullable=False,
        default=BusinessChannel.CASH_REBATE.value,
        server_default=BusinessChannel.CASH_REBATE.value,
        index=True,
    )

    # 仅商城渠道业务使用；现金返现业务必须保持为空。
    claim_status = Column(
        String(30),
        nullable=True,
        default=None,
        index=True,
    )

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
    __table_args__ = (
        CheckConstraint(
            "redemption_mode IN "
            "('CASH_REBATE', 'MALL_REDEMPTION')",
            name="ck_upload_batches_redemption_mode",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    filename = Column(String)
    total_rows = Column(Integer, default=0)
    success_rows = Column(Integer, default=0)
    failed_rows = Column(Integer, default=0)
    acceptance_status = Column(String, default="待承接")
    redemption_mode = Column(
        String(30),
        nullable=False,
        default=BusinessChannel.CASH_REBATE.value,
        server_default=BusinessChannel.CASH_REBATE.value,
        index=True,
    )
    claim_deadline = Column(
        DateTime(timezone=True),
        nullable=True,
    )
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
        server_default=false(),
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
        server_default=false(),
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
    batch_id = Column(
        Integer,
        ForeignKey("voucher_upload_batches.id"),
        index=True,
    )
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


class Member(Base):
    """小程序会员主体；公开编号不能作为登录凭证。"""

    __tablename__ = "members"

    id = Column(Integer, primary_key=True, index=True)
    member_public_id = Column(
        String(32),
        nullable=False,
        unique=True,
        index=True,
    )
    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc8_now,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc8_now,
        onupdate=utc8_now,
    )


class MemberWechatBinding(Base):
    """会员与微信小程序身份的服务端绑定。"""

    __tablename__ = "member_wechat_bindings"
    __table_args__ = (
        UniqueConstraint(
            "wechat_app_id",
            "openid",
            name=(
                "uq_member_wechat_bindings_app_openid"
            ),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    member_id = Column(
        Integer,
        ForeignKey("members.id"),
        nullable=False,
        index=True,
    )
    wechat_app_id = Column(
        String(64),
        nullable=False,
    )
    openid = Column(
        String(128),
        nullable=False,
    )
    unionid = Column(
        String(128),
        nullable=True,
        index=True,
    )
    bound_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc8_now,
    )
    last_login_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )


class MemberActivationCredential(Base):
    """商城业务的一次性激活凭据；绝不保存激活码明文。"""

    __tablename__ = "member_activation_credentials"
    __table_args__ = (
        CheckConstraint(
            "security_method IN ('ONE_TIME_CODE', 'SMS_OTP')",
            name="ck_member_activation_credentials_method",
        ),
        CheckConstraint(
            "status IN "
            "('ACTIVE', 'USED', 'LOCKED', 'EXPIRED', 'REVOKED')",
            name="ck_member_activation_credentials_status",
        ),
        CheckConstraint(
            "failed_attempts >= 0 AND failed_attempts <= max_attempts",
            name="ck_member_activation_credentials_attempts",
        ),
        CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 10",
            name="ck_member_activation_credentials_max_attempts",
        ),
        CheckConstraint(
            "issue_version >= 1",
            name="ck_member_activation_credentials_issue_version",
        ),
        CheckConstraint(
            "secret_iterations >= 100000 "
            "AND secret_iterations <= 2000000",
            name="ck_member_activation_credentials_iterations",
        ),
        CheckConstraint(
            "length(secret_salt) = 32 "
            "AND length(secret_digest) = 64",
            name="ck_member_activation_credentials_hash_shape",
        ),
        CheckConstraint(
            "expires_at > issued_at",
            name="ck_member_activation_credentials_expiry",
        ),
        CheckConstraint(
            "status <> 'USED' OR used_at IS NOT NULL",
            name="ck_member_activation_credentials_used_time",
        ),
        CheckConstraint(
            "status <> 'LOCKED' OR locked_at IS NOT NULL",
            name="ck_member_activation_credentials_locked_time",
        ),
        CheckConstraint(
            "status <> 'REVOKED' OR revoked_at IS NOT NULL",
            name="ck_member_activation_credentials_revoked_time",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    business_record_id = Column(
        Integer,
        ForeignKey("business_records.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    security_method = Column(
        String(30),
        nullable=False,
        default=ActivationSecurityMethod.ONE_TIME_CODE.value,
        server_default=ActivationSecurityMethod.ONE_TIME_CODE.value,
        index=True,
    )
    secret_algorithm = Column(
        String(30),
        nullable=False,
        default="PBKDF2_SHA256",
        server_default="PBKDF2_SHA256",
    )
    secret_iterations = Column(
        Integer,
        nullable=False,
        default=600000,
        server_default="600000",
    )
    secret_salt = Column(String(64), nullable=False)
    secret_digest = Column(String(64), nullable=False)
    failed_attempts = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    max_attempts = Column(
        Integer,
        nullable=False,
        default=5,
        server_default="5",
    )
    issue_version = Column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    status = Column(
        String(20),
        nullable=False,
        default=ActivationCredentialStatus.ACTIVE.value,
        server_default=ActivationCredentialStatus.ACTIVE.value,
        index=True,
    )
    issued_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc8_now,
    )
    expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    used_at = Column(DateTime(timezone=True), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc8_now,
        onupdate=utc8_now,
    )


class PointsAccount(Base):
    """会员积分汇总缓存；必须能够由不可变流水重算。"""

    __tablename__ = "points_accounts"
    __table_args__ = (
        CheckConstraint(
            "available_points >= 0",
            name="ck_points_accounts_available_nonnegative",
        ),
        CheckConstraint(
            "reserved_points >= 0",
            name="ck_points_accounts_reserved_nonnegative",
        ),
        CheckConstraint(
            "version >= 0",
            name="ck_points_accounts_version_nonnegative",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    member_id = Column(
        Integer,
        ForeignKey("members.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    available_points = Column(
        Numeric(18, 2),
        nullable=False,
        default=0,
        server_default="0",
    )
    reserved_points = Column(
        Numeric(18, 2),
        nullable=False,
        default=0,
        server_default="0",
    )
    version = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc8_now,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc8_now,
        onupdate=utc8_now,
    )


class PointsGrant(Base):
    """每条已激活商城业务形成的独立积分批次。"""

    __tablename__ = "points_grants"
    __table_args__ = (
        CheckConstraint(
            "granted_points > 0",
            name="ck_points_grants_granted_positive",
        ),
        CheckConstraint(
            "available_points >= 0",
            name="ck_points_grants_available_nonnegative",
        ),
        CheckConstraint(
            "reserved_points >= 0",
            name="ck_points_grants_reserved_nonnegative",
        ),
        CheckConstraint(
            "available_points + reserved_points <= granted_points",
            name="ck_points_grants_balance_within_grant",
        ),
        CheckConstraint(
            "expires_at > activated_at",
            name="ck_points_grants_expiry_after_activation",
        ),
        CheckConstraint(
            "status IN "
            "('ACTIVE', 'EXHAUSTED', 'EXPIRED', 'FROZEN')",
            name="ck_points_grants_status",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(
        Integer,
        ForeignKey("points_accounts.id"),
        nullable=False,
        index=True,
    )
    business_record_id = Column(
        Integer,
        ForeignKey("business_records.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    granted_points = Column(
        Numeric(18, 2),
        nullable=False,
    )
    available_points = Column(
        Numeric(18, 2),
        nullable=False,
    )
    reserved_points = Column(
        Numeric(18, 2),
        nullable=False,
        default=0,
        server_default="0",
    )
    activated_at = Column(
        DateTime(timezone=True),
        nullable=False,
    )
    expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    status = Column(
        String(30),
        nullable=False,
        default=PointsGrantStatus.ACTIVE.value,
        server_default=PointsGrantStatus.ACTIVE.value,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc8_now,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc8_now,
        onupdate=utc8_now,
    )


class PointsLedgerEntry(Base):
    """只能追加的积分流水；余额缓存不能替代此表。"""

    __tablename__ = "points_ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "entry_type IN "
            "('GRANT', 'RESERVE', 'RELEASE', 'CONSUME', "
            "'REFUND', 'EXPIRE', 'ADJUST')",
            name="ck_points_ledger_entries_type",
        ),
        CheckConstraint(
            "available_points_delta <> 0 "
            "OR reserved_points_delta <> 0",
            name="ck_points_ledger_entries_nonzero_delta",
        ),
        CheckConstraint(
            "entry_type <> 'ADJUST' OR "
            "(actor_admin_id IS NOT NULL AND reason IS NOT NULL "
            "AND length(trim(reason)) > 0)",
            name="ck_points_ledger_entries_adjustment_audit",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    grant_id = Column(
        Integer,
        ForeignKey("points_grants.id"),
        nullable=False,
        index=True,
    )
    entry_type = Column(
        String(30),
        nullable=False,
        index=True,
    )
    available_points_delta = Column(
        Numeric(18, 2),
        nullable=False,
        default=0,
        server_default="0",
    )
    reserved_points_delta = Column(
        Numeric(18, 2),
        nullable=False,
        default=0,
        server_default="0",
    )
    idempotency_key = Column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
    )
    reference_type = Column(
        String(50),
        nullable=True,
    )
    reference_id = Column(
        String(64),
        nullable=True,
    )
    actor_admin_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    reason = Column(
        String(500),
        nullable=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc8_now,
    )
