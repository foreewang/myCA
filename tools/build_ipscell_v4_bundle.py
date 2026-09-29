"""Build a checksummed incremental V4 bundle; never change a deployment target."""
import argparse,hashlib,json,shutil,subprocess,zipfile
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FILES=[*sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/'vision/vision').glob('*.py')),
    'vision/run_detect.py','workflow/detect_api.py',
    'workflow/logging_config.py','workflow/camera_process_supervisor.py',
    'vision/experiments/__init__.py','vision/experiments/single_image_rules.py',
    'tests/test_accepted_single_image_rules.py','tests/test_v4_production_rules.py',
    'tests/test_single_image_gpu.py','requirements-vision-texture-gpu.txt',
    'tests/test_logging_integration.py','tests/test_logging_routes.py','docs/logging.md',
    'tests/fixtures/ipscell_green_acceptance.json','tests/fixtures/ipscell_v4_acceptance.json',
    'tools/verify_ipscell_green_acceptance.py',
    'docs/vision.md','docs/ipscell_v4_production_20260927.md']

VERIFY=r'''"""Read-only payload and target verification. Uses Python standard library only."""
import argparse,hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def digest(data):return hashlib.sha256(data).hexdigest()
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',type=Path,required=True)
    p.add_argument('--mode',choices=['before','after'],required=True)
    args=p.parse_args();target=args.target.resolve(strict=True)
    if not target.is_dir():raise SystemExit('Target must be an existing project directory')
    manifest=json.loads((ROOT/'manifest.json').read_text('utf8'));bad=[]
    for record in manifest['files']:
        relative=Path(record['path'])
        if relative.is_absolute() or '..' in relative.parts:raise SystemExit('Unsafe manifest path')
        payload=(ROOT/'payload'/relative).resolve();path=(target/relative).resolve()
        if ROOT/'payload' not in payload.parents or target not in path.parents:raise SystemExit('Path escapes root')
        if not payload.is_file() or digest(payload.read_bytes())!=record['sha256']:
            bad.append('PAYLOAD '+record['path']);continue
        if not path.exists():
            if args.mode=='after' or not record['allow_missing_before']:bad.append('MISSING '+record['path'])
            continue
        if not path.is_file():bad.append('NOT_FILE '+record['path']);continue
        data=path.read_bytes();hashes={digest(data),digest(data.replace(b'\r\n',b'\n'))}
        accepted={record['sha256'],record['sha256_lf']}
        if args.mode=='before':accepted.update(record['allowed_before_sha256'])
        if not hashes&accepted:bad.append('MISMATCH '+record['path'])
    if bad:
        print('\n'.join(bad));raise SystemExit(1)
    print('PASS',args.mode,len(manifest['files']),'files; no target files modified')
if __name__=='__main__':main()
'''

def digest(data):return hashlib.sha256(data).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,required=True,help='source_before.json captured before this promotion')
    p.add_argument('--validation',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args();before={r['path']:r['sha256'] for r in json.loads(args.baseline.read_text('utf8'))}
    args.out.mkdir(parents=True,exist_ok=False);records=[]
    for relative in FILES:
        source=ROOT/relative;data=source.read_bytes();destination=args.out/'payload'/relative
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)
        blob=subprocess.run(['git','show','HEAD:'+relative],cwd=ROOT,capture_output=True)
        hashes={before[relative]} if relative in before else set()
        if blob.returncode==0:hashes.update([digest(blob.stdout),digest(blob.stdout.replace(b'\r\n',b'\n'))])
        backup=args.validation/'backup_before'/relative
        if backup.exists():hashes.add(digest(backup.read_bytes().replace(b'\r\n',b'\n')))
        records.append(dict(path=relative,sha256=digest(data),sha256_lf=digest(data.replace(b'\r\n',b'\n')),
            allowed_before_sha256=sorted(hashes),allow_missing_before=blob.returncode!=0))
    manifest=dict(algorithm='single_image_rules_v4',created_at=datetime.now(timezone.utc).isoformat(),
        git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip(),
        includes_uncommitted_work=True,files=records,
        local_release_gate_passed=False,release_gate_note='Missing ONNX runtime and test dependency version drift; see validation/release_gate.log.',
        deployment_status='Prepared bundle; no remote deployment performed',
        no_configuration_or_data=True)
    (args.out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    (args.out/'verify_sync.py').write_text(VERIFY,encoding='utf8')
    shutil.copy2(ROOT/'docs/ipscell_v4_production_20260927.md',args.out/'README.md')
    evidence=args.out/'validation';evidence.mkdir()
    for name in ('pytest.xml','pytest.log','release_gate.log','runtime_integrity.json','mask_verification.json'):
        source=args.validation/name
        if source.exists():shutil.copy2(source,evidence/name)
    shutil.copy2(args.validation/'acceptance/verification.json',evidence/'acceptance.json')
    archive=args.out.with_suffix('.zip')
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for file in sorted(args.out.rglob('*')):
            if file.is_file():z.write(file,file.relative_to(args.out))
    archive.with_suffix('.zip.sha256').write_text(digest(archive.read_bytes())+'  '+archive.name+'\n',encoding='ascii')
    print(archive)


if __name__=='__main__':main()
