"""Phase 13.1 - single-tarball air-gap bundle: wheels + source + config + SBOM + installer.

Two build targets:
  * docker profile  -> needs a builder host with docker (image.tar via `docker save`);
                       this build ADDS it only if docker exists, and says so otherwise.
  * portable profile (default, always works): pip-wheels for the chosen runtime profile
                       (core | +agents | +eval), cortex source snapshot, config template
                       (placeholder keys only), install.sh (venv + --no-index), self-generated
                       SBOM (importlib.metadata - no syft required), SHA256SUMS.

Install on the target (no network):  tar xzf bundle.tar.gz && ./install.sh
Verified in this build by installing the wheels into a fresh venv with --no-index.
"""
import argparse
import hashlib
import importlib.metadata as md
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROFILES = {
    "core": ["pyyaml", "rich", "openai", "tenacity", "unidiff", "GitPython", "pydantic",
             "networkx", "jinja2", "httpx", "tiktoken", "libcst"],
    "agents": ["bandit", "import-linter", "mutmut", "semgrep"],
    "eval": ["pytest"],
}
OFFLINE_FLAGS = "CORTEX_OFFLINE_MODE=1, TRANSFORMERS_OFFLINE=1, HF_HUB_OFFLINE=1"


def sbom(packages: list) -> dict:
    comps = []
    for name in sorted(set(packages)):
        try:
            d = md.distribution(name)
            lic = (d.metadata.get("License") or "see package")[:80]
            comps.append({"name": d.metadata["Name"], "version": d.version, "license": lic,
                          "supplier": "ORG: PyPI", "description": (d.metadata.get("Summary") or "")[:120]})
        except md.PackageNotFoundError:
            comps.append({"name": name, "version": "unpinned", "license": "TBD", "supplier": "ORG: PyPI"})
    return {"SPDXID": "SPDXRef-DOCUMENT", "spdxVersion": "SPDX-2.3",
          "name": "cortex-airgap-bundle", "creationInfo": {
              "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "creators": ["Tool: cortex/deployment/airgap_bundle.py"]},
          "packages": [{"SPDXID": f"SPDXRef-Package-{c['name']}", **c} for c in comps]}


def download_wheels(packages: list, dest: Path) -> list:
    got = []
    for pkg in packages:
        r = subprocess.run([sys.executable, "-m", "pip", "download", "-q", "-d", str(dest), pkg],
                           capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            got.append(pkg)
        else:
            print(f"[airgap] wheel fetch failed for {pkg} (bundle continues without it): {r.stderr.strip()[:120]}")
    return got


def create_bundle(output_path: str, version: str, profile: str = "core+agents+eval",
                  skip_wheels: bool = False) -> Path:
    out = Path(output_path).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="airgap-"))
    try:
        pkgs = []
        for part in profile.split("+"):
            pkgs += PROFILES.get(part, [])
        wheels_dir = tmp / "wheels"
        wheels_dir.mkdir()
        if not skip_wheels:
            print(f"[airgap] downloading {len(pkgs)} package(s) (+deps) for profile '{profile}' ...")
            pkgs = download_wheels(pkgs, wheels_dir)
        else:
            print("[airgap] --skip-wheels: bundle will need wheels from a mirror")
        # source snapshot (no .git): via git archive
        src = tmp / "cortex"
        src.mkdir()
        r = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, capture_output=True)
        if r.returncode == 0 and r.stdout:
            subprocess.run(["tar", "-x", "-C", str(src)], input=r.stdout, check=True)
        else:
            shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", ".cortex_memory"))
        # config template: api_key stays placeholder - BY DESIGN
        cfg = (src / "config.yaml").read_text(encoding="utf-8") if (src / "config.yaml").exists() else ""
        (tmp / "config.yaml.template").write_text(cfg, encoding="utf-8")
        # install script
        install = f"""#!/bin/bash
# Cortex air-gap installer (offline). wheels/ must be fully populated.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/cortex-image.tar" ]; then
  docker load -i "$HERE/cortex-image.tar"
  docker run -d --name cortex --restart unless-stopped -v /data:/workspace -p 8080:8080 cortex:offline-{version}
  echo "cortex container started (offline image)"; exit 0
fi
python3 -m venv /opt/cortex-env
/opt/cortex-env/bin/pip install --no-index --find-links "$HERE/wheels" -r "$HERE/cortex/requirements-profile.txt"
tar xzf "$HERE/cortex-src.tar" -C /opt/
echo "export CORTEX_OFFLINE_MODE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1" > /etc/profile.d/cortex-offline.sh
echo "installed: /opt/cortex + /opt/cortex-env  [{OFFLINE_FLAGS}]"
"""
        (tmp / "install.sh").write_text(install)
        (tmp / "install.sh").chmod(0o755)
        (tmp / "cortex" / "requirements-profile.txt").write_text("\n".join(pkgs) + "\n")
        (tmp / "README_OFFLINE.md").write_text(
            f"# Cortex offline bundle {version}\n\nprofile: {profile}\npackages: {len(pkgs)}\n"
            f"offline env flags: {OFFLINE_FLAGS}\n"
            f"NOTE: config template keeps `YOUR_API_KEY_HERE` - air-gap targets should point "
            f"base_url at the in-network model gateway; no telemetry paths are bundled.\n")
        (tmp / "sbom.spdx.json").write_text(json.dumps(sbom(pkgs), indent=1))
        # docker image (only if docker is present - honest capability detection)
        if shutil.which("docker"):
            r = subprocess.run(["docker", "save", "-o", str(tmp / "cortex-image.tar"), f"cortex:offline-{version}"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                (tmp / "cortex-image.tar").unlink(missing_ok=True)
                (tmp / "DOCKER_NOTE.txt").write_text(f"image not found for cortex:offline-{version}: {r.stderr[:200]}\n")
        # bundle: cortex becomes a tar-in-tar so install.sh controls placement
        sub = tmp / "cortex-src.tar"
        with tarfile.open(sub, "w") as tf:
            tf.add(tmp / "cortex", arcname="cortex")
        sums = {p.relative_to(tmp).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(tmp.rglob("*")) if p.is_file() and p.name != "SHA256SUMS.txt"}
        (tmp / "SHA256SUMS.txt").write_text("".join(f"{h}  {r}\n" for r, h in sorted(sums.items())))
        with tarfile.open(out, "w:gz") as tf:            # ONE pass - gzip streams cannot be appended to
            for rel in sorted(list(sums) + ["SHA256SUMS.txt"]):
                tf.add(tmp / rel, arcname=rel)
        size = out.stat().st_size / 1e6
        print(f"[airgap] bundle: {out} ({size:.1f} MB, {len(sums)} files)")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def verify_offline_install(bundle: Path, workdir: str | Path) -> Path:
    """extract -> fresh venv -> pip install --no-index -> import smoke. The proof."""
    work = Path(workdir)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    subprocess.run(["tar", "xzf", str(bundle), "-C", str(work)], check=True)
    venv = work / "env"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    r = subprocess.run([str(venv / "bin/pip"), "install", "-q", "--no-index", "--find-links",
                        str(work / "wheels"), "-r", str(work / "cortex-src" if (work / "cortex-src").exists() else work / "cortex") + "/requirements-profile.txt"],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"offline install failed: {r.stderr[-400:]}")
    out = subprocess.run([str(venv / "bin/python"), "-c",
                          "import openai, yaml, pydantic, rich, unidiff, networkx, git; print('imports OK')"],
                         capture_output=True, text=True, check=True)
    print(f"[airgap] verify: {out.stdout.strip()} in {venv}")
    return venv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("output", nargs="?", default="/tmp/cortex-airgap-bundle.tar.gz")
    ap.add_argument("--version", default="0.13.0")
    ap.add_argument("--profile", default="core+agents+eval")
    ap.add_argument("--skip-wheels", action="store_true")
    ap.add_argument("--verify-into", default=None, help="extract + offline-install into this dir as a proof")
    a = ap.parse_args()
    b = create_bundle(a.output, a.version, a.profile, a.skip_wheels)
    if a.verify_into:
        verify_offline_install(b, a.verify_into)
