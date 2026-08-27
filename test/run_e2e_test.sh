#!/usr/bin/env bash
# End-to-end smoke test: uploads two generated PNGs (one with an XMP
# sidecar, the other with a .description.txt sidecar) through the `out`
# script against a real running Immich instance, verifies
# album/tag/sidecar/description binding via the API, then deletes
# everything it created.
#
# Usage: IMMICH_HOST=http://localhost:2283 IMMICH_API_KEY=... ./run_e2e_test.sh
set -euo pipefail

: "${IMMICH_HOST:?set IMMICH_HOST}"
: "${IMMICH_API_KEY:?set IMMICH_API_KEY}"

RESOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

mkdir -p "$WORKDIR/build/modified-assets"
cd "$WORKDIR/build/modified-assets"

RUN_ID="e2e-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')"
ALBUM_NAME="immich-concourse-resource-test-$RUN_ID"
TAG_A="e2e-tag-a-$RUN_ID"
TAG_B="e2e-tag-b-$RUN_ID"

python3 - "$RUN_ID" <<'PYEOF'
import sys
from PIL import Image
run_id = sys.argv[1]
Image.new("RGB", (64, 64), color=(200, 50, 50)).save(f"{run_id}-1.png")
Image.new("RGB", (64, 64), color=(50, 200, 50)).save(f"{run_id}-2.png")
PYEOF

cat > "${RUN_ID}-1.png.xmp" <<XMPEOF
<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:description>${RUN_ID} sidecar-bound test description</dc:description>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
XMPEOF

# .description.txt sidecar on the second file -- plain text, no XML
# envelope, exercising the direct PUT /api/assets/{id} path rather than
# XMP's async-extraction one. Deliberately includes a character (&) that
# would need escaping if this went through the XMP path, to prove this
# path really doesn't need it.
printf '%s' "${RUN_ID} plain-text description sidecar, with an & in it" > "${RUN_ID}-2.png.description.txt"

cd "$WORKDIR"
cat > out-input.json <<JSONEOF
{
  "source": {"host": "$IMMICH_HOST", "api_key": "$IMMICH_API_KEY"},
  "params": {
    "glob": "modified-assets/*.png",
    "is_favorite": true,
    "visibility": "timeline",
    "album": "$ALBUM_NAME",
    "tags": ["$TAG_A", "$TAG_B"]
  }
}
JSONEOF

echo "--- running out ---"
python3 "$RESOURCE_DIR/out" build < out-input.json | tee out-result.json
echo

echo "--- verifying against live API ---"
python3 - "$IMMICH_HOST" "$IMMICH_API_KEY" "$ALBUM_NAME" "$RUN_ID" > /tmp/e2e-verify-out.txt <<'PYEOF'
import json, sys, time, urllib.request

host, api_key, album_name, run_id = sys.argv[1:5]

def get(path):
    req = urllib.request.Request(f"{host}{path}", headers={"x-api-key": api_key})
    return json.load(urllib.request.urlopen(req))

def post(path, body):
    req = urllib.request.Request(
        f"{host}{path}", data=json.dumps(body).encode(),
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req))

albums = get("/api/albums")
matches = [a for a in albums if a["albumName"] == album_name]
assert len(matches) == 1, f"expected exactly one album named {album_name!r}, found {len(matches)}"
album = get(f"/api/albums/{matches[0]['id']}")
assert album["assetCount"] == 2, f"expected 2 assets in album, got {album['assetCount']}"

# AlbumResponseDto only carries assetCount, not the asset list -- fetch
# members via /search/metadata's albumIds filter instead (verified against
# the live spec.json: 2026-08-24).
album_assets = post("/api/search/metadata", {"albumIds": [matches[0]["id"]]})["assets"]["items"]
assert len(album_assets) == 2, f"expected 2 assets via search, got {len(album_assets)}"

sidecar_asset = next(a for a in album_assets if a["originalFileName"].endswith("-1.png"))
description_asset = next(a for a in album_assets if a["originalFileName"].endswith("-2.png"))
expected_plain_description = f"{run_id} plain-text description sidecar, with an & in it"

# Both of these lag a read-your-write GET, independently of each other and
# of the synchronous upload response:
#  - tag assignment can lag by a couple of seconds even after
#    PUT /api/tags/assets returns 200 with the expected count (observed
#    independently twice during this repo's own E2E testing, see
#    test/e2e-test-log.md)
#  - the XMP sidecar's dc:description only appears once Immich's async
#    metadata-extraction job has run, which on a freshly-provisioned
#    instance (e.g. in CI, right after admin bootstrap) can take longer
#    than a couple of seconds since nothing has warmed up the job queue
#    yet (observed taking 5-10x longer than the steady-state case seen in
#    the original debugging session -- see test/e2e-test-log.md)
# Poll both together rather than treat either lag as a failure. The
# .description.txt path (set_description in `out`) is a direct
# PUT /api/assets/{id} the `out` script already waited on synchronously
# before this script ever ran -- checked once below, no retry loop needed,
# specifically to prove it doesn't share the XMP path's async lag.
expected_tags = {f"e2e-tag-a-{run_id}", f"e2e-tag-b-{run_id}"}
detail = None
for attempt in range(20):
    detail = get(f"/api/assets/{sidecar_asset['id']}")
    tag_names = {t["name"] for t in detail.get("tags", [])}
    desc = (detail.get("exifInfo") or {}).get("description") or ""
    if expected_tags <= tag_names and run_id in desc:
        break
    time.sleep(1)
else:
    raise AssertionError(
        f"tags and/or sidecar description missing after retries, "
        f"got tags={tag_names}, description={desc!r}"
    )

plain_detail = get(f"/api/assets/{description_asset['id']}")
plain_desc = (plain_detail.get("exifInfo") or {}).get("description") or ""
assert plain_desc == expected_plain_description, (
    f"expected plain-text sidecar description {expected_plain_description!r}, got {plain_desc!r}"
)

print("PASS: album created, 2 assets uploaded, XMP sidecar bound, .description.txt sidecar bound, both tags assigned")
print(f"ALBUM_ID={matches[0]['id']}")
for a in album_assets:
    print(f"ASSET_ID={a['id']}")
PYEOF
cat /tmp/e2e-verify-out.txt

echo "--- cleaning up test data created by this run ---"
ALBUM_ID=$(grep ALBUM_ID= /tmp/e2e-verify-out.txt | cut -d= -f2)
ASSET_IDS=$(grep ASSET_ID= /tmp/e2e-verify-out.txt | cut -d= -f2)

curl -s -X DELETE "$IMMICH_HOST/api/albums/$ALBUM_ID" -H "x-api-key: $IMMICH_API_KEY" -o /dev/null -w "album delete: %{http_code}\n"
for id in $ASSET_IDS; do
  curl -s -X DELETE "$IMMICH_HOST/api/assets" -H "x-api-key: $IMMICH_API_KEY" -H "Content-Type: application/json" \
    -d "{\"ids\":[\"$id\"],\"force\":true}" -o /dev/null -w "asset $id delete: %{http_code}\n"
done
for tag in "$TAG_A" "$TAG_B"; do
  TAG_ID=$(curl -s "$IMMICH_HOST/api/tags" -H "x-api-key: $IMMICH_API_KEY" | python3 -c "
import json, sys
name = sys.argv[1]
for t in json.load(sys.stdin):
    if t['name'] == name:
        print(t['id'])
" "$tag")
  if [ -n "$TAG_ID" ]; then
    curl -s -X DELETE "$IMMICH_HOST/api/tags/$TAG_ID" -H "x-api-key: $IMMICH_API_KEY" -o /dev/null -w "tag $tag delete: %{http_code}\n"
  fi
done
rm -f /tmp/e2e-verify-out.txt

echo "--- e2e test complete ---"
