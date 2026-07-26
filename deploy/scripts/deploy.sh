#!/usr/bin/env bash
# deploy.sh — sobe o stack de produção (build + up + tail logs).
#
# Idempotente. Pode rodar para fazer redeploy.
#
# Uso:
#   bash scripts/deploy.sh           # build + up + ps
#   bash scripts/deploy.sh --logs    # idem + tail logs
#   bash scripts/deploy.sh --no-build  # apenas up (sem rebuild)

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[deploy]${NC} $*"; }
ok()   { echo -e "${GREEN}[ ok ]${NC}   $*"; }
warn() { echo -e "${YELLOW}[warn]${NC}  $*"; }
fail() { echo -e "${RED}[fail]${NC}  $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$DEPLOY_DIR"

COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"

[ -f "$ENV_FILE" ]     || fail "$ENV_FILE não existe. Copie de .env.prod.example e preencha."
[ -f "$COMPOSE_FILE" ] || fail "$COMPOSE_FILE não encontrado em $DEPLOY_DIR."

# Verifica acme.json
if [ ! -f traefik/acme.json ]; then
  warn "traefik/acme.json não existe — criando agora..."
  mkdir -p traefik
  touch traefik/acme.json
  chmod 600 traefik/acme.json
elif [ "$(stat -c '%a' traefik/acme.json 2>/dev/null || stat -f '%A' traefik/acme.json)" != "600" ]; then
  warn "Corrigindo permissão de traefik/acme.json para 600..."
  chmod 600 traefik/acme.json
fi

# Pre-check de DNS — se DOMAIN não aponta para esta VPS, Let's Encrypt vai
# falhar o desafio HTTP-01 (~30s perdidos + risco de rate-limit). Aborta cedo.
ENV_DOMAIN=$(grep -E "^DOMAIN=" "$ENV_FILE" | head -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs || true)
if [ -n "$ENV_DOMAIN" ]; then
  VPS_IP=$(curl -s --max-time 5 https://api.ipify.org || true)
  RESOLVED=$(getent ahostsv4 "$ENV_DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)
  if [ -n "$VPS_IP" ] && [ -n "$RESOLVED" ] && [ "$VPS_IP" != "$RESOLVED" ]; then
    warn "DNS de $ENV_DOMAIN aponta para $RESOLVED, mas o IP desta VPS é $VPS_IP."
    warn "Let's Encrypt vai falhar o desafio HTTP-01. Aborte ou aguarde propagação."
    if [ -t 0 ]; then
      read -r -p "Prosseguir mesmo assim? [y/N] " ans
      case "${ans:-N}" in
        [Yy]*) warn "Continuando — Let's Encrypt provavelmente vai falhar." ;;
        *)     fail "Aborte. Confirme o registro A do DNS." ;;
      esac
    else
      fail "Aborte. Em ambiente não-interativo, ajuste o DNS antes de re-executar."
    fi
  elif [ -n "$VPS_IP" ] && [ -n "$RESOLVED" ]; then
    ok "DNS OK ($ENV_DOMAIN → $RESOLVED)."
  fi
fi

# Args
BUILD=1
TAIL=0
for arg in "$@"; do
  case $arg in
    --no-build) BUILD=0 ;;
    --logs)     TAIL=1 ;;
    -h|--help)
      echo "Uso: $0 [--no-build] [--logs]"
      exit 0
      ;;
  esac
done

if [ "$BUILD" -eq 1 ]; then
  log "Build das imagens..."
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build
fi

log "Subindo serviços..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d

log "Status:"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps

# Lê DOMAIN do .env.prod para o usuário
DOMAIN=$(grep -E "^DOMAIN=" "$ENV_FILE" | head -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs || true)

ok "Deploy concluído."
log ""
log "Próximos checks (aguarde ~30s para Let's Encrypt emitir o cert):"
log "  curl -s https://$DOMAIN/health"
log "  curl -s -o /dev/null -w '%{http_code}\\n' https://$DOMAIN/login"
log ""
log "Webhook (substitua o canal pelo que você configurou):"
log "  https://$DOMAIN/webhook/twilio?agent=rhawk_assistant"
log "  https://$DOMAIN/webhook/meta?agent=rhawk_assistant"
log "  https://$DOMAIN/webhook/uazapi?agent=rhawk_assistant"
log ""
log "Painel admin:"
log "  https://$DOMAIN/login"

if [ "$TAIL" -eq 1 ]; then
  log ""
  log "Tail logs (Ctrl+C para sair)..."
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" logs -f
fi
