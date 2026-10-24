"""PostgreSQL write admission primitive; application integration is still pending.

Every writer must enter this transaction before reading authorization or entity
versions. Administrative maintenance/revocation acquires the same control row.
Callers must check current session, permissions and entity scope inside the yield.
"""
from contextlib import contextmanager
from psycopg.pq import TransactionStatus
from psycopg import sql
from sql_dialect import AdmissionRejected


def acquire_control(connection, *, writer_epoch, restore_generation, schema='board_app'):
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise AdmissionRejected('TRANSACTION_REQUIRED')
    connection.execute("SET LOCAL lock_timeout = '5s'")
    connection.execute("SET LOCAL statement_timeout = '15s'")
    control = connection.execute(sql.SQL('SELECT mode,writer_epoch,restore_generation FROM {}.runtime_control WHERE id=1 FOR UPDATE').format(sql.Identifier(schema))).fetchone()
    if not control:
        raise AdmissionRejected('CONTROL_STATE_MISSING')
    if control[0] != 'open':
        raise AdmissionRejected('MAINTENANCE')
    if control[1] != writer_epoch:
        raise AdmissionRejected('WRITER_VERSION_RETIRED')
    if control[2] != restore_generation:
        raise AdmissionRejected('RESTORE_GENERATION_EXPIRED')


@contextmanager
def write_transaction(connection, *, writer_epoch, restore_generation):
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise AdmissionRejected('TRANSACTION_ALREADY_ACTIVE')
    with connection.transaction():
        acquire_control(connection, writer_epoch=writer_epoch, restore_generation=restore_generation)
        yield connection
