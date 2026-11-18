"""Build a new isolated customer-pricing acceptance package. Never reset a package."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
MODE = 'LOCAL_SYNTHETIC_CLIENT_PRICING_ONLY'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def build(output, port, repository=ROOT):
    output, repository = Path(output).resolve(), Path(repository).resolve()
    if output.exists() or output.is_relative_to(repository) or repository.is_relative_to(output):
        raise ValueError('Use a NEW directory outside the business repository')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid port')
    with socket.socket() as probe:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(('127.0.0.1', port))
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip()
    # No working database, credentials, uploads, repository metadata or user files.
    archive = subprocess.check_output(['git', 'archive', 'HEAD', 'app', 'migrations', 'alembic.ini'], cwd=repository)
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output/'application'
    snapshot.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(snapshot, filter='data')
    shutil.copytree(ROOT/'tools/client_pricing_acceptance', output/'harness',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    settings = dict(mode=MODE, application_commit=commit, port=port, identity=secrets.token_hex(8),
                    signing_key=secrets.token_urlsafe(48))
    write(output/'private-local.json', json.dumps(settings, indent=2))
    (output/'data').mkdir()
    manifest = dict(mode=MODE, application_commit=commit, port=port, synthetic_only=True,
        actual_sms_enabled=False, native_wechat_verified=False,
        production_code={p.relative_to(snapshot).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in snapshot.rglob('*') if p.is_file()},
        harness_sha256={p.relative_to(output/'harness').as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (output/'harness').rglob('*') if p.is_file()})
    write(output/'acceptance-manifest.json', json.dumps(manifest, indent=2))
    # Reuse only the established hidden-start / occupied-port refusal logic.
    import importlib.util
    spec=importlib.util.spec_from_file_location('home_package_startup',ROOT/'tools/prepare_home_acceptance.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.startup(output)
    write(output/'验收说明.txt', (output/'harness/guide.txt').read_text(encoding='utf-8').replace('{{PORT}}',str(port)))
    subprocess.run([sys.executable,'-B',str(output/'harness/runtime.py'),'--root',str(output),'--initialize'],check=True)
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True);parser.add_argument('--port',required=True,type=int)
    parser.add_argument('--repository',default=str(ROOT))
    args=parser.parse_args();result=build(args.output,args.port,args.repository)
    print(json.dumps({key:result[key] for key in ('application_commit','port','synthetic_only')}))
