#!/bin/bash
set -euo pipefail

###############################################################################
# build_and_push_images.sh
#
# Builds and pushes all 16 container images (4 archetypes × 4 variants) to ECR.
#
# For each archetype, builds 4 image variants using multi-stage Dockerfiles:
#   - serverless-slim:     --target lambda-target  -f Dockerfile.slim
#   - serverless-standard: --target lambda-target  -f Dockerfile.standard
#   - container-slim:      --target fargate-target -f Dockerfile.slim
#   - container-standard:  --target fargate-target -f Dockerfile.standard
#
# ECR path: {ECR_REGISTRY}/svc-experiment/{archetype}:{tag}
#
# Prerequisites:
#   - experiment-env.sh must exist with ECR_REGISTRY and ACCOUNT_ID
#   - A container runtime (podman, finch, or docker) must be installed
#   - AWS CLI configured with ECR push permissions
#   - Override runtime with: CONTAINER_RUNTIME=finch ./build_and_push_images.sh
#   - Builds linux/amd64 images (required for AWS when building on Apple Silicon)
###############################################################################

REGION="us-east-2"
ENV_FILE="experiment-env.sh"

# Source the env file
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run infrastructure setup scripts first."
  exit 1
fi
source "$ENV_FILE"

if [[ -z "${ECR_REGISTRY:-}" ]]; then
  echo "ERROR: ECR_REGISTRY not set in $ENV_FILE. Run 03_setup_storage.sh first."
  exit 1
fi

###############################################################################
# Detect container runtime (podman preferred, then finch, then docker)
###############################################################################
if [[ -n "${CONTAINER_RUNTIME:-}" ]]; then
  # Allow explicit override via environment variable
  CR="$CONTAINER_RUNTIME"
elif command -v podman &>/dev/null; then
  CR="podman"
elif command -v finch &>/dev/null; then
  CR="finch"
elif command -v docker &>/dev/null; then
  CR="docker"
else
  echo "ERROR: No container runtime found. Install podman, finch, or docker."
  exit 1
fi
echo "Using container runtime: $CR"

###############################################################################
# Target platform — build x86_64 images for AWS Lambda/Fargate
# Required when building on Apple Silicon (M1/M2/M3/M4) or other ARM hosts
###############################################################################
BUILD_PLATFORM="linux/amd64"
echo "Target platform: $BUILD_PLATFORM"

###############################################################################
# Authenticate to ECR
###############################################################################
echo "=== Authenticating $CR to ECR ==="
aws ecr get-login-password --region "$REGION" \
  | $CR login --username AWS --password-stdin "$ECR_REGISTRY"
echo "$CR authenticated to $ECR_REGISTRY"

###############################################################################
# Define archetypes and variants
###############################################################################
ARCHETYPES=(event-driven-api batch-transform ml-inference enterprise-microservice)

# Each variant: TAG  DOCKER_TARGET  DOCKERFILE
VARIANTS=(
  "serverless-slim     lambda-target   Dockerfile.slim"
  "serverless-standard lambda-target   Dockerfile.standard"
  "container-slim      fargate-target  Dockerfile.slim"
  "container-standard  fargate-target  Dockerfile.standard"
)

BUILT_IMAGES=()
TOTAL=0
FAILED=0

###############################################################################
# Build and push all 16 images
###############################################################################
for ARCHETYPE in "${ARCHETYPES[@]}"; do
  echo ""
  echo "============================================================"
  echo "  Archetype: ${ARCHETYPE}"
  echo "============================================================"

  BUILD_CONTEXT="archetypes/${ARCHETYPE}"
  REPO="${ECR_REGISTRY}/svc-experiment/${ARCHETYPE}"

  for VARIANT in "${VARIANTS[@]}"; do
    # Parse variant fields
    TAG=$(echo "$VARIANT" | awk '{print $1}')
    TARGET=$(echo "$VARIANT" | awk '{print $2}')
    DOCKERFILE=$(echo "$VARIANT" | awk '{print $3}')

    FULL_IMAGE="${REPO}:${TAG}"
    TOTAL=$((TOTAL + 1))

    echo ""
    echo "--- Building ${ARCHETYPE}:${TAG} (target=${TARGET}, file=${DOCKERFILE}) ---"

    if $CR build \
      --platform "$BUILD_PLATFORM" \
      --target "$TARGET" \
      -f "${BUILD_CONTEXT}/${DOCKERFILE}" \
      -t "$FULL_IMAGE" \
      "$BUILD_CONTEXT"; then

      echo "--- Pushing ${FULL_IMAGE} ---"
      if $CR push "$FULL_IMAGE"; then
        BUILT_IMAGES+=("$FULL_IMAGE")
        echo "✓ ${FULL_IMAGE}"
      else
        echo "✗ PUSH FAILED: ${FULL_IMAGE}"
        FAILED=$((FAILED + 1))
      fi
    else
      echo "✗ BUILD FAILED: ${FULL_IMAGE}"
      FAILED=$((FAILED + 1))
    fi
  done
done

###############################################################################
# Summary
###############################################################################
echo ""
echo "============================================================"
echo "  Build & Push Summary"
echo "============================================================"
echo "Total images attempted: ${TOTAL}"
echo "Successfully built and pushed: ${#BUILT_IMAGES[@]}"
echo "Failed: ${FAILED}"
echo ""

if [[ ${#BUILT_IMAGES[@]} -gt 0 ]]; then
  echo "Images:"
  for IMG in "${BUILT_IMAGES[@]}"; do
    echo "  ✓ ${IMG}"
  done
fi

if [[ $FAILED -gt 0 ]]; then
  echo ""
  echo "WARNING: ${FAILED} image(s) failed to build or push."
  exit 1
fi

echo ""
echo "=== All 16 images built and pushed successfully ==="
