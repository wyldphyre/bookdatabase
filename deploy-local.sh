#!/bin/bash
set -e

echo "Building Docker image..."
docker compose build

echo "Restarting container..."
docker compose down
docker compose up -d

echo "Done! Container is running."
docker compose ps
