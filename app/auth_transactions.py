"""Shared write serialization for authentication transactions across workers."""
from sqlalchemy import text


def serialize_writes(db, advisory_key):
    if not db.in_transaction():
        raise ValueError('Authentication writes require an owned transaction.')
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('select pg_advisory_xact_lock(:key)'), {'key': advisory_key})
    elif db.bind.dialect.name == 'sqlite':
        transaction = db.get_transaction()
        if db.info.get('authentication_write_transaction') is not transaction:
            db.execute(text('BEGIN IMMEDIATE'))
            db.info['authentication_write_transaction'] = transaction
    else:
        raise ValueError('Unsupported authentication database.')
