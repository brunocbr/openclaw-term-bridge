import argparse
import json
import os
import re
import socket
import time
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
    """
    Remove símbolos APENAS se o caractere NÃO for representável na CP850.
    A CP850 suporta nativamente a acentuação completa em Português (ã, õ, ç, á, é, ê, etc.).
    """
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
    """Converte texto preservando a acentuação compatível com CP850."""
    return text.encode("cp850", errors="ignore")

def stream_openclaw(prompt: str, conn: socket.socket, agent: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENCLAW_TOKEN}"
    }
    payload = {
        "model": agent,
        "user": "brunoc",
        "messages": [
            {"role": "system", "content": (
                "Ambiente: Terminal DOS TeleMate, 78 colunas max. "
                "Para formatar cores ANSI use códigos ANSI literais. "
                "Não use Markdown (sem **, ## ou tabelas com |)."
            )},
            {"role": "user", "content": prompt}
        ],
        "stream": True
    }
    
    try:
        response = requests.post(
            OPENCLAW_URL, 
            headers=headers, 
            json=payload, 
            stream=True, 
            timeout=30
        )
        
        if response.status_code != 200:
            err_msg = f"\r\n{CLR_ERROR}[Erro OpenClaw: {response.status_code}]{CLR_RESET}\r\n"
            conn.sendall(encode_dos(err_msg))
            return

        conn.sendall(encode_dos(f"\r\n{CLR_AGENT}"))

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
                    delta = chunk_json["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    
                    if content:
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

        if raw_accumulator:
            final_clean = re.sub(r'(\\033|\\x1b|\\e)', '\x1b', raw_accumulator)
            for char in final_clean:
                if char == "\n":
                    conn.sendall(encode_dos("\r\n"))
                else:
                    conn.sendall(encode_dos(char))

        conn.sendall(encode_dos(f"{CLR_RESET}\r\n"))

    except Exception as e:
        err_msg = f"\r\n{CLR_ERROR}[Erro no stream: {str(e)}]{CLR_RESET}\r\n"
        conn.sendall(encode_dos(err_msg))

def start_bridge(port: int, agent: str):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, port))
    server.listen(5)
    print(f"[*] Bridge OpenClaw escutando na porta {port} | Agente: {agent}")

    while True:
        conn = None
        addr = None
        try:
            conn, addr = server.accept()
            print(f"[+] Nova conexão estabelecida via {addr}")
           
            banner = (
                f"\r\n{CLR_HEADER}========================================\r\n"
                f"    OPENCLAW LOCAL AGENT - TERMINAL     \r\n"
                f"========================================{CLR_RESET}\r\n\r\n"
                f"{CLR_PROMPT}OpenClaw>{CLR_USER} "
            )
            conn.sendall(encode_dos(banner))

            print("DEBUG: banner enviado")

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
                        if prompt:
                            status_msg = f"\r\n{CLR_STATUS}[Aguardando OpenClaw...]{CLR_RESET}"
                            conn.sendall(encode_dos(status_msg))
                            
                            stream_openclaw(prompt, conn, agent)
                            
                            conn.sendall(encode_dos(f"\r\n{CLR_PROMPT}OpenClaw>{CLR_USER} "))
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

    parser = argparse.ArgumentParser(description="Bridge OpenClaw para TeleMate Terminal (CP850)")
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=env_port,
        help=f"Porta do servidor socket (padrão: {env_port})"
    )
    parser.add_argument(
        "-a", "--agent",
        type=str,
        default=env_agent,
        help=f"Modelo do agente OpenClaw (padrão: {env_agent})"
    )

    args = parser.parse_args()

    try:
        start_bridge(port=args.port, agent=args.agent)
    except KeyboardInterrupt:
        print("\n[*] Bridge encerrada.")
    except Exception as e:
        print(f"\n[!] Erro fatal: {e}")
