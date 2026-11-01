import argparse
import json
import os
import re
import socket
import threading
import time
import uuid
from pathlib import Path
import requests
from dotenv import load_dotenv

# Códigos de Cor ANSI padrão (TeleMate / MS-DOS ANSI.SYS)
CLR_RESET   = "\x1b[0m"
CLR_HEADER  = "\x1b[1;36m"
CLR_STATUS  = "\x1b[1;33m"
CLR_AGENT   = "\x1b[1;32m"
CLR_PROMPT  = "\x1b[1;35m"
CLR_USER    = "\x1b[1;37m"
CLR_ERROR   = "\x1b[1;31m"

OPENCLAW_URL = "http://localhost:18789/v1/chat/completions"
HOST = "127.0.0.1"

DEFAULT_PORT = 8888
DEFAULT_AGENT = "openclaw/main"

load_dotenv(Path.home() / ".env")
OPENCLAW_TOKEN = os.getenv("OPENCLAW_TOKEN", "seu_token_fallback")

# Captura qualquer padrão de escape ANSI (byte ESC real ou notação literal)
ANSI_CAPTURE = re.compile(r'(\\033|\\x1b|\\e|\x1b)\[[0-9;]*[a-zA-Z]')

def sanitize_text(text: str) -> str:
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
    """Spinner clássico de BBS com envio em tempo real via TCP sem buffer."""
    FRAMES = ["|", "/", "-", "\\"]

    def __init__(self, conn: socket.socket, message: str = "Aguardando"):
        self.conn = conn
        self.message = message
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._spin, daemon=True)

    def start():
        pass

    def start(self):
        # Renderiza o 1º frame imediatamente na tela antes de iniciar o timer da thread
        first_frame = f"\r{CLR_STATUS}[{self.message} {self.FRAMES[0]}]{CLR_RESET}"
        try:
            self.conn.sendall(encode_dos(first_frame))
        except Exception:
            pass
        self.thread.start()

    def stop(self):
        if not self.stop_event.is_set():
            self.stop_event.set()
            if self.thread.is_alive():
                self.thread.join(timeout=0.5)
            # Sobrescreve com espaços e volta o cursor para a margem esquerda
            clear_len = len(self.message) + 8
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
            frame = self.FRAMES[idx % len(self.FRAMES)]
            text = f"\r{CLR_STATUS}[{self.message} {frame}]{CLR_RESET}"
            try:
                self.conn.sendall(encode_dos(text))
            except Exception:
                break
            idx += 1

def generate_session_id() -> str:
    return f"term-{uuid.uuid4().hex[:8]}"

def stream_openclaw(prompt: str, conn: socket.socket, agent: str, session_id: str, spinner: BBSSpinner) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENCLAW_TOKEN}"
    }
    payload = {
        "model": agent,
        "user": session_id,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ambiente: Terminal DOS TeleMate em ANSI, CP850, 78 colunas máx.\n"
                    "Sempre use acentuação normal em português (á, é, ã, ç, etc.).\n"
                    "Proibido Markdown (** ## |). Para cores use prefixo \\e[ (reset \\e[0m):\n"
                    "Cores: \\e[1;36m=Ciano \\e[1;33m=Amarelo \\e[1;32m=Verde \\e[1;31m=Vermelho \\e[1;35m=Magenta\n"
                    "Exemplo: \\e[1;35m[AGENTE]\\e[0m \\e[1;36mStatus:\\e[0m \\e[1;32mOK\\e[0m"
                )
            },
            {"role": "user", "content": prompt}
        ],
        "stream": True,
        "stream_options": {"include_usage": True}
    }
    
    usage = {}
    spinner_active = True

    try:
        response = requests.post(
            OPENCLAW_URL, 
            headers=headers, 
            json=payload, 
            stream=True, 
            timeout=300
        )

        if response.status_code != 200:
            spinner.stop()
            spinner_active = False
            err_msg = f"{CLR_ERROR}[Erro OpenClaw: {response.status_code}]{CLR_RESET}\r\n"
            conn.sendall(encode_dos(err_msg))
            return usage

        col_count = 0
        MAX_COLS = 76
        raw_accumulator = ""

        for line in response.iter_lines():
            if not line:
                continue
            
            line_str = line.decode("utf-8")
            if line_str.startswith("data: "):
                data_str = line_str[6:].strip()
                if data_str == "[DONE]":
                    break
                
                try:
                    chunk_json = json.loads(data_str)

                    if chunk_json.get("usage"):
                        usage = chunk_json["usage"]

                    choices = chunk_json.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        
                        if content:
                            # Apaga o spinner APENAS no exato instante em que o primeiro caractere for renderizado
                            if spinner_active:
                                spinner.stop()
                                spinner_active = False
                                conn.sendall(encode_dos(CLR_AGENT))

                            raw_accumulator += sanitize_text(content)
                            
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

                except json.JSONDecodeError:
                    continue

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
        err_msg = f"{CLR_ERROR}[Erro no stream: {str(e)}]{CLR_RESET}\r\n"
        conn.sendall(encode_dos(err_msg))

    return usage

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
                f"{CLR_STATUS}[Sessão: {session_id} | Comandos: /new [id], /reset, /session <id>]{CLR_RESET}\r\n\r\n"
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

                        conn.sendall(encode_dos("\r\n"))
                        spinner = BBSSpinner(conn, message="Aguarde")
                        spinner.start()
                        
                        usage = stream_openclaw(prompt, conn, agent, session_id, spinner)
                        
                        # Extração tolerante de cache (OpenAI, Anthropic, LiteLLM)
                        details = usage.get("prompt_tokens_details", {}) or {}
                        cached_tok = (
                            details.get("cached_tokens")
                            or usage.get("cache_read_input_tokens")
                            or usage.get("cached_tokens")
                            or 0
                        )

                        p_tok = usage.get("prompt_tokens", 0)
                        c_tok = usage.get("completion_tokens", 0)

                        # Tokens líquidos reais de prompt (descontando cache)
                        p_net = max(0, p_tok - cached_tok)
                        req_net_tokens = p_net + c_tok
                        session_tokens += req_net_tokens
                        context_tokens = p_tok + c_tok

                        # Formatação do resumo de tokens
                        if cached_tok > 0:
                            cache_ratio = (cached_tok / p_tok * 100) if p_tok > 0 else 0.0
                            tok_info = (
                                f"\r\n{CLR_RESET}[P:{round(p_net/1024)}k C:{cache_ratio:.0f}% R:{c_tok} | T:{round(context_tokens/1024)}k tok]{CLR_RESET}\r\n"
                            )
                        else:
                            tok_info = (
                                f"\r\n{CLR_RESET}[P:{round(p_net/1024)} R:{c_tok} | T:{round(context_tokens/1024)}k tok]{CLR_RESET}\r\n"
                            )

                        conn.sendall(encode_dos(tok_info))
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

    parser = argparse.ArgumentParser(description="Bridge OpenClaw para TeleMate Terminal (CP850)")
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
