#!/usr/bin/env bash
set -euo pipefail

root=/opt/mcn-ai-automation
release_id=fan-data-newcomer-handshake-v1-20260814T191500CST
source_revision=48ee59b6128a3bf9e098d28c9c31e5fc0d9cf34d
job_dir="${MCN_DEPLOY_QUEUE_JOB_DIR:?missing deploy queue job directory}"
artifact="$job_dir/artifacts/$release_id.tar.gz"
artifact_sha=fc35524b8db47005b7e559b435a5595277f060492ea0987be937f263e4131540
staging="/var/lib/mcn-ai-automation/staging/$release_id"
backup="$root/backups/releases/$release_id"
manifest="/var/lib/mcn-ai-automation/releases/$release_id.json"
plan="$staging/release-plan.json"
config_dir=/etc/mcn-ai-automation
config_file="$config_dir/newcomer-publication.env"
secret_file="$config_dir/newcomer-webhook.secret"
installed=0
restart_started=0
config_existed=0
secret_existed=0

restore_preimage() {
  install -m 0644 "$backup/main_app.py" "$root/app/main_app.py"
  install -m 0644 "$backup/main_service_executor.py" "$root/app/main_service_executor.py"
  install -m 0644 "$backup/main_service_intake.py" "$root/app/main_service_intake.py"
  install -m 0644 "$backup/schema_migrations.py" "$root/app/schema_migrations.py"
  rm -f "$root/app/newcomer_publication.py" "$root/scripts/notify_newcomer_publications.py"
  rm -f /etc/systemd/system/mcn-backend.service.d/65-newcomer-publication.conf
  rm -f /etc/systemd/system/mcn-daily-data-completion-notifier.service.d/20-newcomer-publication.conf
  if [[ "$config_existed" -eq 1 ]]; then install -m 0600 "$backup/newcomer-publication.env" "$config_file"; else rm -f "$config_file"; fi
  if [[ "$secret_existed" -eq 1 ]]; then install -m 0600 "$backup/newcomer-webhook.secret" "$secret_file"; else rm -f "$secret_file"; fi
  systemctl daemon-reload
  "$root/.venv/bin/python" -m py_compile \
    "$root/app/main_app.py" "$root/app/main_service_executor.py" \
    "$root/app/main_service_intake.py" "$root/app/schema_migrations.py"
}

governed_rollback() {
  local rollback_id="${release_id}-rollback"
  local rollback_manifest="/var/lib/mcn-ai-automation/releases/${rollback_id}.json"
  local rollback_plan="$staging/rollback-plan.json"
  restore_preimage
  BACKUP="$backup" ID="$rollback_id" PLAN="$rollback_plan" "$root/.venv/bin/python" - <<'PY'
import hashlib, json, os
from pathlib import Path
backup=Path(os.environ['BACKUP'])
names=('main_app.py','main_service_executor.py','main_service_intake.py','schema_migrations.py')
artifacts=[{'path':str(backup/n),'sha256':hashlib.sha256((backup/n).read_bytes()).hexdigest(),'verification':'exact production preimage'} for n in names]
payload={
  'release_id':os.environ['ID'],
  'change_source':{'kind':'incident_rollback','reference':'fan-data-newcomer-handshake-v1','base_revision':'forward_release_failed'},
  'files':['app/main_app.py','app/main_service_executor.py','app/main_service_intake.py','app/schema_migrations.py'],
  'units':['mcn-backend.service','mcn-daily-data-completion-notifier.service'],
  'databases':[{'name':'automation','path':'/opt/mcn-ai-automation/data/automation.db','health_check':'probe','declared_generation':'additive schema may remain'}],
  'backup':{'required':True,'status':'verified','artifacts':artifacts},
  'tests':[{'name':'preimage-compile','status':'passed','evidence':'exact preimages restored and compiled'}],
  'smokes':[{'name':'backend-health','status':'pending','evidence':'post rollback restart'}],
  'rollback':{'status':'ready','strategy':'Exact preimages restored; additive publication tables and backup retained.'},
}
Path(os.environ['PLAN']).write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n')
PY
  "$root/.venv/bin/python" "$root/scripts/mcn_release_governance.py" create --plan "$rollback_plan" --root "$root" --output "$rollback_manifest"
  "$root/.venv/bin/python" "$root/scripts/mcn_release_governance.py" validate --manifest "$rollback_manifest"
  "$root/scripts/mcn_controlled_restart.sh" --manifest "$rollback_manifest" --unit mcn-backend.service \
    --health-url http://127.0.0.1:8011/health --timeout-seconds 120 -- /bin/systemctl restart mcn-backend.service
}

on_error() {
  status=$?
  trap - ERR
  set +e
  if [[ "$installed" -eq 1 ]]; then
    if [[ "$restart_started" -eq 1 ]]; then governed_rollback; else restore_preimage; fi
  fi
  exit "$status"
}
trap on_error ERR

[[ "${MCN_DEPLOY_QUEUE_ACTIVE:-}" == 1 ]]
"$root/.venv/bin/python" "$root/scripts/mcn_release_governance.py" audit-restart --unit mcn-backend.service >/dev/null
[[ "$(sha256sum "$artifact" | awk '{print $1}')" == "$artifact_sha" ]]
[[ "$(sha256sum "$root/app/main_app.py" | awk '{print $1}')" == 7a54918e9f22550bbb8f4462322574f5d32b5afe04bccc4cfa2ae2efb1e29216 ]]
[[ "$(sha256sum "$root/app/main_service_executor.py" | awk '{print $1}')" == aceae4489575c6fdc8a9606e2f9c6d3d6a45158ef4d802d32c6018e4ee7454b4 ]]
[[ "$(sha256sum "$root/app/main_service_intake.py" | awk '{print $1}')" == 8bc92f1197901f3edd936993f71dfc6562ce909f4505284ccb50ac494963e657 ]]
[[ "$(sha256sum "$root/app/schema_migrations.py" | awk '{print $1}')" == c20957cf31e325c33420bd14f20bbf915e8d9e40a349968135d90f78195534df ]]
[[ ! -e "$root/app/newcomer_publication.py" && ! -e "$root/scripts/notify_newcomer_publications.py" ]]

mkdir -p "$staging/candidate" "$backup" "$backup/sqlite" /var/lib/mcn-ai-automation/releases
tar -xzf "$artifact" -C "$staging/candidate"
declare -A expected=(
  [app/main_app.py]=c8a59a64741e91691c8a46869e2f401a1b6c438950ca3f271a2a223b855e3dd3
  [app/main_service_executor.py]=1c688db4bb19feaa583220d0ea745ee4728481ad8c9bc825a4a4c99d87b4d31e
  [app/main_service_intake.py]=23a35f3894c5923230c5e6595263a0b6c8d32e3e7468d91570aa4a15263fdcfb
  [app/newcomer_publication.py]=e088aaa11b2de7221978b67d37662299e98a40c92c2c9737dd3567a66798c7c7
  [app/schema_migrations.py]=c5e153ae9336971b67bde87ae2420aed7fe70a9a7e3c4375b1e1504e05499638
  [scripts/notify_newcomer_publications.py]=19654578a3e3bb38ea8f32408641b4ba94de1f93cbca753b5c1d03d776dd5b5b
  [scripts/systemd/mcn-backend.service.d/65-newcomer-publication.conf]=7fc6d18bf724b0d2ac1bf4f56804c57fb96d4f08a3e9a3392b45929a20ba5c9c
  [scripts/systemd/mcn-daily-data-completion-notifier.service.d/20-newcomer-publication.conf]=d8757bdaef0681e403a8c008e2df5e1c5f4a1944f419a96219f1366e01bc27bd
)
for path in "${!expected[@]}"; do
  [[ "$(sha256sum "$staging/candidate/$path" | awk '{print $1}')" == "${expected[$path]}" ]]
done
"$root/.venv/bin/python" -m py_compile "$staging/candidate/app/"*.py "$staging/candidate/scripts/notify_newcomer_publications.py"

install -m 0644 "$root/app/main_app.py" "$backup/main_app.py"
install -m 0644 "$root/app/main_service_executor.py" "$backup/main_service_executor.py"
install -m 0644 "$root/app/main_service_intake.py" "$backup/main_service_intake.py"
install -m 0644 "$root/app/schema_migrations.py" "$backup/schema_migrations.py"
if [[ -f "$config_file" ]]; then config_existed=1; install -m 0600 "$config_file" "$backup/newcomer-publication.env"; fi
if [[ -f "$secret_file" ]]; then secret_existed=1; install -m 0600 "$secret_file" "$backup/newcomer-webhook.secret"; fi
"$root/.venv/bin/python" "$root/scripts/create_verified_sqlite_backup.py" \
  --source "$root/data/automation.db" --backup-dir "$backup/sqlite" \
  --min-free-after-gb 6 --working-margin-gb 1 --size-multiplier 1.0 \
  --max-used-percent 75 >/dev/null
sqlite_backup="$(find "$backup/sqlite" -maxdepth 1 -type f -name '*.db' -print | head -1)"
[[ -n "$sqlite_backup" && -s "$sqlite_backup" ]]

mkdir -p "$config_dir" /etc/systemd/system/mcn-backend.service.d \
  /etc/systemd/system/mcn-daily-data-completion-notifier.service.d
CONFIG_FILE="$config_file" SECRET_FILE="$secret_file" "$root/.venv/bin/python" - <<'PY'
import os,secrets
from pathlib import Path
config=Path(os.environ['CONFIG_FILE']); secret=Path(os.environ['SECRET_FILE'])
if not secret.exists():
    secret.write_text(secrets.token_hex(32)+'\n',encoding='utf-8'); secret.chmod(0o600)
if not config.exists():
    token=secrets.token_hex(32)
    config.write_text(
      'NEWCOMER_EXTERNAL_FEED_TOKEN='+token+'\n'
      'NEWCOMER_WEBHOOK_URL=https://nova.hoyisr.com/api/internal/mcn/newcomers/events\n'
      'NEWCOMER_WEBHOOK_SECRET_FILE=/etc/mcn-ai-automation/newcomer-webhook.secret\n',
      encoding='utf-8',
    ); config.chmod(0o600)
PY

install -m 0644 "$staging/candidate/app/main_app.py" "$root/app/main_app.py"
install -m 0644 "$staging/candidate/app/main_service_executor.py" "$root/app/main_service_executor.py"
install -m 0644 "$staging/candidate/app/main_service_intake.py" "$root/app/main_service_intake.py"
install -m 0644 "$staging/candidate/app/newcomer_publication.py" "$root/app/newcomer_publication.py"
install -m 0644 "$staging/candidate/app/schema_migrations.py" "$root/app/schema_migrations.py"
install -m 0755 "$staging/candidate/scripts/notify_newcomer_publications.py" "$root/scripts/notify_newcomer_publications.py"
install -m 0644 "$staging/candidate/scripts/systemd/mcn-backend.service.d/65-newcomer-publication.conf" \
  /etc/systemd/system/mcn-backend.service.d/65-newcomer-publication.conf
install -m 0644 "$staging/candidate/scripts/systemd/mcn-daily-data-completion-notifier.service.d/20-newcomer-publication.conf" \
  /etc/systemd/system/mcn-daily-data-completion-notifier.service.d/20-newcomer-publication.conf
systemctl daemon-reload
installed=1

BACKUP="$backup" SQLITE_BACKUP="$sqlite_backup" ID="$release_id" REVISION="$source_revision" PLAN="$plan" ROOT="$root" "$root/.venv/bin/python" - <<'PY'
import hashlib,importlib.util,json,os
from pathlib import Path
backup=Path(os.environ['BACKUP'])
root=Path(os.environ['ROOT'])
names=('main_app.py','main_service_executor.py','main_service_intake.py','schema_migrations.py')
artifacts=[{'path':str(backup/n),'sha256':hashlib.sha256((backup/n).read_bytes()).hexdigest(),'verification':'exact production preimage'} for n in names]
sqlite=Path(os.environ['SQLITE_BACKUP'])
artifacts.append({'path':str(sqlite),'sha256':hashlib.sha256(sqlite.read_bytes()).hexdigest(),'verification':'verified online SQLite backup'})
spec=importlib.util.spec_from_file_location('mcn_release_governance',root/'scripts/mcn_release_governance.py')
governance=importlib.util.module_from_spec(spec); spec.loader.exec_module(governance)
scope_specs=(
  ('app/main_app.py','main_app.py',[
    'newcomer_external_feed_token = str(',
    'def _require_newcomer_external_feed',
    'def external_newcomer_daily(',
  ]),
  ('app/main_service_executor.py','main_service_executor.py',[
    'from app.newcomer_publication import (',
    'def list_newcomer_daily_publication(',
  ]),
  ('app/main_service_intake.py','main_service_intake.py',[
    'def list_external_fan_conversions(',
  ]),
)
scope_files=[]
for relative,preimage_name,markers in scope_specs:
    preimage=backup/preimage_name
    snapshot=governance._change_scope_diff_snapshot(preimage,root/relative)
    scope_files.append({
      'path':relative,
      'preimage_path':str(preimage),
      'preimage_sha256':snapshot['preimage_sha256'],
      'expected_diff_sha256':snapshot['diff_sha256'],
      'expected_hunks':snapshot['hunks'],
      'expected_changed_lines':snapshot['changed_lines'],
      'expected_deleted_lines':snapshot['deleted_lines'],
      'max_hunks':snapshot['hunks'],
      'max_changed_lines':snapshot['changed_lines'],
      'allowed_regions':markers,
    })
payload={
  'release_id':os.environ['ID'],
  'change_source':{'kind':'codex_task','reference':'Nova fan-data newcomer and CRM handshake','base_revision':os.environ['REVISION']},
  'files':['app/main_app.py','app/main_service_executor.py','app/main_service_intake.py','app/newcomer_publication.py','app/schema_migrations.py','scripts/notify_newcomer_publications.py'],
  'units':['mcn-backend.service','mcn-daily-data-completion-notifier.service'],
  'databases':[{'name':'automation','path':str(root/'data/automation.db'),'health_check':'probe','declared_generation':'additive newcomer publication v1'}],
  'backup':{'required':True,'status':'verified','artifacts':artifacts},
  'tests':[{'name':'newcomer-and-conversion-contract','status':'passed','evidence':'12 focused tests, union compile, exact artifact hashes'}],
  'smokes':[{'name':'external-feed-and-backfill','status':'pending','evidence':'post restart loopback API and 2026-08-13 publication'}],
  'rollback':{'status':'ready','strategy':'Restore exact code/config preimages and governed backend restart; verified SQLite backup retained.'},
  'change_scope':{'mode':'minimal_patch','files':scope_files},
}
Path(os.environ['PLAN']).write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n')
PY
"$root/.venv/bin/python" "$root/scripts/mcn_release_governance.py" create --plan "$plan" --root "$root" --output "$manifest"
"$root/.venv/bin/python" "$root/scripts/mcn_release_governance.py" validate --manifest "$manifest"
restart_started=1
"$root/scripts/mcn_controlled_restart.sh" --manifest "$manifest" --unit mcn-backend.service \
  --health-url http://127.0.0.1:8011/health --timeout-seconds 120 -- /bin/systemctl restart mcn-backend.service

set -a
source "$config_file"
set +a
PYTHONPATH="$root" DB_PATH="$root/data/automation.db" "$root/.venv/bin/python" - <<'PY'
import sqlite3
import os
from app.newcomer_publication import reconcile_newcomer_publication
conn=sqlite3.connect(os.environ['DB_PATH'],timeout=30); conn.row_factory=sqlite3.Row
conn.execute('PRAGMA busy_timeout=30000')
try:
    with conn:
        for platform in ('LINKY','TIMO'):
            result=reconcile_newcomer_publication(conn,platform=platform,business_date='2026-08-13')
            assert result['status'] in {'complete','revised','unchanged'}
finally:
    conn.close()
PY
curl --noproxy '*' -fsS -H "Authorization: Bearer $NEWCOMER_EXTERNAL_FEED_TOKEN" \
  'http://127.0.0.1:8011/api/external/newcomers/daily?platform=LINKY&business_date=2026-08-13&revision=1&limit=1&offset=0' >/dev/null
curl --noproxy '*' -fsS -H "Authorization: Bearer $NEWCOMER_EXTERNAL_FEED_TOKEN" \
  'http://127.0.0.1:8011/api/external/fan-conversions/daily?limit=1&offset=0' >/dev/null
systemctl start mcn-daily-data-completion-notifier.service
current_invocation="$(systemctl show mcn-backend.service -p InvocationID --value)"
restart_audit="$("$root/.venv/bin/python" "$root/scripts/mcn_release_governance.py" audit-restart --unit mcn-backend.service)"
"$root/.venv/bin/python" - "$restart_audit" "$release_id" "$current_invocation" <<'PY'
import json,sys
p=json.loads(sys.argv[1]); rid=sys.argv[2]; invocation=sys.argv[3]
assert p['ok'] is True and p['classification']=='attributed_controlled_restart'
assert p['current_invocation_id']==invocation and p['matching_receipt_status']=='passed'
assert f'/{rid}-' in str(p['matching_receipt'])
print('fan_data_newcomer_handshake_release=passed')
PY

trap - ERR
echo phase=complete
