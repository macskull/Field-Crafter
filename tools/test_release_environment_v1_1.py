#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# FIELD_CRAFTER_RELEASE_ENVIRONMENT_TEST_V1
# FIELD_CRAFTER_RELEASE_ENVIRONMENT_TEST_V1_1

EXPECTED_FROZEN_TOKENS = (
    'memory_profiles.json',
    'memory_update_config.json',
    'memory_structural_recovery',
)

RUNTIME_IMPORTS = (
    ('requests', 'requests'),
    ('beautifulsoup4', 'bs4'),
    ('rapidocr', 'rapidocr'),
    ('onnxruntime', 'onnxruntime'),
    ('pillow', 'PIL'),
    ('tkinterdnd2', 'tkinterdnd2'),
)

FACTORY_FILES = (
    'data/homecoming_recipes.sqlite',
    'data/memory_recipe_aliases.json',
    'data/memory_profiles.json',
    'data/memory_update_config.json',
    'data/release_data_summary.json',
    'data/release_database_info.json',
)


def find_root(start: Path) -> Path:
    start=start.resolve()
    for root in [start]+list(start.parents):
        if (root/'src'/'hc_recipe_db'/'version.py').is_file():
            return root
    raise RuntimeError('Could not find Field Crafter source root.')


def diagnostics_dir() -> Path:
    base=os.environ.get('LOCALAPPDATA')
    root=Path(base)/'FieldCrafter' if base else Path.home()/'.field_crafter'
    out=root/'diagnostics'; out.mkdir(parents=True,exist_ok=True)
    return out


def sha256(path: Path) -> str | None:
    if not path.is_file(): return None
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


def fingerprint(root: Path) -> dict[str,str|None]:
    return {name:sha256(root/name) for name in FACTORY_FILES}


def tail(text: str, lines: int=80) -> str:
    parts=(text or '').splitlines()
    return '\n'.join(parts[-lines:])


def run_step(name: str, args: list[str], *, cwd: Path, env: dict[str,str]|None=None, timeout: int=1800) -> dict[str,Any]:
    print(f'== {name} ==', flush=True)
    started=datetime.now(timezone.utc)
    try:
        p=subprocess.run(args,cwd=str(cwd),env=env,capture_output=True,text=True,timeout=timeout)
        result={
            'name':name,
            'passed':p.returncode==0,
            'returncode':p.returncode,
            'seconds':round((datetime.now(timezone.utc)-started).total_seconds(),3),
            'command':[str(x) for x in args],
            'stdout_tail':tail(p.stdout),
            'stderr_tail':tail(p.stderr),
        }
        print(('PASS' if result['passed'] else 'FAIL')+f': {name}', flush=True)
        return result
    except subprocess.TimeoutExpired as exc:
        print(f'FAIL: {name} timed out', flush=True)
        return {
            'name':name,'passed':False,'timeout':True,'seconds':timeout,
            'command':[str(x) for x in args],
            'stdout_tail':tail(exc.stdout or ''),'stderr_tail':tail(exc.stderr or ''),
        }
    except Exception as exc:
        print(f'FAIL: {name}: {exc}', flush=True)
        return {'name':name,'passed':False,'error':str(exc),'command':[str(x) for x in args]}


def read_release_version(root: Path) -> str:
    text=(root/'src'/'hc_recipe_db'/'version.py').read_text(encoding='utf-8')
    m=re.search(r'(?m)^RELEASE_VERSION\s*=\s*"([^"]+)"\s*$',text)
    if not m: raise RuntimeError('Could not read RELEASE_VERSION.')
    return m.group(1)


def venv_python(venv: Path) -> Path:
    return venv/'Scripts'/'python.exe'


def fail_if(step: dict[str,Any]) -> None:
    if not step.get('passed'):
        raise RuntimeError(f"Step failed: {step.get('name')}")


def main() -> int:
    ap=argparse.ArgumentParser(description='Field Crafter release environment/offline wheelhouse dry run')
    ap.add_argument('--root',default='.')
    ap.add_argument('--keep-work',action='store_true')
    args=ap.parse_args()

    result: dict[str,Any] = {
        'passed':False,
        'test_version':'1.1',
        'generated_at_utc':datetime.now(timezone.utc).isoformat(),
        'steps':[],
    }
    work_root: Path|None=None
    try:
        if os.name!='nt':
            raise RuntimeError('This release-environment test must run on Windows.')
        root=find_root(Path(args.root))
        version=read_release_version(root)
        if version!='1.16':
            raise RuntimeError(f'Expected RELEASE_VERSION 1.16, got {version!r}.')

        required=(
            root/'requirements.txt', root/'requirements-ocr.txt', root/'requirements-build.txt',
            root/'field_crafter_1_16.spec', root/'field_crafter_entry.py', root/'src', root/'data',
        )
        missing=[str(p) for p in required if not p.exists()]
        if missing: raise RuntimeError('Missing release input(s): '+', '.join(missing))

        before=fingerprint(root)
        base=diagnostics_dir()
        work_root=Path(tempfile.mkdtemp(prefix='field_crafter_release_env_',dir=str(base)))
        release_venv=work_root/'release_venv'
        offline_venv=work_root/'offline_venv'
        wheelhouse=work_root/'wheelhouse'
        build_dir=work_root/'pyinstaller_build'
        dist_dir=work_root/'pyinstaller_dist'
        wheelhouse.mkdir(parents=True)
        build_dir.mkdir(parents=True)
        dist_dir.mkdir(parents=True)

        result.update({
            'release_version':version,
            'source_root':str(root),
            'work_root':str(work_root),
            'keep_work':bool(args.keep_work),
            'platform':platform.platform(),
            'system_python':sys.executable,
            'factory_hashes_before':before,
        })

        steps=result['steps']
        s=run_step('Create temporary release venv',[sys.executable,'-m','venv',str(release_venv)],cwd=root); steps.append(s); fail_if(s)
        rp=venv_python(release_venv)
        if not rp.is_file(): raise RuntimeError(f'Release venv Python missing: {rp}')

        s=run_step('Update temporary release pip',[str(rp),'-m','pip','install','--upgrade','pip'],cwd=root); steps.append(s); fail_if(s)
        for label,req in (
            ('Install runtime requirements',root/'requirements.txt'),
            ('Install OCR requirements',root/'requirements-ocr.txt'),
            ('Install build requirements',root/'requirements-build.txt'),
        ):
            s=run_step(label,[str(rp),'-m','pip','install','-r',str(req)],cwd=root); steps.append(s); fail_if(s)

        s=run_step('Report PyInstaller version',[str(rp),'-m','PyInstaller','--version'],cwd=root); steps.append(s); fail_if(s)
        result['pyinstaller_version']=s.get('stdout_tail','').strip()

        s=run_step(
            'Build offline runtime wheelhouse',
            [str(rp),'-m','pip','wheel','--wheel-dir',str(wheelhouse),
             '-r',str(root/'requirements.txt'),'-r',str(root/'requirements-ocr.txt')],
            cwd=root,
        ); steps.append(s); fail_if(s)
        wheels=sorted(p.name for p in wheelhouse.glob('*.whl'))
        if not wheels: raise RuntimeError('Wheelhouse contains no .whl files.')
        result['wheelhouse']={'wheel_count':len(wheels),'wheels':wheels}

        s=run_step('Create fresh offline runtime venv',[sys.executable,'-m','venv',str(offline_venv)],cwd=root); steps.append(s); fail_if(s)
        op=venv_python(offline_venv)
        offline_env=dict(os.environ)
        offline_env.update({'PIP_NO_INDEX':'1','PIP_FIND_LINKS':str(wheelhouse),'PIP_DISABLE_PIP_VERSION_CHECK':'1'})
        for label,req in (
            ('Offline install runtime requirements',root/'requirements.txt'),
            ('Offline install OCR requirements',root/'requirements-ocr.txt'),
        ):
            s=run_step(label,[str(op),'-m','pip','install','--no-index','--find-links',str(wheelhouse),'-r',str(req)],cwd=root,env=offline_env); steps.append(s); fail_if(s)

        import_code='; '.join([f'import {module}' for _,module in RUNTIME_IMPORTS])+'; import tkinter; print("runtime imports ok")'
        s=run_step('Offline runtime import smoke test',[str(op),'-c',import_code],cwd=root,env=offline_env); steps.append(s); fail_if(s)
        result['runtime_imports']=[name for name,_ in RUNTIME_IMPORTS]+['tkinter']

        def _pip_list(python_path: Path, label: str, env=None) -> dict[str, str]:
            step = run_step(
                label,
                [str(python_path), '-m', 'pip', 'list', '--format=json'],
                cwd=root,
                env=env,
            )
            steps.append(step)
            fail_if(step)
            try:
                values = json.loads(step.get('stdout_tail') or '[]')
            except Exception as exc:
                raise RuntimeError(f'Could not parse {label}: {exc}') from exc
            return {
                str(item['name']).casefold(): str(item['version'])
                for item in values
                if isinstance(item, dict)
                and item.get('name')
                and item.get('version')
            }

        release_packages = _pip_list(
            rp,
            'Inventory online release-environment packages',
        )
        offline_packages = _pip_list(
            op,
            'Inventory offline runtime packages',
            env=offline_env,
        )

        ignored_runtime_inventory = {'pip', 'setuptools'}
        runtime_mismatches = []
        for package_name, offline_version in sorted(offline_packages.items()):
            if package_name in ignored_runtime_inventory:
                continue
            release_version = release_packages.get(package_name)
            if release_version != offline_version:
                runtime_mismatches.append({
                    'package': package_name,
                    'release_environment_version': release_version,
                    'offline_runtime_version': offline_version,
                })

        result['runtime_dependency_versions'] = {
            'match': not runtime_mismatches,
            'mismatches': runtime_mismatches,
            'offline_package_count': len(offline_packages),
            'release_environment_package_count': len(release_packages),
            'omegaconf': {
                'release_environment': release_packages.get('omegaconf'),
                'offline_runtime': offline_packages.get('omegaconf'),
            },
        }
        if runtime_mismatches:
            raise RuntimeError(
                'Offline wheelhouse resolved a different runtime dependency set: '
                + ', '.join(
                    f"{item['package']} "
                    f"{item['release_environment_version']} != "
                    f"{item['offline_runtime_version']}"
                    for item in runtime_mismatches[:12]
                )
            )

        # Actual temporary PyInstaller build from the production 1.16 spec. This
        # validates spec loading, dependency collection, hidden imports, and the
        # one-file bootloader path without touching the canonical dist/build dirs.
        s=run_step(
            'Temporary PyInstaller one-file build',
            [str(rp),'-m','PyInstaller','--noconfirm','--clean','--log-level','WARN',
             '--distpath',str(dist_dir),'--workpath',str(build_dir),str(root/'field_crafter_1_16.spec')],
            cwd=root,timeout=3600,
        ); steps.append(s); fail_if(s)

        exe=dist_dir/'Field_Crafter_1.16.exe'
        if not exe.is_file(): raise RuntimeError(f'Temporary PyInstaller EXE missing: {exe}')
        exe_size=exe.stat().st_size
        if exe_size<10*1024*1024: raise RuntimeError(f'Temporary EXE unexpectedly small: {exe_size} bytes')
        result['temporary_exe']={'bytes':exe_size,'sha256':sha256(exe)}

        toc_text=''
        toc_files=[]
        for p in build_dir.rglob('*.toc'):
            toc_files.append(str(p.relative_to(work_root)))
            try: toc_text+='\n'+p.read_text(encoding='utf-8',errors='ignore')
            except Exception: pass
        frozen={token:(token in toc_text) for token in EXPECTED_FROZEN_TOKENS}
        result['pyinstaller_analysis']={'toc_files':toc_files,'expected_tokens':frozen}
        if not all(frozen.values()):
            missing=[k for k,v in frozen.items() if not v]
            raise RuntimeError('PyInstaller analysis did not contain expected runtime token(s): '+', '.join(missing))

        after=fingerprint(root)
        result['factory_hashes_after']=after
        result['factory_files_unchanged']=before==after
        if before!=after:
            changed=[k for k in before if before.get(k)!=after.get(k)]
            raise RuntimeError('Release environment dry run modified factory/release data: '+', '.join(changed))

        result['passed']=True
        return_code=0
    except Exception as exc:
        result['error']=str(exc)
        return_code=1
    finally:
        if work_root is not None and not args.keep_work:
            try:
                shutil.rmtree(work_root)
                result['work_root_cleaned']=True
            except Exception as exc:
                result['work_root_cleaned']=False
                result['cleanup_error']=str(exc)
        elif work_root is not None:
            result['work_root_cleaned']=False

        stamp=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        output=diagnostics_dir()/f'field_crafter_release_environment_test_v1_1_{stamp}.json'
        try:
            output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
            print(f"{'PASS' if result.get('passed') else 'FAIL'}: release environment result written to {output}")
        except Exception as exc:
            print(f'Could not write release environment JSON: {exc}',file=sys.stderr)
    return return_code

if __name__=='__main__':
    raise SystemExit(main())
