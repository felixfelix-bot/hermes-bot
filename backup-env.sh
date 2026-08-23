#!/usr/bin/env bash
# backup-env.sh — Daily backup of .env files (GPG symmetric encryption)
#
# Backs up all .env files under ~/.hermes to encrypted backups.
# Retention: 30 days (can recover from accidental deletion/wipe)
#
# Usage:
#   ./backup-env.sh              # Create backup
#   ./backup-env.sh --restore    # Restore latest backup
#   ./backup-env.sh --list       # List available backups
set -euo pipefail

BACKUP_DIR="$HOME/.hermes/backups/env"
PASSPHRASE_FILE="$HOME/.hermes/backups/.backup-passphrase"
HERMES_ROOT="$HOME/.hermes"
RETENTION_DAYS=30

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Ensure passphrase exists
ensure_passphrase() {
    if [ ! -f "$PASSPHRASE_FILE" ]; then
        echo "[$(ts)] Generating backup passphrase..."
        mkdir -p "$(dirname "$PASSPHRASE_FILE")"
        openssl rand -base64 32 > "$PASSPHRASE_FILE"
        chmod 600 "$PASSPHRASE_FILE"
        echo "[$(ts)] Passphrase stored in $PASSPHRASE_FILE"
        echo "[$(ts)] ⚠️  BACK UP THIS FILE SECURELY: $PASSPHRASE_FILE"
    fi
}

# Find all .env files under hermes (excluding example/template files)
find_env_files() {
    find "$HERMES_ROOT" -name ".env" -type f 2>/dev/null | grep -v ".env.example" | grep -v ".env.template"
}

# Create backup
backup() {
    ensure_passphrase
    PASSPHRASE=$(cat "$PASSPHRASE_FILE")
    
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="$BACKUP_DIR/env_backup_$TIMESTAMP.tar.gz.gpg"
    
    # Create temp tar
    TEMP_TAR=$(mktemp)
    trap "rm -f $TEMP_TAR" EXIT
    
    # Find and tar .env files
    ENV_FILES=$(find_env_files)
    if [ -z "$ENV_FILES" ]; then
        echo "[$(ts)] No .env files found to backup"
        exit 0
    fi
    
    # Create tar with relative paths (so restore works)
    cd "$HOME"
    tar czf "$TEMP_TAR" -T <(echo "$ENV_FILES" | sed "s|^$HOME/||") 2>/dev/null
    
    # Encrypt with GPG symmetric
    echo "$PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 --symmetric \
        --cipher-algo AES256 -o "$BACKUP_FILE" "$TEMP_TAR"
    
    chmod 600 "$BACKUP_FILE"
    echo "[$(ts)] Backup created: $BACKUP_FILE"
    
    # Cleanup old backups
    find "$BACKUP_DIR" -name "env_backup_*.tar.gz.gpg" -mtime +$RETENTION_DAYS -delete 2>/dev/null || true
    
    # Count backups
    BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/env_backup_*.tar.gz.gpg 2>/dev/null | wc -l)
    echo "[$(ts)] Total backups: $BACKUP_COUNT (retention: $RETENTION_DAYS days)"
}

# Restore latest backup
restore() {
    if [ ! -f "$PASSPHRASE_FILE" ]; then
        echo "ERROR: No passphrase file found at $PASSPHRASE_FILE"
        echo "Cannot restore without the encryption passphrase."
        exit 1
    fi
    PASSPHRASE=$(cat "$PASSPHRASE_FILE")
    
    LATEST=$(ls -t "$BACKUP_DIR"/env_backup_*.tar.gz.gpg 2>/dev/null | head -1)
    if [ -z "$LATEST" ]; then
        echo "ERROR: No backups found in $BACKUP_DIR"
        exit 1
    fi
    
    echo "[$(ts)] Restoring from: $LATEST"
    
    TEMP_TAR=$(mktemp)
    trap "rm -f $TEMP_TAR" EXIT
    
    # Decrypt
    echo "$PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 --decrypt "$LATEST" > "$TEMP_TAR" 2>/dev/null
    
    # Extract (with confirmation)
    echo "[$(ts)] Files to restore:"
    tar tzf "$TEMP_TAR" 2>/dev/null | head -10
    echo ""
    read -p "Restore these files? Existing .env files will be overwritten. [y/N] " confirm
    
    if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
        cd "$HOME"
        tar xzf "$TEMP_TAR" 2>/dev/null
        echo "[$(ts)] ✓ Restore complete"
    else
        echo "[$(ts)] Restore cancelled"
    fi
}

# List backups
list_backups() {
    echo "Available backups in $BACKUP_DIR:"
    echo ""
    ls -lht "$BACKUP_DIR"/env_backup_*.tar.gz.gpg 2>/dev/null || echo "  (none)"
    echo ""
    echo "Total: $(ls -1 "$BACKUP_DIR"/env_backup_*.tar.gz.gpg 2>/dev/null | wc -l) backups"
}

case "${1:-}" in
    --restore)
        restore
        ;;
    --list)
        list_backups
        ;;
    *)
        backup
        ;;
esac
