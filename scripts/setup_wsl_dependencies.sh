#!/bin/bash
# ==========================================================
# WSL LOCAL DEVELOPMENT DEPENDENCY SETUP FOR LYNDRIX CORE
# ==========================================================

set -euo pipefail

echo "--- 1. System Update & Base Packages ---"
sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y \
  ca-certificates \
  curl \
  gnupg \
  git \
  build-essential \
  jq \
  bash-completion \
  python3 \
  python3-venv \
  python3-pip \
  python3-full \
  python3-dev \
  libffi-dev \
  libssl-dev \
  pkg-config

echo "--- 2. Installing Official Local Docker Engine ---"
# Remove any conflicting legacy packages
sudo apt-get remove -y docker.io docker-doc docker-compose podman-docker containerd runc || true

# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Set up the repository (detect Debian vs Ubuntu automatically)
source /etc/os-release
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$ID \
  $VERSION_CODENAME stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Grant your user permission to run Docker without sudo
sudo usermod -aG docker "$USER"

if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  echo "NOTE: Docker group membership was added for $USER."
  echo "      Open a new terminal or run: newgrp docker"
fi

echo "--- 3. Setting Up Isolated IaC Python Environment ---"
VENV_PATH="$HOME/.local/share/venv/iac"
mkdir -p "$(dirname "$VENV_PATH")"
python3 -m venv "$VENV_PATH"

# Activate and install Ansible + linters + completion support
source "$VENV_PATH/bin/activate"
pip install --upgrade pip
pip install ansible ansible-lint argcomplete jmespath docker deepmerge jinja2 httpx

# Register Python argument completion for the user
activate-global-python-argcomplete --user

echo "--- 4. Configuring Shell Profiles ---"
if ! grep -q "source $VENV_PATH/bin/activate" "$HOME/.bashrc"; then
  {
    echo ""
    echo "# Auto-activate Infrastructure as Code Environment"
    echo "source $VENV_PATH/bin/activate"
  } >> "$HOME/.bashrc"
fi

echo "--- 5. Repairing Local Dev State Permissions ---"
mkdir -p "$HOME/gitlab/lyndrix-dev/lyndrix-core/.dev/vault_data"
chmod -R a+rwX "$HOME/gitlab/lyndrix-dev/lyndrix-core/.dev/vault_data"
chmod 0777 "$HOME/gitlab/lyndrix-dev/lyndrix-core/.dev/vault_data"
find "$HOME/gitlab/lyndrix-dev/lyndrix-core/.dev/vault_data" -type d -exec chmod 0777 {} +
find "$HOME/gitlab/lyndrix-dev/lyndrix-core/.dev/vault_data" -type f -exec chmod 0666 {} +

mkdir -p "$HOME/gitlab/lyndrix-dev/lyndrix-core/.dev/secure_data"
chmod -R a+rwX "$HOME/gitlab/lyndrix-dev/lyndrix-core/.dev/secure_data"

mkdir -p "$HOME/gitlab/lyndrix-dev/lyndrix-core/.dev/db_data"
chmod -R a+rwX "$HOME/gitlab/lyndrix-dev/lyndrix-core/.dev/db_data"

echo "=========================================================="
echo "SETUP COMPLETE."
echo "CRITICAL: You must open a new terminal, or run: newgrp docker"
echo "so your user group changes (Docker) and bashrc take effect."
echo "=========================================================="