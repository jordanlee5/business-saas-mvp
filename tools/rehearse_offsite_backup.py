"""Opt-in isolated PostgreSQL restore rehearsal for a received offsite recovery point."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from tools.staging_pg_backup import ROOT, StagingProject, prepare_secrets
from tools.staging_backup_job import run_job
from tools.staging_offsite_backup import pull, acknowledge, offsite_check
from tools.staging_backup_watch import record_and_notify
from tools.rehearse_scheduled_backup import SEED, CHECK

BUSINESS = '''
from tools.staging_postgres import configure
configure()
from app.database import engine, SessionLocal
from app.models import Member, ProductSku, PointsAccount, InventoryBalance
from app.mall import OrderLineRequest, execute_order_placement, execute_order_cancellation
from app.time_utils import utc8_now
from sqlalchemy import select
import json
with SessionLocal() as db:
    mid = db.scalar(select(Member.id))
    sid = db.scalar(select(ProductSku.id).order_by(ProductSku.id))
    balance = db.scalar(select(InventoryBalance).where(InventoryBalance.sku_id == sid))
    stock = balance.on_hand_quantity
now = utc8_now()
args = dict(member_id=mid, lines=(OrderLineRequest(sid, 1),), idempotency_key='offsite-restore-check', now=now)
placed = execute_order_placement(engine, **args)
assert not placed.replayed and placed.status == 'CREATED'
assert execute_order_placement(engine, **args).replayed
with SessionLocal() as db:
    account = db.scalar(select(PointsAccount))
    balance = db.scalar(select(InventoryBalance).where(InventoryBalance.sku_id == sid))
    assert str(account.available_points) == '130.00'
    assert str(account.reserved_points) == '40.00'
    assert balance.reserved_quantity == 1
execute_order_cancellation(engine, member_id=mid, order_public_id=placed.order_public_id, now=now)
with SessionLocal() as db:
    account = db.scalar(select(PointsAccount))
    balance = db.scalar(select(InventoryBalance).where(InventoryBalance.sku_id == sid))
    assert str(account.available_points) == '170.00' and str(account.reserved_points) == '0.00'
    assert balance.on_hand_quantity == stock and balance.reserved_quantity == 0
print(json.dumps(dict(placement='ok', replay='idempotent', cancellation='ok', points='170.00', stock_restored=True)))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    if os.environ.get('DATABASE_URL'):
        raise SystemExit('External database configuration refused')
    root = Path(tempfile.mkdtemp(prefix='saas-offsite-rehearsal-'))
    prepare_secrets(root / 'secrets')
    os.environ['STAGING_SECRET_DIR'] = str(root / 'secrets')
    os.environ['STAGING_APP_IMAGE'] = args.image
    suffix = uuid.uuid4().hex[:12]
    source = StagingProject('saas-pg-test-offsite-src-' + suffix)
    restored = StagingProject('saas-pg-test-offsite-dst-' + suffix)
    checks = []
    try:
        for project in (source, restored):
            assert not project.run('ps', '-a', '-q').stdout.strip()
            assert not subprocess.check_output(['docker', 'volume', 'ls', '-q', '--filter',
                'label=com.docker.compose.project=' + project.name]).strip()
        source.run('up', '-d', '--wait', '--wait-timeout', '120', 'database', 'ocr')
        source.run('run', '--rm', '--no-deps', 'app', 'python', '-m', 'tools.staging_postgres', 'initialize')
        source.run('run', '--rm', '--no-deps', '-v', str(ROOT / 'test_order_service.py') + ':/tmp/test_order_service.py:ro',
                   'app', 'python', '-', input=SEED.encode())
        source.run('up', '-d', '--no-build', '--wait', '--wait-timeout', '180', 'app')
        before = json.loads(source.run('exec', '-T', 'app', 'python', '-', input=CHECK.encode()).stdout)
        scheduled = root / 'scheduled'
        code, backup = run_job(source.name, scheduled)
        assert code == 0
        checks.append('real_postgresql_dump_and_files_created_from_virtual_data')
        # Real subprocess transport, isolated locally: no SSH credentials/network.
        # The same pull protocol is used; this stub invokes only the offsite CLI.
        transport = root / 'virtual-ssh'
        transport.write_text('#!' + sys.executable + '\n' +
            'import shlex, subprocess, sys\n' +
            'args = shlex.split(sys.argv[-1])\n' +
            'i = args.index("-m")\n' +
            'assert args[i+1] == "tools.staging_offsite_backup"\n' +
            'assert args[i+2] in ("describe", "export", "ack")\n' +
            'raise SystemExit(subprocess.call([sys.executable] + args[i:], cwd=' + repr(str(ROOT)) + '))\n')
        transport.chmod(0o700)
        config = root / 'virtual-pull.json'
        config.write_text(json.dumps(dict(project=source.name, local_root=str(root / 'second-machine'),
            ssh=str(transport), key='synthetic-unused', known_hosts='synthetic-unused',
            host='local-virtual-transport', remote_code=str(ROOT), remote_root=str(scheduled))))
        first = pull(config)
        assert first['status'] == 'ok' and first['transfer_mode'] == 'downloaded' and first['archive_bytes'] > 0
        repeated = pull(config)
        assert repeated['status'] == 'ok' and repeated['transfer_mode'] == 'reused_verified' and repeated['archive_bytes'] == 0
        assert repeated['run_id'] == first['run_id']
        checks.append('subprocess_pull_downloads_new_point_and_reuses_verified_copy_without_archive_transfer')
        received = json.loads((root / 'second-machine/last-success.json').read_text())
        assert received['run_id'] == backup['run_id']
        checks.append('received_archive_inventory_and_hashes_verified')
        assert acknowledge(scheduled, source.name, received['run_id'], received['manifest_sha256'])['status'] == 'ok'
        checks.append('source_acknowledges_verified_second_copy')
        restored.restore(root / 'second-machine' / received['backup'])
        actual = json.loads(restored.run('exec', '-T', 'app', 'python', '-', input=CHECK.encode()).stdout)
        assert actual == before
        checks.append('received_copy_restores_members_skus_points_files_health_login')
        business = json.loads(restored.run('exec', '-T', 'app', 'python', '-', input=BUSINESS.encode()).stdout)
        checks.append('restored_database_supports_exchange_idempotency_cancel_points_stock')
        unchanged = json.loads(source.run('exec', '-T', 'app', 'python', '-', input=CHECK.encode()).stdout)
        assert unchanged == before
        checks.append('original_virtual_source_unchanged_by_restore')
        notifications = []
        def capture(_secret, message):
            notifications.append(message)
            return 'delivered'
        now = datetime.now(timezone.utc)
        def watch(at):
            result = offsite_check(scheduled, source.name, now=at)
            report = dict(project=source.name, checked_at=at.isoformat(), status='ok' if result == 'ok' else 'alert',
                          checks=[dict(check='offsite_backup', code=result, status='ok' if result == 'ok' else 'alert')])
            return record_and_notify(report, root / 'watch', now=at, sender=capture)
        assert watch(now)['status'] == 'ok' and not notifications
        assert watch(now + timedelta(hours=37))['notification'] == 'delivered'
        assert watch(now + timedelta(hours=37, seconds=1))['notification'] == 'not_needed'
        # Virtual clock returns to present to model a fresh valid recovery point.
        assert watch(now)['notification'] == 'delivered' and len(notifications) == 2
        checks.append('virtual_stale_receipt_failure_deduplication_and_recovery_notifications')
        report = dict(format=1, completed_at=datetime.now(timezone.utc).isoformat(), status='passed',
                      checks=checks, restored=actual, business=business,
                      transfer=dict(first_archive_bytes=first['archive_bytes'], repeated_archive_bytes=repeated['archive_bytes']),
                      scope='New local Docker projects and synthetic data only; notification delivery captured; live cloud copy verified separately.')
        (root / 'evidence.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)
        print('Private evidence: ' + str(root / 'evidence.json'), flush=True)
    finally:
        for project in (source, restored):
            project.run('stop')


if __name__ == '__main__':
    main()
