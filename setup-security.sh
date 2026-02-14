#!/bin/bash
# Security Setup Script for MailJaeger
# This script helps you set up MailJaeger with secure defaults

set -e

echo "=========================================="
echo "MailJaeger Security Setup"
echo "=========================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if running as root
if [ "$EUID" -eq 0 ]; then 
    echo -e "${RED}Error: Do not run this script as root${NC}"
    exit 1
fi

# Function to generate secure random string
generate_api_key() {
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
}

echo "Step 1: Generating secure API key..."
API_KEY=$(generate_api_key)
echo -e "${GREEN}✓ API key generated${NC}"
echo ""

# Check if .env exists
if [ -f .env ]; then
    echo -e "${YELLOW}Warning: .env file already exists${NC}"
    read -p "Do you want to backup and create a new one? (y/N): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        BACKUP_NAME=".env.backup.$(date +%Y%m%d_%H%M%S)"
        cp .env "$BACKUP_NAME"
        echo -e "${GREEN}✓ Backed up to $BACKUP_NAME${NC}"
    else
        echo "Keeping existing .env file"
        exit 0
    fi
fi

# Copy from example
if [ ! -f .env.example ]; then
    echo -e "${RED}Error: .env.example not found${NC}"
    exit 1
fi

cp .env.example .env
echo -e "${GREEN}✓ Created .env from template${NC}"

# Update API key in .env
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    sed -i '' "s/^API_KEY=.*/API_KEY=$API_KEY/" .env
else
    # Linux
    sed -i "s/^API_KEY=.*/API_KEY=$API_KEY/" .env
fi
echo -e "${GREEN}✓ API key added to .env${NC}"
echo ""

# Prompt for IMAP settings
echo "Step 2: Configure IMAP settings"
echo "================================"
read -p "IMAP Host (e.g., imap.gmail.com): " IMAP_HOST
read -p "IMAP Port (default 993): " IMAP_PORT
IMAP_PORT=${IMAP_PORT:-993}
read -p "IMAP Username (your email): " IMAP_USERNAME
read -s -p "IMAP Password (app password): " IMAP_PASSWORD
echo ""

# Update IMAP settings
if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "s/^IMAP_HOST=.*/IMAP_HOST=$IMAP_HOST/" .env
    sed -i '' "s/^IMAP_PORT=.*/IMAP_PORT=$IMAP_PORT/" .env
    sed -i '' "s/^IMAP_USERNAME=.*/IMAP_USERNAME=$IMAP_USERNAME/" .env
    sed -i '' "s/^IMAP_PASSWORD=.*/IMAP_PASSWORD=$IMAP_PASSWORD/" .env
else
    sed -i "s/^IMAP_HOST=.*/IMAP_HOST=$IMAP_HOST/" .env
    sed -i "s/^IMAP_PORT=.*/IMAP_PORT=$IMAP_PORT/" .env
    sed -i "s/^IMAP_USERNAME=.*/IMAP_USERNAME=$IMAP_USERNAME/" .env
    sed -i "s/^IMAP_PASSWORD=.*/IMAP_PASSWORD=$IMAP_PASSWORD/" .env
fi
echo -e "${GREEN}✓ IMAP settings configured${NC}"
echo ""

# Create secrets directory for Docker
echo "Step 3: Creating secrets directory for Docker..."
mkdir -p secrets
chmod 700 secrets

# Save API key to secrets file
echo "$API_KEY" > secrets/api_key.txt
chmod 600 secrets/api_key.txt

# Save IMAP password to secrets file
echo "$IMAP_PASSWORD" > secrets/imap_password.txt
chmod 600 secrets/imap_password.txt

echo -e "${GREEN}✓ Secrets created in ./secrets/ directory${NC}"
echo ""

# Set secure file permissions
echo "Step 4: Setting secure file permissions..."
chmod 600 .env
echo -e "${GREEN}✓ .env permissions set to 600 (owner read/write only)${NC}"

# Create data directories with secure permissions
mkdir -p data/logs data/search_index data/attachments
chmod 700 data
echo -e "${GREEN}✓ Data directories created with secure permissions${NC}"
echo ""

# Security checklist
echo "=========================================="
echo "Setup Complete! 🎉"
echo "=========================================="
echo ""
echo -e "${GREEN}Your API Key:${NC} $API_KEY"
echo ""
echo -e "${YELLOW}IMPORTANT: Save this API key securely!${NC}"
echo "You will need it to access the dashboard."
echo ""
echo "=========================================="
echo "Security Checklist:"
echo "=========================================="
echo ""
echo "✓ API key generated and saved"
echo "✓ IMAP credentials configured"
echo "✓ Secrets directory created"
echo "✓ File permissions secured"
echo "✓ Data directories created"
echo ""
echo -e "${YELLOW}Before starting:${NC}"
echo "1. Ensure Ollama is running: ollama serve"
echo "2. Pull a model: ollama pull mistral:7b-instruct-q4_0"
echo "3. Review .env file and adjust settings as needed"
echo "4. Keep SAFE_MODE=true for initial testing"
echo ""
echo -e "${YELLOW}To start MailJaeger:${NC}"
echo "  Development: python -m uvicorn src.main:app --host 127.0.0.1 --port 8000"
echo "  Docker: docker compose up -d"
echo ""
echo -e "${YELLOW}Access dashboard at:${NC} http://localhost:8000"
echo ""
echo "For production deployment, see:"
echo "  - SECURITY_GUIDE.md"
echo "  - docs/reverse-proxy-examples.md"
echo ""
echo "=========================================="
