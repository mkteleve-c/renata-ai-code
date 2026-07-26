#!/usr/bin/env bash
# init-vps.sh — prepara uma VPS limpa para rodar o stack de produção.
#
# Idempotente: pode rodar múltiplas vezes sem problema.
# Requer: bash, sudo (ou rodar como root).
#
# O que faz:
#   1. Verifica/instala Docker Engine + Compose plugin (repositório oficial).
#   2. Habilita e inicia o serviço docker.
#   3. Configura UFW (se instalado) para permitir 22, 80, 443.
#   4. Cria deploy/traefik/acme.json com permissão 600 (Let's Encrypt exige).
#   5. Sanity-check do .env.prod (existe? campos críticos preenchidos?).

set -euo pipefail

# ---- Cores ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()   { echo -e "${CYAN}[init-vps]${NC} $*"; }
ok()    { echo -e "${GREEN}[ ok ]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}   $*"; }
fail()  { echo -e "${RED}[fail]${NC}   $*" >&2; exit 1; }

# ---- Locate deploy/ ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$DEPLOY_DIR"

# ---- Detecta sudo ----
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  if ! command -v sudo >/dev/null 2>&1; then
    fail "Este script precisa rodar como root ou ter sudo instalado."
  fi
  SUDO="sudo"
fi

# ---- 1. Docker Engine ---------------------------------------------------
log "Verificando Docker..."
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ok "Docker $(docker --version) já instalado."
  ok "Compose $(docker compose version --short) já instalado."
else
  log "Instalando Docker Engine + Compose plugin (repositório oficial)..."

  if [ ! -f /etc/os-release ]; then
    fail "/etc/os-release não encontrado — distro não suportada por este script."
  fi

  . /etc/os-release
  if [ "$ID" != "debian" ] && [ "$ID" != "ubuntu" ]; then
    fail "Distro $ID não suportada automaticamente. Instale Docker manualmente: https://docs.docker.com/engine/install/"
  fi

  $SUDO apt-get update
  $SUDO apt-get install -y ca-certificates curl gnupg

  $SUDO install -m 0755 -d /etc/apt/keyrings
  $SUDO curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  $SUDO chmod a+r /etc/apt/keyrings/docker.asc

  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/$ID $VERSION_CODENAME stable" | \
    $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null

  $SUDO apt-get update
  $SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  $SUDO systemctl enable --now docker
  ok "Docker $(docker --version) instalado."
fi

# ---- 2. Firewall (UFW) --------------------------------------------------
if ! command -v ufw >/dev/null 2>&1; then
  warn "UFW não está instalado nesta VPS."
  if [ -t 0 ]; then
    read -r -p "Instalar UFW agora (recomendado em VPS pública)? [Y/n] " ufw_ans
    ufw_ans=${ufw_ans:-Y}
  else
    ufw_ans=N
    warn "Modo não-interativo — pulando instalação. Confie no firewall externo do provedor."
  fi
  if [[ "$ufw_ans" =~ ^[Yy]$ ]]; then
    $SUDO apt-get install -y ufw
  fi
fi

if command -v ufw >/dev/null 2>&1; then
  log "Configurando regras UFW (22, 80, 443)..."
  $SUDO ufw allow 22/tcp  >/dev/null
  $SUDO ufw allow 80/tcp  >/dev/null
  $SUDO ufw allow 443/tcp >/dev/null
  if ! $SUDO ufw status | grep -q "Status: active"; then
    if [ -t 0 ]; then
      read -r -p "Ativar UFW agora? [Y/n] " ufw_enable
      ufw_enable=${ufw_enable:-Y}
      if [[ "$ufw_enable" =~ ^[Yy]$ ]]; then
        $SUDO ufw --force enable
        ok "UFW ativado e regras aplicadas."
      else
        warn "UFW configurado mas inativo. Para ativar depois: sudo ufw enable"
      fi
    else
      warn "UFW configurado mas inativo (modo não-interativo). Para ativar: sudo ufw enable"
    fi
  else
    ok "UFW ativo, regras aplicadas (22/tcp, 80/tcp, 443/tcp)."
  fi
else
  warn "Sem UFW — garanta que portas 80 e 443 estão liberadas no firewall do provedor."
fi

# ---- 2.1 Swap (auto se RAM < 2GB e sem swap) ----------------------------
RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')
if [ "${RAM_MB:-0}" -lt 2048 ] && [ "${SWAP_MB:-0}" -eq 0 ] && [ ! -f /swapfile ]; then
  log "RAM=${RAM_MB}MB e sem swap — criando 2GB de swap (evita OOM no build do Next.js)..."
  $SUDO fallocate -l 2G /swapfile
  $SUDO chmod 600 /swapfile
  $SUDO mkswap /swapfile >/dev/null
  $SUDO swapon /swapfile
  if ! grep -q "^/swapfile" /etc/fstab; then
    echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
  fi
  ok "Swap de 2GB ativada e persistida em /etc/fstab."
elif [ "${RAM_MB:-0}" -ge 2048 ]; then
  ok "RAM suficiente (${RAM_MB}MB) — swap não é necessário."
else
  ok "Swap já configurado (${SWAP_MB}MB)."
fi

# ---- 3. acme.json para Let's Encrypt -----------------------------------
ACME_FILE="$DEPLOY_DIR/traefik/acme.json"
mkdir -p "$DEPLOY_DIR/traefik"
if [ ! -f "$ACME_FILE" ]; then
  log "Criando $ACME_FILE..."
  touch "$ACME_FILE"
fi
chmod 600 "$ACME_FILE"
ok "acme.json pronto (permissão 600)."

# ---- 4. .env.prod sanity check ------------------------------------------
ENV_FILE="$DEPLOY_DIR/.env.prod"
if [ ! -f "$ENV_FILE" ]; then
  warn ".env.prod não existe. Copie de .env.prod.example e preencha:"
  warn "    cp $DEPLOY_DIR/.env.prod.example $ENV_FILE"
  warn "    nano $ENV_FILE"
  exit 0
fi

get_env_value() {
  grep -E "^${1}=" "$ENV_FILE" | head -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs || true
}

# Checa se variáveis críticas estão preenchidas
missing=()
for var in DOMAIN LETSENCRYPT_EMAIL POSTGRES_PASSWORD INTERNAL_SERVICE_TOKEN BETTER_AUTH_SECRET OPENROUTER_API_KEY PGWEB_AUTH_USER PGWEB_AUTH_PASS ADMIN_EMAIL ADMIN_PASSWORD; do
  if [ -z "$(get_env_value "$var")" ]; then
    missing+=("$var")
  fi
done

if [ ${#missing[@]} -gt 0 ]; then
  warn ".env.prod tem variáveis críticas vazias:"
  for v in "${missing[@]}"; do
    warn "  - $v"
  done
  warn "Preencha-as antes de rodar deploy.sh."
else
  ok ".env.prod parece OK (variáveis críticas preenchidas)."
fi

# Avisa se POSTGRES_PASSWORD tem caracteres URL-reservados — eles quebram
# o parser do psycopg/pgweb porque a senha vai embutida em DATABASE_URL.
pg_pass=$(get_env_value POSTGRES_PASSWORD)
if [ -n "$pg_pass" ] && echo "$pg_pass" | grep -qE '[=/+]'; then
  warn "POSTGRES_PASSWORD contém '=', '/' ou '+' — esses caracteres quebram a URL"
  warn "do psycopg (DATABASE_URL) e do pgweb. Sintoma: API tenta resolver host"
  warn "literal 'postgres' e pgweb crasha com 'Invalid URL'."
  warn "Regenere com:    openssl rand -hex 24"
fi

# Em modo real, exige pelo menos UM canal completo (Twilio, Meta ou uazapi).
# Sem isso, o worker faz SystemExit no boot (worker/main.py:102) e fica em
# loop de restart silencioso. Validar aqui evita debug pós-deploy.
outbound_mode=$(get_env_value OUTBOUND_MODE)
[ -z "$outbound_mode" ] && outbound_mode="real"  # default em prod

if [ "$outbound_mode" != "mock" ]; then
  twilio_complete=true
  for v in TWILIO_ACCOUNT_SID TWILIO_API_KEY_SID TWILIO_API_KEY_SECRET TWILIO_FROM_NUMBER; do
    [ -z "$(get_env_value "$v")" ] && twilio_complete=false
  done

  meta_complete=true
  for v in META_PHONE_NUMBER_ID META_ACCESS_TOKEN META_VERIFY_TOKEN META_APP_SECRET; do
    [ -z "$(get_env_value "$v")" ] && meta_complete=false
  done

  uazapi_complete=true
  [ -z "$(get_env_value UAZAPI_BASE_URL)" ] && uazapi_complete=false

  if ! $twilio_complete && ! $meta_complete && ! $uazapi_complete; then
    warn "Nenhum canal de mensageria está completo em OUTBOUND_MODE=real."
    warn "O worker fará fail-fast no boot (no_outbound_channel_enabled)."
    warn "Escolha um caminho:"
    warn "  a) Preencha credenciais de pelo menos um canal:"
    warn "     Twilio  → TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, TWILIO_FROM_NUMBER"
    warn "     Meta    → META_PHONE_NUMBER_ID, META_ACCESS_TOKEN, META_VERIFY_TOKEN, META_APP_SECRET"
    warn "     uazapi  → UAZAPI_BASE_URL"
    warn "  b) Ou rode em modo mock no primeiro deploy: OUTBOUND_MODE=mock"
  else
    enabled=()
    $twilio_complete && enabled+=("twilio")
    $meta_complete   && enabled+=("meta")
    $uazapi_complete && enabled+=("uazapi")
    ok "Canais habilitados: ${enabled[*]}"
  fi
fi

# ---- 5. Resumo final -----------------------------------------------------
log ""
log "VPS pronta. Próximos passos:"
log "  1) Garanta que o registro A do DOMAIN aponta para o IP desta VPS."
log "     dig +short \$DOMAIN  (deve retornar este IP)"
log "  2) Rode o deploy:"
log "     cd $DEPLOY_DIR && bash scripts/deploy.sh"
log ""
log "Após o deploy:"
log "  - Painel admin:    https://\$DOMAIN/login"
log "  - Visualizador DB: https://\$DOMAIN/banco  (login: \$PGWEB_AUTH_USER)"
