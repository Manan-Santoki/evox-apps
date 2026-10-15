# evox-apps

Build pipeline for the patched apps that ship with my unofficial Evolution X
builds (Xiaomi Pad 6, `pipa`). It produces YouTube, YouTube Music and Reddit
patched with [Morphe](https://github.com/MorpheApp), plus MicroG-RE, and
publishes them for the ROM's built-in app updater.

The apps keep their stock look (original icon and name) and get every Morphe
feature, which lives under **Settings → Morphe** inside each app.

## How it works

Every few hours the pipeline:

1. Fetches the latest stable [morphe-patches](https://github.com/MorpheApp/morphe-patches)
   and [morphe-desktop](https://github.com/MorpheApp/morphe-desktop) releases.
2. Asks the patcher which version of each app it supports best.
3. For any app whose app version or patches version changed:
   - downloads the original from APKMirror,
   - rejects it unless it is signed by the real publisher (pinned certificate
     digests in [`app/config.yaml`](app/config.yaml)),
   - patches it and signs it with the release key,
   - uploads it.
4. Mirrors the latest stable [MicroG-RE](https://github.com/MorpheApp/MicroG-RE),
   which the Google apps need for sign-in.
5. Rewrites `index.json`, which the updater on the device reads.

Published files: `https://s3.msantoki.com/public/EvolutionX/apps/`

- `index.json`: every app with its package, version, download URL, SHA-256,
  size and signing certificate
- `status.json`: time of the last check and any errors

## Verifying an APK

Every patched APK is signed with the same release key. Its certificate SHA-256
is:

```
b535ab8d88644e90add44cb43725cbd9807471d71c730d7f38b8ebb2f2eb0276
```

Check a download with `apksigner verify --print-certs app.apk` and compare the
digest, and compare `sha256sum` against `index.json`.

## Patch choices

[`app/config.yaml`](app/config.yaml) lists only the changes from Morphe's
defaults. Right now that is setting the original icon and name for YouTube and
YouTube Music. Everything else is Morphe's default selection.

To see every patch and option:

```sh
java -jar morphe-desktop-*-all.jar list-patches --patches patches.mpp -p -v -o -u > patches.txt
python3 tools/catalog.py patches.txt > catalog.json
```

## Running it

It runs as a Docker Compose app (deployed with Dokploy):

```sh
cp .env.example .env    # fill in S3 credentials and the keystore
docker compose up -d --build
```

One-off run: `docker compose run --rm pipeline python3 pipeline.py --once`

| Variable | Purpose |
| --- | --- |
| `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_PREFIX` | Where builds are uploaded |
| `PUBLIC_BASE_URL` | Public URL of the bucket, used for links in `index.json` |
| `KEYSTORE_B64`, `KEYSTORE_PASSWORD`, `KEYSTORE_ALIAS` | Release signing key (PKCS12, base64) |
| `CHECK_INTERVAL_HOURS` | Time between checks, default 6 |
| `GITHUB_TOKEN` | Optional, raises the GitHub API rate limit |

The signing key must never change: Android only installs an update over an
app signed with the same key.

## Credits

[Morphe](https://github.com/MorpheApp) for the patches, patcher and MicroG-RE.
This repo only automates building with them.

## License

GPL-3.0, the same as Morphe's patcher and patches.
