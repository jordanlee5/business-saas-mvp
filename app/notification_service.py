from .admin_permissions import (
    can_manage_business_batches,
)
from .models import (
    Notification,
    UploadBatch,
    User,
)
from .time_utils import utc8_now

BUSINESS_BATCH_UPLOADED = (
    "business_batch_uploaded"
)


def get_business_batch_notification_recipients(
    db,
):
    """
    返回需要接收新业务批次通知的管理员。

    当前只通知：
    1. 账号仍处于启用状态；
    2. 拥有业务批次承接/拒绝权限的管理员。

    按现有权限体系，对应超级管理员和运营管理员。
    """
    active_administrators = (
        db.query(User)
        .filter(User.role == "admin")
        .filter(User.is_active.is_(True))
        .order_by(User.id.asc())
        .all()
    )

    return [
        administrator
        for administrator in active_administrators
        if can_manage_business_batches(
            administrator
        )
    ]


def get_unread_business_batch_notifications(
    db,
    recipient_id,
):
    """
    返回当前管理员自己的、仍待处理的新业务通知。

    只显示：
    1. 当前账号尚未读取；
    2. 通知类型为新业务上传；
    3. 对应批次仍处于待承接状态。
    """
    return (
        db.query(Notification)
        .join(
            UploadBatch,
            UploadBatch.id
            == Notification.related_batch_id,
        )
        .filter(
            Notification.recipient_id
            == recipient_id,
            Notification.notification_type
            == BUSINESS_BATCH_UPLOADED,
            Notification.is_read.is_(False),
            UploadBatch.acceptance_status
            == "待承接",
        )
        .order_by(
            Notification.created_at.desc(),
            Notification.id.desc(),
        )
        .all()
    )


def mark_notification_as_read(
    db,
    *,
    notification_id,
    recipient_id,
):
    """
    仅允许接收人本人将通知标记为已读。

    本函数不主动 commit，
    由调用方统一提交事务。
    """
    notification = (
        db.query(Notification)
        .filter(
            Notification.id
            == notification_id,
            Notification.recipient_id
            == recipient_id,
        )
        .first()
    )

    if notification is None:
        return None

    if not notification.is_read:
        notification.is_read = True
        notification.read_at = utc8_now()

    return notification


def create_business_batch_uploaded_notifications(
    db,
    *,
    batch,
    uploader,
):
    """
    为上传方提交的新业务批次创建站内通知。

    本函数只向当前数据库事务添加通知，
    不主动 commit，由上传业务的调用方统一提交，
    避免业务数据与通知状态不一致。
    """
    success_rows = int(
        getattr(batch, "success_rows", 0)
        or 0
    )

    # 只有上传方成功导入至少一条业务时才通知管理员。
    if (
        getattr(uploader, "role", None)
        != "partner"
        or success_rows <= 0
    ):
        return []

    recipients = (
        get_business_batch_notification_recipients(
            db
        )
    )

    uploader_name = (
        getattr(uploader, "username", None)
        or "未知上传方"
    )


    notifications = [
        Notification(
            recipient_id=recipient.id,
            notification_type=(
                BUSINESS_BATCH_UPLOADED
            ),
            title="新业务待承接",
            message=(
                f"{uploader_name}上传了 "
                f"{success_rows} 条新业务，"
                f"等待承接哦～"
            ),
            target_url=(
                f"/business-records?"
                f"batch_id={batch.id}"
            ),
            related_batch_id=batch.id,
            is_read=False,
        )
        for recipient in recipients
    ]

    if notifications:
        db.add_all(notifications)

    return notifications