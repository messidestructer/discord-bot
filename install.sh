#!/bin/bash
set -e

echo "======================================"
echo "  Discord Group Bot — Install Script  "
echo "======================================"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "Python3 not found. Installing..."
    sudo apt update && sudo apt install -y python3 python3-pip
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Python $PYTHON_VERSION found."

# Install Tesseract-OCR (free OCR for event screenshots)
echo ""
echo "Installing Tesseract-OCR..."
if command -v apt &>/dev/null; then
    sudo apt update && sudo apt install -y tesseract-ocr
elif command -v yum &>/dev/null; then
    sudo yum install -y tesseract
elif command -v brew &>/dev/null; then
    brew install tesseract
else
    echo "WARNING: Could not auto-install Tesseract. Install it manually."
fi

echo ""
echo "Installing Python dependencies..."
python3 -m pip install --break-system-packages -r requirements.txt 2>/dev/null || pip3 install -r requirements.txt

echo ""
echo "======================================"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. cp .env.example .env"
echo "  2. nano .env  (fill in your tokens and IDs)"
echo "  3. Edit events_config.json to match your event types"
echo "  4. python3 main.py"
echo "======================================"
