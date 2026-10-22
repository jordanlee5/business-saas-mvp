"""Member-owned address book. The caller owns the transaction.

All mutations lock the member before inspecting addresses. SQLite callers must
start BEGIN IMMEDIATE before authentication/reads; PostgreSQL uses the row lock.
No address content is copied to the event log or to the cart.
"""

import re
import secrets
import unicodedata

from sqlalchemy import select

from ..models import Member, MemberAddress, MemberAddressEvent
from ..time_utils import utc8_now


MAX_MEMBER_ADDRESSES = 20
ADDRESS_FIELDS = {"recipient_name": 80, "phone": 16, "province": 80,
                  "city": 80, "district": 80, "detail": 240}


class AddressInputError(ValueError):
    pass


class AddressNotFound(ValueError):
    pass


class AddressLimitReached(ValueError):
    pass


class AddressMemberUnavailable(ValueError):
    pass


def normalize_address(values):
    if not isinstance(values, dict) or set(values) != set(ADDRESS_FIELDS):
        raise AddressInputError("收货地址字段不完整或包含未知字段")
    result = {}
    for key, limit in ADDRESS_FIELDS.items():
        value = values[key]
        if (not isinstance(value, str) or len(value) > limit
                or any(unicodedata.category(c).startswith("C") for c in value)):
            raise AddressInputError("收货地址格式不正确")
        value = value.strip()
        if not value:
            raise AddressInputError("请填写完整收货地址")
        result[key] = value
    if not re.fullmatch(r"\+?[0-9]{7,15}", result["phone"]):
        raise AddressInputError("联系电话须为7至15位数字，可带国际区号加号")
    return result


def _lock_member(db, member_id):
    member = db.execute(select(Member).where(Member.id == member_id)
                        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if member is None or not member.is_active:
        raise AddressMemberUnavailable("会员会话无效")


def _owned(db, member_id, public_id):
    address = db.execute(select(MemberAddress).where(
        MemberAddress.member_id == member_id,
        MemberAddress.address_public_id == public_id,
    ).execution_options(populate_existing=True)).scalar_one_or_none()
    if address is None:
        raise AddressNotFound("收货地址不存在")
    return address


def _event(db, member_id, public_id, action):
    db.add(MemberAddressEvent(member_id=member_id,
                              address_public_id=public_id, action=action))


def address_payload(address):
    return {"address_public_id": address.address_public_id,
            **{key: getattr(address, key) for key in ADDRESS_FIELDS},
            "is_default": address.is_default}


def list_member_addresses(db, member_id):
    rows = db.execute(select(MemberAddress).where(MemberAddress.member_id == member_id)
                      .order_by(MemberAddress.is_default.desc(), MemberAddress.id.desc())).scalars()
    return [address_payload(row) for row in rows]


def create_member_address(db, member_id, values):
    values = normalize_address(values)
    _lock_member(db, member_id)
    # A member row exists even when the address book is empty, so the first
    # creation and capacity checks are serialized too.
    addresses = db.execute(select(MemberAddress.id).where(
        MemberAddress.member_id == member_id)).all()
    if len(addresses) >= MAX_MEMBER_ADDRESSES:
        raise AddressLimitReached("最多保存20条收货地址")
    address = MemberAddress(address_public_id="ADDR-" + secrets.token_hex(16),
                            member_id=member_id, is_default=not addresses, **values)
    db.add(address)
    db.flush()
    _event(db, member_id, address.address_public_id, "CREATE")
    db.flush()
    return address_payload(address)


def update_member_address(db, member_id, public_id, values):
    values = normalize_address(values)
    _lock_member(db, member_id)
    address = _owned(db, member_id, public_id)
    for key, value in values.items():
        setattr(address, key, value)
    address.updated_at = utc8_now()
    _event(db, member_id, public_id, "UPDATE")
    db.flush()
    return address_payload(address)


def set_default_member_address(db, member_id, public_id):
    _lock_member(db, member_id)
    address = _owned(db, member_id, public_id)
    if not address.is_default:
        # Clear first to satisfy the partial unique index on both databases.
        db.query(MemberAddress).filter(MemberAddress.member_id == member_id,
                                       MemberAddress.is_default.is_(True)).update(
            {MemberAddress.is_default: False, MemberAddress.updated_at: utc8_now()},
            synchronize_session="fetch")
        db.flush()
        address.is_default = True
        address.updated_at = utc8_now()
        _event(db, member_id, public_id, "SET_DEFAULT")
        db.flush()
    return address_payload(address)


def delete_member_address(db, member_id, public_id):
    _lock_member(db, member_id)
    address = _owned(db, member_id, public_id)
    _event(db, member_id, public_id, "DELETE")
    db.delete(address)
    db.flush()
    # No promotion, tombstone, address content in audits, or order changes.
