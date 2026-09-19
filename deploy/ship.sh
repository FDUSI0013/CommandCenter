#!/usr/bin/env bash
# Fulcrum Ops — ship the committed tree to the production host, from a workstation.
#
#   deploy/ship.sh            package HEAD, upload, deploy the CODE phase, verify
#   deploy/ship.sh --dry-run  package and upload only; print what would run
#
# The host is reachable only through SSM and has no AWS CLI, so a release travels
# as a tarball in S3 fetched through a short-lived presigned URL.
#
# THE CODE PHASE touches exactly three containers and no data:
#   control-plane   rebuilt from this commit and recreated with --no-deps
#   metric-runner   recreated (stateless) so its new memory limit and worker
#   safety-scanner  count take effect; recreated (stateless) for its healthcheck
# and republishes the console. It never recreates a datastore, the engine or the
# keeper: those are the INFRA phase (deploy/README.md, "One-time steps for a host
# deployed before 2026-09-18"), which must be run deliberately and in order.
#
# ROLLBACK IS AUTOMATIC. The running image is tagged `rollback` and the files
# being replaced are archived before anything changes; if the new control plane
# does not answer /health within the window, both are put back and the script
# exits non-zero. There are no schema migrations in this release, so the previous
# image runs against the database unchanged.
set -euo pipefail

INSTANCE="${FULCRUM_INSTANCE:-i-02c888d6ca6f07a6b}"
REGION="${AWS_REGION:-us-east-1}"
BUCKET="${FULCRUM_RELEASE_BUCKET:-fulcrum-ops-demo-deploy-155954279114}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain)" ]; then
  echo "refusing to ship: the working tree has uncommitted changes (a release is a commit)" >&2
  exit 2
fi
bash scripts/check-branding.sh >/dev/null
bash scripts/check-config.sh >/dev/null

SHA="$(git rev-parse --short=12 HEAD)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
TARBALL="$WORK/release-$SHA.tgz"
git archive --format=tar.gz -o "$TARBALL" HEAD apps/control-plane apps/web deploy scripts
DIGEST="$(sha256sum "$TARBALL" | cut -d' ' -f1)"
KEY="prod/release-$SHA.tgz"
echo "release $SHA  $(du -h "$TARBALL" | cut -f1)  sha256 $DIGEST"

aws s3 cp --only-show-errors --region "$REGION" "$TARBALL" "s3://$BUCKET/$KEY"
URL="$(aws s3 presign --region "$REGION" --expires-in 3600 "s3://$BUCKET/$KEY")"

REMOTE="$WORK/remote.sh"
cat > "$REMOTE" <<REMOTE_EOF
set -euo pipefail
SHA='$SHA'; DIGEST='$DIGEST'; URL='$URL'
ROOT=/opt/fulcrum; STAMP=\$(date -u +%Y%m%dT%H%M%SZ)
cd \$ROOT/deploy
TAG=\$(grep -oP '^APP_TAG=\K.*' .env || echo local)
IMAGE=fulcrum-ops/control-plane

echo "[1/7] fetch + verify release \$SHA"
curl -fsSL "\$URL" -o /tmp/release-\$SHA.tgz
echo "\$DIGEST  /tmp/release-\$SHA.tgz" | sha256sum -c - >/dev/null

echo "[2/7] keep a way back: image tag 'rollback' + archive of the files being replaced"
mkdir -p \$ROOT/releases
docker tag \$IMAGE:\$TAG \$IMAGE:rollback
tar czf \$ROOT/releases/before-\$SHA-\$STAMP.tgz -C \$ROOT --exclude=deploy/.env apps deploy

restore() {
  echo "ROLLING BACK: \$1"
  tar xzf \$ROOT/releases/before-\$SHA-\$STAMP.tgz -C \$ROOT
  docker tag \$IMAGE:rollback \$IMAGE:\$TAG
  docker compose up -d --no-deps control-plane
  exit 1
}

echo "[3/7] unpack (deploy/.env is not in a release and is left alone)"
tar xzf /tmp/release-\$SHA.tgz -C \$ROOT

echo "[4/7] build the control plane"
docker compose build control-plane > \$ROOT/releases/build-\$SHA.log 2>&1 || { tail -30 \$ROOT/releases/build-\$SHA.log; restore "image build failed"; }

echo "[5/7] recreate control-plane (only)"
docker compose up -d --no-deps control-plane
ok=0
for i in \$(seq 1 80); do
  code=\$(curl -s -m 5 -o /tmp/health.json -w '%{http_code}' http://127.0.0.1:8080/health || true)
  [ "\$code" = "200" ] && { ok=1; break; }
  sleep 3
done
[ "\$ok" = "1" ] || { docker compose logs --tail 40 control-plane; restore "no healthy answer within 4 minutes"; }
bad=0
for i in \$(seq 1 12); do
  code=\$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health || true)
  [ "\$code" = "200" ] || bad=\$((bad+1))
done
[ "\$bad" = "0" ] || restore "\$bad of 12 follow-up health probes failed"
echo "      healthy: \$(cat /tmp/health.json)"

echo "[6/7] recreate the two stateless sidecars"
docker compose up -d --no-deps metric-runner safety-scanner

echo "[7/7] publish the console"
bash publish-console.sh ../apps/web /srv/fulcrum-ops | tail -2

echo "DEPLOYED \$SHA  (previous image kept as \$IMAGE:rollback; files in releases/before-\$SHA-\$STAMP.tgz)"
REMOTE_EOF

if [ "$DRY_RUN" = "1" ]; then
  echo "--- dry run: uploaded s3://$BUCKET/$KEY; the host would run:"
  sed -E "s|URL='[^']*'|URL='<presigned>'|" "$REMOTE"
  exit 0
fi

PARAMS="$WORK/params.json"
printf '{"commands":["echo %s | base64 -d > /tmp/fulcrum-ship.sh","bash /tmp/fulcrum-ship.sh"],"executionTimeout":["1800"]}' \
  "$(base64 -w0 "$REMOTE")" > "$PARAMS"
# A relative path: the Windows AWS CLI cannot open a POSIX-style absolute one.
cp "$PARAMS" ./.ship-params.json
trap 'rm -rf "$WORK" ./.ship-params.json' EXIT
CMD="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript --comment "fulcrum-ops ship $SHA" \
  --parameters file://.ship-params.json --query Command.CommandId --output text)"
echo "ssm command $CMD — building on the host, this takes a few minutes"

STATUS=Pending
for _ in $(seq 1 400); do
  STATUS="$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD" \
    --instance-id "$INSTANCE" --query Status --output text 2>/dev/null || echo Pending)"
  case "$STATUS" in Success|Failed|TimedOut|Cancelled) break ;; esac
  sleep 5
done
aws ssm get-command-invocation --region "$REGION" --command-id "$CMD" --instance-id "$INSTANCE" \
  --query StandardOutputContent --output text
if [ "$STATUS" != "Success" ]; then
  aws ssm get-command-invocation --region "$REGION" --command-id "$CMD" --instance-id "$INSTANCE" \
    --query StandardErrorContent --output text >&2
  echo "SHIP FAILED ($STATUS)" >&2
  exit 1
fi

echo "--- from outside"
for _ in 1 2 3 4 5 6; do
  curl -sk -m 10 -o /dev/null -w '%{http_code} %{time_total}s\n' "${FULCRUM_PUBLIC_URL:-https://controlplane.fdprod.net}/health"
done
