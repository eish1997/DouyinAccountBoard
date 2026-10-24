FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements-pg-evaluation.txt .
RUN pip install --no-cache-dir -r requirements-pg-evaluation.txt
COPY app.py auth.py account_service.py account_lifecycle.py backup.py backup_management.py batch_edit.py collection.py data_exchange.py nurture.py operations.py phone_cards.py phone_devices.py publishing_layout.py workspace_service.py release_policy.py sql_dialect.py office_scope.py idempotency.py web_server.py pg_schema.py pg_service_adapter.py pg_write_protocol.py pg_admin.py pg_synthetic_migration.py test_isolation.py cloudbase_runtime.py cloudbase_prepare.py cloudbase-web.json ./
COPY static ./static
COPY migrations/postgresql ./migrations/postgresql
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-B", "web_server.py", "--config", "cloudbase-web.json"]
