"""Create-request idempotency. Login and session responses are never stored."""
import hashlib
import json
import time
import uuid


RESULT_RETENTION_SECONDS = 30 * 24 * 60 * 60


class IdempotencyError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.status = 409
        super().__init__(message)


def request_uuid(value):
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError('请求标识不正确') from None
    if str(value) != str(parsed):
        raise ValueError('请求标识不正确')
    return str(parsed)


def fingerprint(action, body):
    payload = {key: value for key, value in body.items() if key != 'request_id'}
    encoded = json.dumps({'action': action, 'body': payload}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _rows(db, user_id, request_id):
    return db.execute(
        'SELECT restore_generation,request_sha256,state,result_status,result_json,completed_at FROM idempotency_records WHERE user_id=? AND request_id=? FOR UPDATE',
        (user_id, request_id)).fetchall()


def claim(db, actor, action, body):
    """Reserve or replay one create. Caller holds the write admission lock."""
    request_id = request_uuid(body.get('request_id'))
    digest = fingerprint(action, body)
    generation = db.restore_generation
    rows = _rows(db, actor['id'], request_id)
    current = next((row for row in rows if row['restore_generation'] == generation), None)
    if current is None and rows:
        raise IdempotencyError('RESTORE_GENERATION_EXPIRED', '请求属于已关闭的恢复代次，不能再次执行')
    if current is not None:
        if current['request_sha256'] != digest:
            raise IdempotencyError('REQUEST_ID_REUSED', '同一请求标识不能用于不同内容')
        if current['state'] == 'pending':
            return ('continue', request_id)
        if current['state'] in ('succeeded', 'failed'):
            completed = current['completed_at']
            if completed is not None and int(time.time()) - int(completed) > RESULT_RETENTION_SECONDS:
                if current['result_json'] is not None:
                    db.execute(
                        "UPDATE idempotency_records SET result_status=NULL,result_json=NULL WHERE restore_generation=? AND user_id=? AND request_id=? AND state IN ('succeeded','failed')",
                        (generation, actor['id'], request_id))
                return ('expired', request_id)
            return ('replay', int(current['result_status']), json.loads(current['result_json']))
        raise IdempotencyError('REQUEST_ID_REUSED', '请求标识状态不正确')
    db.execute(
        'INSERT INTO idempotency_records(restore_generation,user_id,request_id,action,request_sha256,state,created_at) VALUES(?,?,?,?,?,?,?)',
        (generation, actor['id'], request_id, action, digest, 'pending', int(time.time())))
    return ('reserved', request_id)


def lookup(db, actor, request_id):
    """Read one stored request. Does not reserve, continue, or execute it."""
    request_id = request_uuid(request_id)
    rows = db.execute(
        'SELECT restore_generation,action,state,result_status,result_json,completed_at FROM idempotency_records WHERE user_id=? AND request_id=?',
        (actor['id'], request_id)).fetchall()
    if not rows:
        return ('missing', request_id)
    generation = db.restore_generation
    current = next((row for row in rows if row['restore_generation'] == generation), None)
    row = current or rows[0]
    if current is None:
        return ('generation', request_id, row['action'])
    if row['state'] == 'pending':
        return ('pending', request_id, row['action'])
    if row['state'] in ('succeeded', 'failed'):
        completed = row['completed_at']
        expired = completed is not None and int(time.time()) - int(completed) > RESULT_RETENTION_SECONDS
        if expired or row['result_json'] is None:
            return ('expired', request_id, row['action'])
        return ('stored', request_id, row['action'], int(row['result_status']), json.loads(row['result_json']))
    raise IdempotencyError('REQUEST_ID_REUSED', '请求标识状态不正确')


def complete(db, actor, body, state, status, result):
    if not isinstance(result, dict) or {'password', 'password_hash', 'csrf', 'token', 'cookie', 'content', 'image', 'bytes'} & set(result):
        raise ValueError('幂等结果不能保存凭据或图片原文')
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    request_id = request_uuid(body.get('request_id'))
    cursor = db.execute(
        "UPDATE idempotency_records SET state=?,result_status=?,result_json=?,completed_at=? WHERE restore_generation=? AND user_id=? AND request_id=? AND state='pending'",
        (state, int(status), encoded, int(time.time()), db.restore_generation, actor['id'], request_id))
    if cursor.rowcount != 1:
        raise IdempotencyError('REQUEST_ID_REUSED', '请求标识已结束，不能重复写入')
