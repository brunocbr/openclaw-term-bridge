#!/bin/bash
# Script de inicializacao do OpenClaw Terminal Bridge no tmux
# Ideal para integracao com Keyboard Maestro ou atalhos do macOS

SESSION_NAME="term-bridge"
BRIDGE_DIR="/Users/brunoc/src/openclaw-term-bridge"
VENV_PYTHON="python3"

# Verifica se a sessao ja existe
tmux has-session -t "$SESSION_NAME" 2>/dev/null

if [ $? != 0 ]; then
    # Cria a sessao desanexada rodando a bridge
    tmux new-session -d -s "$SESSION_NAME" -c "$BRIDGE_DIR" "$VENV_PYTHON openclaw_term_bridge.py"
    echo "Sessao tmux '$SESSION_NAME' criada e bridge iniciada."
else
    echo "Sessao tmux '$SESSION_NAME' ja esta em execucao."
fi
