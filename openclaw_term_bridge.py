import argparse
import select
import json
import os
import re
import shutil
import socket
import subprocess
import textwrap
import unicodedata
import tempfile
import threading
import time
import uuid
from pathlib import Path
from dotenv import load_dotenv
from websockets.sync.client import connect as ws_connect

# Códigos de Cor ANSI padrão (Terminal DOS / MS-DOS ANSI.SYS)
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


def wrap_text_for_dos(text: str, max_cols: int = 76) -> str:
    """Quebra parágrafos longos em linhas físicas de até max_cols para visualização perfeita no QEdit/DOS."""
    out_lines = []
    for line in text.splitlines():
        if len(line) <= max_cols:
            out_lines.append(line)
        else:
            match = re.match(r"^(\s*(?:[-*+]\s+|\d+[.)]\s+)?)", line)
            prefix = match.group(1) if match else ""
            subsequent = " " * len(prefix) if prefix.strip() else prefix
            wrapped = textwrap.fill(
                line,
                width=max_cols,
                subsequent_indent=subsequent,
                break_long_words=True,
                break_on_hyphens=False
            )
            out_lines.extend(wrapped.splitlines())
    return chr(10).join(out_lines)


def convert_utf8_to_cp850_crlf(content_bytes: bytes, wrap: bool = False, max_cols: int = 76) -> bytes:
    """Converte conteúdo de texto UTF-8 (Unix LF) para CP850 (DOS CRLF), aplicando wrap opcional para QEdit."""
    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = content_bytes.decode("latin-1", errors="replace")

    sanitized = sanitize_text(text)
    if wrap:
        sanitized = wrap_text_for_dos(sanitized, max_cols=max_cols)

    crlf = chr(13) + chr(10)
    lf = chr(10)
    normalized_crlf = sanitized.replace(crlf, lf).replace(lf, crlf)
    return normalized_crlf.encode("cp850", errors="replace")


def convert_cp850_to_utf8_lf(content_bytes: bytes) -> bytes:
    """Converte conteúdo de texto CP850 (DOS CRLF) para UTF-8 (Unix LF)."""
    text = content_bytes.decode("cp850", errors="replace")
    normalized_lf = text.replace("\r\n", "\n")
    return normalized_lf.encode("utf-8")


def clear_input_buffer(buffer: str) -> str:
    """Retorna sequências ANSI para apagar buffer multilinhas e reposicionar o cursor no prompt."""
    lines = buffer.split("\n")
    if len(lines) == 1:
        return "\x08 \x08" * len(buffer)
    else:
        seq = ["\r\x1b[K"]
        for _ in range(len(lines) - 1):
            seq.append("\x1b[A\r\x1b[K")
        seq.append(f"\r{CLR_PROMPT}OpenClaw>{CLR_USER} ")
        return "".join(seq)


def render_input_buffer(buffer: str) -> str:
    """Renderiza visualmente um buffer multilinhas no terminal com prompt de continuação."""
    lines = buffer.split("\n")
    if len(lines) == 1:
        return f"{CLR_USER}{lines[0]}"
    else:
        out = [f"{CLR_USER}{lines[0]}"]
        for l in lines[1:]:
            out.append(f"\r\n{CLR_PROMPT}..>{CLR_USER} {l}")
        return "".join(out)


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


BOX_START_CHARS = set("┌┐└┘│─┼├┤┬┴╔╗╚╝║═╠╣╦╩╬░▒▓█+|")

class ANSIWordWrapper:
    """
    Word-wrapper avançado para streaming com ANSI.
    Ignora sequências de escape na largura da tela e quebra
    linhas suavemente nos espaços antes de estourar max_cols.
    Detecta automaticamente linhas de tabelas/quadros/regras horizontais
    (iniciadas por delimitadores como +, |, ┌, etc.) e realiza bypass da quebra.
    """
    def __init__(self, max_cols: int = 76):
        self.max_cols = max_cols
        self.col = 0
        self.word_chars = []
        self.word_vis_len = 0
        self.in_ansi = False
        self.ansi_buf = ""
        self.is_box_line = False
        self.line_prefix_chars = ""

    def _check_box_prefix(self, s: str) -> bool:
        stripped = s.lstrip()
        if not stripped:
            return False
        if stripped[0] in BOX_START_CHARS:
            return True
        if len(stripped) >= 3 and (stripped[:3] in ("---", "===", "***")):
            return True
        return False

    def feed(self, text: str) -> str:
        out = []
        for char in text:
            if self.in_ansi:
                self.ansi_buf += char
                if char.isalpha() or char in ("~", "@"):
                    self.in_ansi = False
                    clean_seq = re.sub(r"^(\\033|\\x1b|\\e|\x1b)", "\x1b", self.ansi_buf)
                    self.word_chars.append(clean_seq)
                    self.ansi_buf = ""
                continue

            if char in ("\\", "\x1b"):
                self.in_ansi = True
                self.ansi_buf = char
                continue

            if char == "\r":
                continue

            if char == "\n":
                if self.word_chars:
                    out.append("".join(self.word_chars))
                    self.word_chars = []
                    self.word_vis_len = 0
                out.append("\r\n")
                self.col = 0
                self.is_box_line = False
                self.line_prefix_chars = ""
                continue

            # Se o word-wrap estiver desativado (max_cols <= 0), repassa direto
            if self.max_cols <= 0:
                if self.word_chars:
                    out.append("".join(self.word_chars))
                    self.word_chars = []
                    self.word_vis_len = 0
                out.append(char)
                continue

            # Verificação de início de linha para bypass de molduras / tabelas / arte
            if not self.is_box_line and len(self.line_prefix_chars) < 5:
                self.line_prefix_chars += char
                if self._check_box_prefix(self.line_prefix_chars):
                    self.is_box_line = True

            # Se for linha de quadro ou tabela, preserva sem quebras forçadas
            if self.is_box_line:
                if char in (" ", "\t"):
                    if self.word_chars:
                        out.append("".join(self.word_chars))
                        self.word_chars = []
                        self.word_vis_len = 0
                    out.append(char)
                    self.col += 1
                else:
                    self.word_chars.append(char)
                    self.word_vis_len += 1
                    if self.word_vis_len > 20:
                        out.append("".join(self.word_chars))
                        self.col += self.word_vis_len
                        self.word_chars = []
                        self.word_vis_len = 0
                continue

            # Fluxo normal de texto com word-wrap suave
            if char in (" ", "\t"):
                if self.col + self.word_vis_len > self.max_cols and self.col > 0:
                    out.append("\r\n")
                    self.col = 0
                if self.word_chars:
                    out.append("".join(self.word_chars))
                    self.col += self.word_vis_len
                    self.word_chars = []
                    self.word_vis_len = 0
                out.append(char)
                self.col += 1
                continue

            self.word_chars.append(char)
            self.word_vis_len += 1

            if self.col + self.word_vis_len > self.max_cols and self.col > 0:
                out.append("\r\n")
                self.col = 0

        return "".join(out)

    def flush(self) -> str:
        out = []
        if self.in_ansi and self.ansi_buf:
            out.append(self.ansi_buf)
            self.in_ansi = False
            self.ansi_buf = ""
        if self.word_chars:
            if not self.is_box_line and self.max_cols > 0 and self.col + self.word_vis_len > self.max_cols and self.col > 0:
                out.append("\r\n")
            out.append("".join(self.word_chars))
            self.word_chars = []
            self.word_vis_len = 0
        return "".join(out)


class BBSPager:
    """Controlador de paginação de tela clássico de BBS (estilo 'Mais / More')."""
    def __init__(self, conn: socket.socket, page_lines: int = 48):
        self.conn = conn
        self.page_lines = page_lines
        self.lines_count = 0
        self.aborted = False
        self.is_first_page = True

    def reset(self):
        self.lines_count = 0
        self.aborted = False
        self.is_first_page = True

    def send(self, text: str, ws=None, session_key: str = None, target_run_id: str = None) -> bool:
        """Envia texto contando quebras de linha e pausando ao atingir o limite da tela. Retorna False se abortado."""
        if not text or self.aborted:
            return not self.aborted
        if self.page_lines <= 0:
            self.conn.sendall(encode_dos(text))
            return True

        for line in text.splitlines(keepends=True):
            self.conn.sendall(encode_dos(line))
            if "\n" in line:
                self.lines_count += 1
                # Na primeira página, desconta a linha do prompt anterior e a linha do prompt -- Mais --
                threshold = max(3, self.page_lines - 2) if self.is_first_page else max(3, self.page_lines - 1)
                if self.lines_count >= threshold:
                    prompt = f"\r{CLR_STATUS}-- Mais --{CLR_RESET} "
                    self.conn.sendall(encode_dos(prompt))
                    
                    user_key = self._wait_key()
                    # Limpa a linha do prompt de forma suave
                    clear_prompt = "\r" + (" " * 55) + "\r"
                    self.conn.sendall(encode_dos(clear_prompt))
                    
                    if user_key in (b"q", b"Q", b"\x03", b"\x1b"):  # Q, Ctrl+C ou ESC
                        self.aborted = True
                        self.conn.sendall(encode_dos(f"\r\n{CLR_ERROR}[Rolagem cancelada pelo usuário]{CLR_RESET}\r\n"))
                        if ws and session_key:
                            abort_payload = {"sessionKey": session_key}
                            if target_run_id:
                                abort_payload["runId"] = target_run_id
                            abort_req = {
                                "type": "req",
                                "id": str(uuid.uuid4()),
                                "method": "chat.abort",
                                "params": abort_payload
                            }
                            try:
                                ws.send(json.dumps(abort_req))
                            except Exception:
                                pass
                        return False
                    
                    # Espaço ou qualquer outra tecla prossegue para a próxima página
                    self.lines_count = 0
                    self.is_first_page = False

        return True

    def _wait_key(self) -> bytes:
        try:
            ch = self.conn.recv(1)
            if not ch:
                return b"q"
            if ch == b"\r":
                r, _, _ = select.select([self.conn], [], [], 0.05)
                if r:
                    self.conn.recv(1)
            return ch
        except Exception:
            return b"q"


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

def transliterate_greek_text(text: str) -> str:
    greek_to_latin = {
        'α': 'a', 'β': 'b', 'γ': 'g', 'δ': 'd', 'ε': 'e', 'ζ': 'z', 'η': 'ê', 'θ': 'th',
        'ι': 'i', 'κ': 'k', 'λ': 'l', 'μ': 'm', 'ν': 'n', 'ξ': 'x', 'ο': 'o', 'π': 'p',
        'ρ': 'r', 'σ': 's', 'ς': 's', 'τ': 't', 'υ': 'u', 'φ': 'ph', 'χ': 'ch', 'ψ': 'ps', 'ω': 'ô',
        'Α': 'A', 'Β': 'B', 'Γ': 'G', 'Δ': 'D', 'Ε': 'E', 'Ζ': 'Z', 'Η': 'Ê', 'Θ': 'Th',
        'Ι': 'I', 'Κ': 'K', 'Λ': 'L', 'Μ': 'M', 'Ν': 'N', 'Ξ': 'X', 'Ο': 'O', 'Π': 'P',
        'Ρ': 'R', 'Σ': 'S', 'Τ': 'T', 'Υ': 'U', 'Φ': 'Ph', 'Χ': 'Ch', 'Ψ': 'Ps', 'Ω': 'Ô'
    }

    # 1. Desmembra todo o texto (grego e latino) separando os acentos
    text_nfd = unicodedata.normalize('NFD', text)
    
    # 2. Converte o espírito áspero para 'h' (minúsculo ou maiúsculo) antes de iterar
    text_nfd = re.sub(r'([α-ω])\u0314', r'h\1', text_nfd)
    text_nfd = re.sub(r'([Α-Ω])\u0314', r'H\1', text_nfd)
    
    result = []
    is_greek_context = False
    
    for char in text_nfd:
        if char in greek_to_latin:
            # É uma letra grega: converte e liga o alerta de contexto grego
            result.append(greek_to_latin[char])
            is_greek_context = True
            
        elif unicodedata.combining(char):
            # É um acento/diacrítico. Só descartamos se a letra anterior for grega.
            # Se a anterior for latina, nós o mantemos na lista.
            if not is_greek_context:
                result.append(char)
                
        else:
            # É pontuação, espaço ou uma letra latina
            result.append(char)
            # Se for uma letra (como 'O', 'l', 'a'), desliga o contexto grego
            if char.isalpha():
                is_greek_context = False

    # 3. Recombina o texto (NFC) para que os acentos latinos voltem a formar 'ê', 'á', etc.
    return unicodedata.normalize('NFC', "".join(result))


def sanitize_text(text: str) -> str:
    # Normaliza primeiro para NFC para compor acentos (ex: a + acento -> á) antes de checar CP850
    text = unicodedata.normalize("NFC", text)
    # Translitera caracteres gregos
    text = transliterate_greek_text(text)
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
    """Barra de status dinâmica de BBS em linha única com ou sem animação giratória."""
    FRAMES = ["|", "/", "-", "\\"]

    def __init__(self, conn: socket.socket, message: str = "Conectando...", enabled: bool = True):
        self.conn = conn
        self.message = message
        self.enabled = enabled  # True = com animação giratória; False = linha única dinâmica sem rotação contínua
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.lock = threading.Lock()
        self.last_render_len = 0

    def _truncate(self, msg: str, max_chars: int = 54) -> str:
        """Limita o tamanho do texto para nunca quebrar linha em 78 colunas."""
        if len(msg) > max_chars:
            return msg[:max_chars - 3] + "..."
        return msg

    def _format_tag(self, safe_msg: str, frame: str = "") -> str:
        if self.enabled and frame:
            return f"[{safe_msg} {frame}]"
        return f"[{safe_msg}]"

    def start(self):
        with self.lock:
            safe_msg = self._truncate(self.message)
            tag = self._format_tag(safe_msg, self.FRAMES[0])
            first_frame = f"\r{CLR_STATUS}{tag}{CLR_RESET}"
            self.last_render_len = len(tag)
            try:
                self.conn.sendall(encode_dos(first_frame))
            except Exception:
                pass
        if self.enabled:
            self.thread.start()

    def update_message(self, new_message: str):
        """Atualiza dinamicamente a MESMA linha via \r (nunca pula linha)."""
        with self.lock:
            if self.stop_event.is_set():
                return
            self.message = new_message
            safe_msg = self._truncate(self.message)
            tag = self._format_tag(safe_msg, self.FRAMES[0])
            current_len = len(tag)
            padding = " " * max(0, self.last_render_len - current_len)
            self.last_render_len = max(self.last_render_len, current_len)
            text = f"\r{CLR_STATUS}{tag}{CLR_RESET}{padding}"
            try:
                self.conn.sendall(encode_dos(text))
            except Exception:
                pass

    def stop(self):
        """Limpa a linha de status com espaços e devolve o cursor ao início via \r."""
        if not self.stop_event.is_set():
            self.stop_event.set()
            if self.enabled and self.thread.is_alive():
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
                frame = self.FRAMES[idx % len(self.FRAMES)]
                idx += 1
                safe_msg = self._truncate(self.message)
                tag = self._format_tag(safe_msg, frame)
                current_len = len(tag)
                padding = " " * max(0, self.last_render_len - current_len)
                self.last_render_len = max(self.last_render_len, current_len)
                text = f"\r{CLR_STATUS}{tag}{CLR_RESET}{padding}"
                try:
                    self.conn.sendall(encode_dos(text))
                except Exception:
                    break

def generate_session_id() -> str:
    return f"term-{uuid.uuid4().hex[:8]}"


def stream_openclaw(prompt: str, conn: socket.socket, agent: str, session_id: str, spinner: BBSSpinner, pager: BBSPager = None, wrap_cols: int = 76):
    """Executa prompt via WebSocket nativo do Gateway OpenClaw com eventos e streaming."""
    usage = {}
    spinner_active = True
    if pager is None:
        pager = BBSPager(conn, page_lines=0)
    directive_filter = DirectiveFilter()
    word_wrapper = ANSIWordWrapper(max_cols=wrap_cols)

    # Normaliza identificador do agente e chave de sessão
    agent_id = agent.split("/", 1)[1] if "/" in agent else agent
    session_key = f"agent:{agent_id}:openai-user:{session_id}"

    # Contexto de formato universal para Terminal DOS injetado no prompt
    env_context = (
        "[Contexto: Terminal DOS em ANSI, CP850.\n"
        "Escreva em português brasileiro correto e natural, com toda a acentuação gráfica padrão (á, é, í, ó, ú, â, ê, ô, ã, õ, ç, etc.). A acentuação em português é obrigatória: responda em UTF-8 normal pois a bridge converte para CP850 do MS-DOS.\n"
        "Proibido emojis, ideogramas ou símbolos gráficos modernos. Para ícones ou divisores visuais, use apenas arte de texto simples ou caracteres semigráficos da CP850.\n"
        "Proibido Markdown (** ## |). Use listas simples com hífen (-). Não insira quebras manuais de linha no meio de frases: escreva parágrafos contínuos e fluidos (o terminal faz o word-wrap automaticamente).\n"
        "Para quadros e tabelas, use bordas com largura máxima de 76 colunas para caber na tela sem quebras artificiais.\n"
        "Para cores use prefixo \\e[ (reset \\e[0m):\n"
        "Cores: \\e[1;36m=Ciano \\e[1;33m=Amarelo \\e[1;32m=Verde \\e[1;31m=Vermelho \\e[1;35m=Magenta\n"
        "Exemplo: \\e[1;35m[AGENTE]\\e[0m \\e[1;36mStatus:\\e[0m \\e[1;32mOK\\e[0m\n"
        "Para enviar arquivos via download Zmodem no terminal DOS, insira no final em linha própria: DOWNLOAD:<caminho_absoluto>. O arquivo deve ser enviado exatamente como está no disco, sem nenhuma modificação ou conversão prévia, pois a bridge gerencia a CP850 e o Zmodem integralmente. Para múltiplos arquivos, insira uma linha DOWNLOAD separada para cada arquivo. O bridge gerencia automaticamente a conversão para CP850, formatação DOS 8.3 e o protocolo Zmodem sem necessidade de etapas manuais ou de inspeção do bridge.]\n\n"
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
            aborted_by_user = False

            while True:
                # 1. Verifica se o usuário pressionou CTRL+C (0x03) no terminal
                try:
                    r_list, _, _ = select.select([conn], [], [], 0)
                    if r_list:
                        user_in = conn.recv(1024)
                        if not user_in:
                            break
                        if b"\x03" in user_in:
                            aborted_by_user = True
                            if spinner_active:
                                spinner.stop()
                                spinner_active = False
                            conn.sendall(encode_dos(f"\r\n{CLR_ERROR}^C [Processamento abortado pelo usuário]{CLR_RESET}\r\n"))
                            # Dispara chat.abort para o Gateway OpenClaw
                            abort_payload = {"sessionKey": session_key}
                            if target_run_id:
                                abort_payload["runId"] = target_run_id
                            abort_req = {
                                "type": "req",
                                "id": str(uuid.uuid4()),
                                "method": "chat.abort",
                                "params": abort_payload
                            }
                            try:
                                ws.send(json.dumps(abort_req))
                            except Exception:
                                pass
                            usage["aborted"] = True
                            break
                except Exception:
                    pass

                # 2. Recebe mensagens do Gateway OpenClaw com timeout curto (100ms)
                try:
                    msg_raw = ws.recv(timeout=0.1)
                except TimeoutError:
                    continue
                except Exception:
                    break

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

                                        wrapped_text = word_wrapper.feed(safe_content)
                                        if wrapped_text:
                                            if not pager.send(wrapped_text, ws=ws, session_key=session_key, target_run_id=target_run_id):
                                                usage["aborted"] = True
                                                break

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

        if usage.get("aborted"):
            return usage, []

        trailing_directive_text = directive_filter.finish()
        if trailing_directive_text:
            safe_trailing = sanitize_text(trailing_directive_text)
            if safe_trailing:
                if spinner_active:
                    spinner.stop()
                    spinner_active = False
                    conn.sendall(encode_dos(CLR_AGENT))
                wrapped_trailing = word_wrapper.feed(safe_trailing)
                if wrapped_trailing:
                    if not pager.send(wrapped_trailing, ws=ws, session_key=session_key, target_run_id=target_run_id):
                        usage["aborted"] = True
                        return usage, []

        if spinner_active:
            spinner.stop()

        flushed = word_wrapper.flush()
        if flushed:
            if not pager.send(flushed, ws=ws, session_key=session_key, target_run_id=target_run_id):
                usage["aborted"] = True
                return usage, []

        conn.sendall(encode_dos(f"{CLR_RESET}\r\n"))

    except Exception as e:
        if spinner_active:
            spinner.stop()
        err_msg = f"{CLR_ERROR}[Erro no stream WebSocket: {str(e)}]{CLR_RESET}\r\n"
        conn.sendall(encode_dos(err_msg))

    return usage, directive_filter.downloads


def handle_zmodem_download(conn: socket.socket, file_arg: str):
    """Envia um arquivo para o terminal via ZMODEM (sz) com conversão automática UTF-8 -> CP850 (CRLF)."""
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
    temp_dir = tempfile.mkdtemp(prefix="dos_term_dl_")
    target_send_path = os.path.join(temp_dir, dos_filename)
    is_text = is_text_file(filepath)

    CODE_EXTENSIONS = {
        ".c", ".cpp", ".h", ".hpp", ".pas", ".bas", ".asm", ".inc",
        ".py", ".sh", ".bat", ".cmd", ".json", ".xml", ".html", ".htm",
        ".css", ".js", ".ts", ".sql", ".ini", ".cfg", ".conf", ".yaml", ".yml"
    }
    ext = os.path.splitext(filepath)[1].lower()
    should_wrap = ext not in CODE_EXTENSIONS

    if is_text:
        try:
            with open(filepath, "rb") as f:
                raw_bytes = f.read()
            converted_bytes = convert_utf8_to_cp850_crlf(raw_bytes, wrap=should_wrap, max_cols=76)
            with open(target_send_path, "wb") as f:
                f.write(converted_bytes)
            status_wrap = " + Wrap 76 cols" if should_wrap else ""
            conn.sendall(encode_dos(f"\r\n{CLR_STATUS}[Conversão ativa: UTF-8 -> CP850 (CRLF{status_wrap}) | Nome DOS: {dos_filename}]{CLR_RESET}"))
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
    """Recebe um arquivo vindo do terminal via ZMODEM (rz) com conversão automática CP850 (DOS CRLF) -> UTF-8 (Unix LF)."""
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
    conn.sendall(encode_dos(f"{CLR_STATUS}[Dica: Pressione PgUp ou o atalho de upload no terminal se o envio não iniciar sozinho]{CLR_RESET}\r\n"))
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


def get_session_model(session_id: str, agent: str) -> str:
    """Consulta o modelo configurado para a sessão no Gateway OpenClaw."""
    agent_id = agent.split("/", 1)[1] if "/" in agent else agent
    session_key = f"agent:{agent_id}:openai-user:{session_id}"
    try:
        with ws_connect(OPENCLAW_WS_URL, close_timeout=3) as ws:
            ws.recv()
            ws.send(json.dumps({
                "type": "req", "id": "c-1", "method": "connect",
                "params": {
                    "minProtocol": 4, "maxProtocol": 4,
                    "client": {"id": "cli", "version": "1.0.0", "platform": "macos", "mode": "cli"},
                    "role": "operator", "scopes": ["operator.read", "operator.write"],
                    "auth": {"token": OPENCLAW_TOKEN}
                }
            }))
            ws.recv()
            req_id = str(uuid.uuid4())
            ws.send(json.dumps({
                "type": "req", "id": req_id, "method": "sessions.list",
                "params": {"search": session_id, "limit": 5}
            }))
            resp = json.loads(ws.recv())
            payload = resp.get("payload", {})
            for s in payload.get("sessions", []):
                if s.get("key") == session_key or session_id in s.get("key", ""):
                    return s.get("model") or payload.get("defaults", {}).get("model", "padrão")
            return payload.get("defaults", {}).get("model", "padrão")
    except Exception:
        return "padrão"


def patch_session_model(session_id: str, new_model: str, agent: str) -> tuple[bool, str]:
    """Altera dinamicamente o modelo da sessão ativa via RPC sessions.patch no Gateway."""
    agent_id = agent.split("/", 1)[1] if "/" in agent else agent
    session_key = f"agent:{agent_id}:openai-user:{session_id}"
    try:
        with ws_connect(OPENCLAW_WS_URL, close_timeout=3) as ws:
            ws.recv()
            ws.send(json.dumps({
                "type": "req", "id": "c-1", "method": "connect",
                "params": {
                    "minProtocol": 4, "maxProtocol": 4,
                    "client": {"id": "cli", "version": "1.0.0", "platform": "macos", "mode": "cli"},
                    "role": "operator", "scopes": ["operator.read", "operator.write"],
                    "auth": {"token": OPENCLAW_TOKEN}
                }
            }))
            ws.recv()
            req_id = str(uuid.uuid4())
            ws.send(json.dumps({
                "type": "req", "id": req_id, "method": "sessions.patch",
                "params": {"key": session_key, "model": new_model}
            }))
            resp = json.loads(ws.recv())
            if resp.get("ok"):
                payload = resp.get("payload", {})
                resolved = payload.get("resolved", {})
                active_model = resolved.get("model") or payload.get("entry", {}).get("modelOverride") or new_model
                return True, active_model
            else:
                err = resp.get("error", {}).get("message", "Falha ao alterar modelo")
                return False, err
    except Exception as e:
        return False, str(e)


POPULAR_MODELS = [
    ("1", "gemini-3.8-flash", "google/gemini-3.8-flash", "Google (Padrão, Rápido)"),
    ("2", "claude-sonnet-5-5", "anthropic/claude-sonnet-5-5", "Anthropic (Poderoso, Preciso)"),
    ("3", "claude-opus-5-5", "anthropic/claude-opus-5-5", "Anthropic (Raciocínio Máximo)"),
    ("4", "gpt-5.6-terra", "openai/gpt-5.6-terra", "OpenAI (Fallback geral)"),
    ("5", "gemini-3.5-flash-lite", "google/gemini-3.5-flash-lite", "Google (Ultra-leve)"),
]


def render_model_menu(current_model: str) -> str:
    """Renderiza o menu clássico de seleção de modelos em estilo BBS ANSI."""
    lines = [
        f"\r\n{CLR_HEADER}+--------------------------------------------------------------+{CLR_RESET}",
        f"{CLR_HEADER}|             SELECAO DE MODELOS - OPENCLAW BBS                |{CLR_RESET}",
        f"{CLR_HEADER}+--------------------------------------------------------------+{CLR_RESET}",
    ]
    for key, name, full_id, desc in POPULAR_MODELS:
        is_cur = (name in current_model or full_id in current_model)
        marker = f"{CLR_AGENT}*" if is_cur else " "
        cur_tag = f" {CLR_AGENT}[ATIVO]{CLR_RESET}" if is_cur else ""
        lines.append(f"{marker} {CLR_STATUS}[{key}]{CLR_RESET} {CLR_USER}{name:<22}{CLR_RESET} {CLR_HEADER}{desc}{CLR_RESET}{cur_tag}")
    lines.extend([
        f"{CLR_HEADER}+--------------------------------------------------------------+{CLR_RESET}",
        f"{CLR_STATUS}Digite o número [1-5] ou use: /model <nome_do_modelo>{CLR_RESET}\r\n"
    ])
    return "\r\n".join(lines)


def start_bridge(port: int, agent: str, fixed_session_id: str = None, default_lines: int = 49, default_spinner: bool = True, default_wrap: int = 76):
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
            command_history = []
            history_idx = -1
            ansi_state = 0
            session_tokens = 0
            current_model = get_session_model(session_id, agent)
            awaiting_model_menu = False
            current_col = 10
            spinner_enabled = default_spinner
            page_lines = default_lines
            wrap_cols = default_wrap
            pager = BBSPager(conn, page_lines=page_lines)

            # Pausa de estabilizacao pos-conexao (carrier settle time):
            # Da tempo para o modem virtual do DOSBox emitir o resultado CONNECT
            # e para o discador do terminal fechar sua janela e voltar ao terminal.
            time.sleep(1.5)

            banner = (
                f"\r\n{CLR_HEADER}"
                f"       ___                   _____ _\r\n"
                f"      / _ \ _ __   ___ _ __ / __ _| | __ ___      __\r\n"
                f"     | | | | '_ \ / _ \ '_ \ |    | |/ _` \ \ /\ \ /\r\n"
                f"     | |_| | |_) |  __/ | | | |___| | (_| |\ V  V /\r\n"
                f"      \___/| .__/ \___|_| |_|\____|_|\__,_| \_/\_/\r\n"
                f"           |_| {CLR_PROMPT}PORT: {port}{CLR_HEADER}    {CLR_AGENT}AGENTE: {agent}{CLR_RESET}\r\n"
                f"{CLR_HEADER}+--------------------------------------------------------------+{CLR_RESET}\r\n"
                f"{CLR_STATUS}    [Sessão: {CLR_HEADER}{session_id}{CLR_STATUS} | Modelo: {CLR_HEADER}{current_model}{CLR_STATUS}]{CLR_RESET}\r\n"
                f"{CLR_STATUS}    [Comandos: /help | /model | /lines {CLR_HEADER}{page_lines}{CLR_STATUS} | /wrap {CLR_HEADER}{wrap_cols}{CLR_STATUS}]{CLR_RESET}\r\n"
                f"{CLR_HEADER}+--------------------------------------------------------------+{CLR_RESET}\r\n\r\n"
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
                        if buffer.endswith("\\"):
                            buffer = buffer[:-1] + "\n"
                            current_col = 4
                            conn.sendall(encode_dos(f"\r\n{CLR_PROMPT}..>{CLR_USER} "))
                            continue
                        prompt = buffer.strip()
                        if prompt:
                            if not command_history or command_history[-1] != prompt:
                                command_history.append(prompt)
                                if len(command_history) > 50:
                                    command_history.pop(0)
                        history_idx = -1
                        buffer = ""
                        current_col = 10
                        if not prompt:
                            conn.sendall(encode_dos(f"\r\n{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                            continue

                        if awaiting_model_menu and prompt in [k for k, _, _, _ in POPULAR_MODELS]:
                            target_model = prompt
                            for key, name, full_id, _ in POPULAR_MODELS:
                                if prompt == key:
                                    target_model = full_id
                                    break
                            conn.sendall(encode_dos(f"\r\n{CLR_STATUS}[Alterando modelo para: {target_model}...]{CLR_RESET}"))
                            ok, res = patch_session_model(session_id, target_model, agent)
                            if ok:
                                current_model = res
                                msg = f"\r\n{CLR_AGENT}[Modelo alterado com sucesso: {CLR_HEADER}{current_model}{CLR_AGENT}]{CLR_RESET}\r\n"
                            else:
                                msg = f"\r\n{CLR_ERROR}[Erro ao alterar modelo: {res}]{CLR_RESET}\r\n"
                            conn.sendall(encode_dos(msg))
                            awaiting_model_menu = False
                            conn.sendall(encode_dos(f"\r\n{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                            continue

                        awaiting_model_menu = False

                        if prompt.startswith("/"):
                            parts = prompt.split(maxsplit=1)
                            cmd = parts[0].lower()

                            if cmd in ("/reset", "/new", "/session"):
                                if len(parts) > 1:
                                    session_id = parts[1].strip()
                                else:
                                    session_id = fixed_session_id or generate_session_id()

                                session_tokens = 0
                                current_model = get_session_model(session_id, agent)
                                reset_msg = (
                                    f"\r\n{CLR_STATUS}[Sessão ativa: {session_id} | Modelo: {CLR_HEADER}{current_model}{CLR_STATUS}]{CLR_RESET}\r\n"
                                    f"{CLR_PROMPT}OpenClaw>{CLR_USER} "
                                )
                                conn.sendall(encode_dos(reset_msg))
                                continue

                            if cmd == "/model":
                                current_model = get_session_model(session_id, agent)
                                if len(parts) > 1 and parts[1].strip():
                                    target_arg = parts[1].strip()
                                    target_model = target_arg
                                    for key, name, full_id, _ in POPULAR_MODELS:
                                        if target_arg == key:
                                            target_model = full_id
                                            break
                                    conn.sendall(encode_dos(f"\r\n{CLR_STATUS}[Alterando modelo para: {target_model}...]{CLR_RESET}"))
                                    ok, res = patch_session_model(session_id, target_model, agent)
                                    if ok:
                                        current_model = res
                                        msg = f"\r\n{CLR_AGENT}[Modelo alterado com sucesso: {CLR_HEADER}{current_model}{CLR_AGENT}]{CLR_RESET}\r\n"
                                    else:
                                        msg = f"\r\n{CLR_ERROR}[Erro ao alterar modelo: {res}]{CLR_RESET}\r\n"
                                    conn.sendall(encode_dos(msg))
                                else:
                                    menu_text = render_model_menu(current_model)
                                    conn.sendall(encode_dos(menu_text))
                                    awaiting_model_menu = True
                                conn.sendall(encode_dos(f"\r\n{CLR_PROMPT}OpenClaw>{CLR_USER} "))
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

                            if cmd in ("/spinner", "/spin"):
                                if len(parts) > 1:
                                    arg = parts[1].strip().lower()
                                    if arg in ("off", "0", "nao", "não", "desativar", "false"):
                                        spinner_enabled = False
                                        msg = f"\r\n{CLR_STATUS}[Spinner desativado (preservando buffer de tela do terminal)]{CLR_RESET}\r\n"
                                    elif arg in ("on", "1", "sim", "ativar", "true"):
                                        spinner_enabled = True
                                        msg = f"\r\n{CLR_STATUS}[Spinner ativado]{CLR_RESET}\r\n"
                                    else:
                                        msg = f"\r\n{CLR_ERROR}Uso: /spinner on | /spinner off{CLR_RESET}\r\n"
                                else:
                                    spinner_enabled = not spinner_enabled
                                    st = "ativado" if spinner_enabled else "desativado"
                                    msg = f"\r\n{CLR_STATUS}[Spinner {st}]{CLR_RESET}\r\n"
                                conn.sendall(encode_dos(msg))
                                conn.sendall(encode_dos(f"{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                                continue

                            if cmd in ("/wrap", "/wordwrap", "/cols"):
                                if len(parts) > 1:
                                    arg = parts[1].strip().lower()
                                    if arg in ("off", "0", "nao", "não", "desativar", "false"):
                                        wrap_cols = 0
                                        msg = f"\r\n{CLR_STATUS}[Word-wrap desativado (transmissão contínua)]{CLR_RESET}\r\n"
                                    elif arg in ("on", "padrao", "padrão"):
                                        wrap_cols = 76
                                        msg = f"\r\n{CLR_STATUS}[Word-wrap configurado para o padrão (76 colunas)]{CLR_RESET}\r\n"
                                    else:
                                        try:
                                            nw = int(arg)
                                            if nw < 20 and nw != 0:
                                                msg = f"\r\n{CLR_ERROR}Número mínimo de colunas para word-wrap é 20{CLR_RESET}\r\n"
                                            else:
                                                wrap_cols = nw
                                                msg = f"\r\n{CLR_STATUS}[Word-wrap configurado para {wrap_cols} colunas (com bypass de quadros)]{CLR_RESET}\r\n"
                                        except ValueError:
                                            msg = f"\r\n{CLR_ERROR}Uso: /wrap <colunas|off> (ex: /wrap 76, /wrap 78 ou /wrap off){CLR_RESET}\r\n"
                                else:
                                    st = f"{wrap_cols} colunas (com bypass de quadros)" if wrap_cols > 0 else "desativado"
                                    msg = f"\r\n{CLR_STATUS}[Word-wrap atual: {st} (use /wrap <n|off> para alterar)]{CLR_RESET}\r\n"
                                conn.sendall(encode_dos(msg))
                                conn.sendall(encode_dos(f"{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                                continue

                            if cmd in ("/lines", "/page", "/rows"):
                                if len(parts) > 1:
                                    arg = parts[1].strip().lower()
                                    if arg in ("off", "0", "desativar", "none", "false"):
                                        page_lines = 0
                                        pager.page_lines = 0
                                        msg = f"\r\n{CLR_STATUS}[Paginação desativada (rolagem contínua)]{CLR_RESET}\r\n"
                                    else:
                                        try:
                                            nl = int(arg)
                                            if nl < 5 and nl != 0:
                                                msg = f"\r\n{CLR_ERROR}Número mínimo de linhas para paginação é 5{CLR_RESET}\r\n"
                                            else:
                                                page_lines = nl
                                                pager.page_lines = nl
                                                msg = f"\r\n{CLR_STATUS}[Paginação configurada para {page_lines} linhas]{CLR_RESET}\r\n"
                                        except ValueError:
                                            msg = f"\r\n{CLR_ERROR}Uso: /lines <número> (ex: /lines 48 ou /lines 0 para desativar){CLR_RESET}\r\n"
                                else:
                                    st = f"{page_lines} linhas" if page_lines > 0 else "desativada"
                                    msg = f"\r\n{CLR_STATUS}[Paginação atual: {st} (use /lines <n> para alterar)]{CLR_RESET}\r\n"
                                conn.sendall(encode_dos(msg))
                                conn.sendall(encode_dos(f"{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                                continue

                            if cmd in ("/help", "/?"):
                                help_msg = (
                                    f"\r\n{CLR_HEADER}--- Comandos Disponíveis ---{CLR_RESET}\r\n"
                                    f"{CLR_STATUS}/new [id]{CLR_RESET}         - Inicia uma nova conversa\r\n"
                                    f"{CLR_STATUS}/session <id>{CLR_RESET}     - Conecta a uma sessão existente\r\n"
                                    f"{CLR_STATUS}/reset{CLR_RESET}            - Reinicia a sessão atual\r\n"
                                    f"{CLR_STATUS}/model [num|nome]{CLR_RESET} - Menu BBS de seleção ou troca de modelo\r\n"
                                    f"{CLR_STATUS}/lines [n]{CLR_RESET}        - Linhas por tela para paginação (default: 49, 0=off)\r\n"
                                    f"{CLR_STATUS}/wrap [n|off]{CLR_RESET}      - Quebra de palavras (default: 76, bypass em quadros)\r\n"
                                    f"{CLR_STATUS}/spinner [on|off]{CLR_RESET}  - Ativa/desativa animação giratória (mantém status na mesma linha)\r\n"
                                    f"{CLR_STATUS}/sz <arquivo>{CLR_RESET}     - Download via Zmodem para o terminal DOS\r\n"
                                    f"{CLR_STATUS}/rz [pasta]{CLR_RESET}       - Upload via Zmodem do terminal DOS para o host\r\n"
                                    f"{CLR_STATUS}/help, /?{CLR_RESET}         - Exibe esta ajuda\r\n"
                                )
                                conn.sendall(encode_dos(help_msg))
                                conn.sendall(encode_dos(f"{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                                continue

                        conn.sendall(encode_dos("\r\n"))
                        spinner = BBSSpinner(conn, message="Conectando...", enabled=spinner_enabled)
                        spinner.start()
                        pager.reset()
                        pager.page_lines = page_lines
                        
                        usage, downloads = stream_openclaw(prompt, conn, agent, session_id, spinner, pager=pager, wrap_cols=wrap_cols)

                        if usage.get("aborted"):
                            conn.sendall(encode_dos(f"\r\n{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                            continue
                        
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

                        conn.sendall(encode_dos(f"\r\n{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                        continue

                    if ansi_state == 0 and char == "\x1b":
                        ansi_state = 1
                        continue
                    elif ansi_state == 1:
                        if char == "[":
                            ansi_state = 2
                            continue
                        else:
                            ansi_state = 0
                    elif ansi_state == 2:
                        ansi_state = 0
                        if char == "A":
                            if command_history:
                                if history_idx == -1:
                                    history_idx = len(command_history) - 1
                                elif history_idx > 0:
                                    history_idx -= 1
                                if len(buffer) > 0:
                                    conn.sendall(encode_dos(clear_input_buffer(buffer)))
                                buffer = command_history[history_idx]
                                conn.sendall(encode_dos(render_input_buffer(buffer)))
                                lines = buffer.split("\n")
                                current_col = (4 if len(lines) > 1 else 10) + len(lines[-1])
                            continue
                        elif char == "B":
                            if command_history and history_idx != -1:
                                if len(buffer) > 0:
                                    conn.sendall(encode_dos(clear_input_buffer(buffer)))
                                if history_idx < len(command_history) - 1:
                                    history_idx += 1
                                    buffer = command_history[history_idx]
                                    conn.sendall(encode_dos(render_input_buffer(buffer)))
                                    lines = buffer.split("\n")
                                    current_col = (4 if len(lines) > 1 else 10) + len(lines[-1])
                                else:
                                    history_idx = -1
                                    buffer = ""
                                    current_col = 10
                            continue

                    elif char in ("\x08", "\x7f"):
                        if len(buffer) > 0:
                            last_char = buffer[-1]
                            if last_char == "\n":
                                buffer = buffer[:-1]
                                lines = buffer.split("\n")
                                prev_line = lines[-1]
                                prev_col = (10 if len(lines) == 1 else 4) + len(prev_line)
                                current_col = prev_col
                                back_seq = f"\r\x1b[K\x1b[A\r" + (f"\x1b[{prev_col}C" if prev_col > 0 else "")
                                conn.sendall(encode_dos(back_seq))
                            else:
                                buffer = buffer[:-1]
                                current_col = max(0, current_col - 1)
                                conn.sendall(b"\x08 \x08")
                    elif char == "\x0c":  # Ctrl+L (limpar tela inteira e reexibir prompt com buffer atual)
                        conn.sendall(encode_dos(f"\x1b[2J\x1b[H{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                        if buffer:
                            conn.sendall(encode_dos(render_input_buffer(buffer)))
                        lines = buffer.split("\n") if buffer else [""]
                        current_col = (4 if len(lines) > 1 else 10) + len(lines[-1])
                    elif char == "\x15":  # Ctrl+U (limpar linha e reexibir prompt)
                        history_idx = -1
                        if len(buffer) > 0:
                            conn.sendall(encode_dos(clear_input_buffer(buffer)))
                            buffer = ""
                            current_col = 10
                        else:
                            conn.sendall(encode_dos(f"\r\n{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                            current_col = 10
                    elif char == "\x03":  # Ctrl+C (cancelar buffer e exibir novo prompt)
                        history_idx = -1
                        ansi_state = 0
                        awaiting_model_menu = False
                        buffer = ""
                        current_col = 10
                        conn.sendall(encode_dos(f"^C\r\n{CLR_PROMPT}OpenClaw>{CLR_USER} "))
                    elif ord(char) < 32 and char not in ("\r", "\n"):
                        pass  # Ignora outros caracteres de controle
                    else:
                        is_cmd = buffer.startswith("/")
                        if char == " " and current_col >= 72 and not is_cmd:
                            buffer += "\n"
                            current_col = 4
                            conn.sendall(encode_dos(f"\r\n{CLR_PROMPT}..>{CLR_USER} "))
                        elif current_col >= 76 and char != " " and not is_cmd:
                            cur_line = buffer.split("\n")[-1]
                            m = re.search(r'(\S+)$', cur_line)
                            if m and len(m.group(1)) < 55:
                                word = m.group(1)
                                erase_len = len(word)
                                erase_seq = "\x08 \x08" * erase_len
                                buffer = buffer[:-erase_len] + "\n" + word + char
                                current_col = 4 + len(word) + 1
                                out_seq = f"{erase_seq}\r\n{CLR_PROMPT}..>{CLR_USER} {word}{char}"
                                conn.sendall(encode_dos(out_seq))
                            else:
                                buffer += "\n" + char
                                current_col = 5
                                conn.sendall(encode_dos(f"\r\n{CLR_PROMPT}..>{CLR_USER} {char}"))
                        else:
                            buffer += char
                            current_col += 1
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

    parser = argparse.ArgumentParser(description="Bridge OpenClaw para Terminal DOS / BBS (CP850 / WebSocket)")
    parser.add_argument("-p", "--port", type=int, default=env_port, help=f"Porta do servidor socket (padrão: {env_port})")
    parser.add_argument("-a", "--agent", type=str, default=env_agent, help=f"Modelo do agente OpenClaw (padrão: {env_agent})")
    parser.add_argument("-s", "--session", type=str, default=env_session, help="ID de sessão fixo para retomar conversas anteriores")
    parser.add_argument("--lines", type=int, default=49, help="Número de linhas por tela para paginação (padrão: 49, 0 para desativar)")
    parser.add_argument("--wrap", type=int, default=76, help="Largura máxima para quebra de palavras (padrão: 76, 0 para desativar)")
    parser.add_argument("--no-spinner", action="store_true", help="Desativa o spinner por padrão")

    args = parser.parse_args()

    try:
        start_bridge(port=args.port, agent=args.agent, fixed_session_id=args.session, default_lines=args.lines, default_spinner=not args.no_spinner, default_wrap=args.wrap)
    except KeyboardInterrupt:
        print("\n[*] Bridge encerrada.")
    except Exception as e:
        print(f"\n[!] Erro fatal: {e}")
