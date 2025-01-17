import threading
import traceback
import time
import socket
import json
import uuid
import os
import queue
import curses
import sys

CONFIG_FILE = 'orch.cf'
ROUTES_FILE = 'routes.cf'

# Default configuration
default_config = {
    'known_ports': '2000-8000',  # Scan ports from 2000 to 8000
    'scan_interval': 5,          # Time interval in seconds to scan for peripherals (changed to integer)
    'command_port': 6000,        # Port to listen for commands and data (changed to integer)
    'data_port_range': '6001-6099',  # Range of ports to receive data from peripherals
    'peripherals': [],           # List of known peripherals (stored as an empty list)
    'script_uuid': str(uuid.uuid4()),  # Generate a unique UUID string for the script
}


config = {}

# UUID for the orchestrator
orchestrator_uuid = str(uuid.uuid4())
orchestrator_name = 'Orchestrator'

# Global variable to store routes
routes = []

# Command queue for processing commands from port and console
command_queue = queue.Queue()

# Locks for thread-safe operations
peripherals_lock = threading.Lock()
routes_lock = threading.Lock()

# Activity logs
activity_log = []

# Event to control the display update
update_event = threading.Event()

# Color mapping for peripherals
peripheral_colors = {}

# Commands received from external sources
external_commands = []

# Flag to indicate if we are in command mode
in_command_mode = threading.Event()

# Lock for configuration file operations
config_lock = threading.Lock()

def read_config():
    global config, orchestrator_uuid
    config = default_config.copy()
    
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            try:
                # Load the config as JSON
                config = json.load(f)
                
                # Ensure peripherals is a list (in case it's missing or incorrectly formatted)
                if 'peripherals' not in config or not isinstance(config['peripherals'], list):
                    config['peripherals'] = []
                    log_message(f"Invalid or missing 'peripherals' in {CONFIG_FILE}. Resetting to empty list.")

            except json.JSONDecodeError:
                log_message(f"Invalid JSON format in {CONFIG_FILE}. Resetting to default config.")
                config = default_config.copy()
                write_config()  # Write default config if there was an error

    else:
        write_config()

    # Ensure UUID is handled correctly
    if 'script_uuid' in config and isinstance(config['script_uuid'], str):
        orchestrator_uuid = config['script_uuid']
    else:
        orchestrator_uuid = str(uuid.uuid4())
        config['script_uuid'] = orchestrator_uuid
        write_config()

    log_message(f"Config: {config}")  # Log the full config to inspect it
    return config


def write_config():
    with peripherals_lock:
        with config_lock:
            with open(CONFIG_FILE, 'w') as f:
                # Write the config dictionary as a formatted JSON object
                json.dump(config, f, indent=4)

def read_routes():
    global routes
    if os.path.exists(ROUTES_FILE):
        with open(ROUTES_FILE, 'r') as f:
            try:
                routes = json.load(f)
            except json.JSONDecodeError:
                routes = []
    else:
        routes = []
        write_routes()


def write_routes():
    with routes_lock:
        with open(ROUTES_FILE, 'w') as f:
            json.dump(routes, f, indent=4)

def parse_port_range(port_range_str):
    """
    Parse a port range string (e.g., '6000-6099') into a list of ports.
    """
    ports = []
    for part in port_range_str.split(','):
        if '-' in part:
            try:
                start, end = map(int, part.split('-'))
                ports.extend(range(start, end + 1))
            except ValueError:
                print(f"Invalid port range: {part}")
        elif part.isdigit():
            ports.append(int(part))
        else:
            print(f"Invalid port entry: {part}")
    return ports


def scan_ports():
    # For testing, we'll use synchronous scanning
    ports = []
    known_ports = config.get('known_ports', '2000-8000')
    for port_entry in known_ports.split(','):
        port_entry = port_entry.strip()
        if '-' in port_entry:
            try:
                start_port, end_port = map(int, port_entry.split('-'))
                ports.extend(range(start_port, end_port + 1))
            except ValueError:
                log_message(f"Invalid port range: {port_entry}")
        elif port_entry.isdigit():
            ports.append(int(port_entry))
        else:
            log_message(f"Invalid port entry: {port_entry}")
    for port in ports:
        check_port(port)

def check_port(port):
    host = 'localhost'
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(b'/info\n')
            response = ''
            while True:
                data = sock.recv(1024)
                if not data:
                    break
                response += data.decode()
                if 'EOF' in response:
                    break
            if response:
                process_response(response, port)
    except Exception as e:
        # Handling peripheral information retrieval
        peripheral = get_peripheral_by_port(port)
        if peripheral:  # Check if peripheral is a valid dictionary
            peripheral_info = f"{peripheral['name']} (Port: {port})" 
        else:
            peripheral_info = f"Unknown Peripheral on Port {port}"
        log_message(f"Error connecting to port {port} for {peripheral_info}: {e}")

def process_response(response, port):
    # Optional logging
    lines = response.strip().split('\n')
    
    # Check if the response has the expected format: at least 3 lines (name, uuid, config)
    if len(lines) < 3:
        log_message(f"Incomplete response from port {port}. Expected at least 3 lines.")
        return

    # Validate UUID format (basic check for valid UUID)
    uuid_pattern = re.compile(r'^[a-fA-F0-9-]{36}$')
    name = lines[0].strip()
    peripheral_uuid = lines[1].strip()

    if not uuid_pattern.match(peripheral_uuid):
        log_message(f"Invalid UUID format from response on port {port}. Ignoring response.")
        return

    config_lines = lines[2:]
    peripheral_config = '\n'.join(config_lines)

    # Lock to update peripherals list
    with peripherals_lock:
        peripheral = {
            'name': name,
            'uuid': peripheral_uuid,
            'config': peripheral_config,
            'port': port,
            'last_seen': time.time(),
        }

        # Check for existing peripheral by UUID
        existing = next((p for p in config['peripherals'] if p['uuid'] == peripheral_uuid), None)
        if existing:
            existing.update(peripheral)
            log_message(f"Updated existing peripheral: {existing['name']} on port {port}")
        else:
            # Ensure unique name
            same_name_count = sum(1 for p in config['peripherals'] if p['name'] == name or p['name'].startswith(f"{name}_"))
            if same_name_count > 0:
                peripheral['name'] = f"{name}_{same_name_count + 1}"
            config['peripherals'].append(peripheral)
            log_message(f"Discovered new peripheral: {peripheral['name']} on port {port}")

    write_config()
    assign_colors_to_peripherals()
    if not in_command_mode.is_set():
        update_event.set()


    write_config()  # Save updated configuration
    assign_colors_to_peripherals()
    if not in_command_mode.is_set():
        update_event.set()  # Signal to update the display




def assign_colors_to_peripherals():
    """Assign colors to peripherals based on their names."""
    with peripherals_lock:
        unique_names = list(set(p['name'].split('_')[0] for p in config['peripherals']))
        for idx, name in enumerate(unique_names):
            color_pair = (idx % 6) + 1  # Use color pairs 1-6
            peripheral_colors[name] = color_pair


def periodic_scan():
    scan_interval = int(config.get('scan_interval', '5'))
    while True:
        scan_ports()
        time.sleep(scan_interval)


def start_orchestrator():
    retry_delay = 5  # Delay in seconds before retrying

    while True:
        try:
            log_message("Reading config...")
            read_config()  # Check config structure here
            log_message("Reading routes...")
            read_routes()  # Check if routes file is processed correctly
            log_message("Assigning colors...")
            assign_colors_to_peripherals()
            
            log_message("Starting threads...")
            threading.Thread(target=periodic_scan, daemon=True).start()
            threading.Thread(target=command_listener, daemon=True).start()
            threading.Thread(target=data_listener, daemon=True).start()
            
            log_message("Running curses interface...")
            run_curses_interface()  # Only if everything else succeeds
        except Exception as e:
            log_message(f"Error in start_orchestrator: {e}")
            
            # Ensure curses is cleaned up if an error occurs
            try:
                curses.endwin()
            except Exception:
                pass

            log_message(f"Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)



def process_command(command, source, conn=None):
    if source == 'port':
        # Log external command
        with threading.Lock():
            external_commands.append(f"From {conn.getpeername()}: {command}")
            if len(external_commands) > 5:
                external_commands.pop(0)
        if not in_command_mode.is_set():
            update_event.set()
    if command.startswith('/data'):
        # Handle incoming data from peripherals
        tokens = command.strip().split(' ', 2)
        if len(tokens) >= 3:
            peripheral_uuid = tokens[1]
            data = tokens[2]
            handle_incoming_data(peripheral_uuid, data)
        else:
            send_response("Invalid data command format.", source, conn)
    elif command.startswith('/register'):
        # Handle registration of peripherals
        tokens = command.strip().split(' ', 3)
        if len(tokens) == 4:
            name = tokens[1]
            peripheral_uuid = tokens[2]
            try:
                port = int(tokens[3])
                register_peripheral(name, peripheral_uuid, port, conn)
            except ValueError:
                send_response("Port must be an integer.", source, conn)
        else:
            send_response("Invalid register command format.", source, conn)
    elif command == '/help':
        help_text = get_help_text()
        send_response(help_text, source, conn)
    elif command == '/list' or command == '/available':
        peripherals_list = list_peripherals()
        send_response(peripherals_list, source, conn)
    elif command.startswith('/routes'):
        process_routes_command(command, source, conn)
    elif command == '/reset':
        reset_system(conn)
    elif command == '/exit':
        send_response("Exiting command mode.", source, conn)
        if source == 'curses':
            in_command_mode.clear()  # Exit command mode
            return False  # Signal to exit command mode
        elif source == 'console':
            exit(0)
    else:
        send_response("Unknown command. Type '/help' for available commands.", source, conn)
    return True  # Continue in command mode


def reset_system(conn):
    """Resets the system by deleting configuration files, clearing peripherals, and restarting the orchestrator."""
    try:
        # Clear the in-memory peripherals list
        with peripherals_lock:
            config['peripherals'] = []
            log_message("Cleared in-memory peripherals list.")

        # Delete configuration files if they exist
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
            log_message(f"Deleted configuration file: {CONFIG_FILE}")
        if os.path.exists(ROUTES_FILE):
            os.remove(ROUTES_FILE)
            log_message(f"Deleted routes file: {ROUTES_FILE}")

        # Inform the user
        send_response("Configuration files deleted and peripherals list cleared. Restarting orchestrator...", 'port', conn)

        # Restart the script
        log_message("Restarting orchestrator...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        log_message(f"Error during reset: {e}")
        send_response(f"Reset failed: {e}", 'port', conn)


def register_peripheral(name, peripheral_uuid, port, conn):
    with peripherals_lock:
        peripheral = {
            'name': name,
            'uuid': peripheral_uuid,
            'config': '',
            'port': port,
            'last_seen': time.time(),
        }

        # Dynamically assign a data port from the available range
        available_data_ports = parse_port_range(config.get('data_port_range', '6000-6099'))
        assigned_data_port = available_data_ports[len(config['peripherals']) % len(available_data_ports)]  # Rotate through the data ports

        peripheral['data_port'] = assigned_data_port  # Assign the unique data port to the peripheral

        # Add or update the peripheral in the config
        existing = next((p for p in config['peripherals'] if p['uuid'] == peripheral_uuid), None)
        if existing:
            existing.update(peripheral)
        else:
            config['peripherals'].append(peripheral)
        
        log_message(f"Registered new peripheral: {peripheral['name']} on port {port}, assigned data port {assigned_data_port}")
    write_config()

    # Send acknowledgment with the assigned data port
    response = f"/ack {assigned_data_port}\n"
    if conn:
        try:
            conn.sendall(response.encode())
        except Exception as e:
            log_message(f"Failed to send ack to peripheral: {e}")


def send_response(message, source, conn):
    if source == 'console':
        print(message)
    elif source == 'port' and conn:
        try:
            conn.sendall((message + "\n").encode())
        except Exception:
            pass
    elif source == 'curses':
        global stdscr
        try:
            # Clear the line before printing the message
            stdscr.move(2, 0)
            stdscr.clrtoeol()
            stdscr.addstr(2, 0, message)
            stdscr.refresh()
        except curses.error:
            pass


def get_help_text():
    return (
        "Available commands:\n"
        "/help - Show this help message\n"
        "/list or /available - List known peripherals\n"
        "/routes - Manage routes\n"
        "    /routes help - Show routes command help\n"
        "/reset - Reset the orchestrator by deleting config files and restarting\n"
        "/exit - Exit command mode or exit the orchestrator\n"
    )


def list_peripherals():
    with peripherals_lock:
        if not config['peripherals']:
            return "No peripherals discovered."
        output = "Known peripherals:\n"
        for idx, peripheral in enumerate(config['peripherals']):
            last_seen = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(peripheral['last_seen']))
            output += f"{idx + 1}. {peripheral['name']} (UUID: {peripheral['uuid']}, Port: {peripheral['port']}, Last Seen: {last_seen})\n"
        return output.strip()


def process_routes_command(command, source, conn):
    tokens = command.strip().split()
    if len(tokens) < 2:
        send_response("Invalid routes command. Type '/routes help' for usage.", source, conn)
        return
    action = tokens[1]
    if action == 'help':
        routes_help = get_routes_help_text()
        send_response(routes_help, source, conn)
    elif action == 'add':
        if len(tokens) != 5:
            send_response("Usage: /routes add <route-name> <incoming-peripheral-name> <outgoing-peripheral-name>", source, conn)
            return
        route_name = tokens[2]
        incoming_name = tokens[3]
        outgoing_name = tokens[4]
        result = add_route(route_name, incoming_name, outgoing_name)
        send_response(result, source, conn)
    elif action == 'remove':
        if len(tokens) != 3:
            send_response("Usage: /routes remove <route-name>", source, conn)
            return
        route_name = tokens[2]
        result = remove_route(route_name)
        send_response(result, source, conn)
    elif action == 'info':
        routes_info = list_routes()
        send_response(routes_info, source, conn)
    else:
        send_response("Unknown routes command. Type '/routes help' for usage.", source, conn)


def get_routes_help_text():
    return (
        "Routes command usage:\n"
        "/routes add <route-name> <incoming-peripheral-name> <outgoing-peripheral-name> - Add a new route\n"
        "/routes remove <route-name> - Remove an existing route\n"
        "/routes info - List all routes\n"
        "/routes help - Show this help message\n"
    )


def add_route(route_name, incoming_name, outgoing_name):
    with peripherals_lock:
        incoming = next((p for p in config['peripherals'] if p['name'] == incoming_name), None)
        outgoing = next((p for p in config['peripherals'] if p['name'] == outgoing_name), None)
    if not incoming:
        return f"Incoming peripheral '{incoming_name}' not found."
    if not outgoing:
        return f"Outgoing peripheral '{outgoing_name}' not found."
    with routes_lock:
        # Check if route already exists
        if any(r for r in routes if r['name'] == route_name):
            return f"Route '{route_name}' already exists."
        route = {
            'name': route_name,
            'incoming': incoming['uuid'],
            'outgoing': outgoing['uuid'],
            'incoming_port': incoming['port'],
            'outgoing_port': outgoing['port'],
            'last_used': None,
        }
        routes.append(route)
    write_routes()
    return f"Route '{route_name}' added successfully."


def remove_route(route_name):
    with routes_lock:  # Lock for thread safety
        # Find the route by name
        route = next((r for r in routes if r['name'] == route_name), None)
        
        if not route:
            return f"Route '{route_name}' not found."

        # Remove the entire route object from the list
        routes.remove(route)

    # Write the updated routes list to the routes.cf file
    write_routes()
    return f"Route '{route_name}' removed successfully."


def list_routes():
    with routes_lock:
        if not routes:
            return "No routes configured."
        output = "Configured routes:\n"
        for route in routes:
            incoming_name = get_peripheral_name_by_uuid(route['incoming'])
            outgoing_name = get_peripheral_name_by_uuid(route['outgoing'])
            last_used = route['last_used']
            if last_used:
                last_used_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_used))
            else:
                last_used_str = 'Never'
            output += f"Route Name: {route['name']}\n"
            output += f"  From: {incoming_name} (UUID: {route['incoming']}, Port: {route['incoming_port']})\n"
            output += f"  To: {outgoing_name} (UUID: {route['outgoing']}, Port: {route['outgoing_port']})\n"
            output += f"  Last Used: {last_used_str}\n"
        return output.strip()


def get_peripheral_name_by_uuid(uuid_str):
    with peripherals_lock:
        peripheral = next((p for p in config['peripherals'] if p['uuid'] == uuid_str), None)
    if peripheral:
        return peripheral['name']
    else:
        return 'Unknown'

def get_peripheral_by_port(port):
    with peripherals_lock:
        peripheral = next((p for p in config['peripherals'] if p['port'] == port), None)
    return peripheral


def handle_incoming_data(peripheral_uuid, data):
    # Re-read the routes to ensure up-to-date data for each incoming request
    global routes
    with routes_lock:
        read_routes()  # Load the latest routes from routes.cf

    # Find routes where this peripheral is the incoming peripheral
    matching_routes = [route for route in routes if route['incoming'] == peripheral_uuid]
    if not matching_routes:
        log_message(f"No routes found for peripheral UUID {peripheral_uuid}")
        return

    for route in matching_routes:
        # Forward data to the outgoing peripheral
        outgoing_port = int(route['outgoing_port'])
        try:
            with socket.create_connection(('localhost', outgoing_port), timeout=5) as s_out:
                s_out.sendall((data + "\n").encode())
                route['last_used'] = time.time()
                write_routes()
                incoming_name = get_peripheral_name_by_uuid(route['incoming'])
                outgoing_name = get_peripheral_name_by_uuid(route['outgoing'])
                log_message(f"{incoming_name} sent data to {outgoing_name} via route '{route['name']}'")
        except Exception as e:
            log_message(f"Error forwarding data on route '{route['name']}': {e}")



# Function to synchronize ports based on UUIDs in orch.cf and routes.cf
def port_sync():
    while True:
        try:
            with config_lock, routes_lock:
                # Read orch.cf to get the current port configuration
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, 'r') as f:
                        orch_data = json.load(f)
                else:
                    orch_data = {}

                # Read routes.cf to get current routing information
                if os.path.exists(ROUTES_FILE):
                    with open(ROUTES_FILE, 'r') as f:
                        routes_data = json.load(f)
                else:
                    routes_data = []

                # Update ports in routes based on UUIDs in orch
                uuid_to_port = {p['uuid']: p.get('port') for p in orch_data.get('peripherals', [])}
                updated = False

                for route in routes_data:
                    incoming_uuid = route.get('incoming')
                    outgoing_uuid = route.get('outgoing')

                    # Synchronize ports if they are different
                    if incoming_uuid in uuid_to_port and route.get('incoming_port') != uuid_to_port[incoming_uuid]:
                        route['incoming_port'] = uuid_to_port[incoming_uuid]
                        updated = True
                    if outgoing_uuid in uuid_to_port and route.get('outgoing_port') != uuid_to_port[outgoing_uuid]:
                        route['outgoing_port'] = uuid_to_port[outgoing_uuid]
                        updated = True

                # Write back to routes.cf if any updates were made
                if updated:
                    with open(ROUTES_FILE, 'w') as f:
                        json.dump(routes_data, f, indent=4)
                    log_message("Ports synchronized between orch.cf and routes.cf.")

        except Exception as e:
            log_message(f"Error in port_sync: {e}\n{traceback.format_exc()}")

        # Run the sync every few seconds
        time.sleep(5)

# Start port_sync in a dedicated thread
threading.Thread(target=port_sync, daemon=True).start()


def command_listener():
    command_ports = [6000, 6001, 6002, 6003, 6004, 6005]  # Define your port range here
    host = '0.0.0.0'
    
    def listen_on_port(port):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Enable address reuse
                s.bind((host, port))
                s.listen(5)
                log_message(f"Command listener started on {host}:{port}")
                while True:
                    try:
                        conn, addr = s.accept()
                        log_message(f"Received connection from {addr} on port {port}")
                        threading.Thread(target=handle_client_connection, args=(conn, addr), daemon=True).start()
                    except socket.error as e:
                        peripheral = get_peripheral_by_port(port)
                        peripheral_info = f"{peripheral['name']} (Port: {port})" if peripheral else f"Unknown Peripheral on Port {port}"
                        log_message(f"Socket error on port {port} for {peripheral_info}: {e}")
        except Exception as e:
            peripheral = get_peripheral_by_port(port)
            peripheral_info = f"{peripheral['name']} (Port: {port})" if peripheral else f"Unknown Peripheral on Port {port}"
            log_message(f"Error in command_listener on port {port} for {peripheral_info}: {e}")

    # Start a listener thread for each port in the range
    for port in command_ports:
        threading.Thread(target=listen_on_port, args=(port,), daemon=True).start()


def data_listener():
    data_port_range = config.get('data_port_range', '6000-6099')
    data_ports = parse_port_range(data_port_range)  # Dynamically parse the port range
    host = '0.0.0.0'
    
    def listen_on_port(port):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                s.listen(5)
                log_message(f"Data listener started on {host}:{port}")
                while True:
                    try:
                        conn, addr = s.accept()
                        threading.Thread(target=handle_data_connection, args=(conn, addr), daemon=True).start()
                    except socket.error as e:
                        log_message(f"Socket error on data port {port}: {e}")
        except Exception as e:
            log_message(f"Error in data_listener on port {port}: {e}")

    # Start listeners for each data port
    for port in data_ports:
        threading.Thread(target=listen_on_port, args=(port,), daemon=True).start()

        
def handle_client_connection(conn, addr):
    with conn:
        conn.settimeout(5)  # Increase timeout
        buffer = ''
        try:
            local_port = conn.getsockname()[1]
            peripheral = get_peripheral_by_port(local_port)
            peripheral_info = f"{peripheral['name']} (Port: {local_port})" if peripheral else f"Unknown Peripheral on Port {local_port}"
        except Exception:
            peripheral_info = f"Unknown Peripheral on Port Unknown"

        while True:
            try:
                data = conn.recv(1024)
                if not data:
                    break
                buffer += data.decode()
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    command = line.strip()
                    if command == '':
                        continue
                    process_command(command, 'port', conn)
            except socket.timeout:
                continue
            except Exception as e:
                log_message(f"Error handling client {addr} for {peripheral_info}: {e}")
                break


def handle_data_connection(conn, addr):
    with conn:
        conn.settimeout(5)
        buffer = ''
        peripheral_uuid = None
        try:
            local_port = conn.getsockname()[1]
            peripheral = get_peripheral_by_port(local_port)
            peripheral_info = f"{peripheral['name']} (Port: {local_port})" if peripheral else f"Unknown Peripheral on Port {local_port}"
        except Exception:
            peripheral_info = f"Unknown Peripheral on Port Unknown"

        while True:
            try:
                data = conn.recv(1024)
                if not data:
                    break
                buffer += data.decode()
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    if peripheral_uuid is None:
                        # Expecting peripheral UUID as the first line
                        peripheral_uuid = line.strip()
                    else:
                        handle_incoming_data(peripheral_uuid, line.strip())
            except socket.timeout:
                continue
            except Exception as e:
                log_message(f"Error handling data connection from {addr} for {peripheral_info}: {e}")
                break



def run_curses_interface():
    global stdscr
    stdscr = curses.initscr()
    curses.start_color()
    # Initialize color pairs
    curses.init_pair(1, curses.COLOR_RED, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_BLUE, curses.COLOR_BLACK)
    curses.init_pair(5, curses.COLOR_MAGENTA, curses.COLOR_BLACK)
    curses.init_pair(6, curses.COLOR_CYAN, curses.COLOR_BLACK)
    curses.noecho()
    curses.cbreak()
    stdscr.nodelay(True)
    stdscr.keypad(True)
    try:
        main_overview()
    except Exception as e:
        log_message(f"Error in curses interface: {e}")
    finally:
        # Clean up curses
        stdscr.keypad(False)
        curses.echo()
        curses.nocbreak()
        curses.endwin()


def main_overview():
    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        # Display title
        title = "Orchestrator Overview"
        try:
            stdscr.attron(curses.A_BOLD | curses.A_UNDERLINE)
            stdscr.addstr(1, (width - len(title)) // 2, title)
            stdscr.attroff(curses.A_BOLD | curses.A_UNDERLINE)
        except curses.error:
            pass  # Handle cases where window size is too small

        # Display session UUID in top right
        session_uuid_str = f"Session UUID: {orchestrator_uuid}"
        if len(session_uuid_str) < width - 1:
            try:
                stdscr.addstr(1, width - len(session_uuid_str) - 2, session_uuid_str)
            except curses.error:
                pass
        else:
            truncated_uuid = (session_uuid_str[:width - 5] + '...') if len(session_uuid_str) > width else session_uuid_str
            try:
                stdscr.addstr(1, width - len(truncated_uuid) - 2, truncated_uuid)
            except curses.error:
                pass

        # Display peripherals
        try:
            stdscr.addstr(3, 2, "Peripherals:", curses.A_BOLD | curses.A_UNDERLINE)
        except curses.error:
            pass
        with peripherals_lock:
            for idx, peripheral in enumerate(config['peripherals']):
                color = peripheral_colors.get(peripheral['name'].split('_')[0], 0)
                if color != 0:
                    color_attr = curses.color_pair(color)
                else:
                    color_attr = curses.A_NORMAL
                last_seen = time.strftime('%H:%M:%S', time.localtime(peripheral['last_seen']))
                line = f"{peripheral['name']} (Port: {peripheral['port']}, Last Seen: {last_seen})"
                # Truncate line if it's too long
                if len(line) > width - 4:
                    truncated_line = line[:width - 7] + '...'
                else:
                    truncated_line = line
                try:
                    stdscr.addstr(4 + idx, 4, truncated_line, color_attr)
                except curses.error:
                    pass  # Handle cases where window size is too small

        # Display routes
        try:
            stdscr.addstr(6 + len(config['peripherals']), 2, "Routes:", curses.A_BOLD | curses.A_UNDERLINE)
        except curses.error:
            pass
        with routes_lock:
            for idx, route in enumerate(routes):
                incoming_name = get_peripheral_name_by_uuid(route['incoming'])
                outgoing_name = get_peripheral_name_by_uuid(route['outgoing'])
                last_used = route['last_used']
                if last_used:
                    last_used_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_used))
                else:
                    last_used_str = 'Never'
                # Use colors for peripherals
                in_color = peripheral_colors.get(incoming_name.split('_')[0], 0)
                out_color = peripheral_colors.get(outgoing_name.split('_')[0], 0)
                route_line = f"{route['name']}: "
                try:
                    stdscr.addstr(7 + len(config['peripherals']) + idx, 4, route_line)
                except curses.error:
                    pass
                # Truncate incoming name if necessary
                incoming_display = incoming_name
                if len(incoming_display) > width - 20:
                    incoming_display = (incoming_display[:width - 23] + '...') if len(incoming_display) > width - 20 else incoming_display
                try:
                    if in_color != 0:
                        stdscr.addstr(incoming_display, curses.color_pair(in_color))
                    else:
                        stdscr.addstr(incoming_display)
                except curses.error:
                    pass
                try:
                    stdscr.addstr(" -> ")
                except curses.error:
                    pass
                # Truncate outgoing name if necessary
                outgoing_display = outgoing_name
                if len(outgoing_display) > width - 20:
                    outgoing_display = (outgoing_display[:width - 23] + '...') if len(outgoing_display) > width - 20 else outgoing_display
                try:
                    if out_color != 0:
                        stdscr.addstr(outgoing_display, curses.color_pair(out_color))
                    else:
                        stdscr.addstr(outgoing_display)
                except curses.error:
                    pass
                try:
                    stdscr.addstr(f" | Last Used: {last_used_str}")
                except curses.error:
                    pass

        # Display activity log
        try:
            stdscr.addstr(9 + len(config['peripherals']) + len(routes), 2, "Recent Activity:", curses.A_BOLD | curses.A_UNDERLINE)
        except curses.error:
            pass
        log_start_line = 10 + len(config['peripherals']) + len(routes)
        max_log_lines = height - log_start_line - 4
        with threading.Lock():
            recent_logs = activity_log[-max_log_lines:]
        for idx, (timestamp, message) in enumerate(recent_logs):
            time_str = time.strftime('%H:%M:%S', time.localtime(timestamp))
            log_line = f"[{time_str}] {message}"
            if len(log_line) > width - 4:
                log_line = log_line[:width - 7] + '...'
            try:
                stdscr.addstr(log_start_line + idx, 4, log_line)
            except curses.error:
                pass  # Handle cases where window size is too small

        # Display instructions
        instruction = "Press 'm' to open the menu."
        try:
            stdscr.addstr(height - 2, 2, instruction, curses.A_DIM)
        except curses.error:
            pass

        stdscr.refresh()

        # Handle keypresses
        key = stdscr.getch()
        if key != -1:
            if key in [ord('m'), ord('M')]:
                main_menu()

        # Wait before next update
        time.sleep(0.1)


def main_menu():
    menu_items = [
        "List Peripherals",
        "Add Route",
        "Edit Route",
        "Remove Route",
        "Remove Peripheral",  # Add this new option
        "Reset Orchestrator",
        "Exit Menu"
    ]
    current_row = 0

    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        title = "Orchestrator Menu"
        try:
            stdscr.attron(curses.A_BOLD | curses.A_UNDERLINE)
            stdscr.addstr(1, (width - len(title)) // 2, title)
            stdscr.attroff(curses.A_BOLD | curses.A_UNDERLINE)
        except curses.error:
            pass

        for idx, row in enumerate(menu_items):
            x = (width - len(row)) // 2
            y = 3 + idx
            if idx == current_row:
                try:
                    stdscr.attron(curses.color_pair(1))
                    stdscr.addstr(y, x, row)
                    stdscr.attroff(curses.color_pair(1))
                except curses.error:
                    pass
            else:
                try:
                    stdscr.addstr(y, x, row)
                except curses.error:
                    pass
        stdscr.refresh()

        key = stdscr.getch()

        if key == curses.KEY_UP and current_row > 0:
            current_row -= 1
        elif key == curses.KEY_DOWN and current_row < len(menu_items) - 1:
            current_row += 1
        elif key == curses.KEY_ENTER or key in [10, 13]:
            selected_item = menu_items[current_row]
            if selected_item == "List Peripherals":
                list_peripherals_menu()
            elif selected_item == "Add Route":
                add_route_menu()
            elif selected_item == "Edit Route":
                edit_route_menu()
            elif selected_item == "Remove Route":
                remove_route_menu()
            elif selected_item == "Remove Peripheral":  # Handle new menu item
                remove_peripheral_menu()  # Call the new function
            elif selected_item == "Reset Orchestrator":
                reset_orchestrator_menu()
            elif selected_item == "Exit Menu":
                break
        elif key == 27:  # ESC key
            break
        time.sleep(0.1)



def list_peripherals_menu():
    with peripherals_lock:
        if not config['peripherals']:
            message = "No peripherals discovered."
        else:
            message = "Known Peripherals:\n"
            for idx, peripheral in enumerate(config['peripherals']):
                last_seen = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(peripheral['last_seen']))
                message += f"{idx + 1}. {peripheral['name']} (UUID: {peripheral['uuid']})\n"

    display_message("List Peripherals", message)

def remove_peripheral_menu():
    try:
        log_message("Entered remove_peripheral_menu.")
        
        # Lock the peripherals to safely access the shared resource
        with peripherals_lock:
            if not config['peripherals']:
                display_message("Remove Peripheral", "No peripherals to remove.")
                log_message("No peripherals to remove.")
                return

            # List the available peripherals with names and UUIDs
            peripheral_info = [f"{p['name']} (UUID: {p['uuid']})" for p in config['peripherals']]
            peripheral_uuids = {p['uuid']: p['name'] for p in config['peripherals']}
        
        log_message("Peripherals fetched successfully.")

        # Ask the user to select a peripheral UUID to remove
        selected_peripheral_info = select_item("Select Peripheral to Remove (press Enter to remove, ESC to cancel):", peripheral_info)
        
        # Handle ESC key or cancellation by user
        if not selected_peripheral_info:
            log_message("Remove Peripheral canceled by user during selection.")
            return  # Properly exit if no peripheral is selected (ESC was pressed)
        
        # Extract UUID from selected information
        selected_uuid = next((uuid for uuid, name in peripheral_uuids.items() if name in selected_peripheral_info), None)
        
        if selected_uuid is None:
            log_message(f"No matching UUID found for selected peripheral: {selected_peripheral_info}")
            display_message("Remove Peripheral", "Failed to locate the selected peripheral.")
            return
        
        log_message(f"User selected peripheral with UUID: {selected_uuid}")

        # Confirm removal
        confirmation = prompt_user(f"Are you sure you want to remove the peripheral with UUID '{selected_uuid}'? (y/n or press ESC to cancel): ")
        if confirmation is None or confirmation.lower() != 'y':
            log_message(f"Remove Peripheral for UUID '{selected_uuid}' canceled by user.")
            return

        # Proceed with peripheral removal by UUID
        with peripherals_lock:
            config['peripherals'] = [p for p in config['peripherals'] if p['uuid'] != selected_uuid]
        
        write_config()  # Update the config file after removal
        
        display_message("Remove Peripheral", f"Peripheral with UUID '{selected_uuid}' removed successfully.")
        log_message(f"Peripheral with UUID '{selected_uuid}' removed successfully.")
    
    except Exception as e:
        # Log the error with traceback
        log_message(f"Exception in remove_peripheral_menu: {e}\n{traceback.format_exc()}")


def add_route_menu():
    while True:
        route_name = prompt_user("Enter Route Name (or press ESC to cancel): ")
        if route_name is None:
            log_message("Add Route canceled by user.")
            return
        if not route_name:
            display_message("Add Route", "Route name cannot be empty.")
            continue
        break

    incoming_peripheral = select_peripheral("Select Peripheral to route FROM (or press ESC to cancel):")
    if incoming_peripheral is None:
        log_message("Add Route canceled by user during incoming selection.")
        return

    outgoing_peripheral = select_peripheral("Select Peripheral to route TO (or press ESC to cancel):")
    if outgoing_peripheral is None:
        log_message("Add Route canceled by user during outgoing selection.")
        return

    # Confirm addition
    confirmation = prompt_user(f"Add route '{route_name}' from '{incoming_peripheral['name']}' to '{outgoing_peripheral['name']}'? (y/n or press ESC to cancel): ")
    if confirmation is None or confirmation.lower() != 'y':
        log_message("Add Route canceled by user during confirmation.")
        return

    # Add route
    with routes_lock:
        if any(r for r in routes if r['name'] == route_name):
            display_message("Add Route", f"Route '{route_name}' already exists.")
            return
        route = {
            'name': route_name,
            'incoming': incoming_peripheral['uuid'],
            'outgoing': outgoing_peripheral['uuid'],
            'incoming_port': incoming_peripheral['port'],
            'outgoing_port': outgoing_peripheral['port'],
            'last_used': None,
        }
        routes.append(route)
    write_routes()
    log_message(f"Added route '{route_name}' from '{incoming_peripheral['name']}' to '{outgoing_peripheral['name']}'")
    display_message("Add Route", f"Route '{route_name}' added successfully.")


def edit_route_menu():
    with routes_lock:
        if not routes:
            display_message("Edit Route", "No routes to edit.")
            return
        route_names = [route['name'] for route in routes]

    selected_route = select_item("Select Route to Edit (or press ESC to cancel):", route_names)
    if not selected_route:
        return

    with routes_lock:
        route = next((r for r in routes if r['name'] == selected_route), None)
        if not route:
            display_message("Edit Route", "Selected route not found.")
            return

    # Edit Route Name
    new_route_name = prompt_user(f"Enter new name for route '{route['name']}' (leave blank to keep current or press ESC to cancel): ")
    if new_route_name is None:
        log_message("Edit Route canceled by user during name input.")
        return
    if new_route_name:
        with routes_lock:
            if any(r for r in routes if r['name'] == new_route_name and r != route):
                display_message("Edit Route", f"Route name '{new_route_name}' already exists.")
                return
            route['name'] = new_route_name
            log_message(f"Route name changed to '{new_route_name}'")

    # Edit Incoming Peripheral
    incoming_peripheral = select_peripheral(f"Select new Peripheral to route FROM (current: {get_peripheral_name_by_uuid(route['incoming'])}) or press ESC to keep current:")
    if incoming_peripheral is None:
        # User chose to keep current
        pass
    else:
        with routes_lock:
            route['incoming'] = incoming_peripheral['uuid']
            route['incoming_port'] = incoming_peripheral['port']
            log_message(f"Route '{route['name']}' updated incoming peripheral to '{incoming_peripheral['name']}'")

    # Edit Outgoing Peripheral
    outgoing_peripheral = select_peripheral(f"Select new Peripheral to route TO (current: {get_peripheral_name_by_uuid(route['outgoing'])}) or press ESC to keep current:")
    if outgoing_peripheral is None:
        # User chose to keep current
        pass
    else:
        with routes_lock:
            route['outgoing'] = outgoing_peripheral['uuid']
            route['outgoing_port'] = outgoing_peripheral['port']
            log_message(f"Route '{route['name']}' updated outgoing peripheral to '{outgoing_peripheral['name']}'")

    # Confirm and Save
    confirmation = prompt_user(f"Save changes to route '{route['name']}'? (y/n or press ESC to cancel): ")
    if confirmation is None or confirmation.lower() != 'y':
        log_message("Edit Route canceled by user during confirmation.")
        return

    write_routes()
    display_message("Edit Route", f"Route '{route['name']}' updated successfully.")


def remove_route_menu():
    try:
        log_message("Entered remove_route_menu.")
        
        # Lock the routes to safely access the shared resource
        with routes_lock:
            if not routes:
                display_message("Remove Route", "No routes to remove.")
                log_message("No routes to remove.")
                return

            # List the available routes
            route_names = [route['name'] for route in routes if route['name'].strip()]
        
        log_message("Routes fetched successfully.")

        # Ask the user to select a route to remove
        selected_route = select_item("Select Route to Remove (press Enter to remove, ESC to cancel):", route_names)
        
        # Handle ESC key or cancellation by user
        if not selected_route:
            log_message("Remove Route canceled by user during selection.")
            return  # Properly exit if no route is selected (ESC was pressed)
        
        log_message(f"User selected route: {selected_route}")

        # Confirm removal
        confirmation = prompt_user(f"Are you sure you want to remove the route '{selected_route}'? (y/n or press ESC to cancel): ")
        if confirmation is None or confirmation.lower() != 'y':
            log_message(f"Remove Route for '{selected_route}' canceled by user.")
            return

        # Proceed with route removal
        result = remove_route(selected_route)
        display_message("Remove Route", result)
        
        # Clear and refresh the screen after removal
        stdscr.clear()  # Clear the screen
        stdscr.refresh()  # Refresh the curses interface to show updated routes
        log_message(f"UI refreshed successfully after removal of route '{selected_route}'.")

    except Exception as e:
        # Log the error with traceback
        log_message(f"Exception in remove_route_menu: {e}\n{traceback.format_exc()}")



def reset_orchestrator_menu():
    confirmation = prompt_user("Are you sure you want to reset the orchestrator? This will delete all configurations and peripherals. (y/n or press ESC to cancel): ")
    if confirmation is None or confirmation.lower() != 'y':
        log_message("Reset Orchestrator canceled by user.")
        return

    try:
        # Clear the in-memory peripherals list
        with peripherals_lock:
            config['peripherals'] = []
            log_message("Cleared in-memory peripherals list.")

        # Delete configuration files if they exist
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
            log_message(f"Deleted configuration file: {CONFIG_FILE}")
        if os.path.exists(ROUTES_FILE):
            os.remove(ROUTES_FILE)
            log_message(f"Deleted routes file: {ROUTES_FILE}")

        # Inform the user
        display_message("Reset Orchestrator", "Configuration files deleted and peripherals list cleared. Restarting orchestrator...")

        # Restart the script
        log_message("Restarting orchestrator...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        log_message(f"Error during reset: {e}")
        display_message("Reset Orchestrator", f"Reset failed: {e}")


def prompt_user(prompt_text):
    """Prompts the user for input in a separate curses window. Returns None if ESC is pressed."""
    stdscr.clear()
    height, width = stdscr.getmaxyx()
    try:
        stdscr.addstr(1, 2, prompt_text)
    except curses.error:
        pass
    stdscr.refresh()
    curses.echo()
    curses.curs_set(1)
    input_str = ""
    while True:
        stdscr.timeout(5000)  # Timeout after 5 seconds
        key = stdscr.getch()
        if key == 27:  # ESC key
            curses.noecho()
            curses.curs_set(0)
            return None
        elif key in [10, 13]:  # Enter key
            break
        elif key in [curses.KEY_BACKSPACE, 127, 8]:
            if len(input_str) > 0:
                input_str = input_str[:-1]
                y, x = 3, len(input_str) + 2
                try:
                    stdscr.move(3, x)
                    stdscr.delch(3, x)
                except curses.error:
                    pass
        else:
            if 0 <= key <= 255:
                input_str += chr(key)
                try:
                    stdscr.addch(3, len(input_str) + 1, key)
                except curses.error:
                    pass
    curses.noecho()
    curses.curs_set(0)
    return input_str.strip()



def select_item(prompt_text, options):
    """Displays a list of options and allows the user to select one. Returns None if ESC is pressed."""
    current_row = 0
    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, prompt_text, curses.A_BOLD | curses.A_UNDERLINE)
        except curses.error:
            pass
        for idx, option in enumerate(options):
            x = 2
            y = 2 + idx
            if idx == current_row:
                try:
                    stdscr.attron(curses.color_pair(1))
                    stdscr.addstr(y, x, option)
                    stdscr.attroff(curses.color_pair(1))
                except curses.error:
                    pass
            else:
                try:
                    stdscr.addstr(y, x, option)
                except curses.error:
                    pass
        stdscr.refresh()

        key = stdscr.getch()

        if key == curses.KEY_UP and current_row > 0:
            current_row -= 1
        elif key == curses.KEY_DOWN and current_row < len(options) - 1:
            current_row += 1
        elif key == curses.KEY_ENTER or key in [10, 13]:
            return options[current_row]
        elif key == 27:  # ESC key
            return None
        time.sleep(0.1)


def select_peripheral(prompt_text, allow_skip=False):
    """Allows the user to select a peripheral from the list. Returns None if ESC is pressed or if skipping is allowed and chosen."""
    with peripherals_lock:
        peripherals = config['peripherals'].copy()
    if not peripherals:
        display_message("Select Peripheral", "No peripherals available.")
        return None

    options = [p['name'] for p in peripherals]
    if allow_skip:
        options.insert(0, "Keep Current")

    selected_name = select_item(prompt_text, options)
    if not selected_name:
        return None
    if allow_skip and selected_name == "Keep Current":
        return None
    selected_peripheral = next((p for p in peripherals if p['name'] == selected_name), None)
    return selected_peripheral


def display_message(title, message):
    """Displays a message to the user in a separate window."""
    stdscr.clear()
    height, width = stdscr.getmaxyx()
    lines = message.split('\n')
    try:
        stdscr.attron(curses.A_BOLD | curses.A_UNDERLINE)
        stdscr.addstr(1, max((width - len(title)) // 2, 0), title)
        stdscr.attroff(curses.A_BOLD | curses.A_UNDERLINE)
    except curses.error:
        pass
    for idx, line in enumerate(lines):
        if 3 + idx < height - 2:
            try:
                stdscr.addstr(3 + idx, 2, line)
            except curses.error:
                pass
    try:
        stdscr.addstr(height - 2, 2, "Press any key to return to the overview.")
    except curses.error:
        pass
    stdscr.refresh()
    stdscr.getch()


def log_message(message):
    """Logs a message to the activity log and updates the display if not in command mode."""
    timestamp = time.time()
    activity_log.append((timestamp, message))
    if len(activity_log) > 1000:
        activity_log.pop(0)  # Keep activity log from growing indefinitely
    if not in_command_mode.is_set():
        update_event.set()
    # If curses is not initialized, print to console
    if 'stdscr' not in globals():
        print(message)


if __name__ == '__main__':
    try:
        start_orchestrator()
    except KeyboardInterrupt:
        # Clean up curses
        try:
            curses.endwin()
        except Exception:
            pass
        print("Orchestrator terminated by user.")
    except Exception as e:
        # Handle any other exceptions
        print(f"An unexpected error occurred: {e}")
        try:
            curses.endwin()
        except Exception:
            pass
