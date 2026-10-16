#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T localstack sh -s <<'INNER'
set -eu
if ! awslocal s3api head-bucket --bucket codespy-artifacts-dev >/dev/null 2>&1; then
  awslocal s3api create-bucket --bucket codespy-artifacts-dev --region ap-south-1 --create-bucket-configuration LocationConstraint=ap-south-1 >/dev/null
fi
awslocal sqs create-queue --queue-name codespy-analysis-dlq --region ap-south-1 >/dev/null
awslocal sqs create-queue --queue-name codespy-analysis-jobs --region ap-south-1 >/dev/null
if ! awslocal secretsmanager describe-secret --secret-id codespy/dev/api-keys --region ap-south-1 >/dev/null 2>&1; then
  awslocal secretsmanager create-secret --name codespy/dev/api-keys --secret-string '{"placeholder":true}' --region ap-south-1 >/dev/null
fi
INNER
printf '%s\n' 'LocalStack resources initialized. Real AWS was not contacted.'
