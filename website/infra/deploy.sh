#!/usr/bin/env bash
# Applies the website's AWS configuration, then publishes the built site (dist/).
# Run with credentials for the AWS account that owns turbodrf.com, after `bun run build`.
#
#   infra/deploy.sh          apply both stacks, upload dist/, invalidate CloudFront
#   infra/deploy.sh plan     show what applying the stacks would change, change nothing
#
# 1. infra/website-certs.yml -> stack turbodrf-website-certs (us-east-1: CloudFront takes certificates from there only)
# 2. infra/website.yml       -> stack turbodrf-website (bucket, CloudFront, routing function, DNS)
# 3. dist/ -> the bucket, with cache headers per kind of file
# 4. CloudFront invalidation of /*
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=turbodrf
REGION=ap-southeast-2
DOMAINS=(
  ProjectName=$PROJECT
  HostedZoneId=Z000216722BHY2AKP4BDI
  WebsiteDomain=turbodrf.com
  WebsiteAltDomain=www.turbodrf.com
)
CERTS_STACK=$PROJECT-website-certs
SITE_STACK=$PROJECT-website
MODE=${1:-apply}
TAGS=(ManagedBy=website/infra/deploy.sh Project=$PROJECT)

apply() {
  local region=$1 stack=$2 template=$3; shift 3
  if [[ $MODE == plan ]]; then
    local out arn
    out=$(aws cloudformation deploy --region "$region" --stack-name "$stack" --template-file "$template" \
      --parameter-overrides "$@" --tags "${TAGS[@]}" \
      --no-execute-changeset --no-fail-on-empty-changeset 2>&1) || { echo "$out" >&2; return 1; }
    arn=$(grep -o 'arn:aws:cloudformation:[^ ]*changeSet/[^ ]*' <<<"$out" || true)
    if [[ -z $arn ]]; then echo "$stack: no changes"; return; fi
    echo "$stack:"
    aws cloudformation describe-change-set --region "$region" --change-set-name "$arn" \
      --query 'Changes[].ResourceChange.[Action,LogicalResourceId,ResourceType,Replacement]' --output text | sed 's/^/  /'
    aws cloudformation delete-change-set --region "$region" --change-set-name "$arn"
  else
    aws cloudformation deploy --region "$region" --stack-name "$stack" --template-file "$template" \
      --parameter-overrides "$@" --tags "${TAGS[@]}" --no-fail-on-empty-changeset
  fi
}
output() {
  aws cloudformation describe-stacks --region "$1" --stack-name "$2" \
    --query "Stacks[0].Outputs[?OutputKey=='$3'].OutputValue" --output text
}

apply us-east-1 "$CERTS_STACK" infra/website-certs.yml "${DOMAINS[@]}"
CERT=$(output us-east-1 "$CERTS_STACK" CertificateArn)
apply "$REGION" "$SITE_STACK" infra/website.yml "${DOMAINS[@]}" CertificateArn="$CERT"
[[ $MODE == plan ]] && exit 0

BUCKET=$(output "$REGION" "$SITE_STACK" WebsiteBucketName)
DIST=$(output "$REGION" "$SITE_STACK" WebsiteDistributionId)
[[ -d dist ]] || { echo "dist/ missing: run bun run build first" >&2; exit 1; }

# Hashed bundles, fonts and the search index's fragments never change under a name: cached for good.
aws s3 sync ./dist/ "s3://$BUCKET/" --exclude "*" --include "_astro/*" --include "fonts/*" \
  --cache-control "public, max-age=31536000, immutable" --only-show-errors
# Pages: a minute in browsers (stale-while-revalidate for hover prefetch), a day at the edge (cleared below).
aws s3 sync ./dist/ "s3://$BUCKET/" --delete --exclude "*" --include "*.html" \
  --cache-control "public, max-age=60, stale-while-revalidate=600, s-maxage=86400" --content-type "text/html; charset=utf-8" --only-show-errors
# Everything else keeps its name across changes (videos, icons, search index, sitemaps): an hour in browsers.
aws s3 sync ./dist/ "s3://$BUCKET/" --delete --exclude "_astro/*" --exclude "fonts/*" --exclude "*.html" \
  --cache-control "public, max-age=3600, s-maxage=86400" --only-show-errors

aws cloudfront create-invalidation --distribution-id "$DIST" --paths '/*' --query 'Invalidation.Id' --output text
echo "Deployed to $BUCKET (CloudFront $DIST)."
