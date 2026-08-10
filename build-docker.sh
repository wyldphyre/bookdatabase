#!/bin/bash
set -e

# The production host is x86-64 Windows, but this build machine is Apple
# Silicon. A plain `docker compose build` produces an image for whatever the
# builder happens to be (arm64 here), which the target cannot execute — it
# fails at startup with "exec /usr/local/bin/gunicorn: input/output error"
# rather than anything that names the real problem. Build for the target's
# architecture explicitly.
TARGET_PLATFORM="${TARGET_PLATFORM:-linux/amd64}"
IMAGE="bookdatabase-bookdatabase:latest"

echo "Building Docker image for $TARGET_PLATFORM..."
docker buildx build --platform "$TARGET_PLATFORM" -t "$IMAGE" --load .

# Fail here rather than shipping a tar that only fails on the far side, where
# the error message gives no hint that architecture is the problem.
built="$(docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}')"
if [ "$built" != "$TARGET_PLATFORM" ]; then
    echo "ERROR: built $built but the deploy target needs $TARGET_PLATFORM." >&2
    echo "       Refusing to export an image the production host cannot run." >&2
    exit 1
fi
echo "Verified image architecture: $built"

echo "Exporting to bookdatabase.tar..."
docker save "$IMAGE" -o bookdatabase.tar

echo "Done! $(du -h bookdatabase.tar | cut -f1) - bookdatabase.tar"
