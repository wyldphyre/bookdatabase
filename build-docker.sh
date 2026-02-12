#!/bin/bash
set -e

echo "Building Docker image..."
docker compose build

echo "Exporting to bookdatabase.tar..."
docker save bookdatabase-bookdatabase:latest -o bookdatabase.tar

echo "Done! $(du -h bookdatabase.tar | cut -f1) - bookdatabase.tar"
