"""会员激活凭据、微信绑定与首笔积分入账服务。"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import hashlib
import hmac
import re
import secrets

from .channel_service import ACCEPTED_BATCH_STATUS
from .domain import (
    ActivationCredentialStatus,
    ActivationSecurityMethod,
    BusinessChannel,
    BusinessClaimStatus,
    PointsGrantStatus,
    PointsLedgerEntryType,
    calculate_points_expiry,
    is_activation_within_deadline,
    normalize_activation_security_method,
    normalize_points,
)
from ..time_utils import utc8_now


ACTIVATION_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
ACTIVATION_CODE_LENGTH = 10
ACTIVATION_CODE_HASH_ALGORITHM = "PBKDF2_SHA256"
ACTIVATION_CODE_HASH_ITERATIONS = 600_000
ACTIVATION_CODE_MAX_HASH_ITERATIONS = 2_000_000
ACTIVATION_CODE_MAX_ATTEMPTS = 5
ACTIVATION_FAILURE_MESSAGE = "激活信息无效或权益当前不可领取"
MEMBER_PUBLIC_ID_ALPHABET = ACTIVATION_CODE_ALPHABET
MEMBER_PUBLIC_ID_LENGTH = 20
DUMMY_ACTIVATION_CODE_SALT = (
    "6a26a01001bb7448c57543530551b9e8"
)


@dataclass(frozen=True)
class IssuedActivationCredential:
    """仅在签发时返回一次的激活凭据明文。"""

    business_public_no: str
    activation_code: str = field(repr=False)
    expires_at: datetime
    issue_version: int
    security_method: str


@dataclass(frozen=True)
class MallActivationResult:
    """预期中的激活失败也作为结果返回，以便提交失败次数。"""

    success: bool
    message: str
    member_public_id: str | None = None
    grant_id: int | None = None
    granted_points: Decimal | None = None
    available_points: Decimal | None = None
    activated_at: datetime | None = None
    expires_at: datetime | None = None


def _generate_activation_code() -> tuple[str, str]:
    normalized = "".join(
        secrets.choice(ACTIVATION_CODE_ALPHABET)
        for _ in range(ACTIVATION_CODE_LENGTH)
    )
    return normalized, f"{normalized[:5]}-{normalized[5:]}"


def _normalize_activation_code(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[-\s]", "", value).upper()


def _derive_activation_digest(
    normalized_code: str,
    salt_hex: str,
    iterations: int,
) -> str:
    try:
        salt = bytes.fromhex(salt_hex)
    except (TypeError, ValueError):
        return ""

    return hashlib.pbkdf2_hmac(
        "sha256",
        normalized_code.encode("utf-8"),
        salt,
        iterations,
    ).hex()


def _derive_candidate_digest(
    normalized_code: str,
    credential=None,
) -> str:
    """Always spend one current-cost hash for a well-formed request."""
    if (
        credential is not None
        and credential.security_method
        == ActivationSecurityMethod.ONE_TIME_CODE.value
        and credential.secret_algorithm
        == ACTIVATION_CODE_HASH_ALGORITHM
        and isinstance(credential.secret_iterations, int)
        and 100_000 <= credential.secret_iterations
        <= ACTIVATION_CODE_MAX_HASH_ITERATIONS
    ):
        return _derive_activation_digest(
            normalized_code,
            credential.secret_salt,
            credential.secret_iterations,
        )

    _derive_activation_digest(
        normalized_code,
        DUMMY_ACTIVATION_CODE_SALT,
        ACTIVATION_CODE_HASH_ITERATIONS,
    )
    return ""


def _generate_member_public_id() -> str:
    random_part = "".join(
        secrets.choice(MEMBER_PUBLIC_ID_ALPHABET)
        for _ in range(MEMBER_PUBLIC_ID_LENGTH)
    )
    return f"MEM-{random_part}"


def _normalize_phone(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\D", "", str(value))


def _normalize_plate(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s", "", str(value)).upper()


def _secure_text_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(
        left.encode("utf-8"),
        right.encode("utf-8"),
    )


def _failure_result(
    credential=None,
) -> MallActivationResult:
    del credential
    return MallActivationResult(
        success=False,
        message=ACTIVATION_FAILURE_MESSAGE,
    )


def _register_failed_attempt(
    credential,
    current_time: datetime,
) -> MallActivationResult:
    credential.failed_attempts = min(
        credential.failed_attempts + 1,
        credential.max_attempts,
    )
    if credential.failed_attempts >= credential.max_attempts:
        credential.status = ActivationCredentialStatus.LOCKED.value
        credential.locked_at = current_time
    credential.updated_at = current_time
    return _failure_result(credential)


def issue_activation_credential(
    db,
    *,
    business_record,
    security_method=(
        ActivationSecurityMethod.ONE_TIME_CODE.value
    ),
    max_attempts: int = ACTIVATION_CODE_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> IssuedActivationCredential:
    """
    签发或重签一条商城业务的激活凭据。

    一期只实现一次性激活码；``SMS_OTP`` 保留相同服务边界，
    在短信供应商、发送限流和回执校验接入前失败关闭。
    本函数不提交事务，调用方必须与签发审计一起提交。
    """
    method = normalize_activation_security_method(security_method)
    if method is not ActivationSecurityMethod.ONE_TIME_CODE:
        raise NotImplementedError("短信验证码通道尚未接入")
    if not 1 <= max_attempts <= 10:
        raise ValueError("最大尝试次数必须在 1 到 10 之间")
    if business_record is None or business_record.id is None:
        raise ValueError("商城业务不存在")

    from ..models import (
        BusinessRecord,
        MemberActivationCredential,
        PointsGrant,
        UploadBatch,
    )

    current_time = now or utc8_now()
    record = (
        db.query(BusinessRecord)
        .filter(BusinessRecord.id == business_record.id)
        .with_for_update()
        .one_or_none()
    )
    if record is None:
        raise ValueError("商城业务不存在")
    batch = (
        db.query(UploadBatch)
        .filter(UploadBatch.id == record.batch_id)
        .with_for_update()
        .one_or_none()
    )
    if batch is None:
        raise ValueError("商城业务所属批次不存在")
    if (
        record.redemption_mode
        != BusinessChannel.MALL_REDEMPTION.value
        or batch.redemption_mode
        != BusinessChannel.MALL_REDEMPTION.value
    ):
        raise ValueError("只有商城积分业务可以签发激活凭据")
    if batch.acceptance_status != ACCEPTED_BATCH_STATUS:
        raise ValueError("只有已承接商城批次可以签发激活凭据")
    if (
        record.claim_status
        != BusinessClaimStatus.PENDING_ACTIVATION.value
    ):
        raise ValueError("只有待激活商城业务可以签发激活凭据")
    if (
        batch.claim_deadline is None
        or current_time >= batch.claim_deadline
    ):
        raise ValueError("商城业务已到或超过激活截止时间")
    if not record.public_business_no:
        raise ValueError("商城业务缺少公开业务单号")
    if (
        not _normalize_phone(record.phone)
        or not _normalize_plate(record.plate_number)
    ):
        raise ValueError("商城业务缺少激活匹配信息")
    points = normalize_points(
        record.points_amount,
        "业务积分",
    )
    if points <= 0:
        raise ValueError("商城业务积分必须大于零")
    existing_grant = (
        db.query(PointsGrant.id)
        .filter(
            PointsGrant.business_record_id == record.id
        )
        .first()
    )
    if existing_grant is not None:
        raise ValueError("商城业务已经生成积分权益")

    normalized_code, display_code = _generate_activation_code()
    salt_hex = secrets.token_bytes(16).hex()
    digest = _derive_activation_digest(
        normalized_code,
        salt_hex,
        ACTIVATION_CODE_HASH_ITERATIONS,
    )
    credential = (
        db.query(MemberActivationCredential)
        .filter(
            MemberActivationCredential.business_record_id
            == record.id
        )
        .with_for_update()
        .one_or_none()
    )

    if credential is None:
        issue_version = 1
        credential = MemberActivationCredential(
            business_record_id=record.id,
        )
        db.add(credential)
    else:
        if credential.status == ActivationCredentialStatus.USED.value:
            raise ValueError("已使用的激活凭据不能重新签发")
        issue_version = credential.issue_version + 1

    credential.security_method = method.value
    credential.secret_algorithm = ACTIVATION_CODE_HASH_ALGORITHM
    credential.secret_iterations = ACTIVATION_CODE_HASH_ITERATIONS
    credential.secret_salt = salt_hex
    credential.secret_digest = digest
    credential.failed_attempts = 0
    credential.max_attempts = max_attempts
    credential.issue_version = issue_version
    credential.status = ActivationCredentialStatus.ACTIVE.value
    credential.issued_at = current_time
    credential.expires_at = batch.claim_deadline
    credential.used_at = None
    credential.locked_at = None
    credential.revoked_at = None
    credential.updated_at = current_time
    db.flush()

    return IssuedActivationCredential(
        business_public_no=record.public_business_no,
        activation_code=display_code,
        expires_at=batch.claim_deadline,
        issue_version=issue_version,
        security_method=method.value,
    )


def issue_one_time_activation_code(
    db,
    *,
    business_record,
    max_attempts: int = ACTIVATION_CODE_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> IssuedActivationCredential:
    """一期一次性激活码签发入口。"""
    return issue_activation_credential(
        db,
        business_record=business_record,
        security_method=ActivationSecurityMethod.ONE_TIME_CODE,
        max_attempts=max_attempts,
        now=now,
    )


def activate_mall_business(
    db,
    *,
    business_public_no,
    phone,
    plate_number,
    activation_code,
    wechat_app_id,
    openid,
    unionid=None,
    now: datetime | None = None,
) -> MallActivationResult:
    """
    校验一次性激活码并原子建立会员积分权益。

    所有可预期失败使用相同公开文案，避免枚举业务或身份。
    失败次数、锁定和成功入账都不在本函数内提交；调用方必须
    对返回结果统一提交，数据库异常则回滚整个事务。
    """
    from ..models import (
        BusinessRecord,
        Member,
        MemberActivationCredential,
        MemberWechatBinding,
        PointsAccount,
        PointsGrant,
        PointsLedgerEntry,
        UploadBatch,
    )

    current_time = now or utc8_now()
    public_no = (
        business_public_no.strip()
        if isinstance(business_public_no, str)
        else ""
    )
    app_id = (
        wechat_app_id.strip()
        if isinstance(wechat_app_id, str)
        else ""
    )
    normalized_openid = (
        openid.strip()
        if isinstance(openid, str)
        else ""
    )
    normalized_unionid = (
        unionid.strip()
        if isinstance(unionid, str) and unionid.strip()
        else None
    )
    if (
        not public_no
        or not app_id
        or not normalized_openid
        or len(app_id) > 64
        or len(normalized_openid) > 128
        or (
            normalized_unionid is not None
            and len(normalized_unionid) > 128
        )
    ):
        return _failure_result()

    normalized_code = _normalize_activation_code(activation_code)
    business = (
        db.query(BusinessRecord)
        .filter(BusinessRecord.public_business_no == public_no)
        .with_for_update()
        .one_or_none()
    )
    if business is None:
        _derive_candidate_digest(normalized_code)
        return _failure_result()

    credential = (
        db.query(MemberActivationCredential)
        .filter(
            MemberActivationCredential.business_record_id
            == business.id
        )
        .with_for_update()
        .one_or_none()
    )
    if credential is None:
        _derive_candidate_digest(normalized_code)
        return _failure_result()

    digest = _derive_candidate_digest(
        normalized_code,
        credential,
    )

    batch = (
        db.query(UploadBatch)
        .filter(UploadBatch.id == business.batch_id)
        .with_for_update()
        .one_or_none()
    )
    if (
        batch is None
        or batch.acceptance_status != ACCEPTED_BATCH_STATUS
        or batch.redemption_mode
        != BusinessChannel.MALL_REDEMPTION.value
        or business.redemption_mode
        != BusinessChannel.MALL_REDEMPTION.value
        or business.claim_status
        != BusinessClaimStatus.PENDING_ACTIVATION.value
    ):
        return _failure_result(credential)

    if (
        batch.claim_deadline is None
        or credential.expires_at < current_time
        or not is_activation_within_deadline(
            current_time,
            batch.claim_deadline,
        )
    ):
        credential.status = ActivationCredentialStatus.EXPIRED.value
        credential.updated_at = current_time
        business.claim_status = BusinessClaimStatus.EXPIRED.value
        return _failure_result(credential)

    if (
        credential.status
        != ActivationCredentialStatus.ACTIVE.value
        or credential.failed_attempts >= credential.max_attempts
    ):
        if credential.failed_attempts >= credential.max_attempts:
            credential.status = ActivationCredentialStatus.LOCKED.value
            credential.locked_at = credential.locked_at or current_time
            credential.updated_at = current_time
        return _failure_result(credential)

    submitted_phone = _normalize_phone(phone)
    stored_phone = _normalize_phone(business.phone)
    submitted_plate = _normalize_plate(plate_number)
    stored_plate = _normalize_plate(business.plate_number)
    identity_matches = (
        bool(
            submitted_phone
            and stored_phone
            and submitted_plate
            and stored_plate
        )
        and _secure_text_equal(submitted_phone, stored_phone)
        and _secure_text_equal(
            submitted_plate,
            stored_plate,
        )
    )
    code_matches = (
        len(normalized_code) == ACTIVATION_CODE_LENGTH
        and all(
            character in ACTIVATION_CODE_ALPHABET
            for character in normalized_code
        )
        and bool(digest)
        and isinstance(credential.secret_digest, str)
        and hmac.compare_digest(digest, credential.secret_digest)
    )
    if not (identity_matches and code_matches):
        return _register_failed_attempt(credential, current_time)

    existing_grant = (
        db.query(PointsGrant.id)
        .filter(PointsGrant.business_record_id == business.id)
        .first()
    )
    if existing_grant is not None:
        return _failure_result(credential)

    points = normalize_points(business.points_amount, "业务积分")
    if points <= 0:
        return _failure_result(credential)

    binding = (
        db.query(MemberWechatBinding)
        .filter(
            MemberWechatBinding.wechat_app_id == app_id,
            MemberWechatBinding.openid == normalized_openid,
        )
        .with_for_update()
        .one_or_none()
    )
    if binding is None:
        member = Member(
            member_public_id=_generate_member_public_id(),
            is_active=True,
            created_at=current_time,
            updated_at=current_time,
        )
        db.add(member)
        db.flush()
        binding = MemberWechatBinding(
            member_id=member.id,
            wechat_app_id=app_id,
            openid=normalized_openid,
            unionid=normalized_unionid,
            bound_at=current_time,
            last_login_at=current_time,
        )
        db.add(binding)
    else:
        member = (
            db.query(Member)
            .filter(Member.id == binding.member_id)
            .with_for_update()
            .one_or_none()
        )
        if member is None or not member.is_active:
            return _failure_result(credential)
        if (
            normalized_unionid is not None
            and binding.unionid is not None
            and not _secure_text_equal(
                normalized_unionid,
                binding.unionid,
            )
        ):
            return _failure_result(credential)
        if binding.unionid is None:
            binding.unionid = normalized_unionid
        binding.last_login_at = current_time

    account = (
        db.query(PointsAccount)
        .filter(PointsAccount.member_id == member.id)
        .with_for_update()
        .one_or_none()
    )
    if account is None:
        account = PointsAccount(
            member_id=member.id,
            available_points=Decimal("0.00"),
            reserved_points=Decimal("0.00"),
            version=0,
            created_at=current_time,
            updated_at=current_time,
        )
        db.add(account)
        db.flush()

    grant_expires_at = calculate_points_expiry(current_time)
    grant = PointsGrant(
        account_id=account.id,
        business_record_id=business.id,
        granted_points=points,
        available_points=points,
        reserved_points=Decimal("0.00"),
        activated_at=current_time,
        expires_at=grant_expires_at,
        status=PointsGrantStatus.ACTIVE.value,
        created_at=current_time,
        updated_at=current_time,
    )
    db.add(grant)
    db.flush()

    ledger_entry = PointsLedgerEntry(
        grant_id=grant.id,
        entry_type=PointsLedgerEntryType.GRANT.value,
        available_points_delta=points,
        reserved_points_delta=Decimal("0.00"),
        idempotency_key=f"mall-activation-grant:{business.id}",
        reference_type="BUSINESS_RECORD",
        reference_id=business.public_business_no,
        created_at=current_time,
    )
    db.add(ledger_entry)
    account.available_points = normalize_points(
        account.available_points,
    ) + points
    account.version = (account.version or 0) + 1
    account.updated_at = current_time
    business.claim_status = BusinessClaimStatus.ACTIVATED.value
    credential.status = ActivationCredentialStatus.USED.value
    credential.used_at = current_time
    credential.updated_at = current_time
    db.flush()

    return MallActivationResult(
        success=True,
        message="激活成功",
        member_public_id=member.member_public_id,
        grant_id=grant.id,
        granted_points=points,
        available_points=normalize_points(account.available_points),
        activated_at=current_time,
        expires_at=grant_expires_at,
    )
