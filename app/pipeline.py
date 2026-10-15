"""Builds Morphe-patched apps and publishes them for the ROM's app updater.

One pass:
  1. resolve the latest stable morphe-patches and morphe-desktop releases
  2. ask the patcher which app version each package should be patched from
  3. for every app whose (app version, patches version) changed since the
     published index: download the original from APKMirror, check it is signed
     by the real publisher, patch and sign it with our keystore
  4. fetch the latest MicroG-RE release
  5. hold the new builds as pending until they are approved on the dashboard,
     then upload them and update index.json, which the on-device updater reads
"""

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import boto3
import requests
import yaml
from bs4 import BeautifulSoup
from pyaxmlparser import APK

log = logging.getLogger("evox-apps")

APP_DIR = Path(__file__).resolve().parent
WORK = Path(os.environ.get("WORK_DIR", "/data"))
CONFIG = yaml.safe_load((APP_DIR / "config.yaml").read_text())

S3_BUCKET = os.environ.get("S3_BUCKET", "public")
S3_PREFIX = os.environ.get("S3_PREFIX", "EvolutionX/apps").strip("/")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "https://s3.msantoki.com/public").rstrip("/")
KEEP_BUILDS = int(os.environ.get("KEEP_BUILDS", "2"))
AUTO_APPROVE = os.environ.get("AUTO_APPROVE", "false").lower() == "true"
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
APKMIRROR = "https://www.apkmirror.com"


# --------------------------------------------------------------------------- utils

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, **kw):
    log.debug("$ %s", " ".join(map(str, cmd)))
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Http:
    """requests session that paces itself, APKMirror answers bursts with 403."""

    def __init__(self, delay=6.0):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.delay = delay
        self.last = 0.0

    def get(self, url, **kw):
        for attempt in range(5):
            wait = self.delay - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            r = self.s.get(url, timeout=60, **kw)
            if r.status_code in (403, 429, 503):
                backoff = 60 * (attempt + 1)
                log.warning("%s -> %s, retrying in %ss", url, r.status_code, backoff)
                time.sleep(backoff)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError(f"giving up on {url}")

    def download(self, url, dest):
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self.get(url, stream=True) as r, open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        tmp.rename(dest)
        return dest


def github_latest(repo):
    """Latest non-prerelease release of a GitHub repo."""
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest",
                     headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_release_asset(http, repo, pattern, cache):
    rel = github_latest(repo)
    for a in rel["assets"]:
        if re.fullmatch(pattern, a["name"]):
            dest = cache / a["name"]
            if not dest.exists():
                log.info("downloading %s %s", repo, a["name"])
                http.download(a["browser_download_url"], dest)
            return rel["tag_name"], dest
    raise RuntimeError(f"{repo} {rel['tag_name']}: no asset matching {pattern}")


# --------------------------------------------------------------------------- S3

class Store:
    def __init__(self):
        self.s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY"],
            aws_secret_access_key=os.environ["S3_SECRET_KEY"],
            region_name=os.environ.get("S3_REGION", "us-east-1"),
        )

    def key(self, *parts):
        return "/".join([S3_PREFIX, *parts])

    def url(self, key):
        return f"{PUBLIC_BASE}/{key}"

    def get_json(self, key):
        try:
            obj = self.s3.get_object(Bucket=S3_BUCKET, Key=key)
        except self.s3.exceptions.NoSuchKey:
            return None
        return json.loads(obj["Body"].read())

    def put_json(self, key, data):
        self.s3.put_object(Bucket=S3_BUCKET, Key=key,
                           Body=json.dumps(data, indent=2).encode(),
                           ContentType="application/json",
                           CacheControl="no-cache")

    def put_file(self, key, path):
        self.s3.upload_file(str(path), S3_BUCKET, key, ExtraArgs={
            "ContentType": "application/vnd.android.package-archive"})

    def delete(self, key):
        self.s3.delete_object(Bucket=S3_BUCKET, Key=key)


# --------------------------------------------------------------------------- tools

class Tools:
    def __init__(self, patcher_jar, patches_mpp, keystore):
        self.jar = patcher_jar
        self.mpp = patches_mpp
        self.keystore = keystore

    def morphe(self, *args):
        return run(["java", "-jar", str(self.jar), *map(str, args)])

    def recommended_versions(self):
        """{package: newest version with the most patches}, from list-versions."""
        out = self.morphe("list-versions", "--patches", self.mpp)
        text = out.stdout + out.stderr
        result, pkg = {}, None
        for line in text.splitlines():
            m = re.search(r"Package name:\s*(\S+)", line)
            if m:
                pkg = m.group(1)
                continue
            m = re.match(r"\s+(\S+) \((\d+) patch", line)
            if m and pkg and pkg not in result:
                result[pkg] = m.group(1)
        return result

    def patch(self, src, out, result_file, choices):
        self.morphe(
            "patch", "-p", self.mpp, *patch_flags(choices),
            "--keystore", self.keystore,
            "--keystore-password", os.environ["KEYSTORE_PASSWORD"],
            "--keystore-entry-alias", os.environ.get("KEYSTORE_ALIAS", "evox"),
            "--keystore-entry-password", os.environ["KEYSTORE_PASSWORD"],
            "--signer", "EvolutionX",
            "--striplibs", CONFIG["abi"],
            "-o", out, "-r", result_file,
            "-t", WORK / "tmp",
            src,
        )


def patch_flags(choices):
    """-e/-d/-O flags for {patch name: {enabled, options}}. -O applies to the
    patch named by the -e before it."""
    flags = []
    for name, choice in (choices or {}).items():
        if choice.get("enabled") is False:
            flags += ["-d", name]
            continue
        flags += ["-e", name]
        for key, value in (choice.get("options") or {}).items():
            if isinstance(value, bool):
                value = str(value).lower()
            flags.append(f"-O{key}" if value is None else f"-O{key}={value}")
    return flags


def signer_digests(apk):
    out = run(["java", "-jar", str(APP_DIR / "apksigner.jar"), "verify", "--print-certs", str(apk)])
    return sorted({m.group(1) for line in out.stdout.splitlines()
                   if not line.startswith("Source Stamp")
                   for m in [re.search(r"Signer.*certificate SHA-256 digest: ([0-9a-f]{64})", line)] if m})



# --------------------------------------------------------------------------- APKMirror

def apkmirror_release_page(http, app, version):
    """URL path of the APKMirror release page for an exact version."""
    dev_app = app["apkmirror"]
    slug_guess = f"/apk/{dev_app}/{dev_app.split('/')[1]}-{version.replace('.', '-')}-release/"
    q = f"{app['name']} {version}".replace(" ", "+")
    page = http.get(f"{APKMIRROR}/?post_type=app_release&searchtype=apk&s={q}").text
    links = set(re.findall(rf'href="(/apk/{re.escape(dev_app)}/[^"]+-release/)"', page))
    want = f"-{version.replace('.', '-')}-release/"
    for link in sorted(links):
        if link.endswith(want):
            return link
    if slug_guess in links:
        return slug_guess
    raise RuntimeError(f"APKMirror has no release page for {app['name']} {version}")


def pick_variant(http, release_path, abi):
    """Best download page on a release: our ABI or universal, single APK over bundle."""
    soup = BeautifulSoup(http.get(APKMIRROR + release_path).text, "html.parser")
    best = None
    for row in soup.select("div.variants-table div.table-row"):
        cells = row.select("div.table-cell")
        link = row.select_one('a[href$="-android-apk-download/"]')
        if len(cells) < 4 or not link:
            continue
        arch = cells[1].get_text(" ", strip=True)
        dpi = cells[3].get_text(" ", strip=True)
        kind = "BUNDLE" if "BUNDLE" in cells[0].get_text() else "APK"
        if abi in arch:
            arch_score = 3 if arch == abi else 2
        elif arch in ("universal", "noarch"):
            arch_score = 1
        else:
            continue
        score = (arch_score, kind == "APK", dpi == "nodpi")
        if best is None or score > best[0]:
            best = (score, link["href"], kind)
    if not best:
        raise RuntimeError(f"no {abi} variant on {release_path}")
    return best[1], best[2]


def apkmirror_download(http, variant_path, dest):
    page = http.get(APKMIRROR + variant_path).text
    m = re.search(r'href="([^"]+/download/\?key=[^"]+)"', page)
    if not m:
        raise RuntimeError(f"no download button on {variant_path}")
    page = http.get(APKMIRROR + m.group(1).replace("&amp;", "&")).text
    m = re.search(r'href="(/wp-content/themes/APKMirror/download\.php\?[^"]+)"', page)
    if not m:
        raise RuntimeError(f"no final download link on {variant_path}")
    return http.download(APKMIRROR + m.group(1).replace("&amp;", "&"), dest)


def original_signers(path):
    """Signers of the original: the APK itself, or base.apk inside a bundle."""
    if path.suffix == ".apk":
        return signer_digests(path)
    base = path.with_suffix(".base.apk")
    with open(base, "wb") as f:
        subprocess.run(["unzip", "-p", str(path), "base.apk"], stdout=f, check=True)
    try:
        return signer_digests(base)
    finally:
        base.unlink()


# --------------------------------------------------------------------------- state

class State:
    """Pending builds, rejections and run history, kept in /data/state.json.

    Published builds live in the public index.json on S3. A new build waits in
    `pending` (its APK stays on the local volume, not public) until it is
    approved from the dashboard, unless AUTO_APPROVE is on.
    """

    def __init__(self, path=None):
        self.path = path or WORK / "state.json"
        self.lock = threading.RLock()
        self.data = {"pending": {}, "rejected": [], "history": [], "lastRun": None}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)

    def event(self, action, app_id, build, detail="", name=None):
        with self.lock:
            self.data["history"].insert(0, {"time": now_iso(), "action": action, "id": app_id,
                                            "name": name or app_name(app_id), "build": build,
                                            "detail": detail})
            del self.data["history"][100:]
            self.save()


def app_name(app_id):
    for a in [*CONFIG["apps"], CONFIG["microg"]]:
        if a["id"] == app_id:
            return a["name"]
    return app_id


def release_links(app, version, patches_tag):
    return {
        "patchesNotes": f"https://github.com/{CONFIG['patches_repo']}/releases/tag/{patches_tag}",
        "appPage": f"{APKMIRROR}/apk/{app['apkmirror']}/",
    }


# --------------------------------------------------------------------------- build

def build_app(http, tools, app, version, patches_tag):
    """Patch one app into WORK/pending. Returns its pending entry."""
    build = f"{version}+{patches_tag}"
    log.info("%s: building %s", app["id"], build)
    cache = WORK / "originals"
    cache.mkdir(parents=True, exist_ok=True)

    release = apkmirror_release_page(http, app, version)
    variant, kind = pick_variant(http, release, CONFIG["abi"])
    original = cache / f"{app['id']}-{version}.{'apkm' if kind == 'BUNDLE' else 'apk'}"
    if not original.exists():
        log.info("%s: downloading %s", app["id"], variant)
        apkmirror_download(http, variant, original)

    signers = original_signers(original)
    pinned = app.get("signers") or []
    if pinned and not set(signers) & set(pinned):
        original.unlink()
        raise RuntimeError(f"{app['id']}: original signed by {signers}, expected {pinned}")
    if not pinned:
        log.warning("%s: no pinned signer, original is signed by %s (pin it in config.yaml)",
                    app["id"], signers)

    out_dir = WORK / "pending" / app["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    patched = out_dir / f"{app['id']}-{version}-{patches_tag}.apk"
    result = out_dir / "result.json"
    tools.patch(original, patched, result, app.get("patches"))
    res = json.loads(result.read_text())
    result.unlink()
    failed = [s for s in res.get("patchingSteps", []) if not s.get("success")]
    if failed:
        raise RuntimeError(f"{app['id']}: patching failed: {failed}")

    for old in cache.glob(f"{app['id']}-*"):
        if old != original:
            old.unlink()
    return describe(patched, {
        "id": app["id"],
        "name": app["name"],
        "originalPackage": app["package"],
        "versionName": version,
        "patches": patches_tag,
        "build": build,
        "appliedPatches": sorted(p["name"] for p in res.get("appliedPatches", [])),
        "originalSigners": signers,
        "links": release_links(app, version, patches_tag),
    })


def build_microg(http):
    cfg = CONFIG["microg"]
    rel = github_latest(cfg["repo"])
    version = rel["tag_name"]
    out_dir = WORK / "pending" / cfg["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    _, apk = fetch_release_asset(http, cfg["repo"], re.escape(cfg["asset"].format(version=version)), out_dir)
    return describe(apk, {
        "id": cfg["id"],
        "name": cfg["name"],
        "versionName": version,
        "build": version,
        "links": {"releaseNotes": rel["html_url"]},
    })


def describe(apk, meta):
    pkg, code = package_of(apk)
    return {
        **meta,
        "package": pkg,
        "versionCode": code,
        "file": str(apk),
        "size": apk.stat().st_size,
        "sha256": sha256_file(apk),
        "signer": signer_digests(apk),
        "built": now_iso(),
    }


def package_of(apk):
    """(package name, versionCode) read from the APK manifest."""
    a = APK(str(apk))
    return a.package, int(a.version_code)


# --------------------------------------------------------------------------- publish

def app_order():
    return [a["id"] for a in CONFIG["apps"]] + [CONFIG["microg"]["id"]]


def approve(state, store, app_id, by="dashboard"):
    """Upload a pending build and put it in the public index."""
    with state.lock:
        entry = state.data["pending"].get(app_id)
        if not entry:
            raise KeyError(f"no pending build for {app_id}")
        apk = Path(entry["file"])
        index_key = store.key("index.json")
        index = store.get_json(index_key) or {"schema": 1, "apps": []}
        current = {a["id"]: a for a in index.get("apps", [])}

        key = store.key(app_id, apk.name)
        log.info("%s: publishing %s", app_id, key)
        store.put_file(key, apk)
        published = {k: v for k, v in entry.items() if k not in ("file", "built")}
        published.update({"url": store.url(key), "key": key, "published": now_iso()})
        prune(store, app_id, current.get(app_id), published)

        current[app_id] = published
        index["apps"] = [current[i] for i in app_order() if i in current]
        index["generated"] = now_iso()
        if entry.get("patches"):
            index["patches"] = entry["patches"]
        store.put_json(index_key, index)

        apk.unlink(missing_ok=True)
        del state.data["pending"][app_id]
        state.event("approved", app_id, entry["build"], by)
        write_status(state, store)
        return published


def reject(state, app_id, by="dashboard"):
    """Drop a pending build. The same build is not made again."""
    with state.lock:
        entry = state.data["pending"].pop(app_id, None)
        if not entry:
            raise KeyError(f"no pending build for {app_id}")
        Path(entry["file"]).unlink(missing_ok=True)
        state.data["rejected"].append(f"{app_id}:{entry['build']}")
        state.event("rejected", app_id, entry["build"], by)


def prune(store, app_id, old_entry, new_entry):
    """Keep the new build plus KEEP_BUILDS-1 previous ones."""
    history = []
    if old_entry:
        history = [old_entry["key"], *old_entry.get("previous", [])]
    keep, drop = history[:KEEP_BUILDS - 1], history[KEEP_BUILDS - 1:]
    new_entry["previous"] = [k for k in keep if k != new_entry["key"]]
    for k in drop:
        if k != new_entry["key"]:
            log.info("%s: removing old build %s", app_id, k)
            store.delete(k)


def write_status(state, store):
    run = state.data.get("lastRun") or {}
    store.put_json(store.key("status.json"), {
        "checked": run.get("finished"),
        "errors": run.get("errors", []),
        "pending": {k: v["build"] for k, v in state.data["pending"].items()},
    })


def notify(text):
    """Telegram message, when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30,
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
    except Exception:
        log.exception("telegram notification failed")


# --------------------------------------------------------------------------- run

def keystore_path():
    path = WORK / "signing.p12"
    path.write_bytes(base64.b64decode(os.environ["KEYSTORE_B64"]))
    path.chmod(0o600)
    return path


def run_once(state, store=None):
    """Check for new versions and build them into pending."""
    WORK.mkdir(parents=True, exist_ok=True)
    tools_dir = WORK / "tools"
    tools_dir.mkdir(exist_ok=True)
    http = Http()
    store = store or Store()
    run = {"started": now_iso(), "finished": None, "errors": [], "built": []}
    with state.lock:
        state.data["lastRun"] = run
        state.save()

    try:
        patcher_tag, jar = fetch_release_asset(http, CONFIG["patcher_repo"], r".*-all\.jar", tools_dir)
        patches_tag, mpp = fetch_release_asset(http, CONFIG["patches_repo"], r".*\.mpp", tools_dir)
        for f in [*tools_dir.glob("*.jar"), *tools_dir.glob("*.mpp")]:
            if f not in (jar, mpp):
                f.unlink()
        run.update({"patcher": patcher_tag, "patches": patches_tag})
        log.info("patcher %s, patches %s", patcher_tag, patches_tag)

        tools = Tools(jar, mpp, keystore_path())
        versions = tools.recommended_versions()
        run["supported"] = versions
        log.info("recommended versions: %s", versions)

        index = store.get_json(store.key("index.json")) or {"apps": []}
        published = {a["id"]: a.get("build") for a in index.get("apps", [])}

        def wanted(app_id, build):
            if published.get(app_id) == build:
                log.info("%s: %s already published", app_id, build)
                return False
            pend = state.data["pending"].get(app_id)
            if pend and pend["build"] == build:
                log.info("%s: %s already waiting for approval", app_id, build)
                return False
            if f"{app_id}:{build}" in state.data["rejected"]:
                log.info("%s: %s was rejected, skipping", app_id, build)
                return False
            return True

        def add_pending(entry):
            with state.lock:
                old = state.data["pending"].get(entry["id"])
                if old and old["file"] != entry["file"]:
                    Path(old["file"]).unlink(missing_ok=True)
                state.data["pending"][entry["id"]] = entry
                state.save()
            state.event("built", entry["id"], entry["build"])
            run["built"].append(f"{entry['name']} {entry['versionName']}")

        for app in CONFIG["apps"]:
            try:
                version = versions.get(app["package"])
                if not version:
                    raise RuntimeError(f"patches {patches_tag} support no version of {app['package']}")
                if wanted(app["id"], f"{version}+{patches_tag}"):
                    add_pending(build_app(http, tools, app, version, patches_tag))
            except Exception as e:  # keep going with the other apps
                log.exception("%s failed", app["id"])
                run["errors"].append(f"{app['id']}: {e}")

        try:
            cfg = CONFIG["microg"]
            if wanted(cfg["id"], github_latest(cfg["repo"])["tag_name"]):
                add_pending(build_microg(http))
        except Exception as e:
            log.exception("microg failed")
            run["errors"].append(f"microg: {e}")
    except Exception as e:
        log.exception("run failed")
        run["errors"].append(str(e))

    run["finished"] = now_iso()
    with state.lock:
        state.save()

    if AUTO_APPROVE:
        for app_id in list(state.data["pending"]):
            approve(state, store, app_id, by="auto")
    elif run["built"]:
        notify("New EvoX app builds waiting for approval: " + ", ".join(run["built"])
               + (f"\n{DASHBOARD_URL}" if DASHBOARD_URL else ""))
    write_status(state, store)
    log.info("check finished: %d built, %d errors", len(run["built"]), len(run["errors"]))
    return not run["errors"]


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    ok = run_once(State())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
