#!/usr/bin/env bash
# One-time server setup for Oracle Cloud (Ubuntu 22.04/24.04, ARM or x86). Idempotent.
#
#   curl -fsSL https://raw.githubusercontent.com/Sai-Prashanth123/Perpetual-X-bot/main/deploy/setup.sh | bash
#
# Installs Docker, opens ports 80/443 in the OS firewall (Oracle images block them by
# default), adds swap, clones the repo to ~/Perpetual-X-bot, and creates deploy/.env with
# generated secrets and a free sslip.io domain for HTTPS. It does not start anything until
# the keys you must provide are filled in.
set -euo pipefail

REPO="${REPO:-https://github.com/Sai-Prashanth123/Perpetual-X-bot.git}"
DIR="$HOME/Perpetual-X-bot"

echo "== packages"
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl git iptables-persistent >/dev/null

if ! command -v docker >/dev/null; then
    echo "== docker"
    curl -fsSL https://get.docker.com | sudo sh >/dev/null
    sudo usermod -aG docker "$USER"
fi

echo "== firewall: allow 80/443 (Oracle Ubuntu images REJECT them in iptables)"
for port in 80 443; do
    sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null \
        || sudo iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
done
sudo netfilter-persistent save >/dev/null

if [ ! -f /swapfile ]; then
    echo "== 4 GB swap (headroom for image builds)"
    sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
    echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
fi

echo "== code"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q; else git clone -q "$REPO" "$DIR"; fi

ENV="$DIR/deploy/.env"
if [ ! -f "$ENV" ]; then
    echo "== deploy/.env with generated secrets"
    cp "$DIR/deploy/env.example" "$ENV"
    chmod 600 "$ENV"
    ip="$(curl -fsS https://api.ipify.org)"
    rand() { openssl rand -hex 32; }
    fernet="$(openssl rand -base64 32 | tr '+/' '-_')"
    sed -i "s|^DOMAIN=.*|DOMAIN=${ip//./-}.sslip.io|; s|^PG_PASSWORD=.*|PG_PASSWORD=$(rand)|;
            s|^INTERNAL_TOKEN=.*|INTERNAL_TOKEN=$(rand)|; s|^SESSION_SECRET=.*|SESSION_SECRET=$(rand)|;
            s|^TOKEN_ENCRYPTION_KEY=.*|TOKEN_ENCRYPTION_KEY=$fernet|" "$ENV"
fi

cat <<EOF

Done. Dashboard address: https://$(grep ^DOMAIN= "$ENV" | cut -d= -f2)

Next:
  1. Fill in the keys in $ENV  (nano $ENV)
  2. Log out and back in once (so 'docker' works without sudo), then:
       cd $DIR && docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
EOF
