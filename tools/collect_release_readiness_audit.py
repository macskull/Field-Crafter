#!/usr/bin/env python3
from __future__ import annotations

import argparse, hashlib, importlib.metadata, json, os, platform, re, subprocess, sys, tempfile, zipfile
from datetime import datetime, timezone
from pathlib import Path

# FIELD_CRAFTER_RELEASE_READINESS_AUDIT_V1

EXPECTED_MEMORY_MODULES = (
    "memory_profiles.py","memory_profile_updates.py","memory_update_crypto.py",
    "memory_diagnostics.py","memory_recovery.py","memory_root_recovery.py",
    "memory_structural_diagnostics.py","memory_structural_recovery.py",
)
EXPECTED_DATA = ("homecoming_recipes.sqlite","memory_profiles.json","memory_update_config.json")
TOP_LEVEL = (
    "prepare_release.py","refresh_release_data.py","make_release_zip.py",
    "release_self_test.py","field_crafter_entry.py","Field Crafter.pyw",
    "requirements.txt","requirements-ocr.txt","requirements-dev.txt",
    "pyproject.toml","setup.cfg","setup.py","DEV_BUILD.json",
)
PATTERNS = (
    "*.spec","build*.py","build*.ps1","build*.bat","build*.cmd",
    "package*.py","package*.ps1","package*.bat","package*.cmd",
    "prepare*.ps1","prepare*.bat","prepare*.cmd",
    "release*.ps1","release*.bat","release*.cmd",
)
SENSITIVE_NAMES = ("signing_key","private_key","secret","token","credential","password",".pem",".pfx",".p12")
SENSITIVE_CONTENT = (
    re.compile(r'"seed_base64"\s*:', re.I),
    re.compile(r'BEGIN [A-Z ]*PRIVATE KEY', re.I),
    re.compile(r'(?i)\b(?:api[_-]?key|access[_-]?token|secret[_-]?key)\s*[:=]\s*["\'][^"\']+'),
)
TEXT_EXTS = {".py",".pyw",".spec",".ps1",".bat",".cmd",".txt",".toml",".cfg",".ini",".json",".md",".yml",".yaml"}


def find_root(start: Path) -> Path:
    start = start.resolve()
    for root in [start] + list(start.parents):
        if (root/"src"/"hc_recipe_db").is_dir():
            return root
    raise RuntimeError("Could not find Field Crafter source root.")


def diag_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    p = (Path(base)/"FieldCrafter" if base else Path.home()/".field_crafter")/"diagnostics"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def sensitive_name(path: Path) -> bool:
    name = path.name.casefold()
    return any(x in name for x in SENSITIVE_NAMES)


def safe_text(path: Path):
    if sensitive_name(path): return None, "sensitive_filename"
    if path.suffix.casefold() not in TEXT_EXTS: return None, "non_text"
    if path.stat().st_size > 2*1024*1024: return None, "over_2mb"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None, "decode_failed"
    if any(p.search(text) for p in SENSITIVE_CONTENT):
        return None, "sensitive_content"
    return text, None


def record(path: Path, root: Path):
    return {"path": rel(path,root),"size":path.stat().st_size,"sha256":sha256(path)}


def cmd(root: Path, args):
    try:
        p = subprocess.run(args,cwd=root,capture_output=True,text=True,timeout=20)
        return {"ok":p.returncode==0,"returncode":p.returncode,"stdout":p.stdout.strip(),"stderr":p.stderr.strip()}
    except Exception as e:
        return {"ok":False,"error":str(e)}


def version(name):
    try: return importlib.metadata.version(name)
    except Exception: return None


def collect_sources(root: Path):
    chosen = {}
    for name in TOP_LEVEL:
        p=root/name
        if p.is_file(): chosen[rel(p,root)] = p
    for pat in PATTERNS:
        for p in root.glob(pat):
            if p.is_file(): chosen[rel(p,root)] = p
    for name in ("src/hc_recipe_db/version.py","src/hc_recipe_db/__init__.py"):
        p=root/name
        if p.is_file(): chosen[rel(p,root)] = p
    safe=[]; skipped=[]
    for name,p in sorted(chosen.items()):
        text,reason=safe_text(p)
        if text is None: skipped.append({"path":name,"reason":reason})
        else: safe.append((p,text))
    return safe,skipped


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--root",default=".")
    args=ap.parse_args()
    try:
        root=find_root(Path(args.root))
        now=datetime.now(timezone.utc); stamp=now.strftime("%Y%m%d_%H%M%S")
        pkg=root/"src"/"hc_recipe_db"; data=root/"data"
        modules=sorted(p.name for p in pkg.glob("*.py") if p.is_file())
        safe_sources,skipped=collect_sources(root)

        git_branch=cmd(root,["git","branch","--show-current"])
        git_commit=cmd(root,["git","rev-parse","HEAD"])
        git_status=cmd(root,["git","status","--short"])
        git_diff=cmd(root,["git","diff","--check"])

        expected_data={}
        for name in EXPECTED_DATA:
            p=data/name
            expected_data[name]=record(p,root) if p.is_file() else {"present":False}

        specs=[record(p,root) for p in sorted(root.glob("*.spec")) if p.is_file()]
        build={}
        for pat in PATTERNS:
            for p in root.glob(pat):
                if p.is_file() and not sensitive_name(p): build[rel(p,root)]=record(p,root)

        dev=None
        p=root/"DEV_BUILD.json"
        if p.is_file():
            try: dev=json.loads(p.read_text(encoding="utf-8"))
            except Exception as e: dev={"error":str(e)}

        report={
            "schema_version":1,
            "audit_version":"release_readiness_v1",
            "generated_at_utc":now.isoformat(),
            "source_root":str(root),
            "environment":{
                "python":sys.version,"executable":sys.executable,
                "platform":platform.platform(),"machine":platform.machine(),
                "packages":{n:version(n) for n in ("pyinstaller","pillow","psutil","cryptography","pywin32")},
            },
            "git":{
                "branch":git_branch.get("stdout") if git_branch.get("ok") else None,
                "commit":git_commit.get("stdout") if git_commit.get("ok") else None,
                "status_short":git_status.get("stdout") if git_status.get("ok") else None,
                "diff_check_passed":bool(git_diff.get("ok") and not git_diff.get("stdout") and not git_diff.get("stderr")),
            },
            "dev_build":dev,
            "modules":{
                "python_modules":modules,
                "expected_memory_modules":{n:n in modules for n in EXPECTED_MEMORY_MODULES},
                "all_expected_memory_modules_present":all(n in modules for n in EXPECTED_MEMORY_MODULES),
            },
            "data":{"directory_present":data.is_dir(),"expected":expected_data},
            "packaging":{
                "spec_files":specs,
                "build_scripts":list(build.values()),
                "safe_source_files_copied":[rel(p,root) for p,_ in safe_sources],
                "safe_source_files_skipped":skipped,
            },
        }
        report["checks"]={
            "has_pyinstaller_spec":bool(specs),
            "has_pyinstaller_installed":bool(report["environment"]["packages"]["pyinstaller"]),
            "all_memory_modules_present":report["modules"]["all_expected_memory_modules_present"],
            "all_required_data_present":all(v.get("path") for v in expected_data.values()),
            "git_diff_check_passed":report["git"]["diff_check_passed"],
        }

        lines=[
            "Field Crafter Release Readiness Audit",
            "====================================",
            f"Generated UTC: {report['generated_at_utc']}",
            f"Git branch: {report['git']['branch'] or 'unknown'}",
            f"Git commit: {report['git']['commit'] or 'unknown'}",
            f"PyInstaller: {report['environment']['packages']['pyinstaller'] or 'not installed'}",
            f".spec files: {len(specs)}",
            f"All memory modules present: {report['modules']['all_expected_memory_modules_present']}",
            f"All required data present: {report['checks']['all_required_data_present']}",
            "",
            "Safety: private signing keys/secrets, databases themselves, caches, build output, and arbitrary user files are not copied.",
        ]

        out=diag_dir()/f"field_crafter_release_readiness_audit_{stamp}.zip"
        with tempfile.TemporaryDirectory(prefix="field_crafter_release_audit_") as td:
            stage=Path(td)
            (stage/"audit.json").write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
            (stage/"audit.txt").write_text("\n".join(lines)+"\n",encoding="utf-8")
            safe_root=stage/"safe_source"
            for source,text in safe_sources:
                dest=safe_root/source.resolve().relative_to(root.resolve())
                dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_text(text,encoding="utf-8")
            with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED,compresslevel=9) as z:
                for p in sorted(stage.rglob("*")):
                    if p.is_file(): z.write(p,p.relative_to(stage))
        print(f"PASS: release-readiness audit written to {out}")
        return 0
    except Exception as e:
        print(f"RELEASE AUDIT FAILED: {e}",file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
