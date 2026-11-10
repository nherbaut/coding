#!/usr/bin/env bash
set -euo pipefail

checkout="${CODING_CHECKOUT:-$HOME/coding}"
repository="${CODING_REPOSITORY:-git@github.com:nherbaut/coding.git}"
image="${CODING_IMAGE:-docker.io/nherbaut/coding}"
tag="${CODING_TAG:-arm64}"

if [[ ! -e "$checkout" ]]; then
    git clone --branch main --single-branch "$repository" "$checkout"
fi
if [[ ! -d "$checkout/.git" ]]; then
    printf 'Expected a Git checkout at %s\n' "$checkout" >&2
    exit 1
fi
if [[ -n "$(git -C "$checkout" status --porcelain)" ]]; then
    printf 'The coding checkout contains local changes. Commit or clear them before building.\n' >&2
    exit 1
fi
if [[ "$(git -C "$checkout" branch --show-current)" != main ]]; then
    printf 'The coding checkout must be on main.\n' >&2
    exit 1
fi
git -C "$checkout" pull --ff-only origin main
revision="$(git -C "$checkout" rev-parse --short=12 HEAD)"
docker buildx build \
    --platform linux/arm64 \
    --tag "$image:$tag" \
    --tag "$image:$revision-arm64" \
    --label "org.opencontainers.image.revision=$(git -C "$checkout" rev-parse HEAD)" \
    --push "$checkout"
printf 'Published %s:%s and %s:%s-arm64\n' "$image" "$tag" "$image" "$revision"
