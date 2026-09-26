#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

# Define some environment variables
export IMAGE_NAME="agent-harness"
export BASE_DIR=$(pwd)
export SECRETS_DIR=$(pwd)/../secrets/
export GCP_PROJECT="ac215-project" # CHANGE TO YOUR PROJECT ID

# Check if container is already running
if docker ps --format "table {{.Names}}" | grep -q "^${IMAGE_NAME}$"; then
    echo "Container '${IMAGE_NAME}' is already running. Shelling into existing container..."
    docker exec -it $IMAGE_NAME /bin/bash ./docker-entrypoint.sh
else
    echo "Container '${IMAGE_NAME}' is not running. Building and starting new container..."
    
    # Build the image based on the Dockerfile
    docker build -t $IMAGE_NAME -f Dockerfile .

    # Run the container
    docker run --rm --name $IMAGE_NAME -ti \
    -v "$BASE_DIR":/app \
    -v "$SECRETS_DIR":/secrets \
    -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/ml-workflow.json \
    -e GCP_PROJECT=$GCP_PROJECT \
    -e OPENAI_API_KEY=$OPENAI_API_KEY \
    $IMAGE_NAME
fi