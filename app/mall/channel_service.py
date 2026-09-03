from dataclasses import dataclass
from datetime import datetime, time

from .domain import (
    BusinessChannel,
    BusinessClaimStatus,
    normalize_business_channel,
)
from ..time_utils import utc8_now


BUSINESS_CHANNEL_ALL = "ALL"
PENDING_BATCH_STATUS = "待承接"
ACCEPTED_BATCH_STATUS = "已承接"
REJECTED_BATCH_STATUS = "已拒绝"
BUSINESS_CHANNEL_LABELS = {
    BusinessChannel.CASH_REBATE.value: "现金返现",
    BusinessChannel.MALL_REDEMPTION.value: "商城积分",
}
BUSINESS_CLAIM_STATUS_LABELS = {
    BusinessClaimStatus.PENDING_ACTIVATION.value: "待激活",
    BusinessClaimStatus.ACTIVATED.value: "已激活",
    BusinessClaimStatus.EXPIRED.value: "已过期",
    BusinessClaimStatus.FROZEN.value: "已冻结",
}


@dataclass(frozen=True)
class UploadChannelSnapshot:
    redemption_mode: str
    claim_deadline: datetime | None
    claim_status: str | None


def business_channel_label(value) -> str:
    """Return a stable Chinese label without silently accepting unknown data."""
    try:
        channel = normalize_business_channel(value)
    except ValueError:
        return "未知渠道"

    return BUSINESS_CHANNEL_LABELS[channel.value]


def business_claim_status_label(value) -> str:
    if value is None:
        return "-"

    return BUSINESS_CLAIM_STATUS_LABELS.get(
        value,
        "未知状态",
    )


def normalize_business_channel_filter(value) -> str:
    """Normalize list/export filters; invalid values fail closed to all."""
    if value == BUSINESS_CHANNEL_ALL:
        return BUSINESS_CHANNEL_ALL

    try:
        return normalize_business_channel(value).value
    except ValueError:
        return BUSINESS_CHANNEL_ALL


def build_upload_channel_snapshot(
    redemption_mode,
    claim_deadline_text="",
    *,
    now: datetime | None = None,
) -> UploadChannelSnapshot:
    """
    Validate upload-channel form values and build immutable batch/record fields.

    Mall deadlines are entered as a UTC+8 calendar date and stored as the end
    of that day. Cash uploads must not carry a mall claim deadline.
    """
    channel = normalize_business_channel(redemption_mode)
    deadline_text = (
        claim_deadline_text.strip()
        if isinstance(claim_deadline_text, str)
        else ""
    )

    if channel is BusinessChannel.CASH_REBATE:
        if deadline_text:
            raise ValueError("现金返现渠道不能设置商城领取截止日")

        return UploadChannelSnapshot(
            redemption_mode=channel.value,
            claim_deadline=None,
            claim_status=None,
        )

    if not deadline_text:
        raise ValueError("商城积分渠道必须设置领取截止日")

    try:
        deadline_date = datetime.strptime(
            deadline_text,
            "%Y-%m-%d",
        ).date()
    except ValueError as exc:
        raise ValueError("领取截止日必须是有效日期") from exc

    claim_deadline = datetime.combine(
        deadline_date,
        time.max,
    )
    current_time = now or utc8_now()

    if claim_deadline < current_time:
        raise ValueError("商城领取截止日不能早于今天")

    return UploadChannelSnapshot(
        redemption_mode=channel.value,
        claim_deadline=claim_deadline,
        claim_status=(
            BusinessClaimStatus.PENDING_ACTIVATION.value
        ),
    )


def correct_pending_batch_channel(
    db,
    *,
    batch,
    redemption_mode,
    claim_deadline_text="",
    now: datetime | None = None,
) -> tuple[UploadChannelSnapshot, int]:
    """
    Correct a mistaken channel before acceptance and keep all snapshots equal.

    This function does not commit. The caller must add its audit row and commit
    the correction in the same transaction.
    """
    if batch is None:
        raise ValueError("上传批次不存在")

    if batch.acceptance_status != PENDING_BATCH_STATUS:
        raise ValueError("只有待承接批次允许纠正渠道")

    snapshot = build_upload_channel_snapshot(
        redemption_mode,
        claim_deadline_text,
        now=now,
    )

    if snapshot.redemption_mode == batch.redemption_mode:
        raise ValueError("新渠道必须与当前渠道不同")

    batch.redemption_mode = snapshot.redemption_mode
    batch.claim_deadline = snapshot.claim_deadline

    from ..models import BusinessRecord

    records = (
        db.query(BusinessRecord)
        .filter(BusinessRecord.batch_id == batch.id)
        .all()
    )

    for record in records:
        record.redemption_mode = snapshot.redemption_mode
        record.claim_status = snapshot.claim_status

    return snapshot, len(records)


def decide_pending_batch(batch, target_status: str) -> None:
    """Accept or reject a batch only from the pending state."""
    if batch is None:
        raise ValueError("上传批次不存在")

    if batch.acceptance_status != PENDING_BATCH_STATUS:
        raise ValueError("只有待承接批次允许执行承接或拒绝")

    if target_status not in (
        ACCEPTED_BATCH_STATUS,
        REJECTED_BATCH_STATUS,
    ):
        raise ValueError("不支持的批次处理状态")

    batch.acceptance_status = target_status


def batch_revert_block_reason(db, *, batch) -> str | None:
    """Return why a processed batch cannot safely return to pending."""
    if batch is None:
        return "上传批次不存在"

    previous_status = batch.acceptance_status

    if previous_status == PENDING_BATCH_STATUS:
        return "待承接批次没有可撤销的处理结果"

    if previous_status not in (
        ACCEPTED_BATCH_STATUS,
        REJECTED_BATCH_STATUS,
    ):
        return "当前批次状态不支持撤销"

    if previous_status == ACCEPTED_BATCH_STATUS:
        from ..models import (
            BusinessRecord,
            MatchReview,
            PointsGrant,
        )

        records = (
            db.query(
                BusinessRecord.id,
                BusinessRecord.claim_status,
            )
            .filter(BusinessRecord.batch_id == batch.id)
            .all()
        )
        record_ids = [record.id for record in records]

        if record_ids:
            has_reviews = (
                db.query(MatchReview.id)
                .filter(
                    MatchReview.business_record_id.in_(record_ids)
                )
                .first()
                is not None
            )
            if has_reviews:
                return "该批次已经产生凭证匹配或审核记录，不能撤销承接"

            has_points_grants = (
                db.query(PointsGrant.id)
                .filter(
                    PointsGrant.business_record_id.in_(record_ids)
                )
                .first()
                is not None
            )
            if has_points_grants:
                return "该批次已经产生积分权益，不能撤销承接"

        if batch.redemption_mode == BusinessChannel.MALL_REDEMPTION.value:
            invalid_claim_state = any(
                record.claim_status
                != BusinessClaimStatus.PENDING_ACTIVATION.value
                for record in records
            )
            if invalid_claim_state:
                return "该商城批次已经进入领取处理，不能撤销承接"

    return None


def revert_batch_decision(db, *, batch) -> str:
    """
    Revert an accepted/rejected decision back to pending when still safe.

    Rejected batches have no downstream work and can be restored directly.
    Accepted cash batches are blocked once matching reviews exist. Accepted
    mall batches are blocked once activation state or a points grant exists.
    This function does not commit so the caller can write the audit row in the
    same transaction.
    """
    block_reason = batch_revert_block_reason(db, batch=batch)
    if block_reason:
        raise ValueError(block_reason)

    previous_status = batch.acceptance_status
    batch.acceptance_status = PENDING_BATCH_STATUS
    return previous_status
