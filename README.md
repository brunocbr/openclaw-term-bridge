# OpenClaw Terminal Bridge

Ponte (bridge) TCP/Socket para conectar emuladores de terminal retrô (como **TeleMate**, **Terminus**, ou clientes Telnet no MS-DOS/DOSBox) ao agente local **OpenClaw** via API de chat completions com streaming.

---

## 🎯 Características

- **Codificação CP850:** Suporte nativo à acentuação do português no padrão DOS (Code Page 850), com sanitização automática de caracteres Unicode incompatíveis.
- **Cores ANSI:** Compatível com `ANSI.SYS`, convertendo sequências de escape literais (`\033`, `\x1b`, `\e`) em bytes de escape reais para renderização colorida no terminal.
- **Quebra de Linha Automática:** Ajuste de fluxo em ~76 colunas para evitar estouro em terminais padrão 80x25.
- **Streaming em Tempo Real:** As respostas do OpenClaw são transmitidas caractere a caractere conforme são geradas via SSE (Server-Sent Events).
- **Line Editing Básico:** Suporte a eco local e tecla *Backspace* (`\x08`).

---

## 📋 Pré-requisitos

- Python 3.8+
- Instância do **OpenClaw** rodando (padrão em `http://localhost:18789`)
- Dependências Python:
  ```bash
  pip install requests python-dotenv
  ```

---

## ⚙️ Configuração

Crie ou edite o arquivo `~/.env` (ou no diretório do projeto) com seu token:

```ini
OPENCLAW_TOKEN=seu_token_aqui
PORT=8888
AGENT=openclaw/main
```

---

## 🚀 Como Executar

### Uso padrão:
```bash
python openclaw_term_bridge.py
```

### Com argumentos customizados:
```bash
python openclaw_term_bridge.py -p 9000 -a openclaw/outro-modelo
```

### Opções de linha de comando:
- `-p`, `--port`: Porta TCP para escuta (padrão: `8888` ou variável `PORT`).
- `-a`, `--agent`: Nome/ID do modelo do OpenClaw (padrão: `openclaw/main` ou variável `AGENT`).

---

## 🖥️ Como Conectar

### 1. Via TeleMate / DOSBox (Porta Serial Virtual / Modem IP)
Configure seu emulador de terminal para conectar via socket/TCP:
- **Host:** `127.0.0.1`
- **Porta:** `8888`
- **Emulação:** ANSI-BBS ou ANSI.SYS
- **Tabela de Caracteres:** CP850 / IBM PC 8-bit

### 2. Via Telnet / Netcat (teste rápido no Linux/macOS):
```bash
nc 127.0.0.1 8888
```

---

## 📂 Estrutura das Cores ANSI

| Elemento | Código ANSI | Cor |
| :--- | :--- | :--- |
| Banner / Título | `1;36m` | Ciano Brilhante |
| Status / Espera | `1;33m` | Amarelo |
| Prompt (`OpenClaw>`) | `1;35m` | Magenta |
| Texto do Usuário | `1;37m` | Branco Brilhante |
| Resposta do Agente | `1;32m` | Verde Brilhante |
| Erros | `1;31m` | Vermelho |

