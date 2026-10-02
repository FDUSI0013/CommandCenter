#!/usr/bin/env bash
# FD AI Command Center — push the engine images to our own registry.
#
# The three private images are built from vendored source by engine/build.sh.
# This pushes them to an ECR registry in the account that runs the deployment,
# so a host only ever pulls from somewhere we control: no third-party registry
# is contacted at deploy time, and the tag a host runs is one we published.
#
# Usage:
#   AWS_REGION=us-east-1 ENGINE_TAG=1.0.0 deploy/mirror-images.sh
#   ENFORCE_IMMUTABLE=1 ...            also switch existing repositories to IMMUTABLE
#
# Tag mutability. "The tag a host runs is one we published" only holds if a tag
# cannot be pushed over. This script has always CREATED its repositories
# IMMUTABLE — and said nothing about one that already existed, which is how the
# live repositories came to be MUTABLE while this file claimed otherwise. It now
# reports what each repository actually is, and warns when a tag can be replaced
# underneath a running host. It does not flip a live registry's setting on its
# own: with IMMUTABLE, re-pushing an existing ENGINE_TAG is refused, and that
# has to be a decision (ENFORCE_IMMUTABLE=1, and a new ENGINE_TAG per build),
# not a side effect of the next push.
#
# Requires the AWS CLI with credentials that can create and push to ECR.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_REGION:-us-east-1}"
TAG="${ENGINE_TAG:-dev}"
REPOS=(telemetry-engine metric-runner safety-scanner control-plane)
MUTABLE_REPOS=()

command -v aws >/dev/null    || { echo "error: the AWS CLI is not installed" >&2; exit 1; }
command -v docker >/dev/null || { echo "error: docker is not installed" >&2; exit 1; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

echo "account  $ACCOUNT"
echo "registry $REGISTRY"
echo "tag      $TAG"
echo

# ---------------------------------------------------------------- repositories
for repo in "${REPOS[@]}"; do
  name="fulcrum-ops/${repo}"
  if mutability="$(aws ecr describe-repositories --region "$REGION" --repository-names "$name" \
        --query 'repositories[0].imageTagMutability' --output text 2>/dev/null)"; then
    echo "repository exists: $name ($mutability)"
    if [ "$mutability" != "IMMUTABLE" ]; then
      if [ "${ENFORCE_IMMUTABLE:-0}" = "1" ]; then
        aws ecr put-image-tag-mutability --region "$REGION" --repository-name "$name" \
          --image-tag-mutability IMMUTABLE >/dev/null
        echo "  switched to IMMUTABLE"
      else
        MUTABLE_REPOS+=("$name")
      fi
    fi
  else
    echo "creating repository: $name"
    aws ecr create-repository \
      --region "$REGION" \
      --repository-name "$name" \
      --image-scanning-configuration scanOnPush=true \
      --image-tag-mutability IMMUTABLE \
      --encryption-configuration encryptionType=AES256 \
      >/dev/null
    # Keep the last ten tagged images; untagged layers go after a day.
    aws ecr put-lifecycle-policy \
      --region "$REGION" \
      --repository-name "$name" \
      --lifecycle-policy-text '{"rules":[
        {"rulePriority":1,"description":"keep the last 10 releases",
         "selection":{"tagStatus":"tagged","tagPatternList":["*"],"countType":"imageCountMoreThan","countNumber":10},
         "action":{"type":"expire"}},
        {"rulePriority":2,"description":"expire untagged layers",
         "selection":{"tagStatus":"untagged","countType":"sinceImagePushed","countUnit":"days","countNumber":1},
         "action":{"type":"expire"}}]}' \
      >/dev/null
  fi
done

echo
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

# --------------------------------------------------------------------- push
for repo in "${REPOS[@]}"; do
  local_image="fulcrum-ops/${repo}:${TAG}"
  remote_image="${REGISTRY}/fulcrum-ops/${repo}:${TAG}"

  if ! docker image inspect "$local_image" >/dev/null 2>&1; then
    echo "skip: $local_image is not built locally"
    continue
  fi

  echo
  echo "=== $repo ==================================================="
  docker tag "$local_image" "$remote_image"
  if ! docker push "$remote_image"; then
    echo "error: pushing $remote_image failed." >&2
    echo "       If the registry said the tag already exists, the repository is IMMUTABLE and" >&2
    echo "       $TAG is already published: build under a new ENGINE_TAG rather than replacing it." >&2
    exit 1
  fi
done

if [ "${#MUTABLE_REPOS[@]}" -gt 0 ]; then
  echo
  echo "warning: these repositories are MUTABLE — a later push can replace the image behind" >&2
  echo "         a tag a host is already running, and nothing will say so:" >&2
  printf '           %s\n' "${MUTABLE_REPOS[@]}" >&2
  echo "         Re-run with ENFORCE_IMMUTABLE=1 to lock them, and publish each build under" >&2
  echo "         its own ENGINE_TAG from then on." >&2
fi

# The names compose actually reads (deploy/docker-compose.yml): IMAGE_PREFIX is
# the registry AND the fulcrum-ops/ namespace. This used to print REGISTRY=...,
# a variable nothing reads, so a host configured from this hint kept running its
# locally built images.
echo
echo "done. Set these in deploy/.env:"
echo "  IMAGE_PREFIX=$REGISTRY/fulcrum-ops"
echo "  ENGINE_TAG=$TAG"
