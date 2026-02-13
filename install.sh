#!/bin/bash
# MailJaeger Installation Script for Raspberry Pi 5 / Linux

set -e

echo "================================================"
echo "  MailJaeger Installation Script"
echo "  For Raspberry Pi 5 and Linux systems"
echo "================================================"
echo ""

# Check if running on Linux
if [ "$(uname)" != "Linux" ]; then
    echo "Error: This script is designed for Linux systems"
    exit 1
fi

# Check Python version
echo "Checking Python version..."
if ! command -v python3.11 &> /dev/null; then
    echo "Python 3.11 not found. Installing..."
    sudo apt update
    sudo apt install -y python3.11 python3.11-venv python3-pip
fi

PYTHON_VERSION=$(python3.11 --version | cut -d' ' -f2 | cut -d'.' -f1,2)
echo "✓ Python $PYTHON_VERSION found"

# Check Ollama
echo ""
echo "Checking Ollama installation..."
if ! command -v ollama &> /dev/null; then
    echo "Ollama not found. Installing..."
    curl -fsSL https://ollama.com/install.sh | sh
    echo "✓ Ollama installed"
else
    echo "✓ Ollama already installed"
fi

# Create virtual environment
echo ""
echo "Creating Python virtual environment..."
if [ ! -d "venv" ]; then
    python3.11 -m venv venv
    echo "✓ Virtual environment created"
else
    echo "✓ Virtual environment already exists"
fi

# Activate virtual environment and install dependencies
echo ""
echo "Installing Python dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo "✓ Dependencies installed"

# Initialize database
echo ""
echo "Initializing database..."
python cli.py init
echo "✓ Database initialized"

# Create .env if it doesn't exist
if [ ! -f ".env" ]; then
    echo ""
    echo "Creating .env file..."
    cp .env.example .env
    echo "✓ .env file created"
    echo ""
    echo "⚠️  IMPORTANT: Edit .env file with your IMAP credentials!"
    echo "   nano .env"
fi

# Ask if user wants to pull AI model
echo ""
read -p "Do you want to pull the AI model now? (recommended: mistral:7b-instruct-q4_0) [y/N]: " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Available models:"
    echo "  1) mistral:7b-instruct-q4_0 (recommended, ~4GB)"
    echo "  2) phi3:mini (most efficient, ~2-3GB)"
    echo "  3) llama3.2:3b (alternative, ~2-3GB)"
    echo ""
    read -p "Enter choice [1-3]: " -n 1 -r
    echo
    
    case $REPLY in
        1)
            MODEL="mistral:7b-instruct-q4_0"
            ;;
        2)
            MODEL="phi3:mini"
            ;;
        3)
            MODEL="llama3.2:3b"
            ;;
        *)
            MODEL="mistral:7b-instruct-q4_0"
            ;;
    esac
    
    echo "Pulling model: $MODEL (this may take several minutes)..."
    ollama pull $MODEL
    echo "✓ Model pulled successfully"
    
    # Update .env with selected model
    sed -i "s/^AI_MODEL=.*/AI_MODEL=$MODEL/" .env
fi

# Ask if user wants to install systemd service
echo ""
read -p "Do you want to install MailJaeger as a systemd service? [y/N]: " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Update service file with current directory
    INSTALL_DIR=$(pwd)
    sed "s|/home/pi/MailJaeger|$INSTALL_DIR|g" mailjaeger.service > /tmp/mailjaeger.service
    sed -i "s|User=pi|User=$USER|g" /tmp/mailjaeger.service
    sed -i "s|Group=pi|Group=$USER|g" /tmp/mailjaeger.service
    
    sudo cp /tmp/mailjaeger.service /etc/systemd/system/mailjaeger.service
    sudo systemctl daemon-reload
    sudo systemctl enable mailjaeger.service
    
    echo "✓ Systemd service installed"
    echo ""
    echo "Service commands:"
    echo "  Start:   sudo systemctl start mailjaeger"
    echo "  Stop:    sudo systemctl stop mailjaeger"
    echo "  Status:  sudo systemctl status mailjaeger"
    echo "  Logs:    sudo journalctl -u mailjaeger -f"
fi

echo ""
echo "================================================"
echo "  ✓ Installation Complete!"
echo "================================================"
echo ""
echo "Next steps:"
echo "  1. Edit .env file with your IMAP credentials:"
echo "     nano .env"
echo ""
echo "  2. Start Ollama (if not running as service):"
echo "     ollama serve"
echo ""
echo "  3. Start MailJaeger:"
echo "     source venv/bin/activate"
echo "     python -m src.main"
echo ""
echo "  Or if installed as systemd service:"
echo "     sudo systemctl start mailjaeger"
echo ""
echo "  4. Access the API at:"
echo "     http://localhost:8000/docs"
echo ""
echo "  5. Check system health:"
echo "     python cli.py health"
echo ""
echo "================================================"
