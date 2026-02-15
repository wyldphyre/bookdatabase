<#
.SYNOPSIS
    Backup script for Book Database - SQLite database and cover images.

.DESCRIPTION
    Creates compressed backups with daily/weekly/monthly rotation.
    Uses sqlite3 .backup for safe database copies.

.PARAMETER BackupDir
    Destination directory for backups (e.g., a NAS mount). Defaults to .\backups

.PARAMETER ContainerName
    Docker container name. Defaults to auto-detection from docker compose.

.PARAMETER DataDir
    Path to the data directory containing instance/ and uploads/. Defaults to .\data

.PARAMETER DailyKeep
    Number of daily backups to retain. Default: 7

.PARAMETER WeeklyKeep
    Number of weekly backups to retain (created on Sundays). Default: 4

.PARAMETER MonthlyKeep
    Number of monthly backups to retain (created on 1st of month). Default: 12

.EXAMPLE
    .\backup.ps1 -BackupDir "Z:\Backups\BookDatabase"

.EXAMPLE
    .\backup.ps1 -BackupDir "\\nas\backups\bookdatabase" -DailyKeep 14
#>

param(
    [string]$BackupDir = "",
    [string]$ContainerName = "",
    [string]$DataDir = "",
    [int]$DailyKeep = 7,
    [int]$WeeklyKeep = 4,
    [int]$MonthlyKeep = 12
)

$ErrorActionPreference = "Stop"
$timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$date = Get-Date

# Default paths relative to where the script lives, not the working directory
$scriptDir = $PSScriptRoot
if (-not $DataDir) { $DataDir = Join-Path $scriptDir "data" }
if (-not $BackupDir) { $BackupDir = Join-Path $scriptDir "backups" }

# Resolve to absolute paths
$BackupDir = [System.IO.Path]::GetFullPath($BackupDir)
$DataDir = [System.IO.Path]::GetFullPath($DataDir)

Write-Host "Book Database Backup" -ForegroundColor Cyan
Write-Host "  Backup destination: $BackupDir"
Write-Host "  Data directory:     $DataDir"
Write-Host ""

# Validate data directory exists
if (-not (Test-Path "$DataDir\instance")) {
    Write-Host "ERROR: Data directory not found: $DataDir\instance" -ForegroundColor Red
    Write-Host "Make sure you're running this from the bookdatabase directory, or specify -DataDir" -ForegroundColor Yellow
    exit 1
}

# Create backup subdirectories
$dailyDir = Join-Path $BackupDir "daily"
$weeklyDir = Join-Path $BackupDir "weekly"
$monthlyDir = Join-Path $BackupDir "monthly"
foreach ($dir in @($dailyDir, $weeklyDir, $monthlyDir)) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

# Create a temp directory for this backup
$tempDir = Join-Path $env:TEMP "bookdatabase-backup-$timestamp"
New-Item -ItemType Directory -Path $tempDir -Force | Out-Null

try {
    # Step 1: Safe database backup using sqlite3 .backup command
    Write-Host "Backing up database..." -ForegroundColor Yellow

    # Try to use docker exec for a safe backup if container is running
    if (-not $ContainerName) {
        $ContainerName = docker compose -f docker-compose.prod.yml ps -q 2>$null
        if (-not $ContainerName) {
            $ContainerName = docker compose ps -q 2>$null
        }
    }

    $dbBackupPath = Join-Path $tempDir "books.db"
    $usedDockerBackup = $false

    if ($ContainerName) {
        Write-Host "  Using sqlite3 .backup via Docker container..." -ForegroundColor Gray
        try {
            docker exec $ContainerName sqlite3 /app/instance/books.db ".backup '/tmp/books_backup.db'" 2>$null
            docker cp "${ContainerName}:/tmp/books_backup.db" $dbBackupPath 2>$null
            docker exec $ContainerName rm /tmp/books_backup.db 2>$null
            $usedDockerBackup = $true
        } catch {
            Write-Host "  Docker backup failed, falling back to file copy..." -ForegroundColor Yellow
        }
    }

    if (-not $usedDockerBackup) {
        # Fall back to direct file copy (safe if app is stopped, or low-traffic)
        $dbSource = Join-Path $DataDir "instance\books.db"
        if (Test-Path $dbSource) {
            Copy-Item $dbSource $dbBackupPath
            Write-Host "  Copied database file directly" -ForegroundColor Gray
        } else {
            Write-Host "ERROR: Database not found at $dbSource" -ForegroundColor Red
            exit 1
        }
    }

    # Step 2: Copy uploads directory
    Write-Host "Backing up cover images..." -ForegroundColor Yellow
    $uploadsSource = Join-Path $DataDir "uploads"
    $uploadsDest = Join-Path $tempDir "uploads"
    if (Test-Path $uploadsSource) {
        Copy-Item $uploadsSource $uploadsDest -Recurse
        $imageCount = (Get-ChildItem $uploadsDest -File -Recurse | Measure-Object).Count
        Write-Host "  Copied $imageCount image files" -ForegroundColor Gray
    } else {
        New-Item -ItemType Directory -Path $uploadsDest -Force | Out-Null
        Write-Host "  No uploads directory found, creating empty" -ForegroundColor Gray
    }

    # Step 3: Create compressed archive
    Write-Host "Compressing backup..." -ForegroundColor Yellow
    $zipName = "bookdatabase-$timestamp.zip"
    $dailyZipPath = Join-Path $dailyDir $zipName
    Compress-Archive -Path "$tempDir\*" -DestinationPath $dailyZipPath -CompressionLevel Optimal
    $zipSize = (Get-Item $dailyZipPath).Length / 1MB
    Write-Host "  Created: $dailyZipPath ($([math]::Round($zipSize, 2)) MB)" -ForegroundColor Green

    # Step 4: Copy to weekly (Sundays)
    if ($date.DayOfWeek -eq [DayOfWeek]::Sunday) {
        $weeklyZipPath = Join-Path $weeklyDir $zipName
        Copy-Item $dailyZipPath $weeklyZipPath
        Write-Host "  Weekly backup: $weeklyZipPath" -ForegroundColor Green
    }

    # Step 5: Copy to monthly (1st of month)
    if ($date.Day -eq 1) {
        $monthlyZipPath = Join-Path $monthlyDir $zipName
        Copy-Item $dailyZipPath $monthlyZipPath
        Write-Host "  Monthly backup: $monthlyZipPath" -ForegroundColor Green
    }

    # Step 6: Prune old backups
    Write-Host "Pruning old backups..." -ForegroundColor Yellow

    function Remove-OldBackups {
        param([string]$Dir, [int]$Keep)
        $files = Get-ChildItem $Dir -Filter "bookdatabase-*.zip" | Sort-Object Name -Descending
        if ($files.Count -gt $Keep) {
            $toRemove = $files | Select-Object -Skip $Keep
            foreach ($file in $toRemove) {
                Remove-Item $file.FullName
                Write-Host "  Removed: $($file.Name)" -ForegroundColor Gray
            }
        }
    }

    Remove-OldBackups -Dir $dailyDir -Keep $DailyKeep
    Remove-OldBackups -Dir $weeklyDir -Keep $WeeklyKeep
    Remove-OldBackups -Dir $monthlyDir -Keep $MonthlyKeep

    # Summary
    Write-Host ""
    Write-Host "Backup complete!" -ForegroundColor Green
    $dailyCount = (Get-ChildItem $dailyDir -Filter "bookdatabase-*.zip" | Measure-Object).Count
    $weeklyCount = (Get-ChildItem $weeklyDir -Filter "bookdatabase-*.zip" | Measure-Object).Count
    $monthlyCount = (Get-ChildItem $monthlyDir -Filter "bookdatabase-*.zip" | Measure-Object).Count
    Write-Host "  Daily:   $dailyCount / $DailyKeep"
    Write-Host "  Weekly:  $weeklyCount / $WeeklyKeep"
    Write-Host "  Monthly: $monthlyCount / $MonthlyKeep"

} finally {
    # Clean up temp directory
    if (Test-Path $tempDir) {
        Remove-Item $tempDir -Recurse -Force
    }
}
