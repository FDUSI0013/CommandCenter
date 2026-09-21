#!/usr/bin/env bash
# Fulcrum Ops — ship the committed tree to the production host, from a workstation.
#
#   deploy/ship.sh            package HEAD, upload, deploy the CODE phase, verify
#   deploy/ship.sh --dry-run  package and upload only; print what would run
#
# The host is reachable only through SSM and has no AWS CLI, so a release travels
# as a tarball in S3 fetched through a short-lived presigned URL.
#
# THE CODE PHASE touches exactly three containers:
#   control-plane   rebuilt from this commit and recreated with --no-deps
#   metric-runner   recreated (stateless) so its new memory limit and worker
#   safety-scanner  count take effect; recreated (stateless) for its healthcheck
# and republishes the console. It never recreates a datastore, the engine or the
# keeper: those are the INFRA phase (deploy/README.md, "One-time steps for a host
# deployed before 2026-09-18"), which must be run deliberately and in order.
#
# It does touch DATA, in one way: the control plane runs its own migrations at
# start. Read the next two blocks together -- they are one mechanism.
#
# ROLLBACK IS AUTOMATIC. The running image is tagged `rollback` and the files
# being replaced are archived before anything changes; if the new control plane
# does not answer /health within the window, both are put back and the script
# exits non-zero.
#
# MIGRATIONS run inside the container, before uvicorn: the CMD is
# `alembic upgrade head && exec uvicorn ...` (apps/control-plane/Dockerfile), and
# the migration scripts are baked into the image, not mounted. Two consequences,
# and the second one is a trap that cost a production outage to find:
#
#   A migration that FAILS is harmless. The database stays where it was, the
#   container exits, /health never answers, and the rollback below happens by
#   itself.
#
#   A migration that SUCCEEDS closes the ordinary way back. The database is now
#   at a revision whose script exists only in the NEW image. Re-tag the old image
#   and start it and its `alembic upgrade head` cannot resolve the revision the
#   database reports -- it exits 255, uvicorn is never reached, and
#   `restart: unless-stopped` turns that into a crash loop. A perfectly additive
#   revision breaks this way just as readily as a destructive one: it is the boot
#   that fails, not the schema.
#
# So restore() below downgrades the database back to the revision recorded before
# the deploy, using the NEW image while it is still the tagged one, BEFORE putting
# the old image back. If that downgrade fails it does not roll the image back at
# all -- an unhealthy container an operator can exec into beats a crash loop they
# have to diagnose from scratch -- and it prints exactly what to run by hand.
#
# What that costs you: every revision needs a downgrade() that actually works,
# because it is now on the rollback path rather than a formality. And ship a
# revision the previous image can still live with -- one that adds a table, a
# nullable column or an index, never one that drops or narrows something the
# running code still writes -- because the old image may run against the new
# schema for the seconds between the health gate failing and the downgrade
# finishing. Rehearse anything less obvious against the real data first: render it
# with `alembic upgrade <cur>:head --sql`, wrap that SQL in BEGIN ... ROLLBACK by
# hand, run it on the host, and read the row counts before shipping it for real.
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

echo "[1/8] fetch + verify release \$SHA"
curl -fsSL "\$URL" -o /tmp/release-\$SHA.tgz
echo "\$DIGEST  /tmp/release-\$SHA.tgz" | sha256sum -c - >/dev/null

echo "[2/8] keep a way back: image tag 'rollback', archive of the files, database revision"
mkdir -p \$ROOT/releases
docker tag \$IMAGE:\$TAG \$IMAGE:rollback
tar czf \$ROOT/releases/before-\$SHA-\$STAMP.tgz -C \$ROOT --exclude=deploy/.env apps deploy

# The revision the database is at BEFORE this release. It is the only thing that
# makes a rollback possible once a migration has committed, because the old image
# cannot even name the revisions it does not carry.
#
# Read from the DATABASE, not from the control plane. The moment this matters
# most is the moment the control-plane container is crash-looping, when
# 'docker compose exec' into it fails and hands back an empty string -- which
# restore() could only read as "nothing to undo", and it would then roll the
# image back into exactly the loop this exists to prevent. app-db is a
# service_healthy dependency, so it is up whenever any of this is running.
DB_USER=\$(grep -oP '^APP_DB_USER=\K.*' .env 2>/dev/null || echo fulcrum)
DB_NAME=\$(grep -oP '^APP_DB_NAME=\K.*' .env 2>/dev/null || echo fulcrum_ops)
db_revision() {
  docker compose exec -T app-db psql -U "\$DB_USER" -d "\$DB_NAME" -tAc \
    'SELECT version_num FROM alembic_version' 2>/dev/null | tr -d '[:space:]' || true
}
REV_BEFORE=\$(db_revision)
if [ -z "\$REV_BEFORE" ]; then
  # Refusing is the whole point. Going on without this reading means that if a
  # migration then commits and the health gate fails, restore() has nothing to
  # downgrade TO -- it would skip the downgrade and put back an image that
  # cannot boot. A deploy that cannot be rolled back is not one to start.
  echo "refusing to deploy: could not read alembic_version from the app database." >&2
  echo "  the rollback path needs it. check: docker compose exec -T app-db psql -U \$DB_USER -d \$DB_NAME -c 'select * from alembic_version'" >&2
  exit 3
fi
echo "      database is at revision: \$REV_BEFORE"

restore() {
  echo "ROLLING BACK: \$1"

  # Put the schema back first, while the NEW image -- the only one that has the
  # scripts for whatever the database is now at -- is still the tagged one.
  # Skipping this is what turns a rollback into a crash loop: the old image's
  # own CMD runs 'alembic upgrade head', cannot resolve the revision the
  # database reports, exits 255, and 'restart: unless-stopped' does the rest.
  #
  # 'compose run --rm' rather than 'compose exec': the container this has to run
  # FROM is very likely the one that just failed its health gate, and may be
  # restarting or gone. run starts a throwaway container from the same (new)
  # image with the same environment, and needs nothing to be healthy.
  rev_now=\$(db_revision)
  if [ -n "\$REV_BEFORE" ] && [ -n "\$rev_now" ] && [ "\$rev_now" != "\$REV_BEFORE" ]; then
    echo "      database moved \$REV_BEFORE -> \$rev_now; downgrading before the image goes back"
    if ! docker compose run --rm --no-deps -T control-plane alembic downgrade "\$REV_BEFORE"; then
      echo ""
      echo "  !!  THE DOWNGRADE FAILED. The image is NOT being rolled back, on purpose:"
      echo "  !!  the previous image cannot boot against revision \$rev_now and would"
      echo "  !!  crash-loop. What is running now is the NEW image -- unhealthy,"
      echo "  !!  but its files and its migration scripts are all still here."
      echo "  !!"
      echo "  !!  To finish the rollback by hand, from \$ROOT/deploy:"
      echo "  !!    docker compose run --rm --no-deps control-plane alembic downgrade \$REV_BEFORE"
      echo "  !!    tar xzf \$ROOT/releases/before-\$SHA-\$STAMP.tgz -C \$ROOT"
      echo "  !!    docker tag \$IMAGE:rollback \$IMAGE:\$TAG"
      echo "  !!    docker compose up -d --no-deps control-plane"
      echo ""
      exit 1
    fi
    echo "      database back at \$REV_BEFORE"
  fi

  tar xzf \$ROOT/releases/before-\$SHA-\$STAMP.tgz -C \$ROOT
  docker tag \$IMAGE:rollback \$IMAGE:\$TAG
  docker compose up -d --no-deps control-plane
  exit 1
}

echo "[3/8] unpack (deploy/.env is not in a release and is left alone)"
tar xzf /tmp/release-\$SHA.tgz -C \$ROOT

echo "[4/8] build the control plane"
docker compose build control-plane > \$ROOT/releases/build-\$SHA.log 2>&1 || { tail -30 \$ROOT/releases/build-\$SHA.log; restore "image build failed"; }

echo "[5/8] recreate control-plane (only)"
# Guarded: without this, a failure here (a bad compose file, a port already
# taken, no disk) exits under 'set -e' with the new files unpacked, the old
# image untagged and restore() never called -- the one outcome worse than a
# failed deploy is a half-finished one.
docker compose up -d --no-deps control-plane || restore "could not start the new container"
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

echo "[6/8] recreate the two stateless sidecars"
docker compose up -d --no-deps metric-runner safety-scanner

echo "[7/8] publish the console"
bash publish-console.sh ../apps/web /srv/fulcrum-ops | tail -2

echo "[8/8] the edge, only if its configuration actually changed"
# Caddy terminates TLS for the whole site, so a bad file here takes everything
# down, not just the thing being changed. It is validated before it is
# installed, reloaded rather than restarted (graceful: existing connections
# finish), and put back if the site stops answering afterwards.
if ! cmp -s Caddyfile /etc/caddy/Caddyfile; then
  cp /etc/caddy/Caddyfile \$ROOT/releases/Caddyfile-before-\$SHA-\$STAMP
  # systemd's EnvironmentFile is not a shell script. The site addresses are
  # written unquoted and comma-separated -- FOO=a.example, b.example -- which
  # systemd reads as one value, while sourcing it reads an assignment prefixing
  # the command "b.example" and so sets nothing at all: the site block then
  # expands to no address and Caddy rejects it as a second global block. Read
  # the file the way systemd does: everything after the first "=" is the value.
  # (No backticks anywhere in this heredoc -- it is unquoted, so they would run
  # on the workstation while the release is being built.)
  while IFS= read -r line; do
    case "\$line" in ""|"#"*) continue ;; esac
    export "\${line%%=*}"="\${line#*=}"
  done < /etc/default/caddy
  if ! caddy validate --config Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    echo "      REFUSED: the new Caddyfile does not validate; the edge is untouched"
  else
    cp Caddyfile /etc/caddy/Caddyfile
    systemctl reload caddy
    sleep 2
    # Ask for the site by name, resolved to this host: Caddy serves named sites
    # and answers a request for a bare IP with a TLS handshake failure, which
    # the previous probe read as "the site is down" and rolled back a working
    # configuration for.
    addr=\$(printf '%s' "\$FULCRUM_SITE_ADDRESSES" | cut -d, -f1 | tr -d ' ')
    edge=\$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 \
      --resolve "\$addr:443:127.0.0.1" "https://\$addr/health" || true)
    if [ "\$edge" = "200" ] || [ "\$edge" = "503" ]; then
      echo "      edge reloaded (answers \$edge)"
    else
      cp \$ROOT/releases/Caddyfile-before-\$SHA-\$STAMP /etc/caddy/Caddyfile
      systemctl reload caddy
      echo "      edge did not answer after the reload (\$edge); previous Caddyfile restored"
    fi
  fi
else
  echo "      unchanged"
fi

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
