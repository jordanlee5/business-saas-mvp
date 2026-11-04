"""Operator control of the same private configuration read by checkout and handoff."""
from contextlib import contextmanager
import ctypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from ..admin_permissions import can_manage_mall_catalog
from ..models import Supplier, User
from ..time_utils import UTC8_TIMEZONE
from .fulfillment_notice import settings


def profile_revision(supplier_id, profile):
    return hashlib.sha256(json.dumps([supplier_id, profile], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def _settings_lock(source):
    # Keep the lock on a separate inode so atomic replacement does not release it.
    with source.with_name(source.name + ".lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _publish(source, config):
    content = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode()
    if len(content) > 131072:
        raise ValueError("配置超过容量限制，未保存")
    out = tempfile.NamedTemporaryFile(dir=source.parent, delete=False)
    temp = Path(out.name)
    try:
        with out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        if os.name == "nt":
            # ReplaceFile preserves the destination security descriptor on Windows.
            replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
            if not replace(str(source), str(temp), None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            original, replacement = source.stat(), temp.stat()
            if (original.st_uid, original.st_gid) != (replacement.st_uid, replacement.st_gid):
                os.chown(temp, original.st_uid, original.st_gid)
            shutil.copystat(source, temp)
            os.replace(temp, source)
    finally:
        temp.unlink(missing_ok=True)


def set_enabled(db, actor_id, supplier_id, enabled, expected_revision):
    if db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    actor = db.query(User).filter_by(id=actor_id).with_for_update().one_or_none()
    if not actor or not actor.is_active or not can_manage_mall_catalog(actor):
        raise PermissionError("当前账号无权管理供应商委托")
    supplier = db.query(Supplier).filter_by(id=supplier_id).with_for_update().one_or_none()
    if not supplier or (enabled and not supplier.is_active):
        raise ValueError("供应商不存在或档案已停用")
    path = os.environ.get("MALL_FULFILLMENT_SETTINGS")
    if not path:
        raise ValueError("尚未配置受托发货资料")
    source = Path(path).resolve(strict=True)
    with _settings_lock(source):
        config = settings()
        profile = config and config["suppliers"].get(supplier.supplier_public_id)
        if not profile:
            raise ValueError("该供应商尚无委托依据")
        if profile_revision(supplier_id, profile) != expected_revision:
            raise ValueError("委托资料已变化，请刷新后核对")
        if profile["enabled"] == enabled:
            return
        profile["enabled"] = enabled
        profile["last_change"] = {"actor_id": actor.id, "time": datetime.now(UTC8_TIMEZONE).isoformat(timespec="seconds"),
                                  "revision": uuid4().hex}
        _publish(source, config)
