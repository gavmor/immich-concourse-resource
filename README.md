# immich-concourse-resource

A custom [Concourse](https://concourse-ci.org/) resource type for pushing build-directory assets into [Immich](https://immich.app/) via its REST API: `put`-only, with optional album and tag auto-provisioning and XMP sidecar binding.

This is a `put`-only resource — there is no upstream version to poll, so `check` and `get` are no-op stubs. All the real behavior is in `out`.

## Source Configuration

| Field Name | Description |
|---|---|
| `host` *(Required)* | Base URL of the Immich instance, e.g. `http://192.168.16.1:2283`. No trailing slash. |
| `api_key` *(Required)* | Immich API key, sent as the `x-api-key` header on every request. |

## Behavior

### `check`: no-op

Always returns an empty version list. There is nothing to check against — this resource only ever pushes.

### `in`: pass-through

Does not fetch anything. Echoes back whatever `version` was given to it (the ref `out` produced) as its own version, so downstream steps referencing a `get` of this resource don't error, but there's no actual content to retrieve.

### `out`: upload assets, bind sidecars, assign album/tags

1. Globs files in the build directory matching `params.glob` (skipping any `.xmp` files themselves).
2. For each matching file: computes a SHA-1 content hash (for dedup/tracking in build metadata — not a cryptographic integrity claim, just what Immich itself uses for its own duplicate detection), looks for a matching `.xmp` sidecar (`<name>.xmp` or `<name.ext>.xmp`) and binds it if found.
3. Uploads all matches concurrently (`ThreadPoolExecutor`, 4 workers) via multipart `POST /api/assets`.
4. Resolves or auto-creates the requested album (by name) and tags (by name).
5. Bulk-assigns the uploaded assets to that album and those tags.

If any individual upload fails, the step fails after attempting all uploads (it doesn't abort early on the first failure).

#### `out` Parameters

| Field Name | Description |
|---|---|
| `glob` *(Required)* | Glob pattern (relative to the build/put directory) matching files to upload, e.g. `modified-assets/*.png`. |
| `is_favorite` *(Optional, default `false`)* | Mark uploaded assets as favorites. |
| `visibility` *(Optional, default `timeline`)* | One of `archive`, `timeline`, `hidden`, `locked`. |
| `album` *(Optional)* | Album name (or an existing album's UUID) to assign uploaded assets to. Created if no album with that name exists yet — see the album-creation race caveat below. |
| `tags` *(Optional)* | List of tag names to assign to uploaded assets. Created via an atomic upsert if they don't already exist. |

## Example

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
          album: "Render Output"
          tags:
            - "concourse"
            - "nightly-render"
```

See `concourse/pipeline.example.yml` for the full example.

## Verified API shapes

Everything below was checked against a **live Immich v3.1.0 instance's own OpenAPI document** (`GET /api/spec.json` — the human-readable docs are also served, at `/.well-known/openapi.json` and other UI paths, but the raw JSON is the ground truth this was checked against), not assumed from an AI research draft or from stale documentation.

| Area | Verified shape |
|---|---|
| Upload | `POST /api/assets`, multipart. Required: `fileCreatedAt`, `fileModifiedAt`, `assetData`. Optional: `filename`, `isFavorite`, `visibility`, `sidecarData`. Response: `{"status": "created"\|"duplicate", "id": "<uuid>"}` — `201` for a new asset, `200` if Immich recognized it as a duplicate by content hash and just returned the existing asset's ID. |
| `visibility` enum | `archive`, `timeline`, `hidden`, `locked`. |
| Album create | `POST /api/albums` `{"albumName": "..."}` → `201` with the full album object. |
| Album list | `GET /api/albums` → array of albums, matched by `albumName` (case-insensitive here). |
| Album assign | `PUT /api/albums/{id}/assets` body is `{"ids": [...]}` — **not** `{"assetIds": [...]}`. Response is an array of `{"id", "success", "error"}` per asset (a 200 overall can still contain per-asset failures). |
| Tag create/upsert | `PUT /api/tags` `{"tags": ["name1", "name2"]}` → atomically creates-or-returns-existing for every name in one call, returning the full `TagResponseDto[]`. This is a real upsert endpoint, so tag resolution is race-safe by construction — no client-side locking needed. |
| Tag bulk-assign | `PUT /api/tags/assets` body is `{"tagIds": [...], "assetIds": [...]}`, response is `{"count": N}` (total asset-tag associations made, i.e. `len(tagIds) * len(assetIds)` on full success). |
| Delete | `DELETE /api/assets {"ids": [...], "force": true}` — without `force`, this is a **soft delete to trash**; the asset still counts for duplicate-detection-by-checksum until it's actually purged (`POST /api/trash/empty` or `force: true`). Worth knowing if you're scripting cleanup during testing. |

Notably, uploads deliberately omit a top-level `filename` form field: Immich's server-side `canUploadFile()` validates *every* multipart field — including `sidecarData` — against that single shared value if it's present, rather than against each file part's own name, which breaks XMP sidecar binding (the sidecar's `.xmp` extension gets checked against the asset's filename instead and fails). Leaving `filename` unset makes Immich fall back to each part's own filename, which is what's actually wanted.

### Observed but not a code bug: tag-assignment can silently short under concurrent write load

During E2E testing, one `PUT /api/tags/assets` call returned `200` with a `count` matching the request, but a follow-up read showed one of the two target assets was missing the tags — this happened while a second, unrelated agent process was concurrently hammering the same local Immich instance with its own test uploads. A manual retry of the identical call succeeded and self-corrected. The `out` script checks the returned `count` against the expected `len(tag_ids) * len(asset_ids)` and logs a warning (visible in the Concourse step output and in build metadata) if it comes back short, rather than trusting a `200` status code blindly. It does not currently retry automatically — this is intentionally left as an operational signal, not an auto-heal, since this instance is small enough that a retry-storm isn't worth the added complexity yet.

## Known limitations

**Album-name race condition.** Immich enforces a uniqueness constraint on **tag** names server-side (verified: `POST /api/tags` for a name that already exists returns `400 "A tag with that name already exists"`), which makes tag resolution safely race-proof via the atomic `PUT /api/tags` upsert. **Albums have no such constraint** (verified: `POST /api/albums` with a duplicate `albumName` happily returns `201` twice, producing two distinct albums with the same name) and there's no upsert-by-name endpoint for albums. That means a get-by-name-then-create-if-missing pattern — which is the only pattern available — has an unavoidable window: two concurrent `out` steps both targeting a brand-new album name can both miss the "already exists" check and each create their own album, splitting the uploaded assets across two same-named albums.

Mitigation actually available: **serialize** any pipeline jobs that might create the same new album name concurrently — e.g. use Concourse's `serial: true` on jobs that touch the same not-yet-existing album/tag (or a resource pool/lock, if you need it across multiple jobs) — so the check-then-create only ever runs one at a time for a given album name. This resource type does not attempt a client-side workaround (e.g. picking an arbitrary "canonical" duplicate after the fact) because that only hides the duplicate-row symptom without preventing it, and adds complexity for a case that's fully avoidable operationally. If you already have duplicate same-named albums from before this was understood, merge them manually (move assets, delete the empty one) — there's no API shortcut for that either.

**Soft-delete affects dedup.** `DELETE /api/assets` without `force: true` moves assets to trash rather than deleting them; Immich's checksum-based duplicate detection still matches trashed assets. If you're scripting test cleanup, use scoped `force: true` deletes on the specific asset IDs you created — **not** `POST /api/trash/empty`. That endpoint purges *all* trashed items on the instance, not just yours; on a shared instance this can permanently destroy someone else's soft-deleted assets that hadn't hit their retention window yet (this happened during this repo's own E2E testing — see `test/e2e-test-log.md` Cleanup section for specifics).

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
