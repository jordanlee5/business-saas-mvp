"""Read-only source -> NEW local rehearsal copy, never a live erasure command.

python -m app.privacy_rehearsal --source DB --output NEW_DIRECTORY --policy JSON --reviews JSON
Policy days are intentionally absent from repository examples. Reports contain
public order IDs and reasons, never recipient data or provider credentials.
"""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session
from .database import create_database_engine
from .models import Order, OrderPrivacyReceipt
from .order_delivery_migration_check import connect_readonly, snapshot, file_fingerprints
from .mall.order_privacy import validate_policy, preview, clear_snapshot, export_receipts, restore_receipts
from .schema_readiness import assert_database_schema_ready
from .time_utils import utc8_now


def rehearse(source, output, policy, reviews, now=None, receipt_journal=None):
    validate_policy(policy)
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file() or output.exists() or not isinstance(reviews, dict):
        raise ValueError('源库须存在，输出须为新目录，逐单复核资料须有效')
    original = file_fingerprints(source)
    output.mkdir(mode=0o700,parents=True)
    copied = output / 'rehearsal.db'
    with closing(connect_readonly(source)) as connection:
        before = snapshot(connection)
        if before['revision'] not in ('0013_order_delivery_snapshot', '0014_order_privacy'):
            raise ValueError('源库版本不支持')
        with closing(sqlite3.connect(copied)) as target:
            connection.backup(target)
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    config.attributes['database_url'] = 'sqlite:///' + copied.as_posix()
    command.upgrade(config, '0014_order_privacy')
    engine = create_database_engine(config.attributes['database_url'])
    try:
        assert_database_schema_ready(engine)
        with closing(connect_readonly(copied)) as connection:
            migrated = snapshot(connection)
        for table, data in before['tables'].items():
            if migrated['tables'][table] != data:
                raise RuntimeError('演练迁移改变了既有表')
        with engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            with Session(bind=connection) as db:
                if receipt_journal is not None:
                    restore_receipts(db,receipt_journal)
                results = []
                for order in db.query(Order).order_by(Order.id):
                    result = preview(db, order, policy, reviews, now or utc8_now())
                    if result['eligible']:
                        result = clear_snapshot(db, order, policy, reviews, now or utc8_now())
                    results.append(result)
                receipts=export_receipts(db)
                connection.commit()
        with closing(connect_readonly(copied)) as connection:
            after = snapshot(connection)
        # The erasure operation is limited to snapshots and minimal receipts.
        for table, data in migrated['tables'].items():
            if table not in {'order_delivery_snapshots', 'order_privacy_receipts'} and after['tables'][table] != data:
                raise RuntimeError('清理改变了非目标业务表')
        assert file_fingerprints(source) == original
        report = {'source_unchanged': True, 'migration_existing_tables_preserved': True,
                  'scope': 'isolated_copy_only', 'policy_version': policy['version'],
                  'approved_policy': policy['approved'], 'orders': results,
                  'counts': {'reviewed': len(results), 'cleared': sum(r['reason'] == 'cleared' for r in results)},
                  'non_target_tables_unchanged': True}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (output / 'erasure-receipts.json').write_text(json.dumps(receipts,ensure_ascii=False,indent=2),encoding='utf-8')
        return report
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'output', 'policy', 'reviews'):
        parser.add_argument('--'+name, required=True, type=Path)
    parser.add_argument('--receipt-journal',type=Path)
    args = parser.parse_args()
    try:
        result = rehearse(args.source, args.output, json.loads(args.policy.read_text('utf-8')),
                          json.loads(args.reviews.read_text('utf-8')),
                          receipt_journal=json.loads(args.receipt_journal.read_text('utf-8')) if args.receipt_journal else None)
        print(json.dumps({key: value for key, value in result.items() if key != 'orders'}, ensure_ascii=False))
    except Exception:
        # No exception values, SQL parameters, or file contents in shared output.
        print('演练失败；未对源库执行写操作。请检查本机配置和演练目录。')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
