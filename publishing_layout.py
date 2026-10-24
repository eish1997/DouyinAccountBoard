"""Publishing coordinates come from the work-progress matrix, independently of account metadata."""
import time
from sql_dialect import integer_order


class ConflictError(ValueError):pass


def migrate(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS publishing_layout(
      batch TEXT NOT NULL,computer_id TEXT NOT NULL REFERENCES computers(id),slot INTEGER NOT NULL,
      account_id TEXT REFERENCES accounts(id),label TEXT NOT NULL,office TEXT NOT NULL,
      source_cell TEXT NOT NULL,source_url TEXT NOT NULL,reason TEXT NOT NULL,
      PRIMARY KEY(batch,computer_id,slot));
    CREATE TABLE IF NOT EXISTS publishing_layout_state(id INTEGER PRIMARY KEY CHECK(id=1),batch TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS publishing_layout_links(
      batch TEXT NOT NULL,computer_id TEXT NOT NULL,slot INTEGER NOT NULL,
      previous_reason TEXT NOT NULL,actor_id INTEGER NOT NULL,updated_at INTEGER NOT NULL,
      PRIMARY KEY(batch,computer_id,slot),
      FOREIGN KEY(batch,computer_id,slot) REFERENCES publishing_layout(batch,computer_id,slot));
    ''')


def overview(db, allowed_accounts=None, allowed_computers=None):
    if allowed_computers is not None and not allowed_computers:return dict(enabled=False,cells=[])
    row=db.execute('SELECT batch FROM publishing_layout_state WHERE id=1').fetchone()
    if not row:return dict(enabled=False,cells=[])
    order=integer_order(db,"REPLACE(computer_id,'office3:','')")
    cells=[dict(r) for r in db.execute('SELECT computer_id,slot,account_id,label,office,source_cell,source_url,reason FROM publishing_layout WHERE batch=? ORDER BY CASE WHEN computer_id LIKE \'office3:%\' THEN 1 ELSE 0 END,'+order+',slot',(row['batch'],))]
    if allowed_accounts is not None:cells=[row for row in cells if row['account_id'] is None or row['account_id'] in allowed_accounts]
    if allowed_computers is not None:cells=[row for row in cells if row['computer_id'] in allowed_computers]
    manual={(r[0],r[1]) for r in db.execute('SELECT computer_id,slot FROM publishing_layout_links WHERE batch=?',(row['batch'],))}
    for cell in cells:cell['manual_link']=(cell['computer_id'],cell['slot']) in manual
    return dict(enabled=True,cells=cells,batch=row['batch'])


def link(db,body,actor_id):
    state=db.execute('SELECT batch FROM publishing_layout_state WHERE id=1').fetchone()
    if not state or body.get('batch')!=state['batch']:raise ConflictError('发布安排已更新，请刷新后重新选择')
    if type(body.get('slot')) is not int:raise ValueError('发布位置不正确')
    key=(state['batch'],str(body.get('computer_id','')),body['slot'])
    cell=db.execute('SELECT * FROM publishing_layout WHERE batch=? AND computer_id=? AND slot=?',key).fetchone()
    if not cell or not cell['label'].strip():raise ValueError('只能处理已有文字的待匹配格子')
    if 'expected_account_id' not in body or body['expected_account_id']!=cell['account_id']:raise ConflictError('这个格子已被其他人处理，请刷新后重试')
    manual=db.execute('SELECT * FROM publishing_layout_links WHERE batch=? AND computer_id=? AND slot=?',key).fetchone()
    if cell['account_id'] and not manual:raise ValueError('此格子已由来源表匹配，本入口仅处理待匹配和手动关联的格子')
    action=body.get('action','link')
    if action=='clear':
        if not manual:raise ValueError('该格子没有可取消的手动关联')
        db.execute('UPDATE publishing_layout SET account_id=NULL,reason=? WHERE batch=? AND computer_id=? AND slot=?',(manual['previous_reason'],*key))
        db.execute('DELETE FROM publishing_layout_links WHERE batch=? AND computer_id=? AND slot=?',key)
        return dict(source_cell=cell['source_cell'],account_id=None,previous_account_id=cell['account_id'])
    if action!='link':raise ValueError('关联操作不正确')
    account_id=str(body.get('account_id',''))
    account=db.execute('SELECT * FROM accounts WHERE id=?',(account_id,)).fetchone()
    if not account:raise ValueError('请选择工作台中已存在的账号')
    office_three=account['office_group']=='three' or str(account['phone_device_id'] or '').startswith('office3:')
    if key[1].startswith('office3:')!=office_three:raise ValueError('来源批次不同：办公室三与原办公室的重号设备必须分开处理')
    duplicate=db.execute('SELECT source_cell FROM publishing_layout WHERE batch=? AND account_id=? AND NOT(computer_id=? AND slot=?)', (key[0],account_id,key[1],key[2])).fetchone()
    if duplicate:raise ConflictError('该账号已有发布位置 '+duplicate['source_cell']+'，请先核对，不能重复关联')
    if not manual:db.execute('INSERT INTO publishing_layout_links VALUES(?,?,?,?,?,?)',(*key,cell['reason'],actor_id,int(time.time())))
    else:db.execute('UPDATE publishing_layout_links SET actor_id=?,updated_at=? WHERE batch=? AND computer_id=? AND slot=?',(actor_id,int(time.time()),*key))
    db.execute('UPDATE publishing_layout SET account_id=?,reason=? WHERE batch=? AND computer_id=? AND slot=?',(account_id,'',*key))
    return dict(source_cell=cell['source_cell'],account_id=account_id,previous_account_id=cell['account_id'])
