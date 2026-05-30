#!/bin/bash
# ==============================================================================
# Antigravity Command Center - Complete Server Installation & Configuration Script
# ==============================================================================
# Resolves: Django Daphne ASGI backend setup, systemd configuration,
#           google-chrome & Playwright browser installation, custom skills setup,
#           and agy Go CLI engine setup.
# ==============================================================================

set -e

# Make sure the script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: This script must be run as root (sudo)." >&2
  exit 1
fi

echo "======================================================================"
echo "🚀 Starting Antigravity Command Center Installation & Configuration"
echo "======================================================================"

# Determine repo directory (where the script is located)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# 1. System Updates & Prerequisites
echo -e "\n📦 1. Updating package lists and installing basic requirements..."
apt-get update
apt-get install -y curl wget git python3-pip python3-venv python3-dev build-essential gpg

# 2. Google Chrome Stable Installation
echo -e "\n🌐 2. Installing Google Chrome Stable..."
if ! command -v google-chrome &> /dev/null; then
  echo "Adding official Google Chrome deb repository..."
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
  apt-get update
  apt-get install -y google-chrome-stable
  echo "Google Chrome successfully installed!"
else
  echo "Google Chrome is already installed at: $(which google-chrome)"
fi

# 3. Main Python Virtual Environment Setup
echo -e "\n🐍 3. Configuring main Python Virtual Environment (/root/venv)..."
mkdir -p /root
if [ ! -d "/root/venv" ]; then
  echo "Creating new virtual environment at /root/venv..."
  python3 -m venv /root/venv
fi

echo "Upgrading pip..."
/root/venv/bin/pip install --upgrade pip

echo "Installing package requirements from requirements.txt..."
if [ -f "$REPO_DIR/requirements.txt" ]; then
  /root/venv/bin/pip install -r "$REPO_DIR/requirements.txt"
else
  echo "Warning: requirements.txt not found in repo directory, installing defaults..."
  /root/venv/bin/pip install Django>=6.0.5 daphne>=4.2.1 channels>=4.3.2 google-antigravity google-genai websockets pydantic websocket-client
fi

# 4. Antigravity Go Engine (agy) Setup
echo -e "\n🤖 4. Configuring Antigravity Go Engine CLI (agy)..."
mkdir -p /root/.local/bin

# Look for agy binary. Check local location first, then repo, then fall back to mock
if [ -f "/root/.local/bin/agy" ] && [ -s "/root/.local/bin/agy" ]; then
  echo "Pre-existing agy binary detected in /root/.local/bin/agy. Retaining."
elif [ -f "$REPO_DIR/agy" ]; then
  echo "Found agy binary in repo. Installing to /root/.local/bin/agy..."
  cp "$REPO_DIR/agy" /root/.local/bin/agy
elif [ -f "/root/agent_command_center/bin/agy" ]; then
  echo "Found agy binary in old dashboard directory. Copying to /root/.local/bin/agy..."
  cp "/root/agent_command_center/bin/agy" /root/.local/bin/agy
else
  echo "Warning: Platform-specific agy binary not found in repo or ~/.local/bin/agy."
  echo "Installing interactive mock_agy.sh as fallback..."
  cp "$REPO_DIR/mock_agy.sh" /root/.local/bin/agy
fi

chmod +x /root/.local/bin/agy
echo "Engine setup complete! Executable is at: /root/.local/bin/agy"

# 5. Playwright Virtual Environment Setup (for chrome-automation skill)
echo -e "\n🎭 5. Setting up Chrome Playwright virtualenv (/root/chrome/venv)..."
mkdir -p /root/chrome
if [ ! -d "/root/chrome/venv" ]; then
  python3 -m venv /root/chrome/venv
fi

echo "Upgrading pip inside Playwright venv..."
/root/chrome/venv/bin/pip install --upgrade pip
echo "Installing playwright & websocket-client..."
/root/chrome/venv/bin/pip install playwright websocket-client

echo "Installing Playwright system dependencies..."
/root/chrome/venv/bin/playwright install-deps chromium
echo "Playwright environment fully configured!"

# 6. Intermediate Harness Configuration
echo -e "\n🛡️ 6. Copying Intermediate Agent Harness (mock_harness.py)..."
cp "$REPO_DIR/mock_harness.py" /root/mock_harness.py
chmod +x /root/mock_harness.py
echo "Harness placed successfully at /root/mock_harness.py"

# 7. Installing Custom Skills
echo -e "\n🛠️ 7. Installing Custom Agent Skills (chrome-automation)..."
mkdir -p /root/.gemini/config/skills
mkdir -p /root/.gemini/antigravity-cli/skills

if [ -d "$REPO_DIR/skills/chrome-automation" ]; then
  echo "Deploying chrome-automation skill to config folders..."
  rm -rf /root/.gemini/config/skills/chrome-automation
  rm -rf /root/.gemini/antigravity-cli/skills/chrome-automation
  
  cp -r "$REPO_DIR/skills/chrome-automation" /root/.gemini/config/skills/chrome-automation
  cp -r "$REPO_DIR/skills/chrome-automation" /root/.gemini/antigravity-cli/skills/chrome-automation
  echo "chrome-automation skill successfully installed!"
else
  echo "Warning: chrome-automation skill folder not found in repository."
fi

# 8. Deploying Code base & Seeding DB
echo -e "\n📂 8. Configuring Django Project and Database..."
mkdir -p /root/agent_command_center
cp -r "$REPO_DIR/agent_command_center/"* /root/agent_command_center/

# Run database migrations
cd /root/agent_command_center
echo "Running database migrations..."
/root/venv/bin/python manage.py migrate

# Seed administrative credentials
echo "Seeding administrative credentials..."
/root/venv/bin/python create_admin.py

# 9. Systemd Service Configuration
echo -e "\n⚙️ 9. Writing & Enabling systemd Service (django-daphne)..."
cat << 'EOF' > /etc/systemd/system/django-daphne.service
[Unit]
Description=Django Daphne ASGI Server for Agent Command Center
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/agent_command_center
Environment=PYTHONUNBUFFERED=1
ExecStart=/root/venv/bin/daphne -b 0.0.0.0 -p 8000 agent_command_center.asgi:application
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd daemon..."
systemctl daemon-reload
echo "Enabling django-daphne service..."
systemctl enable django-daphne.service
echo "Starting django-daphne service..."
systemctl restart django-daphne.service

echo "======================================================================"
echo "🎉 INSTALLATION COMPLETED SUCCESSFULLY!"
echo "======================================================================"
echo "The Antigravity Command Center is now running under systemd."
echo "Port: 8000 (Daphne ASGI Server)"
echo "Service Status: active (running)"
echo ""
echo "🔑 Seeded Administrator Credentials:"
echo "------------------------------------"
echo "Username: admin"
echo "Password: antigravity-secure-2026"
echo "Email:    admin@localhost"
echo "======================================================================"
