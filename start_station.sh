#!/usr/bin/env bash
# ==============================================================================
# OpenClaw BBS Station Launcher (macOS + DOSBox)
# Dispara as bridges de HAL (8888) e SAL (8889) e inicia o DOSBox.
# Ao fechar o DOSBox, encerra automaticamente as bridges em background.
# ==============================================================================

BRIDGE_DIR="/Users/brunoc/src/openclaw-term-bridge"
PYTHON_BIN="/usr/bin/python3"
DOSBOX_BIN="/opt/homebrew/bin/dosbox"

echo -e "\033[1;36m[BBS Station]\033[0m Iniciando bridges do OpenClaw..."

# 1. Inicia Bridge do HAL (porta 8888, agent openclaw/main)
$PYTHON_BIN "$BRIDGE_DIR/openclaw_term_bridge.py" --agent openclaw/main --port 8888 --session term-hal > /tmp/bridge_hal.log 2>&1 &
PID_HAL=$!
echo -e " \033[1;32m*\033[0m Bridge HAL conectada na porta \033[1;33m8888\033[0m (PID: $PID_HAL)"

# 2. Inicia Bridge da SAL (porta 8889, agent sal)
$PYTHON_BIN "$BRIDGE_DIR/openclaw_term_bridge.py" --agent sal --port 8889 --session term-sal > /tmp/bridge_sal.log 2>&1 &
PID_SAL=$!
echo -e " \033[1;32m*\033[0m Bridge SAL conectada na porta \033[1;33m8889\033[0m (PID: $PID_SAL)"

# Funcao de encerramento
cleanup() {
    echo ""
    echo -e "\033[1;35m[BBS Station]\033[0m Encerrando bridges em background..."
    kill $PID_HAL 2>/dev/null
    kill $PID_SAL 2>/dev/null
    echo -e "\033[1;32m[BBS Station]\033[0m Sessao finalizada com sucesso."
    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

sleep 1

echo -e "\033[1;36m[BBS Station]\033[0m Disparando DOSBox..."
$DOSBOX_BIN
