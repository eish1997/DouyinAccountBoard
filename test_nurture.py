import base64
import json
import unittest
from datetime import date,timedelta
from unittest.mock import patch
import app,backup,nurture,operations,auth,test_auth_support,test_workspace

PNG='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='

class NurtureTest(unittest.TestCase):
    setUp=test_workspace.WorkspaceTest.setUp
    tearDown=test_workspace.WorkspaceTest.tearDown
    call=test_workspace.WorkspaceTest.call
    reject=test_workspace.WorkspaceTest.reject
    board=test_workspace.WorkspaceTest.board
    create=test_workspace.WorkspaceTest.create

    def record(self):return next(r for r in self.board()['nurture']['records'] if r['account_id']=='43')
    def start(self,days):
        started=(date.fromisoformat(app.today())-timedelta(days=days)).isoformat()
        self.call('/api/workspace/nurture/join',dict(id='43',started=started),self.owner)
        self.call('/api/workspace/nurture/verification',dict(id='43',verification_state='无需核对，可使用'),self.owner)
        self.call('/api/workspace/nurture/start',dict(id='43',started=started),self.owner)
        return started

    def test_account_verification_first_registration_permissions_and_conflict(self):
        self.create(self.owner,'no_checks',permissions=['account_status'])
        staff=test_auth_support.client(self.base,'no_checks')
        self.reject(403,'/api/workspace/nurture/verification',dict(id='43',verification_state='核对处理中'),staff)
        versions=self.board()['record_versions']
        body=dict(id='43',verification_state='核对处理中',verification_owner='甲',verification_note='等待核对',_expected_versions={key:versions.get(key) for key in ['account:43','nurture:43']})
        before=next(a for a in self.board()['accounts'] if a['id']=='43')
        self.call('/api/workspace/nurture/verification',body,self.owner)
        record=self.record()
        self.assertEqual(record['verification_owner'],'甲')
        self.assertEqual(record['verification_note'],'等待核对')
        self.assertEqual(record['workflow'],'待开始')
        self.assertFalse(record['started_on'])
        self.assertEqual(next(a for a in self.board()['accounts'] if a['id']=='43')['status'],before['status'])
        self.reject(409,'/api/workspace/nurture/verification',body,self.owner)
        self.reject(400,'/api/workspace/nurture/verification',dict(id='43',verification_owner='x'*101),self.owner)

    def test_finished_nurture_can_update_verification_without_reopening(self):
        self.start(7)
        self.call('/api/workspace/nurture/finish',dict(id='43'),self.owner)
        before=self.record()
        self.call('/api/workspace/nurture/verification',dict(id='43',verification_state='核对处理中',verification_owner='乙',verification_note='结束后核对'),self.owner)
        after=self.record()
        for key in ['completed_on','started_on','cycle','workflow','banked_days','image_count']:
            self.assertEqual(after[key],before[key],key)
        self.assertEqual(after['verification_state'],'核对处理中')
        self.assertEqual(after['stage'],'已结束')
        self.assertEqual(next(a for a in self.board()['accounts'] if a['id']=='43')['status'],'正常')

    def test_seven_day_boundary_due_persists_until_finish_and_daily_preserved(self):
        start=self.start(6)
        self.assertEqual(self.record()['stage'],'养号中')
        self.reject(400,'/api/workspace/nurture/finish',dict(id='43'),self.owner)
        self.call('/api/daily',dict(id='43',date=start,published=True),self.owner)
        tomorrow=(date.fromisoformat(app.today())+timedelta(days=1)).isoformat()
        with patch('app.today',return_value=tomorrow):
            self.assertEqual(self.record()['stage'],'到期待转发布')
            self.call('/api/account/status',dict(id='43',status='正常'),self.owner)
            self.assertEqual(self.record()['stage'],'到期待转发布')
            self.call('/api/workspace/nurture/finish',dict(id='43'),self.owner)
        self.assertEqual(self.record()['stage'],'已结束')
        self.assertEqual(next(a for a in self.board()['accounts'] if a['id']=='43')['status'],'正常')
        board=self.call('/api/board?date='+start,client=self.owner)
        self.assertTrue(next(a for a in board['accounts'] if a['id']=='43')['published'])

    def test_images_access_validation_concurrency_and_archive_cleanup(self):
        start=self.start(7);versions=self.board()['record_versions']
        body=dict(id='43',day=start,content=PNG,_expected_versions={k:versions[k] for k in ['account:43','nurture:43']})
        self.call('/api/workspace/nurture/upload',body,self.owner)
        history=self.call('/api/workspace/history?entity=account:43',client=self.owner)
        self.assertTrue(any(row['entity']=='nurture:43' for row in history['entries']))
        self.reject(409,'/api/workspace/nurture/upload',body,self.owner)
        image=self.record()['images'][0]['id']
        with self.owner.open(self.base+'/api/nurture/image?id='+image) as response:
            self.assertEqual(response.headers['Content-Type'],'image/png');self.assertEqual(response.read(),base64.b64decode(PNG))
        self.reject(401,'/api/nurture/image?id='+image)
        self.reject(400,'/api/workspace/nurture/upload',dict(id='43',day=start,content=base64.b64encode(b'<svg/>').decode()),self.owner)
        self.reject(400,'/api/workspace/nurture/purge',dict(id='43'),self.owner)
        self.call('/api/workspace/nurture/finish',dict(id='43'),self.owner)
        self.assertEqual(self.record()['image_count'],1)
        self.call('/api/workspace/nurture/purge',dict(id='43'),self.owner)
        self.assertEqual(self.record()['image_count'],0);self.assertEqual(self.record()['stage'],'已结束')
        self.reject(404,'/api/nurture/image?id='+image,client=self.owner)

    def test_new_permission_and_abnormal_finish_guard(self):
        self.start(7);self.create(self.owner,'nurture_staff');staff=test_auth_support.client(self.base,'nurture_staff')
        self.reject(403,'/api/workspace/nurture/upload',dict(id='43',day=app.today(),content=PNG),staff)
        self.call('/api/account/status',dict(id='43',status='封禁'),self.owner)
        self.assertEqual(self.record()['stage'],'状态待核对')
        self.reject(400,'/api/workspace/nurture/finish',dict(id='43'),self.owner)
        self.assertEqual(next(a for a in self.board()['accounts'] if a['id']=='43')['status'],'封禁')

    def test_recent_registration_auto_entry_and_backup_contains_images(self):
        with app.connect() as db:
            db.execute('UPDATE accounts SET registered_on=? WHERE id=?',(app.today(),'43'));nurture.sync(db,app.today())
        self.assertEqual(self.record()['started_on'],'')
        self.assertEqual(self.record()['stage'],'待检查')
        self.call('/api/workspace/nurture/verification',dict(id='43',verification_state='无需核对，可使用'),self.owner)
        self.call('/api/workspace/nurture/start',dict(id='43'),self.owner)
        self.call('/api/workspace/nurture/upload',dict(id='43',day=app.today(),content=PNG),self.owner)
        result=self.call('/api/workspace/backup-now',{},self.owner)
        preview=self.call('/api/workspace/restore-preview',dict(backup_id=result['name']),self.owner)
        self.assertEqual(preview['counts']['nurture_images']['backup'],1)
        image=self.record()['images'][0]['id']
        self.call('/api/workspace/nurture/delete-image',dict(id='43',image_id=image),self.owner)
        preview=self.call('/api/workspace/restore-preview',dict(backup_id=result['name']),self.owner)
        self.call('/api/workspace/restore-apply',dict(preview_id=preview['preview_id'],confirmation='恢复业务数据',password=test_auth_support.TEST_PASSWORD),self.owner)
        self.assertEqual(self.record()['image_count'],1)
        with self.owner.open(self.base+'/api/nurture/image?id='+image) as response:self.assertEqual(response.read(),base64.b64decode(PNG))

    def test_repeat_nurture_keeps_old_evidence_out_of_new_cycle(self):
        start=self.start(7)
        self.call('/api/workspace/nurture/upload',dict(id='43',day=start,content=PNG),self.owner)
        self.call('/api/workspace/nurture/finish',dict(id='43'),self.owner)
        self.call('/api/workspace/nurture/join',dict(id='43',started=app.today()),self.owner)
        self.assertEqual(self.record()['cycle'],2);self.assertEqual(self.record()['image_count'],0)
        self.assertEqual(self.record()['images'],[]);self.assertEqual(self.record()['stage'],'待检查')

    def test_checks_do_not_start_clock_and_manual_start_can_follow_late_verification(self):
        self.call('/api/workspace/nurture/join',dict(id='43'),self.owner)
        self.reject(400,'/api/workspace/nurture/start',dict(id='43'),self.owner)
        self.call('/api/workspace/nurture/verification',dict(id='43',verification_state='核对处理中',verification_owner='测试负责人',verification_note='等待处理'),self.owner)
        self.assertEqual(self.record()['stage'],'核对处理中');self.assertEqual(self.record()['elapsed'],0)
        later=(date.fromisoformat(app.today())+timedelta(days=12)).isoformat()
        with patch('app.today',return_value=later):
            self.assertEqual(self.record()['elapsed'],0)
            self.call('/api/workspace/nurture/verification',dict(id='43',verification_state='核对已通过'),self.owner)
            self.assertEqual(self.record()['stage'],'待开始养号');self.assertEqual(self.record()['due_on'],'')
            self.call('/api/workspace/nurture/start',dict(id='43'),self.owner)
            self.assertEqual(self.record()['started_on'],later);self.assertEqual(self.record()['elapsed'],0)
            self.assertEqual(self.record()['verification_owner'],'测试负责人')

    def test_midway_check_freezes_progress_and_resume_extends_deadline(self):
        start=self.start(3);today=app.today()
        self.call('/api/workspace/nurture/verification',dict(id='43',verification_state='核对处理中'),self.owner)
        self.assertEqual(self.record()['stage'],'养号暂停');self.assertEqual(self.record()['elapsed'],3)
        self.assertEqual(self.record()['due_on'],'')
        later=(date.fromisoformat(today)+timedelta(days=5)).isoformat()
        with patch('app.today',return_value=later):
            self.assertEqual(self.record()['elapsed'],3)
            self.reject(400,'/api/workspace/nurture/resume',dict(id='43'),self.owner)
            self.reject(400,'/api/workspace/nurture/finish',dict(id='43'),self.owner)
            self.call('/api/workspace/nurture/verification',dict(id='43',verification_state='核对已通过'),self.owner)
            self.assertEqual(self.record()['stage'],'养号暂停')
            self.call('/api/workspace/nurture/resume',dict(id='43'),self.owner)
            record=self.record();self.assertEqual(record['elapsed'],3)
            self.assertEqual(record['due_on'],(date.fromisoformat(later)+timedelta(days=4)).isoformat())
            self.assertEqual(record['day_dates'][:3],[(date.fromisoformat(start)+timedelta(days=i)).isoformat() for i in range(3)])
            self.assertEqual(record['day_dates'][3],later)
        end=(date.fromisoformat(later)+timedelta(days=4)).isoformat()
        with patch('app.today',return_value=end):
            self.assertEqual(self.record()['stage'],'到期待转发布')
            self.call('/api/workspace/nurture/finish',dict(id='43'),self.owner)
            self.assertEqual(self.record()['elapsed'],7)

    def test_manual_pause_multiple_same_day_resume_and_status_guard(self):
        self.start(2)
        for _ in range(2):
            self.call('/api/workspace/nurture/pause',dict(id='43'),self.owner)
            self.assertEqual(self.record()['elapsed'],2)
            self.call('/api/workspace/nurture/resume',dict(id='43'),self.owner)
        self.assertEqual(self.record()['elapsed'],2)
        self.reject(400,'/api/workspace/nurture/start',dict(id='43',started='2020-01-01'),self.owner)
        self.call('/api/account/status',dict(id='43',status='封禁'),self.owner)
        self.assertEqual(self.record()['workflow'],'暂停');self.assertEqual(self.record()['elapsed'],2)

    def test_old_date_edit_and_reimport_cannot_start_or_reset_clock(self):
        self.call('/api/account/status',dict(id='43',status='养号7天'),self.owner)
        self.call('/api/account/nurture',dict(id='43',started='2026-01-01'),self.owner)
        self.assertEqual(self.record()['stage'],'待检查');self.assertEqual(self.record()['elapsed'],0)
        self.call('/api/workspace/nurture/verification',dict(id='43',verification_state='无需核对，可使用'),self.owner)
        self.call('/api/workspace/nurture/start',dict(id='43'),self.owner)
        self.call('/api/account/nurture',dict(id='43',started='2026-01-01'),self.owner)
        self.assertEqual(self.record()['started_on'],app.today());self.assertEqual(self.record()['elapsed'],0)

    def test_v1_migration_keeps_dates_and_images_as_unconfirmed_reference(self):
        with app.connect() as db:
            db.execute('DROP TABLE nurture_records')
            db.execute("CREATE TABLE nurture_records(account_id TEXT PRIMARY KEY REFERENCES accounts(id),started_on TEXT NOT NULL DEFAULT '',completed_on TEXT NOT NULL DEFAULT '',cycle INTEGER NOT NULL DEFAULT 1,image_count INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO nurture_records(account_id,started_on,image_count) VALUES('43','2026-01-01',1)")
            db.execute("INSERT INTO nurture_images VALUES('legacy-proof','43',1,'2026-01-01','image/png',?,'测试人员')",(base64.b64decode(PNG),))
            registered=db.execute("SELECT registered_on FROM accounts WHERE id='43'").fetchone()[0]
            nurture.migrate(db);nurture.sync(db,app.today())
            self.assertEqual(registered,db.execute("SELECT registered_on FROM accounts WHERE id='43'").fetchone()[0])
        record=self.record();self.assertEqual(record['stage'],'待检查');self.assertEqual(record['elapsed'],0)
        self.assertEqual(record['started_on'],'');self.assertEqual(record['legacy_started_on'],'2026-01-01')
        self.assertEqual(record['images'][0]['id'],'legacy-proof')

    def test_manage_only_staff_can_check_but_cannot_change_account_status(self):
        self.create(self.owner,'check_only',permissions=['nurture_manage'])
        staff=test_auth_support.client(self.base,'check_only')
        self.call('/api/workspace/nurture/join',dict(id='43'),staff)
        self.call('/api/workspace/nurture/verification',dict(id='43',verification_state='无需核对，可使用'),staff)
        self.reject(403,'/api/workspace/nurture/start',dict(id='43'),staff)
        self.call('/api/workspace/nurture/start',dict(id='43'),self.owner)
        self.call('/api/workspace/nurture/pause',dict(id='43'),staff)
        self.reject(403,'/api/workspace/nurture/resume',dict(id='43'),staff)

    def test_completed_cycle_is_not_reopened_by_source_status(self):
        self.start(7)
        self.call('/api/workspace/nurture/finish',dict(id='43'),self.owner)
        self.call('/api/account/status',dict(id='43',status='养号7天'),self.owner)
        self.assertEqual(self.record()['stage'],'已结束');self.assertEqual(self.record()['cycle'],1)
