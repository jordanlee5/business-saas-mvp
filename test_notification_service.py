import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.admin_permissions import (
    OPERATOR,
    PRIMARY_REVIEWER,
    SUPER_ADMIN,
)
from app.database import Base
from app.models import (
    Notification,
    UploadBatch,
    User,
)
from app.notification_service import (
    BUSINESS_BATCH_UPLOADED,
    create_business_batch_uploaded_notifications,
    get_business_batch_notification_recipients,
    get_unread_business_batch_notifications,
    mark_notification_as_read,
)


class NotificationServiceTests(
    unittest.TestCase
):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={
                "check_same_thread": False,
            },
        )

        Base.metadata.create_all(
            bind=self.engine
        )

        test_session = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=self.engine,
        )

        self.db = test_session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_user(
        self,
        username,
        role,
        admin_level=None,
        is_active=True,
    ):
        user = User(
            username=username,
            password_hash="test-password-hash",
            role=role,
            admin_level=admin_level,
            is_active=is_active,
            service_rate=0.0,
            upstream_cost_rate=0.0,
        )

        self.db.add(user)
        self.db.flush()

        return user

    def add_batch(
        self,
        uploader,
        filename="business.xlsx",
        success_rows=3,
    ):
        batch = UploadBatch(
            user_id=uploader.id,
            filename=filename,
            total_rows=success_rows,
            success_rows=success_rows,
            failed_rows=0,
            acceptance_status="待承接",
        )

        self.db.add(batch)
        self.db.flush()

        return batch

    def test_only_active_batch_managers_receive_notifications(
        self,
    ):
        super_admin = self.add_user(
            "super_admin",
            "admin",
            SUPER_ADMIN,
        )
        operator = self.add_user(
            "operator",
            "admin",
            OPERATOR,
        )
        self.add_user(
            "primary_reviewer",
            "admin",
            PRIMARY_REVIEWER,
        )
        self.add_user(
            "inactive_operator",
            "admin",
            OPERATOR,
            is_active=False,
        )
        self.add_user(
            "partner",
            "partner",
        )

        recipients = (
            get_business_batch_notification_recipients(
                self.db
            )
        )

        self.assertEqual(
            {
                recipient.id
                for recipient in recipients
            },
            {
                super_admin.id,
                operator.id,
            },
        )

    def test_partner_upload_creates_notification_payloads(
        self,
    ):
        uploader = self.add_user(
            "partner_a",
            "partner",
        )
        super_admin = self.add_user(
            "super_admin",
            "admin",
            SUPER_ADMIN,
        )
        operator = self.add_user(
            "operator",
            "admin",
            OPERATOR,
        )
        self.add_user(
            "primary_reviewer",
            "admin",
            PRIMARY_REVIEWER,
        )

        batch = self.add_batch(
            uploader,
            filename="八月业务清单.xlsx",
            success_rows=3,
        )

        notifications = (
            create_business_batch_uploaded_notifications(
                self.db,
                batch=batch,
                uploader=uploader,
            )
        )

        self.db.flush()

        self.assertEqual(
            len(notifications),
            2,
        )
        self.assertEqual(
            {
                notification.recipient_id
                for notification in notifications
            },
            {
                super_admin.id,
                operator.id,
            },
        )

        for notification in notifications:
            self.assertEqual(
                notification.notification_type,
                BUSINESS_BATCH_UPLOADED,
            )
            self.assertEqual(
                notification.title,
                "新业务待承接",
            )
            self.assertEqual(
                notification.message,
                "partner_a上传了 3 条新业务，等待承接哦～",
            )
            self.assertEqual(
                notification.target_url,
                (
                    f"/business-records?"
                    f"batch_id={batch.id}"
                ),
            )
            self.assertEqual(
                notification.related_batch_id,
                batch.id,
            )
            self.assertFalse(
                notification.is_read
            )
            self.assertIsNone(
                notification.read_at
            )

    def test_zero_success_rows_does_not_create_notifications(
        self,
    ):
        uploader = self.add_user(
            "partner_empty",
            "partner",
        )
        self.add_user(
            "super_admin",
            "admin",
            SUPER_ADMIN,
        )

        batch = self.add_batch(
            uploader,
            filename="空业务清单.xlsx",
            success_rows=0,
        )

        notifications = (
            create_business_batch_uploaded_notifications(
                self.db,
                batch=batch,
                uploader=uploader,
            )
        )

        self.db.flush()

        self.assertEqual(
            notifications,
            [],
        )
        self.assertEqual(
            self.db.query(Notification).count(),
            0,
        )

    def test_unread_notifications_and_mark_read_are_recipient_isolated(
        self,
    ):
        uploader = self.add_user(
            "partner_read_test",
            "partner",
        )
        super_admin = self.add_user(
            "super_admin",
            "admin",
            SUPER_ADMIN,
        )
        operator = self.add_user(
            "operator",
            "admin",
            OPERATOR,
        )

        batch = self.add_batch(
            uploader,
        )

        notifications = (
            create_business_batch_uploaded_notifications(
                self.db,
                batch=batch,
                uploader=uploader,
            )
        )

        self.db.flush()

        super_admin_notification = next(
            notification
            for notification in notifications
            if (
                notification.recipient_id
                == super_admin.id
            )
        )

        super_admin_unread = (
            get_unread_business_batch_notifications(
                self.db,
                super_admin.id,
            )
        )
        operator_unread = (
            get_unread_business_batch_notifications(
                self.db,
                operator.id,
            )
        )

        self.assertEqual(
            [
                notification.id
                for notification
                in super_admin_unread
            ],
            [
                super_admin_notification.id
            ],
        )
        self.assertEqual(
            len(operator_unread),
            1,
        )

        wrong_recipient_result = (
            mark_notification_as_read(
                self.db,
                notification_id=(
                    super_admin_notification.id
                ),
                recipient_id=operator.id,
            )
        )

        self.db.flush()

        self.assertIsNone(
            wrong_recipient_result
        )
        self.assertEqual(
            len(
                get_unread_business_batch_notifications(
                    self.db,
                    super_admin.id,
                )
            ),
            1,
        )

        marked_notification = (
            mark_notification_as_read(
                self.db,
                notification_id=(
                    super_admin_notification.id
                ),
                recipient_id=super_admin.id,
            )
        )

        self.db.flush()

        self.assertIsNotNone(
            marked_notification
        )
        self.assertTrue(
            marked_notification.is_read
        )
        self.assertIsNotNone(
            marked_notification.read_at
        )
        self.assertEqual(
            get_unread_business_batch_notifications(
                self.db,
                super_admin.id,
            ),
            [],
        )
        self.assertEqual(
            len(
                get_unread_business_batch_notifications(
                    self.db,
                    operator.id,
                )
            ),
            1,
        )

    def test_admin_upload_does_not_create_notifications(
        self,
    ):
        administrator = self.add_user(
            "super_admin",
            "admin",
            SUPER_ADMIN,
        )
        self.add_user(
            "operator",
            "admin",
            OPERATOR,
        )

        batch = self.add_batch(
            administrator,
        )

        notifications = (
            create_business_batch_uploaded_notifications(
                self.db,
                batch=batch,
                uploader=administrator,
            )
        )

        self.db.flush()

        self.assertEqual(
            notifications,
            [],
        )
        self.assertEqual(
            self.db.query(Notification).count(),
            0,
        )


if __name__ == "__main__":
    unittest.main()