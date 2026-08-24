#!/usr/bin/env bash
# Fulcrum Ops — push the engine images to our own registry.
#
# The three private images are built from vendored source by engine/build.sh.
# This pushes them to an ECR registry in the account that runs the deployment,
# so a host only ever pulls from somewhere we control: no third-party registry
# is contacted at deploy time, and the tag a host runs is one we published.
#
# Usage:
#   AWS_REGION=us-east-1 ENGINE_TAG=1.0.0 deploy/mirror-images.sh
#
# Requires the AWS CLI with credentials that can create and push to ECR.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_REGION:-us-east-1}"
TAG="${ENGINE_TAG:-dev}"
REPOS=(telemetry-engine metric-runner safety-scanner control-plane)

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
  if aws ecr describe-repositories --region "$REGION" --repository-names "$name" >/dev/null 2>&1; then
    echo "repository exists: $name"
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
  docker push "$remote_image"
done

echo
echo "done. Set these in deploy/.env:"
echo "  REGISTRY=$REGISTRY"
echo "  ENGINE_TAG=$TAG"
