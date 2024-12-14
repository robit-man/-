#!/usr/bin/env python3
import os
import sys
import subprocess
import socket
import re
import json
import argparse
import threading
from queue import Queue

#############################################
# Step 1: Ensure we're running inside a venv #
#############################################

VENV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv")
NEEDED_PACKAGES = ["requests", "num2words"]

def in_venv():
    return (hasattr(sys, 'real_prefix') or
            (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix))

def setup_venv():
    # Create venv if it doesn't exist
    if not os.path.isdir(VENV_DIR):
        print("Creating virtual environment...")
        subprocess.check_call([sys.executable, '-m', 'venv', VENV_DIR])

    pip_path = os.path.join(VENV_DIR, 'bin', 'pip') if os.name != 'nt' else os.path.join(VENV_DIR, 'Scripts', 'pip.exe')
    subprocess.check_call([pip_path, 'install'] + NEEDED_PACKAGES)

def relaunch_in_venv():
    # Relaunch inside venv python
    python_path = os.path.join(VENV_DIR, 'bin', 'python') if os.name != 'nt' else os.path.join(VENV_DIR, 'Scripts', 'python.exe')
    os.execv(python_path, [python_path] + sys.argv)

if not in_venv():
    setup_venv()
    relaunch_in_venv()

#############################################
# Step 2: Imports after venv set up          #
#############################################

import requests
from num2words import num2words

#############################################
# Step 3: Config Defaults & File
#############################################

DEFAULT_CONFIG = {
    "model": "llama3.2-vision",
    "stream": False,
    "format": None,
    "system": None,
    "raw": False,
    "history": None,
    "images": [],
    "tools": None,
    "options": {},
    "host": "0.0.0.0",
    "port": 64162,
    "tts_url": "http://localhost:61637/synthesize",
    "ollama_url": "http://localhost:11434/api/chat"
}
CONFIG_PATH = "config.json"

def load_config():
    if not os.path.exists(CONFIG_PATH):
        print("No config.json found. Creating default config.json...")
        with open(CONFIG_PATH, 'w') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return dict(DEFAULT_CONFIG)
    else:
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
            # Merge with DEFAULT_CONFIG
            for key, value in DEFAULT_CONFIG.items():
                if key not in cfg:
                    cfg[key] = value
            return cfg
        except Exception as e:
            print(f"Error loading config.json: {e}. Using default settings.")
            return dict(DEFAULT_CONFIG)

CONFIG = load_config()

#############################################
# Step 4: Parse Command-Line Arguments       #
#############################################

parser = argparse.ArgumentParser(description="Ollama Chat Server with TTS and advanced features.")

parser.add_argument("--model", type=str, help="Model name to use.")
parser.add_argument("--stream", action="store_true", help="Enable streaming responses from the model.")
parser.add_argument("--format", type=str, help="Structured output format: 'json' or path to JSON schema file.")
parser.add_argument("--system", type=str, help="System message override.")
parser.add_argument("--raw", action="store_true", help="If set, use raw mode (no template).")
parser.add_argument("--history", type=str, help="Path to a JSON file containing conversation history messages.")
parser.add_argument("--images", type=str, nargs='*', help="List of base64-encoded image files.")
parser.add_argument("--tools", type=str, help="Path to a JSON file defining tools.")
parser.add_argument("--option", action="append", help="Additional model parameters (e.g. --option temperature=0.7)")

args = parser.parse_args()

def merge_config_and_args(config, args):
    if args.model:
        config["model"] = args.model
    if args.stream:
        config["stream"] = True
    if args.format is not None:
        config["format"] = args.format
    if args.system is not None:
        config["system"] = args.system
    if args.raw:
        config["raw"] = True
    if args.history is not None:
        config["history"] = args.history
    if args.images is not None:
        config["images"] = args.images
    if args.tools is not None:
        config["tools"] = args.tools
    if args.option:
        for opt in args.option:
            if '=' in opt:
                k, v = opt.split('=', 1)
                k = k.strip()
                v = v.strip()
                if v.isdigit():
                    v = int(v)
                else:
                    try:
                        v = float(v)
                    except ValueError:
                        pass
                config["options"][k] = v
    return config

CONFIG = merge_config_and_args(CONFIG, args)

#############################################
# Step 5: Load Optional Configurations       #
#############################################

def safe_load_json_file(path, default):
    if not path:
        return default
    if not os.path.exists(path):
        print(f"Warning: File '{path}' not found. Using default {default}.")
        if path == CONFIG["history"] and default == []:
            # Create empty history file
            with open(path, 'w') as f:
                json.dump([], f)
        return default
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Could not load '{path}': {e}. Using default {default}.")
        return default

def load_format_schema(fmt):
    if not fmt:
        return None
    if fmt.lower() == "json":
        return "json"
    if os.path.exists(fmt):
        try:
            with open(fmt, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load format schema from '{fmt}': {e}. Ignoring format.")
            return None
    else:
        print(f"Warning: Format file '{fmt}' not found. Ignoring format.")
        return None

history_messages = safe_load_json_file(CONFIG["history"], [])
tools_data = safe_load_json_file(CONFIG["tools"], None)
format_schema = load_format_schema(CONFIG["format"])

#############################################
# Step 6: Ollama chat interaction
#############################################

OLLAMA_CHAT_URL = CONFIG["ollama_url"]

def convert_numbers_to_words(text):
    """
    Convert all standalone numbers in text to their word equivalents.
    """
    def replace_num(match):
        number_str = match.group(0)
        try:
            number_int = int(number_str)
            return num2words(number_int)
        except ValueError:
            return number_str
    return re.sub(r'\b\d+\b', replace_num, text)

def build_payload(user_message):
    messages = []
    if CONFIG["system"]:
        messages.append({"role": "system", "content": CONFIG["system"]})
    messages.extend(history_messages)
    messages.append({"role": "user", "content": user_message})

    payload = {
        "model": CONFIG["model"],
        "messages": messages,
        "stream": CONFIG["stream"]
    }

    if format_schema:
        payload["format"] = format_schema
    if CONFIG["raw"]:
        payload["raw"] = True
    if CONFIG["images"]:
        if payload["messages"] and payload["messages"][-1]["role"] == "user":
            payload["messages"][-1]["images"] = CONFIG["images"]
    if tools_data:
        payload["tools"] = tools_data
    if CONFIG["options"]:
        payload["options"] = CONFIG["options"]

    return payload

stop_flag = False
thread_lock = threading.Lock()

#############################################
# Step 7: TTS Playback with Queue and Thread
#############################################

tts_queue = None
tts_stop_flag = False
tts_thread = None

def tts_worker():
    global tts_stop_flag
    while not tts_stop_flag:
        try:
            sentence = tts_queue.get(timeout=0.1)
        except:
            # No item, just continue if not stopped
            if tts_stop_flag:
                break
            continue

        if tts_stop_flag:
            break

        # Play this sentence
        synthesize_and_play(sentence)

def start_tts_thread():
    global tts_queue, tts_thread, tts_stop_flag
    tts_stop_flag = False
    tts_queue = Queue()
    tts_thread = threading.Thread(target=tts_worker, daemon=True)
    tts_thread.start()

def stop_tts_thread():
    global tts_stop_flag, tts_thread, tts_queue
    if tts_thread and tts_thread.is_alive():
        tts_stop_flag = True
        # Flush queue
        with tts_queue.mutex:
            tts_queue.queue.clear()
        tts_thread.join()
    tts_stop_flag = False
    tts_queue = None
    tts_thread = None

def enqueue_sentence_for_tts(sentence):
    if tts_queue and not tts_stop_flag:
        tts_queue.put(sentence)

def synthesize_and_play(prompt):
    prompt = prompt.strip()
    if not prompt:
        return
    try:
        payload = {"prompt": prompt}
        with requests.post(CONFIG["tts_url"], json=payload, stream=True) as response:
            if response.status_code != 200:
                print(f"Warning: TTS received status code {response.status_code}")
                try:
                    error_msg = response.json().get('error', 'No error message provided.')
                    print(f"TTS error: {error_msg}")
                except:
                    print("No JSON error message provided for TTS.")
                return

            # Check stop condition before playing each chunk?
            # If we want to stop mid-audio, we'd need non-blocking
            # For simplicity, if tts_stop_flag is set mid-playback, we can end early
            # We do this by reading chunks and checking tts_stop_flag
            aplay = subprocess.Popen(['aplay', '-r', '22050', '-f', 'S16_LE', '-t', 'raw'],
                                     stdin=subprocess.PIPE)
            try:
                for chunk in response.iter_content(chunk_size=4096):
                    if tts_stop_flag:
                        break
                    if chunk:
                        aplay.stdin.write(chunk)
            except BrokenPipeError:
                print("Warning: aplay subprocess terminated unexpectedly.")
            finally:
                # Close aplay even if stopped early
                aplay.stdin.close()
                aplay.wait()
    except Exception as e:
        print(f"Unexpected error during TTS: {e}")

#############################################
# Step 8: Streaming the Output
#############################################

def chat_completion_stream(user_message):
    global stop_flag
    payload = build_payload(user_message)
    headers = {"Content-Type": "application/json"}

    try:
        with requests.post(OLLAMA_CHAT_URL, json=payload, headers=headers, stream=True) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if stop_flag:
                    print("Stream canceled due to new request.")
                    break
                if line:
                    obj = json.loads(line.decode('utf-8'))
                    msg = obj.get("message", {})
                    content = msg.get("content", "")
                    done = obj.get("done", False)
                    yield content, done
                    if done:
                        break
    except Exception as e:
        print(f"Error during streaming inference: {e}")
        yield "", True

def chat_completion_nonstream(user_message):
    payload = build_payload(user_message)
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(OLLAMA_CHAT_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message", {})
        return msg.get("content", "")
    except Exception as e:
        print(f"Error during non-stream inference: {e}")
        return ""

#############################################
# Step 9: Processing the model output
#############################################

def process_text(text):
    global stop_flag
    processed_text = convert_numbers_to_words(text)
    sentence_endings = re.compile(r'[.?!]+')

    if CONFIG["stream"]:
        buffer = ""
        sentences = []
        for content, done in chat_completion_stream(processed_text):
            if stop_flag:
                # If stopped mid-way, just return what we have
                break
            buffer += content
            while True:
                if stop_flag:
                    break
                match = sentence_endings.search(buffer)
                if not match:
                    break
                end_index = match.end()
                sentence = buffer[:end_index].strip()
                buffer = buffer[end_index:].strip()
                if sentence and not stop_flag:
                    # Enqueue sentence for TTS immediately
                    sentences.append(sentence)
                    enqueue_sentence_for_tts(sentence)
            if done or stop_flag:
                break

        if not stop_flag:
            # If not stopped, handle leftover
            leftover = buffer.strip()
            if leftover:
                sentences.append(leftover)
                enqueue_sentence_for_tts(leftover)
            return " ".join(sentences)
        else:
            # Stopped early
            return " ".join(sentences)
    else:
        # Non-stream mode
        result = chat_completion_nonstream(processed_text)
        sentences = []
        buffer = result
        sentence_endings = re.compile(r'[.?!]+')
        while True:
            match = sentence_endings.search(buffer)
            if not match:
                break
            end_index = match.end()
            sentence = buffer[:end_index].strip()
            buffer = buffer[end_index:].strip()
            if sentence:
                enqueue_sentence_for_tts(sentence)
                sentences.append(sentence)

        leftover = buffer.strip()
        if leftover:
            enqueue_sentence_for_tts(leftover)
            sentences.append(leftover)

        return " ".join(sentences)

#############################################
# Step 10: Update History File with New Messages
#############################################

def update_history(user_message, assistant_message):
    if not CONFIG["history"]:
        return
    current_history = safe_load_json_file(CONFIG["history"], [])
    current_history.append({"role": "user", "content": user_message})
    current_history.append({"role": "assistant", "content": assistant_message})
    try:
        with open(CONFIG["history"], 'w') as f:
            json.dump(current_history, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not write to history file {CONFIG['history']}: {e}")

#############################################
# Step 11: Handling Concurrent Requests and Cancellation
#############################################

stop_flag = False
current_thread = None

def inference_thread(user_message, result_holder):
    global stop_flag
    stop_flag = False
    result = process_text(user_message)
    result_holder.append(result)

def new_request(user_message):
    global stop_flag, current_thread

    # Cancel ongoing inference if any
    if current_thread and current_thread.is_alive():
        stop_flag = True
        current_thread.join()
        stop_flag = False

    # Cancel ongoing TTS
    stop_tts_thread()
    # Restart TTS thread (empty queue)
    start_tts_thread()

    # Start new inference thread
    result_holder = []
    current_thread = threading.Thread(target=inference_thread, args=(user_message, result_holder))
    current_thread.start()

    # Wait for inference to finish
    current_thread.join()

    result = result_holder[0] if result_holder else ""
    return result

#############################################
# Step 12: Start Server
#############################################

HOST = CONFIG["host"]
PORT = CONFIG["port"]

def handle_client_connection(client_socket, address):
    global stop_flag, current_thread
    print(f"Accepted connection from {address}")
    try:
        data = client_socket.recv(65536)
        if not data:
            print(f"No data from {address}, closing connection.")
            return
        user_message = data.decode('utf-8').strip()
        if not user_message:
            print(f"Empty prompt from {address}, ignoring.")
            return
        print(f"Received prompt from {address}: {user_message}")

        result = new_request(user_message)
        client_socket.sendall(result.encode('utf-8'))

        # Update history
        update_history(user_message, result)

    except Exception as e:
        print(f"Error handling client {address}: {e}")
    finally:
        client_socket.close()

def start_server():
    # Start TTS thread initially
    start_tts_thread()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((HOST, PORT))
    except Exception as e:
        print(f"Error binding to {HOST}:{PORT} - {e}. Using defaults: 0.0.0.0:64162")
        HOST_D = '0.0.0.0'
        PORT_D = 64162
        server.bind((HOST_D, PORT_D))

    server.listen(5)
    print(f"Listening for incoming connections on {HOST}:{PORT}...")
    try:
        while True:
            client_sock, addr = server.accept()
            handle_client_connection(client_sock, addr)
    except KeyboardInterrupt:
        print("\nShutting down server.")
    finally:
        # Stop TTS thread before exit
        stop_tts_thread()
        server.close()

if __name__ == "__main__":
    start_server()
