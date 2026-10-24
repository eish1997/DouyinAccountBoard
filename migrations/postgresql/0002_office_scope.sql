-- Stable scope metadata. Legacy records and employees remain unassigned.
CREATE TABLE offices(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,
  active BIGINT NOT NULL DEFAULT 1 CHECK(active IN (0,1)));
CREATE TABLE user_office_grants(user_id BIGINT NOT NULL REFERENCES auth_users(id),
  office_id TEXT NOT NULL REFERENCES offices(id),PRIMARY KEY(user_id,office_id));
CREATE INDEX user_office_grants_office ON user_office_grants(office_id,user_id);
CREATE TABLE account_offices(account_id TEXT PRIMARY KEY REFERENCES accounts(id),
  office_id TEXT NOT NULL REFERENCES offices(id));
CREATE INDEX account_offices_office ON account_offices(office_id,account_id);
CREATE TABLE computer_offices(computer_id TEXT PRIMARY KEY REFERENCES computers(id),
  office_id TEXT NOT NULL REFERENCES offices(id));
CREATE TABLE phone_card_offices(card_id BIGINT NOT NULL REFERENCES phone_cards(id),
  office_id TEXT NOT NULL REFERENCES offices(id),PRIMARY KEY(card_id,office_id));
CREATE TABLE phone_device_offices(device_id TEXT NOT NULL REFERENCES phone_devices(id),
  office_id TEXT NOT NULL REFERENCES offices(id),PRIMARY KEY(device_id,office_id));
ALTER TABLE auth_users ADD COLUMN authorization_version BIGINT NOT NULL DEFAULT 1 CHECK(authorization_version>0);
ALTER TABLE operations_history ADD COLUMN scope_offices TEXT;
