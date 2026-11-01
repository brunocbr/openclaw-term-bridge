import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import unicodedata
import tempfile
import threading
import time
import uuid
from pathlib import Path
from dotenv import load_dotenv
from websockets.sync.client import connect as ws_connect

# Códigos de Cor ANSI padrão (TeleMate / MS-DOS ANSI.SYS)
CLR_RESET   = "\x1b[0m"
CLR_HEADER  = "\x1b[1;36m"
CLR_STATUS  = "\x1b[1;33m"
CLR_AGENT   = "\x1b[1;32m"
CLR_PROMPT  = "\x1b[1;35m"
CLR_USER    = "\x1b[1;37m"
CLR_ERROR   = "\x1b[1;31m"

OPENCLAW_WS_URL = os.getenv("OPENCLAW_WS_URL", "ws://127.0.0.1:18789")
HOST = "127.0.0.1"

DEFAULT_PORT = 8888
DEFAULT_AGENT = "openclaw/main"

load_dotenv(Path.home() / ".env")
OPENCLAW_TOKEN = os.environ.get("OPENCLAW_TOKEN", "seu_token_fallback")

# Captura qualquer padrão de escape ANSI (byte ESC real ou notação literal)
ANSI_CAPTURE = re.compile(r'(\\033|\\x1b|\\e|\x1b)\[[0-9;]*[a-zA-Z]')
DIRECTIVE_PREFIXES = ("DOWNLOAD:", "MEDIA:", "[[DOWNLOAD:")

# Dicionários de tradução e mapeamento amigável para o terminal retrô
TOOL_LABELS = {
    "exec": "Terminal",
    "read": "Lendo arquivo",
    "write": "Gravando arquivo",
    "edit": "Editando arquivo",
    "web_search": "Pesquisando na web",
    "web_fetch": "Baixando página web",
    "memory_search": "Buscando na memória",
    "memory_get": "Lendo memória",
    "sessions_search": "Buscando histórico",
    "sessions_history": "Lendo histórico",
    "apply_patch": "Aplicando patch",
}

PHASE_LABELS = {
    "preparing_workspace": "Preparando ambiente...",
    "preparing_context": "Lendo contexto e memória...",
    "starting_model": "Pensando...",
    "thinking": "Raciocinando...",
}

def to_dos_83_filename(filename: str) -> str:
    """Converte qualquer nome de arquivo moderno para o padrão estrito MS-DOS 8.3 (FAT)."""
    nfkd = unicodedata.normalize("NFKD", filename)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    base, ext = os.path.splitext(ascii_name)
    clean_base = re.sub(r"[^a-zA-Z0-9_\-]", "_", base)
    clean_base = re.sub(r"_+", "_", clean_base).strip("_")
    clean_ext = re.sub(r"[^a-zA-Z0-9]", "", ext)
    EXT_MAP = {
        "markdown": "md",
        "text": "txt",
        "html": "htm",
        "jpeg": "jpg",
        "yaml": "yml",
        "python": "py",
    }
    ext_lower = clean_ext.lower()
    if ext_lower in EXT_MAP:
        clean_ext = EXT_MAP[ext_lower]
    else:
        clean_ext = clean_ext[:3]
    if not clean_base:
        clean_base = "FILE"
    clean_base = clean_base[:8].upper()
    clean_ext = clean_ext[:3].upper()
    if clean_ext:
        return f"{clean_base}.{clean_ext}"
    return clean_base

TEXT_EXTENSIONS = {
    ".txt", ".text", ".md", ".markdown", ".c", ".h", ".cpp", ".pas",
    ".bas", ".asm", ".py", ".sh", ".bat", ".cmd", ".json", ".xml",
    ".html", ".htm", ".css", ".js", ".ts", ".ini", ".cfg", ".conf",
    ".log", ".csv", ".tsv", ".nfo", ".diz", ".ans", ".asc", ".me", ".1st"
}


def is_text_file(filepath: str) -> bool:
    """Verifica se o arquivo é texto através da extensão ou inspeção de bytes."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in TEXT_EXTENSIONS:
        return True
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(1024)
            if not chunk:
                return True
            if b"\x00" in chunk:
                return False
            try:
                chunk.decode("utf-8")
                return True
            except UnicodeDecodeError:
                try:
                    chunk.decode("cp850")
                    return True
                except UnicodeDecodeError:
                    return False
    except Exception:
        return False


def convert_utf8_to_cp850_crlf(content_bytes: bytes) -> bytes:
    """Converte conteúdo de texto UTF-8 (Unix LF) para CP850 (DOS CRLF)."""
    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = content_bytes.decode("latin-1", errors="replace")

    sanitized = sanitize_text(text)
    normalized_crlf = sanitized.replace("\r\n", "\n").replace("\n", "\r\n")
    return normalized_crlf.encode("cp850", errors="replace")


def convert_cp850_to_utf8_lf(content_bytes: bytes) -> bytes:
    """Converte conteúdo de texto CP850 (DOS CRLF) para UTF-8 (Unix LF)."""
    text = content_bytes.decode("cp850", errors="replace")
    normalized_lf = text.replace("\r\n", "\n")
    return normalized_lf.encode("utf-8")


def format_tool_status(name: str, title: str = None) -> str:
    """Formata o status de execução de ferramenta para o spinner de 78 colunas."""
    label = TOOL_LABELS.get(name, name)
    if not title:
        return f"{label}..."
    t = title.strip()
    for prefix in ("Exec ", "exec ", "command ", "Command "):
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    return f"{label}: {t}"


class DirectiveFilter:
    """Filtra diretivas de download em tempo real durante o streaming.
    Linhas iniciadas por DOWNLOAD:<path> ou MEDIA:<path> são suprimidas da tela
    e os caminhos são acumulados para disparo automático via Zmodem."""
    def __init__(self):
        self.current_line = ""
        self.at_line_start = True
        self.downloads = []

    def _check_directive(self, line: str) -> str:
        stripped = ANSI_CAPTURE.sub('', line).strip()
        upper = stripped.upper()
        for p in DIRECTIVE_PREFIXES:
            if upper.startswith(p):
                raw = stripped[len(p):].strip()
                if p == "[[DOWNLOAD:" and raw.endswith("]]"):
                    raw = raw[:-2].strip()
                return unicodedata.normalize("NFC", raw.strip('"\''))
        return ""

    def feed(self, text: str) -> str:
        output = []
        for char in text:
            if self.at_line_start:
                self.current_line += char
                if char == "\n":
                    dl = self._check_directive(self.current_line)
                    if dl:
                        self.downloads.append(dl)
                    else:
                        output.append(self.current_line)
                    self.current_line = ""
                    self.at_line_start = True
                    continue

                stripped = ANSI_CAPTURE.sub('', self.current_line).lstrip()
                if not stripped:
                    continue

                upper = stripped.upper()
                matches_prefix = any(p.startswith(upper) or upper.startswith(p) for p in DIRECTIVE_PREFIXES)
                if not matches_prefix:
                    self.at_line_start = False
                    output.append(self.current_line)
                    self.current_line = ""
            else:
                if char == "\n":
                    output.append("\n")
                    self.at_line_start = True
                    self.current_line = ""
                else:
                    output.append(char)
        return "".join(output)

    def finish(self) -> str:
        output = ""
        if self.current_line:
            dl = self._check_directive(self.current_line)
            if dl:
                self.downloads.append(dl)
            else:
                output = self.current_line
            self.current_line = ""
        return output


def sanitize_text(text: str) -> str:
    # Normaliza primeiro para NFC para compor acentos (ex: a + acento -> á) antes de checar CP850
    text = unicodedata.normalize("NFC", text)
    unsupported_map = {
        '“': '"', '”': '"', '‘': "'", '’': "'", '—': '-', '–': '-', '…': '...', '•': '*',
    }
    res = []
    for char in text:
        if char in unsupported_map:
            res.append(unsupported_map[char])
        else:
            try:
                char.encode("cp850")
                res.append(char)
            except UnicodeEncodeError:
                res.append("?")
    return "".join(res)


def encode_dos(text: str) -> bytes:
    return text.encode("cp850", errors="ignore")


class BBSSpinner:
    """Spinner clássico de BBS com mensagem dinâmica em tempo real via TCP sem buffer."""
    FRAMES = ["|", "/", "-", "\\"]

    def __init__(self, conn: socket.socket, message: str = "Conectando..."):
        self.conn = conn
        self.message = message
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.lock = threading.Lock()
        self.last_render_len = 0

    def _truncate(self, msg: str, max_chars: int = 54) -> str:
        """Limita o tamanho do texto para nunca quebrar linha em 78 colunas."""
        if len(msg) > max_chars:
            return msg[:max_chars - 3] + "..."
        return msg

    def start(self):
        with self.lock:
            safe_msg = self._truncate(self.message)
            first_frame = f"\r{CLR_STATUS}[{safe_msg} {self.FRAMES[0]}]{CLR_RESET}"
            self.last_render_len = len(safe_msg) + 5
            try:
                self.conn.sendall(encode_dos(first_frame))
            except Exception:
                pass
        self.thread.start()

    def update_message(self, new_message: str):
        """Atualiza a mensagem exibida no spinner imediatamente."""
        with self.lock:
            if self.stop_event.is_set():
                return
            self.message = new_message
            safe_msg = self._truncate(self.message)
            frame = self.FRAMES[0]
            current_len = len(safe_msg) + 5
            padding = " " * max(0, self.last_render_len - current_len)
            self.last_render_len = max(self.last_render_len, current_len)
            text = f"\r{CLR_STATUS}[{safe_msg} {frame}]{CLR_RESET}{padding}"
            try:
                self.conn.sendall(encode_dos(text))
            except Exception:
                pass

    def stop(self):
        if not self.stop_event.is_set():
            self.stop_event.set()
            if self.thread.is_alive():
                self.thread.join(timeout=0.5)
            with self.lock:
                clear_len = max(self.last_render_len, len(self.message) + 8) + 8
                try:
                    self.conn.sendall(encode_dos("\r" + (" " * clear_len) + "\r"))
                except Exception:
                    pass

    def _spin(self):
        idx = 1
        while not self.stop_event.is_set():
            self.stop_event.wait(0.12)
            if self.stop_event.is_set():
                break
            with self.lock:
                safe_msg = self._truncate(self.message)
                frame = self.FRAMES[idx % len(self.FRAMES)]
                current_len = len(safe_msg) + 5
                padding = " " * max(0, self.last_render_len - current_len)
                self.last_render_len = max(self.last_render_len, current_len)
                text = f"\r{CLR_STATUS}[{safe_msg} {frame}]{CLR_RESET}{padding}"
                try:
                    self.conn.sendall(encode_dos(text))
                except Exception:
                    break
            idx += 1


def generate_session_id() -> str:
    return f"term-{uuid.uuid4().hex[:8]}"


def stream_openclaw(prompt: str, conn: socket.socket, agent: str, session_id: str, spinner: BBSSpinner):
    """Executa prompt via WebSocket nativo do Gateway OpenClaw com eventos e streaming."""
    usage = {}
    spinner_active = True
    directive_filter = DirectiveFilter()
    col_count = 0
    MAX_COLS = 76
    raw_accumulator = ""

    # Normaliza identificador do agente e chave de sessão
    agent_id = agent.split("/", 1)[1] if "/" in agent else agent
    session_key = f"agent:{agent_id}:openai-user:{session_id}"

    # Contexto de formato para TeleMate injetado no prompt
    env_context = (
        "[Contexto: Terminal DOS TeleMate em ANSI, CP850, 78 colunas máx.\n"
        "Sempre use acentuação normal em português (á, é, ã, ç, etc.).\n"
        "Proibido Markdown (** ## |). Para cores use prefixo \\e[ (reset \\e[0m):\n"
        "Cores: \\e[1;36m=Ciano \\e[1;33m=Amarelo \\e[1;32m=Verde \\e[1;31m=Vermelho \\e[1;35m=Magenta\n"
        "Exemplo: \\e[1;35m[AGENTE]\\e[0m \\e[1;36mStatus:\\e[0m \\e[1;32mOK\\e[0m\n"
        "Para enviar arquivos ao usuário via download Zmodem no TeleMate, insira no final em linha própria: DOWNLOAD:<caminho_do_arquivo>]\n\n"
    )
    full_message = env_context + prompt

    try:
        spinner.update_message("Conectando ao Gateway...")
        with ws_connect(OPENCLAW_WS_URL, close_timeout=5) as ws:
            # 1. Aguarda desafio de conexão (connect.challenge)
            challenge_raw = ws.recv()
            challenge = json.loads(challenge_raw)

            # 2. Handshake autenticado (connect -> hello-ok)
            connect_req_id = "connect-1"
            connect_req = {
                "type": "req",
                "id": connect_req_id,
                "method": "connect",
                "params": {
                    "minProtocol": 4,
                    "maxProtocol": 4,
                    "client": {
                        "id": "cli",
                        "version": "1.0.0",
                        "platform": "macos",
                        "mode": "cli"
                    },
                    "role": "operator",
                    "scopes": ["operator.read", "operator.write", "operator.approvals"],
                    "caps": ["tool-events"],
                    "auth": {"token": OPENCLAW_TOKEN},
                    "locale": "pt-BR",
                    "userAgent": "openclaw-term-bridge/2.0"
                }
            }
            ws.send(json.dumps(connect_req))
            hello_resp = json.loads(ws.recv())
            if not hello_resp.get("ok"):
                spinner.stop()
                spinner_active = False
                err = hello_resp.get("error", {}).get("message", "Falha no handshake")
                conn.sendall(encode_dos(f"{CLR_ERROR}[Erro no Gateway: {err}]{CLR_RESET}\r\n"))
                return usage, directive_filter.downloads

            # 3. Dispara chat.send
            spinner.update_message("Iniciando requisição...")
            chat_req_id = str(uuid.uuid4())
            send_req = {
                "type": "req",
                "id": chat_req_id,
                "method": "chat.send",
                "params": {
                    "sessionKey": session_key,
                    "message": full_message,
                    "idempotencyKey": str(uuid.uuid4())
                }
            }
            ws.send(json.dumps(send_req))

            target_run_id = None

            while True:
                msg_raw = ws.recv()
                if not msg_raw:
                    break
                msg = json.loads(msg_raw)
                m_type = msg.get("type")

                # Resposta da chamada chat.send
                if m_type == "res" and msg.get("id") == chat_req_id:
                    if not msg.get("ok"):
                        spinner.stop()
                        spinner_active = False
                        err = msg.get("error", {}).get("message", "Erro ao enviar chat")
                        conn.sendall(encode_dos(f"{CLR_ERROR}[Erro chat.send: {err}]{CLR_RESET}\r\n"))
                        return usage, directive_filter.downloads
                    target_run_id = msg.get("payload", {}).get("runId")
                    continue

                if m_type == "event":
                    event_name = msg.get("event")
                    payload = msg.get("payload", {})

                    # Filtra apenas eventos pertencentes a esta execução específica
                    if target_run_id and payload.get("runId") and payload.get("runId") != target_run_id:
                        continue

                    # 1. Ciclo de Vida do Agente e Ferramentas
                    if event_name == "agent":
                        stream = payload.get("stream")
                        data = payload.get("data", {}) or {}

                        if stream == "run_status":
                            phase = data.get("phase")
                            label = PHASE_LABELS.get(phase, f"Processando: {phase}")
                            if spinner_active:
                                spinner.update_message(label)

                        elif stream == "item":
                            phase = data.get("phase")
                            tool_name = data.get("name") or "tool"
                            tool_title = data.get("title")

                            if phase == "start" and spinner_active:
                                status_msg = format_tool_status(tool_name, tool_title)
                                spinner.update_message(status_msg)
                            elif phase == "end" and spinner_active:
                                spinner.update_message("Processando resultado...")

                        elif stream == "usage":
                            usage = data

                    # 2. Eventos de Aprovação de Execução
                    elif event_name == "exec.approval.requested":
                        if spinner_active:
                            spinner.update_message("Aguardando aprovação de execução...")

                    # 3. Stream de Chat (Texto de Resposta)
                    elif event_name == "chat":
                        state = payload.get("state")

                        if state == "delta":
                            content = payload.get("deltaText", "")
                            if content:
                                filtered_content = directive_filter.feed(content)
                                if filtered_content:
                                    safe_content = sanitize_text(filtered_content)
                                    if safe_content:
                                        # Desliga o spinner no primeiro caractere renderizável
                                        if spinner_active:
                                            spinner.stop()
                                            spinner_active = False
                                            conn.sendall(encode_dos(CLR_AGENT))

                                        raw_accumulator += safe_content

                                    while True:
                                        match = ANSI_CAPTURE.search(raw_accumulator)
                                        if not match:
                                            break

                                        start, end = match.span()
                                        text_before = raw_accumulator[:start]
                                        for char in text_before:
                                            if char == "\n":
                                                conn.sendall(encode_dos("\r\n"))
                                                col_count = 0
                                            else:
                                                if col_count >= MAX_COLS and char == " ":
                                                    conn.sendall(encode_dos("\r\n"))
                                                    col_count = 0
                                                else:
                                                    conn.sendall(encode_dos(char))
                                                    col_count += 1

                                        raw_seq = match.group(0)
                                        clean_ansi = re.sub(r'^(\\033|\\x1b|\\e|\x1b)', '\x1b', raw_seq)
                                        conn.sendall(encode_dos(clean_ansi))
                                        raw_accumulator = raw_accumulator[end:]

                                    if raw_accumulator:
                                        last_esc_pos = max(
                                            raw_accumulator.rfind("\\"),
                                            raw_accumulator.rfind("\x1b")
                                        )
                                        if last_esc_pos == -1:
                                            safe_text = raw_accumulator
                                            raw_accumulator = ""
                                        else:
                                            safe_text = raw_accumulator[:last_esc_pos]
                                            raw_accumulator = raw_accumulator[last_esc_pos:]

                                        for char in safe_text:
                                            if char == "\n":
                                                conn.sendall(encode_dos("\r\n"))
                                                col_count = 0
                                            else:
                                                if col_count >= MAX_COLS and char == " ":
                                                    conn.sendall(encode_dos("\r\n"))
                                                    col_count = 0
                                                else:
                                                    conn.sendall(encode_dos(char))
                                                    col_count += 1

                        elif state == "final":
                            # Consulta dados consolidados da sessão no Gateway (input, output, total de contexto)
                            try:
                                sess_req_id = str(uuid.uuid4())
                                ws.send(json.dumps({
                                    "type": "req",
                                    "id": sess_req_id,
                                    "method": "sessions.list",
                                    "params": {"search": session_id, "limit": 5}
                                }))
                                while True:
                                    resp_raw = ws.recv(timeout=1.5)
                                    resp_msg = json.loads(resp_raw)
                                    if resp_msg.get("type") == "res" and resp_msg.get("id") == sess_req_id:
                                        sessions = resp_msg.get("payload", {}).get("sessions", [])
                                        for s in sessions:
                                            if s.get("key") == session_key or session_id in s.get("key", ""):
                                                usage["inputTokens"] = s.get("inputTokens", 0)
                                                if not usage.get("outputTokens"):
                                                    usage["outputTokens"] = s.get("outputTokens", 0)
                                                usage["totalTokens"] = s.get("totalTokens", 0)
                                                usage["contextTokens"] = s.get("contextTokens", 0)
                                                usage["model"] = s.get("model", "")
                                                usage["modelProvider"] = s.get("modelProvider", "")
                                                break
                                        break
                            except Exception:
                                pass
                            break

                        elif state == "error":
                            if spinner_active:
                                spinner.stop()
                                spinner_active = False
                            err = payload.get("errorMessage") or "Erro na execução da resposta"
                            conn.sendall(encode_dos(f"\r\n{CLR_ERROR}[Erro: {err}]{CLR_RESET}\r\n"))
                            break

        trailing_directive_text = directive_filter.finish()
        if trailing_directive_text:
            safe_trailing = sanitize_text(trailing_directive_text)
            if safe_trailing:
                if spinner_active:
                    spinner.stop()
                    spinner_active = False
                    conn.sendall(encode_dos(CLR_AGENT))
                raw_accumulator += safe_trailing

        if spinner_active:
            spinner.stop()

        if raw_accumulator:
            final_clean = re.sub(r'(\\033|\\x1b|\\e)', '\x1b', raw_accumulator)
            for char in final_clean:
                if char == "\n":
                    conn.sendall(encode_dos("\r\n"))
                else:
                    conn.sendall(encode_dos(char))

        conn.sendall(encode_dos(f"{CLR_RESET}\r\n"))

    except Exception as e:
        if spinner_active:
            spinner.stop()
        err_msg = f"{CLR_ERROR}[Erro no stream WebSocket: {str(e)}]{CLR_RESET}\r\n"
        conn.sendall(encode_dos(err_msg))

    return usage, directive_filter.downloads


def handle_zmodem_download(conn: socket.socket, file_arg: str):
    """Envia um arquivo para o TeleMate via ZMODEM (sz) com conversão automática UTF-8 -> CP850 (CRLF)."""
    sz_bin = shutil.which("sz")
    if not sz_bin:
        conn.sendall(encode_dos(f"\r\n{CLR_ERROR}[Erro: 'sz' (lrzsz) não encontrado. Instale com: brew install lrzsz]{CLR_RESET}\r\n"))
        return

    cleaned_arg = unicodedata.normalize("NFC", file_arg.strip().strip('"\''))
    filepath = os.path.expanduser(cleaned_arg)
    if not os.path.isfile(filepath):
        # Tenta também forma decomposta NFD (comum em sistemas de arquivos macOS)
        nfd_path = unicodedata.normalize("NFD", filepath)
        if os.path.isfile(nfd_path):
            filepath = nfd_path
        else:
            conn.sendall(encode_dos(f"\r\n{CLR_ERROR}[Erro: Arquivo não encontrado ou inválido: {filepath}]{CLR_RESET}\r\n"))
            return

    dos_filename = to_dos_83_filename(os.path.basename(filepath))
    temp_dir = tempfile.mkdtemp(prefix="telemate_dl_")
    target_send_path = os.path.join(temp_dir, dos_filename)
    is_text = is_text_file(filepath)

    if is_text:
        try:
            with open(filepath, "rb") as f:
                raw_bytes = f.read()
            converted_bytes = convert_utf8_to_cp850_crlf(raw_bytes)
            with open(target_send_path, "wb") as f:
                f.write(converted_bytes)
            conn.sendall(encode_dos(f"\r\n{CLR_STATUS}[Conversão ativa: UTF-8 -> CP850 (CRLF) | Nome DOS: {dos_filename}]{CLR_RESET}"))
        except Exception:
            shutil.copy2(filepath, target_send_path)
    else:
        shutil.copy2(filepath, target_send_path)

    file_size = os.path.getsize(target_send_path)
    file_name = dos_filename
    conn.sendall(encode_dos(f"\r\n{CLR_STATUS}[Iniciando ZMODEM Download: {file_name} ({file_size} bytes)...]{CLR_RESET}\r\n"))
    time.sleep(0.3)

    # Drena eventuais resíduos no socket antes de passar o controle ao sz
    conn.setblocking(False)
    try:
        while conn.recv(1024):
            pass
    except (BlockingIOError, socket.error):
        pass
    conn.setblocking(True)

    try:
        fd = conn.fileno()
        proc = subprocess.run(
            [sz_bin, "-b", "-e", "--zmodem", target_send_path],
            stdin=fd,
            stdout=fd,
            stderr=subprocess.DEVNULL
        )
        if proc.returncode == 0:
            msg = f"\r\n{CLR_AGENT}[ZMODEM: Download concluído com sucesso]{CLR_RESET}\r\n"
        else:
            msg = f"\r\n{CLR_ERROR}[ZMODEM: Transferência abortada ou erro ({proc.returncode})]{CLR_RESET}\r\n"
    except Exception as e:
        msg = f"\r\n{CLR_ERROR}[Erro na execução do Zmodem: {e}]{CLR_RESET}\r\n"
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

    conn.sendall(encode_dos(msg))


def handle_zmodem_upload(conn: socket.socket, target_dir_arg: str = ""):
    """Recebe um arquivo vindo do TeleMate via ZMODEM (rz) com conversão automática CP850 (DOS CRLF) -> UTF-8 (Unix LF)."""
    rz_bin = shutil.which("rz")
    if not rz_bin:
        conn.sendall(encode_dos(f"\r\n{CLR_ERROR}[Erro: 'rz' (lrzsz) não encontrado. Instale com: brew install lrzsz]{CLR_RESET}\r\n"))
        return

    dest_dir = os.path.expanduser(target_dir_arg.strip()) if target_dir_arg else os.getcwd()
    if not os.path.isdir(dest_dir):
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception as e:
            conn.sendall(encode_dos(f"\r\n{CLR_ERROR}[Erro ao criar pasta destino: {e}]{CLR_RESET}\r\n"))
            return

    # Mapeia arquivos existentes e seus timestamps para detectar novos uploads
    files_before = {}
    try:
        for fname in os.listdir(dest_dir):
            full_p = os.path.join(dest_dir, fname)
            if os.path.isfile(full_p):
                files_before[fname] = os.path.getmtime(full_p)
    except Exception:
        pass

    conn.sendall(encode_dos(f"\r\n{CLR_STATUS}[Aguardando ZMODEM Upload em: {dest_dir}...]{CLR_RESET}\r\n"))
    conn.sendall(encode_dos(f"{CLR_STATUS}[Dica: Pressione PgUp no TeleMate se o envio não iniciar sozinho]{CLR_RESET}\r\n"))
    time.sleep(0.3)

    conn.setblocking(False)
    try:
        while conn.recv(1024):
            pass
    except (BlockingIOError, socket.error):
        pass
    conn.setblocking(True)

    try:
        fd = conn.fileno()
        proc = subprocess.run(
            [rz_bin, "-b", "-y"],
            cwd=dest_dir,
            stdin=fd,
            stdout=fd,
            stderr=subprocess.DEVNULL
        )
        if proc.returncode == 0:
            msg = f"\r\n{CLR_AGENT}[ZMODEM: Upload concluído com sucesso]{CLR_RESET}\r\n"
            conn.sendall(encode_dos(msg))

            # Detecta arquivos novos ou modificados para converter CP850 -> UTF-8
            try:
                for fname in os.listdir(dest_dir):
                    full_p = os.path.join(dest_dir, fname)
                    if os.path.isfile(full_p):
                        mtime = os.path.getmtime(full_p)
                        if fname not in files_before or mtime > files_before[fname]:
                            if is_text_file(full_p):
                                with open(full_p, "rb") as f:
                                    raw_b = f.read()
                                utf8_b = convert_cp850_to_utf8_lf(raw_b)
                                with open(full_p, "wb") as f:
                                    f.write(utf8_b)
                                conn.sendall(encode_dos(f"{CLR_STATUS}[Arquivo convertido para UTF-8 (Unix LF): {fname}]{CLR_RESET}\r\n"))
            except Exception as conv_err:
                conn.sendall(encode_dos(f"{CLR_STATUS}[Aviso na conversão UTF-8: {conv_err}]{CLR_RESET}\r\n"))
            return
        else:
            msg = f"\r\n{CLR_ERROR}[ZMODEM: Transferência abortada ou erro ({proc.returncode})]{CLR_RESET}\r\n"
    except Exception as e:
        msg = f"\r\n{CLR_ERROR}[Erro na execução do Zmodem: {e}]{CLR_RESET}\r\n"

    conn.sendall(encode_dos(msg))


def start_bridge(port: int, agent: str, fixed_session_id: str = None):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, port))
    server.listen(5)
    print(f"[*] Bridge OpenClaw escutando na porta {port} | Agente: {agent}")
    if fixed_session_id:
        print(f"[*] Sessão padrão configurada: {fixed_session_id}")

    while True:
        conn = None
        addr = None
        try:
            conn, addr = server.accept()
            # Desativa o buffer Nagle para envio imediato de bytes de controle e do spinner
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print(f"[+] Nova conexão estabelecida via {addr}")
           
            session_id = fixed_session_id or generate_session_id()
            session_tokens = 0

            banner = (
                f"\r\n{CLR_HEADER}========================================\r\n"
                f"    OPENCLAW LOCAL AGENT - TERMINAL     \r\n"
                f"========================================{CLR_RESET}\r\n"
                f"{CLR_STATUS}[Sessão: {session_id} | /help para comandos | /sz <arq>, /rz [dir]]{CLR_RESET}\r\n\r\n"
                f"{CLR_PROMPT}OpenClaw>{CLR_USER} "
            )
            conn.sendall(encode_dos(banner))

            buffer = ""
            while True:
                data = conn.recv(1024)
                if not data:
                    print(f"[-] Cliente {addr} encerrou a conexão graciosamente.")
                    break
                
                text = data.decode("cp850", errors="ignore")
                for char in text:
                    if char in ("\r", "\n"):
                        prompt = buffer.strip()
                        buffer = ""
                        if not prompt:
                            conn.sendall(encode_dos(f"\r\n{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                            continue

                        parts = prompt.split(maxsplit=1)
                        cmd = parts[0].lower()

                        if cmd in ("/reset", "/new", "/session"):
                            if len(parts) > 1:
                                session_id = parts[1].strip()
                            else:
                                session_id = fixed_session_id or generate_session_id()

                            session_tokens = 0
                            reset_msg = (
                                f"\r\n{CLR_STATUS}[Sessão ativa: {session_id}]{CLR_RESET}\r\n"
                                f"{CLR_PROMPT}OpenClaw>{CLR_USER} "
                            )
                            conn.sendall(encode_dos(reset_msg))
                            continue

                        if cmd in ("/download", "/sz"):
                            if len(parts) > 1:
                                handle_zmodem_download(conn, parts[1])
                            else:
                                conn.sendall(encode_dos(f"\r\n{CLR_ERROR}Uso: /sz <caminho_do_arquivo> ou /download <arquivo>{CLR_RESET}\r\n"))
                            conn.sendall(encode_dos(f"{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                            continue

                        if cmd in ("/upload", "/rz"):
                            target = parts[1] if len(parts) > 1 else ""
                            handle_zmodem_upload(conn, target)
                            conn.sendall(encode_dos(f"{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                            continue

                        if cmd in ("/help", "/?"):
                            help_msg = (
                                f"\r\n{CLR_HEADER}--- Comandos Disponíveis ---{CLR_RESET}\r\n"
                                f"{CLR_STATUS}/new [id]{CLR_RESET}         - Inicia uma nova conversa\r\n"
                                f"{CLR_STATUS}/session <id>{CLR_RESET}     - Conecta a uma sessão existente\r\n"
                                f"{CLR_STATUS}/reset{CLR_RESET}            - Reinicia a sessão atual\r\n"
                                f"{CLR_STATUS}/sz <arquivo>{CLR_RESET}     - Download via Zmodem para o TeleMate\r\n"
                                f"{CLR_STATUS}/rz [pasta]{CLR_RESET}       - Upload via Zmodem do TeleMate para o host\r\n"
                                f"{CLR_STATUS}/help, /?{CLR_RESET}         - Exibe esta ajuda\r\n"
                            )
                            conn.sendall(encode_dos(help_msg))
                            conn.sendall(encode_dos(f"{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                            continue

                        conn.sendall(encode_dos("\r\n"))
                        spinner = BBSSpinner(conn, message="Conectando...")
                        spinner.start()
                        
                        usage, downloads = stream_openclaw(prompt, conn, agent, session_id, spinner)
                        
                        # Extração tolerante de tokens
                        details = usage.get("prompt_tokens_details", {}) or {}
                        cached_tok = (
                            details.get("cached_tokens")
                            or usage.get("cache_read_input_tokens")
                            or usage.get("cacheReadTokens")
                            or 0
                        )

                        p_tok = usage.get("prompt_tokens") or usage.get("inputTokens") or 0
                        c_tok = usage.get("completion_tokens") or usage.get("outputTokens") or 0
                        total_tok = usage.get("total_tokens") or usage.get("totalTokens") or 0
                        model_name = usage.get("model") or ""
                        model_tag = f"{CLR_HEADER}{model_name}{CLR_RESET} | " if model_name else ""

                        if p_tok > 0:
                            p_net = max(0, p_tok - cached_tok)
                            context_tokens = total_tok if total_tok > 0 else (p_tok + c_tok)
                            p_display = f"{round(p_net/1024)}k" if p_net >= 1024 else str(p_net)
                            t_display = f"{round(context_tokens/1024)}k" if context_tokens >= 1024 else str(context_tokens)
                            if cached_tok > 0:
                                cache_ratio = (cached_tok / p_tok * 100)
                                tok_info = f"\r\n{CLR_RESET}[{model_tag}P:{p_display} C:{cache_ratio:.0f}% R:{c_tok} | T:{t_display} tok]{CLR_RESET}\r\n"
                            else:
                                tok_info = f"\r\n{CLR_RESET}[{model_tag}P:{p_display} R:{c_tok} | T:{t_display} tok]{CLR_RESET}\r\n"
                        elif c_tok > 0:
                            tok_info = f"\r\n{CLR_RESET}[{model_tag}R:{c_tok} tok]{CLR_RESET}\r\n"
                        elif model_name:
                            tok_info = f"\r\n{CLR_RESET}[{CLR_HEADER}{model_name}{CLR_RESET}]{CLR_RESET}\r\n"
                        else:
                            tok_info = ""

                        if tok_info:
                            conn.sendall(encode_dos(tok_info))

                        if downloads:
                            for dl_path in downloads:
                                handle_zmodem_download(conn, dl_path)

                        conn.sendall(encode_dos(f"{CLR_PROMPT}OpenClaw>{CLR_USER} "))

                    elif char == "\x08":
                        if len(buffer) > 0:
                            buffer = buffer[:-1]
                            conn.sendall(b"\x08 \x08")
                    else:
                        buffer += char
                        conn.sendall(encode_dos(f"{CLR_USER}{char}"))

        except (ConnectionResetError, BrokenPipeError):
            print(f"[!] Conexão com {addr} perdida abruptamente.")
        except Exception as e:
            print(f"[!] Erro durante a sessão com cliente: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            print("[*] Aguardando 2 segundos antes de reabrir escuta...")
            time.sleep(2)
            print("[*] Pronto para aceitar nova conexão.\n")


if __name__ == "__main__":
    env_port = int(os.getenv("PORT", DEFAULT_PORT))
    env_agent = os.getenv("AGENT", DEFAULT_AGENT)
    env_session = os.getenv("SESSION_ID", None)

    parser = argparse.ArgumentParser(description="Bridge OpenClaw para TeleMate Terminal (CP850 / WebSocket)")
    parser.add_argument("-p", "--port", type=int, default=env_port, help=f"Porta do servidor socket (padrão: {env_port})")
    parser.add_argument("-a", "--agent", type=str, default=env_agent, help=f"Modelo do agente OpenClaw (padrão: {env_agent})")
    parser.add_argument("-s", "--session", type=str, default=env_session, help="ID de sessão fixo para retomar conversas anteriores")

    args = parser.parse_args()

    try:
        start_bridge(port=args.port, agent=args.agent, fixed_session_id=args.session)
    except KeyboardInterrupt:
        print("\n[*] Bridge encerrada.")
    except Exception as e:
        print(f"\n[!] Erro fatal: {e}")
