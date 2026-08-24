# immich-concourse-resource

A custom [Concourse](https://concourse-ci.org/) resource type for pushing build-directory assets into [Immich](https://immich.app/) via its REST API: `put`-only, with optional album and tag auto-provisioning and XMP sidecar binding.

## Origin and status

The first draft of this resource type (check/in/out scripts, Dockerfile, pipeline snippet) came out of an AI research session (Gemini), not from Immich's own docs or an existing reference implementation. That draft got the overall shape right but had several concrete bugs that only surfaced once it was run against a real, live Immich instance. This repo is the corrected, verified version. See "What the original draft got wrong" below for the specifics — they're the reason you shouldn't trust AI-research output like this without an end-to-end test against the real API.

It supersedes `blades68-lora`'s `concourse/scripts/sync_immich_albums.py` (branch `feature/immich-album-sync`, never merged) for that repo's Immich-publishing needs. That script is a bespoke, manually-run tool that mirrors an existing folder hierarchy onto albums after the fact; this resource type is a declarative Concourse `put` step that publishes+organizes at pipeline-run time. If you're setting up Immich publishing in a new pipeline, use this. See "Relationship to sync_immich_albums.py" below.

This standalone repo is intentionally not wired into any live pipeline yet — that's a follow-up integration step.

## What it does

- `check`: no-op stub (this is a put-only resource; there is no upstream version to poll).
- `in`: no-op pass-through stub.
- `out`: the real logic —
  1. globs files in the build directory,
  2. for each: computes a SHA-1 content hash (for dedup/tracking in build metadata, not a security claim), looks for a matching `.xmp` sidecar and binds it,
  3. uploads all matches concurrently (`ThreadPoolExecutor(max_workers=4)`) via multipart `POST /api/assets`,
  4. resolves or auto-creates an album and/or tags by name,
  5. bulk-assigns the uploaded assets to that album/those tags.

Auth is via the `x-api-key` header; `host`/`api_key` come from the Concourse resource `source` config, matching the `((immich_local_url))`/`((immich_api_key))` var pattern already used elsewhere in blades68-lora.

## Verified API shapes

Everything below was checked against a **live Immich v3.1.0 instance's own OpenAPI document** (`GET /api/spec.json` — the human-readable docs are also served, at `/.well-known/openapi.json` and other UI paths, but the raw JSON is the ground truth this was checked against), not assumed from the source doc or from stale documentation. Where the source doc's code disagreed with the real spec or with actual server behavior, the real behavior wins; the differences are called out explicitly since they were the actual bugs that made the original draft not work.

| Area | Verified shape |
|---|---|
| Upload | `POST /api/assets`, multipart. Required: `fileCreatedAt`, `fileModifiedAt`, `assetData`. Optional: `filename`, `isFavorite`, `visibility`, `sidecarData`. Response: `{"status": "created"\|"duplicate", "id": "<uuid>"}` — `201` for a new asset, `200` if Immich recognized it as a duplicate by content hash and just returned the existing asset's ID. |
| `visibility` enum | `archive`, `timeline`, `hidden`, `locked`. |
| Album create | `POST /api/albums` `{"albumName": "..."}` → `201` with the full album object. |
| Album list | `GET /api/albums` → array of albums, matched by `albumName` (case-insensitive here). |
| Album assign | `PUT /api/albums/{id}/assets` body is `{"ids": [...]}` — **not** `{"assetIds": [...]}**. Response is an array of `{"id", "success", "error"}` per asset (a 200 overall can still contain per-asset failures). |
| Tag create/upsert | `PUT /api/tags` `{"tags": ["name1", "name2"]}` → atomically creates-or-returns-existing for every name in one call, returning the full `TagResponseDto[]`. This is a real upsert endpoint the source doc didn't know about (it POSTed one at a time and handled the resulting 400s itself). |
| Tag bulk-assign | `PUT /api/tags/assets` body is `{"tagIds": [...], "assetIds": [...]}`, response is `{"count": N}` (total asset-tag associations made, i.e. `len(tagIds) * len(assetIds)` on full success). |
| Delete | `DELETE /api/assets {"ids": [...], "force": true}` — without `force`, this is a **soft delete to trash**; the asset still counts for duplicate-detection-by-checksum until it's actually purged (`POST /api/trash/empty` or `force: true`). Worth knowing if you're scripting cleanup during testing. |

### What the original draft got wrong

Found by actually running the code against a live instance, not by reading more carefully:

1. **Album assignment used the wrong field name** (`assetIds` instead of `ids`). This alone would have made every album assignment in the original draft fail outright (missing required field) — it was never going to work as shipped.
2. **`filename` form field was stripped of its extension** (`os.path.splitext(filename)[0]` before sending). Immich determines the asset's file type from this field's extension, not from the multipart part's own filename/content-type — so this made *every* upload fail with `"Unsupported file type"`.
3. **XMP sidecar binding is fundamentally incompatible with sending a `filename` field at all**, once (2) is fixed. Immich's server-side `canUploadFile()` validates *every* multipart field — including `sidecarData` — against the single shared `body.filename` value if it's present, rather than against each file part's own name. Since `body.filename` is always the *asset's* name, the sidecar field always fails its "is this a `.xmp`" check and the whole upload gets rejected with `"Unsupported file type"`, even though the asset itself is fine. Verified by reproducing with raw `curl` (independent of this script), and by reading the relevant chunk of Immich server source directly (`asset-media.service.js`'s `canUploadFile`). **Fix: don't send a `filename` form field at all.** Immich then falls back to each multipart part's own filename, which was already correct.
4. **SHA-1 was oversold as "cryptographic integrity"** in the original doc's marketing-heavy framing. It's fine for dedup/change-tracking (which is genuinely how Immich itself uses content hashes for its own duplicate detection) but isn't a security or tamper-evidence property, and this README and the code comments don't claim otherwise.
5. **`requests` was unpinned** in the Dockerfile (`pip install requests`). Pinned to `2.34.2` here.

### Observed but not a code bug: tag-assignment can silently short under concurrent write load

During E2E testing, one `PUT /api/tags/assets` call returned `200` with a `count` matching the request, but a follow-up read showed one of the two target assets was missing the tags — this happened while a second, unrelated agent process was concurrently hammering the same local Immich instance with its own test uploads. A manual retry of the identical call succeeded and self-corrected. The `out` script now checks the returned `count` against the expected `len(tag_ids) * len(asset_ids)` and logs a warning (visible in the Concourse step output and in build metadata) if it comes back short, rather than trusting a `200` status code blindly. It does not currently retry automatically — this is intentionally left as an operational signal, not an auto-heal, since this instance is small enough that a retry-storm isn't worth the added complexity yet.

## Known limitations

**Album-name race condition (the concern flagged before this was built).** Immich enforces a uniqueness constraint on **tag** names server-side (verified: `POST /api/tags` for a name that already exists returns `400 "A tag with that name already exists"`), which makes tag resolution safely race-proof via the atomic `PUT /api/tags` upsert. **Albums have no such constraint** (verified: `POST /api/albums` with a duplicate `albumName` happily returns `201` twice, producing two distinct albums with the same name) and there's no upsert-by-name endpoint for albums. That means a get-by-name-then-create-if-missing pattern — which is the only pattern available — has an unavoidable window: two concurrent `out` steps both targeting a brand-new album name can both miss the "already exists" check and each create their own album, splitting the uploaded assets across two same-named albums.

Mitigation actually available: **serialize** any pipeline jobs that might create the same new album name concurrently (e.g. via a Concourse resource pool/lock, the same way this project already uses `gpu-lock` for GPU exclusivity — see `TOOLS.md`), so the check-then-create only ever runs one at a time for a given album name. This resource type does not attempt a client-side workaround (e.g. picking an arbitrary "canonical" duplicate after the fact) because that only hides the duplicate-row symptom without preventing it, and adds complexity for a case that's fully avoidable operationally. If you already have duplicate same-named albums from before this was understood, merge them manually (move assets, delete the empty one) — there's no API shortcut for that either.

**Soft-delete affects dedup.** `DELETE /api/assets` without `force: true` moves assets to trash rather than deleting them; Immich's checksum-based duplicate detection still matches trashed assets. If you're scripting test cleanup, use scoped `force: true` deletes on the specific asset IDs you created — **not** `POST /api/trash/empty`. That endpoint purges *all* trashed items on the instance, not just yours; on a shared instance this can permanently destroy someone else's soft-deleted assets that hadn't hit their retention window yet (this happened during this repo's own E2E testing — see `test/e2e-test-log.md` Cleanup section for specifics).

## Relationship to `sync_immich_albums.py`

`blades68-lora`'s unmerged `feature/immich-album-sync` branch has `concourse/scripts/sync_immich_albums.py`: a standalone, manually re-run script that mirrors the `blades68-refs` reference library's *existing* folder structure onto Immich albums (one album per folder, recursively, so a nested folder lands in more than one album). It's a batch reconciliation tool for content that already exists in an Immich library — it doesn't upload anything, and it isn't a Concourse resource type.

This resource type solves a different, narrower problem: publishing *new* render output as part of a pipeline run, at the moment it's produced, with an explicit album/tag list rather than folder-derived names, uploading real bytes rather than just re-tagging existing library assets. The two aren't in conflict, and there isn't really a "supersede" relationship in the sense of one making the other obsolete — `sync_immich_albums.py`'s folder-mirroring behavior isn't something this resource type replicates or intends to (it's a fundamentally different indexing approach: folder-derived collections vs. explicit per-`put` album/tag lists). If blades68-lora eventually wires this resource type into `pipeline.yml` for crew-group-portraits-style publishing, the two tools would likely coexist: this resource type for what a pipeline actively renders and pushes going forward, `sync_immich_albums.py` for reconciling anything that lands in the `blades68-refs` library outside of a Concourse run. That integration is out of scope for this repo.

## Usage

```yaml
resource_types:
  - name: immich-api
    type: registry-image
    source:
      repository: ghcr.io/gavmor/immich-concourse-resource
      tag: ((immich_resource_version)) # e.g. "1.0.0" -- see "CI" below

resources:
  - name: local-immich-gallery
    type: immich-api
    source:
      host: ((immich_local_url))   # e.g. http://192.168.16.1:2283
      api_key: ((immich_api_key))

jobs:
  - name: process-and-sample-portraits
    plan:
      - get: git-repo
        trigger: true
      # ... render/alter/quality-gate tasks ...
      - put: local-immich-gallery
        no_get: true
        params:
          glob: "modified-assets/*.png"
          is_favorite: true
          visibility: "timeline"
          album: "Blades68 Crew Groups"
          tags:
            - "Blades68"
            - "Concourse Render"
```

See `concourse/pipeline.example.yml` for the full example.

## CI

`.github/workflows/ci.yml` builds and publishes the resource image to `ghcr.io/gavmor/immich-concourse-resource`:

| Trigger | Job | What happens |
|---|---|---|
| Pull request | `build-validate` | Builds the Dockerfile (no push). Catches a broken image before merge. |
| Push to `main` | `publish` | Builds and pushes `:latest` and `:edge-<short-sha>`. |
| Push to `main` | `e2e` | Stands up a real Immich instance (the project's own official release `docker-compose.yml`), bootstraps an admin account and API key via the live API, and runs `test/run_e2e_test.sh` against it. |
| Push of a `vX.Y.Z` tag | `publish` | Builds and pushes semver tags: `X.Y.Z`, `X.Y`, `X`. |
| Manual (`workflow_dispatch`) | `e2e` | Same live smoke test, on demand. |

Pin pipelines to a semver tag (`X.Y.Z`), not `:latest` — that's what `((immich_resource_version))` is for in `concourse/pipeline.example.yml`.

**Why `e2e` doesn't run on every PR:** standing up Postgres + Redis + the Immich server/ML services is real time and real image-pull weight per run, and this is a small `put`-only resource where a broken Dockerfile (caught by `build-validate`) is the far more common failure mode. `e2e` instead runs post-merge on `main` as a continuous canary against whatever Immich currently ships as its latest release — so if a future Immich release changes one of the API shapes documented above (an upsert response shape, a required upload field, etc.), this surfaces as a red job on `main` rather than as a silent break discovered in production. It is intentionally not wired as a required check on `publish`: a canary catching upstream API drift shouldn't block cutting a release of code that hasn't changed.

No manual visibility step needed: verified directly against the live package after the first `publish` run — `ghcr.io/gavmor/immich-concourse-resource` is pullable with an anonymous registry token, no auth required. GHCR packages pushed via `GITHUB_TOKEN` from a public repo inherit that repo's public visibility automatically.

## Building

```
docker build -t immich-concourse-resource .
```

## End-to-end test

`test/run_e2e_test.sh` is a self-contained, repeatable script: it generates two test PNGs (one with an XMP sidecar), runs `out` against a real Immich instance, verifies the resulting album/tag/sidecar state via the API, then deletes everything it created.

```
IMMICH_HOST=http://localhost:2283 IMMICH_API_KEY=... ./test/run_e2e_test.sh
```

See `test/e2e-test-log.md` for the narrative log of the debugging session that produced the fixes above (attempt-by-attempt, including the raw `curl` reproductions and the trash-purge incident) — a real run against a live local Immich v3.1.0 instance.
