# OpenClaw Terminal Bridge

Ponte (bridge) TCP/Socket para conectar emuladores de terminal retrô (como **TeleMate**, **Terminus**, ou clientes Telnet no MS-DOS/DOSBox) ao agente local **OpenClaw** via protocolo **WebSocket nativo** do Gateway com streaming e eventos de ferramentas em tempo real.

## 🎯 Características

- **Status Dinâmico no Spinner BBS:** Mostra em tempo real o que o agente está fazendo (`[Preparando ambiente... /]`, `[Terminal: git status -]`, `[Lendo arquivo: teste.txt \]`, `[Pensando... |]`) mantendo a animação clássica de BBS/TeleMate sem quebrar a linha.
- **WebSocket Nativo do Gateway:** Conexão direta com o Gateway OpenClaw (`ws://127.0.0.1:18789`), recebendo eventos de ciclo de vida (`run_status`, `item` de ferramentas e aprovações de execução).
- **ZMODEM com Conversão Automática UTF-8 <-> CP850:** Ponte bidirecional entre o DOS e o sistema moderno!
  - **Download (Host -> DOS):** Arquivos texto UTF-8 são convertidos automaticamente para CP850 com finais de linha CRLF (
), permitindo edição perfeita no DOS Edit ou visualização no TeleMate.
  - **Upload (DOS -> Host):** Arquivos texto recebidos do DOS em CP850 são convertidos automaticamente para UTF-8 com finais de linha Unix LF (
).
  - **Binários Protegidos:** Arquivos binários (.zip, .exe, .gif, etc.) permanecem intocados em modo binário puro.
  - **Sanitização Automática para FAT 8.3:** Nomes modernos longos ou com caracteres proibidos no DOS são automaticamente truncados e higienizados para o padrão 8.3 (ex: `1923 O intelectualismo socrático (Mênon, 77b).markdown` -> `1923_O_I.MD`).
  - **Resolução Unicode NFC/NFD:** Suporte transparente a caminhos acentuados em sistemas de arquivos macOS (APFS) sem corrupção de diretivas.
- **ZMODEM Integrado:** Disparo automático de download (`DOWNLOAD:<path>`) e comandos in-band `/sz <arquivo>` e `/rz [pasta]`.
- **Codificação CP850:** Suporte nativo à acentuação do português no padrão DOS (Code Page 850), com sanitização automática de caracteres Unicode incompatíveis.
- **Cores ANSI:** Compatível com `ANSI.SYS`, convertendo sequências de escape literais (`\033`, `\x1b`, `\e`) em bytes de escape reais para renderização colorida no terminal.
- **Quebra de Linha Automática:** Ajuste de fluxo em ~76 colunas para evitar estouro em terminais padrão 80x25.
- **Streaming em Tempo Real:** Transmissão imediata dos tokens gerados com desativação do buffer de Nagle (`TCP_NODELAY`).
- **Gerenciamento de Sessão:** Suporte a sessões persistentes ou dinâmicas, com comandos in-band para troca ou reset de contexto (`/new`, `/session`, `/reset`).
- **Telemetria de Tokens & Cache:** Exibição do consumo de tokens (prompt, cache hit ratio, resposta e contexto total) ao término de cada resposta.
- **Line Editing Básico:** Suporte a eco local e tecla *Backspace* (`\x08`).

## 📋 Pré-requisitos

- Python 3.8+
- Instância do **OpenClaw** rodando com Gateway habilitado (padrão em `ws://localhost:18789`)
- Dependências Python:
  ```bash
  pip install websockets python-dotenv
  ```
- Para transferências Zmodem (opcional, mas recomendado):
  ```bash
  brew install lrzsz
  ```

## ⚙️ Configuração

Crie ou edite o arquivo `~/.env` (ou no diretório do projeto):

```ini
OPENCLAW_TOKEN=seu_token_aqui
PORT=8888
AGENT=openclaw/main
SESSION_ID=minha-sessao-fixa   # Opcional: fixa a sessão padrão
```

## 🚀 Como Executar

### Uso padrão:
```bash
python openclaw_term_bridge.py
```

### Com argumentos customizados:
```bash
python openclaw_term_bridge.py -p 9000 -a openclaw/main -s sessao-dos
```

### Opções de linha de comando:
- `-p`, `--port`: Porta TCP para escuta (padrão: `8888` ou variável `PORT`).
- `-a`, `--agent`: Modelo/agente OpenClaw (padrão: `openclaw/main` ou variável `AGENT`).
- `-s`, `--session`: ID de sessão fixo para preservar ou retomar contexto anterior (padrão: gerado aleatoriamente por conexão ou variável `SESSION_ID`).

## 💬 Comandos no Terminal

Durante a sessão ativa no terminal, você pode usar os seguintes comandos inline:

- `/new [id]` ou `/reset`: Inicia uma nova sessão limpa (gera novo ID ou usa o fornecido).
- `/session <id>`: Alterna para uma sessão específica existente.
- `/sz <arquivo>`: Download via Zmodem para o TeleMate.
- `/rz [pasta]`: Upload via Zmodem do TeleMate para o host.
- `/help`, `/?`: Exibe a lista de comandos.

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

## 📂 Estrutura das Cores ANSI

| Elemento | Código ANSI | Cor |
| :--- | :--- | :--- |
| Banner / Título | `1;36m` | Ciano Brilhante |
| Status / Spinner / Sessão | `1;33m` | Amarelo |
| Prompt (`OpenClaw>`) | `1;35m` | Magenta |
| Texto do Usuário | `1;37m` | Branco Brilhante |
| Resposta do Agente | `1;32m` | Verde Brilhante |
| Erros | `1;31m` | Vermelho |
