"""Local-only external observer. No notification transport, database access or service mutation."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

from tools.staging_backup_job import private_directory, exclusive_lock, write_json, BusyError
from tools.staging_backup_watch import age_seconds
from tools.staging_offsite_backup import read_json


# Fixed remote program, stdin transport: no config-derived shell command.
REMOTE = r"""
import json,subprocess,time
from pathlib import Path
p=Path('/srv/saas-backups/watch/saas-pg-test-cvm/latest.json')
result={'server_epoch':time.time()}
try:
    if not p.is_file() or p.stat().st_size>65536: raise ValueError()
    report=json.loads(p.read_text())
    result['watch']={k:report.get(k) for k in ('project','checked_at','status','notification')}
except Exception:
    result['watch']=None
result['timers']={}
for name in ('backup','backup-watch'):
    process=subprocess.run(['systemctl','is-active','saas-staging-'+name+'@saas-pg-test-cvm.timer'],capture_output=True,text=True,timeout=10)
    result['timers'][name]=process.returncode==0 and process.stdout.strip()=='active'
print(json.dumps(result))
"""


def probe(config):
    host = config.get('host')
    if not isinstance(host,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+',host):
        raise ValueError('Invalid SSH target')
    for name in ('key','known_hosts'):
        path=Path(config[name])
        if not path.is_file() or any(p.is_symlink() for p in (path,*path.parents)):
            raise ValueError('Invalid SSH material path')
    command=[config['ssh'],'-i',config['key'],'-o','IdentitiesOnly=yes','-o','BatchMode=yes',
             '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+config['known_hosts'],
             '-o','ConnectTimeout=12','-o','ConnectionAttempts=1',host,'sudo -n python3 -']
    try:
        result=subprocess.run(command,input=REMOTE.encode(),capture_output=True,timeout=35,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {'code':'remote_unreachable_or_ssh_failed'}
    if result.returncode:
        # Never infer a server power outage from an SSH/network/authentication error.
        return {'code':'remote_unreachable_or_ssh_failed'}
    if len(result.stdout)>65536:
        return {'code':'remote_response_invalid'}
    try:
        value=json.loads(result.stdout)
        if not isinstance(value,dict): raise ValueError()
        return {'code':'received','value':value}
    except Exception:
        return {'code':'remote_response_invalid'}


def classify(response, now):
    if response.get('code') != 'received':
        return response['code'] if response.get('code') in ('remote_unreachable_or_ssh_failed','remote_response_invalid') else 'remote_response_invalid'
    try:
        value=response['value']
        epoch=value['server_epoch']
        if type(epoch) not in (int,float) or not abs(now.timestamp()-epoch)<=300:
            return 'observer_server_clock_mismatch'
        if value['timers'] != {'backup':True,'backup-watch':True}:
            return 'remote_timer_inactive'
        report=value['watch']
        if not isinstance(report,dict) or report.get('project')!='saas-pg-test-cvm':
            return 'remote_watch_invalid'
        if age_seconds(report['checked_at'],now)>15*60:
            return 'remote_watch_stale'
        if report.get('status')=='alert':
            return 'remote_watch_alert'
        if report.get('status')!='ok':
            return 'remote_watch_invalid'
        if report.get('notification') not in ('not_needed','delivered'):
            return 'remote_notification_problem'
        return 'ok'
    except Exception:
        return 'remote_response_invalid'


def observe(config_path, *, now=None, reader=probe):
    if os.environ.get('DATABASE_URL'):
        raise ValueError('No database configuration accepted')
    config=read_json(Path(config_path))
    if config.get('project')!='saas-pg-test-cvm':
        raise ValueError('Dedicated staging project required')
    # This observer deliberately has no notification configuration or sender.
    if any(k in config for k in ('webhook','webhook_file','email','notification_url')):
        raise ValueError('Outbound notifications are not supported')
    root=private_directory(Path(config['state_root']))
    now=now or datetime.now(timezone.utc)
    with exclusive_lock(root/'.observer.lock'):
        prior=read_json(root/'latest.json') if (root/'latest.json').exists() else {}
        if prior and prior.get('project')!=config['project']:
            raise ValueError('Wrong observer state owner')
        code=classify(reader(config),now)
        # A long PC sleep/logout gap starts a new observation streak.
        continuous=bool(prior) and 0<=age_seconds(prior['checked_at'],now)<=10*60
        failures=(prior.get('consecutive_failures',0) if continuous else 0)+1 if code!='ok' else 0
        status='ok' if code=='ok' else 'alert' if failures>=2 else 'pending'
        changed=prior.get('status')!=status or prior.get('code')!=code
        report=dict(format=1,project=config['project'],checked_at=now.isoformat(),status=status,
                    code=code,consecutive_failures=failures,observation_gap_reset=bool(prior) and not continuous,
                    previous_checked_at=prior.get('checked_at'),outbound_notifications=False)
        if changed:
            events=private_directory(root/'events')
            import uuid
            write_json(events/(uuid.uuid4().hex+'.json'),report)
        write_json(root/'latest.json',report)
        return report


def main():
    if os.name == "nt":
        from tools.windows_background import prepare_stdio
        prepare_stdio()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args()
    try:
        report=observe(args.config)
    except BusyError:
        report={'status':'skipped','code':'observer_busy','outbound_notifications':False}
    except Exception:
        report={'status':'alert','code':'observer_configuration_or_state_failed','outbound_notifications':False}
    print(json.dumps(report))
    return 0 if report['status'] in ('ok','pending','skipped') else 1


if __name__=='__main__':
    raise SystemExit(main())
