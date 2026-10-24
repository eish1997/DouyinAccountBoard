CREATE TABLE idempotency_records(
  restore_generation TEXT NOT NULL,
  user_id BIGINT NOT NULL REFERENCES auth_users(id),
  request_id TEXT NOT NULL,
  action TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','succeeded','failed')),
  result_status BIGINT,
  result_json TEXT,
  created_at BIGINT NOT NULL,
  completed_at BIGINT,
  PRIMARY KEY(restore_generation,user_id,request_id));
CREATE INDEX idempotency_records_user_request ON idempotency_records(user_id,request_id);
