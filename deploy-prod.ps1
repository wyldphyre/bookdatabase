Write-Host "Loading Docker image from bookdatabase.tar..."
docker load -i bookdatabase.tar

Write-Host "Restarting container..."
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d

Write-Host "Done! Container is running."
docker compose -f docker-compose.prod.yml ps
