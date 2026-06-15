import os
import json
import subprocess
import re
import time
import shutil
import threading
import socket
import sqlite3
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    send_from_directory,
    jsonify,
)
import bcrypt
from waitress import serve

app = Flask(__name__)

# Enforce a 2GB maximum limit on upload sizes
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

START_TIME = time.time()

if os.name == "nt":
    DATA_DIR = os.path.abspath("./data")
    TUNNEL_PID_FILE = os.path.abspath("./data/config/tunnel.pid")
else:
    DATA_DIR = "/media/data"
    TUNNEL_PID_FILE = "/tmp/chimera-tunnel.pid"

CONFIG_DIR = os.path.join(DATA_DIR, "config")
SHARED_DIR = os.path.join(DATA_DIR, "shared")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# Ensure directories exist
try:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(SHARED_DIR, exist_ok=True)
except Exception as e:
    print(f"Warning: Directory creation deferred or failed: {e}")

# Global states
tunnel_process = None
tunnel_status = "offline"  # offline, connecting, online, error
tunnel_url = None
tunnel_lock = threading.Lock()

# PostgreSQL mock state
mock_postgres_running = False


def is_postgres_running():
    if os.name == "nt" or app.config.get("TESTING"):
        return mock_postgres_running
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    try:
        s.connect(("127.0.0.1", 5432))
        s.close()
        return True
    except Exception:
        return False


system_info = {
    "cpu": 0.0,
    "ram": {"total_gb": 0.0, "used_gb": 0.0, "percent": 0.0},
    "disk": {"total_gb": 0.0, "used_gb": 0.0, "percent": 0.0},
    "network": {
        "rx_bytes_sec": 0.0,
        "tx_bytes_sec": 0.0,
        "rx_formatted": "0.0 B/s",
        "tx_formatted": "0.0 B/s",
    },
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"Error parsing config JSON: {e}")
    # Return empty template
    return {
        "password_hash": None,
        "tunnel_url": None,
        "secret_key": None,
        "discord_webhook": None,
    }


def save_config(cfg):
    # Atomic write pattern to avoid file corruption
    temp_file = CONFIG_FILE + ".tmp"
    try:
        with open(temp_file, "w") as f:
            json.dump(cfg, f, indent=4)
        os.replace(temp_file, CONFIG_FILE)
    except Exception as e:
        print(f"Atomic config write failed: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)


# Persistent Session Cryptographic Key
def init_secret_key():
    cfg = load_config()
    if not cfg.get("secret_key"):
        # Generate a cryptographically secure key and save it
        cfg["secret_key"] = os.urandom(32).hex()
        save_config(cfg)
    app.secret_key = bytes.fromhex(cfg["secret_key"])


# Initialize secret session key at module import time
init_secret_key()


# Lightweight CSRF Middleware protection
@app.before_request
def csrf_protect():
    # Make sure session contains a CSRF token
    if "csrf_token" not in session:
        session["csrf_token"] = os.urandom(16).hex()

    # Check CSRF token on all POST requests
    if request.method == "POST":
        # Bypass static files if Nginx routes them (handled by Nginx directly)
        if request.path.startswith("/static/"):
            return

        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        session_token = session.get("csrf_token")

        if not session_token or not token or token != session_token:
            # Render a clean, safe error page
            return (
                render_template(
                    "base.html",
                    content_html="<div class='glass-panel' style='text-align:center;'><h4>Security Check Failed</h4><p>Cross-Site Request Forgery (CSRF) token is missing or invalid. Action blocked.</p><a href='/' class='action-btn secondary-btn'>Return to Dashboard</a></div>",
                ),
                400,
            )


# System statistics helper methods
def get_cpu_raw():
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        parts = line.split()
        if len(parts) >= 5:
            cpu_times = [float(x) for x in parts[1:5]]
            total = sum(cpu_times)
            idle = cpu_times[3]
            return {"total": total, "idle": idle}
    except Exception:
        pass
    return None


def get_ram_usage():
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        mem_info = {}
        for line in lines:
            parts = line.split(":")
            if len(parts) == 2:
                mem_info[parts[0].strip()] = int(parts[1].replace("kB", "").strip())
        total = mem_info.get("MemTotal", 0)
        free = mem_info.get("MemFree", 0)
        buffers = mem_info.get("Buffers", 0)
        cached = mem_info.get("Cached", 0)
        used = total - free - buffers - cached
        pct = (used / total * 100) if total > 0 else 0
        return {
            "total_gb": round(total / (1024 * 1024), 2),
            "used_gb": round(used / (1024 * 1024), 2),
            "percent": round(pct, 1),
        }
    except Exception:
        return {"total_gb": 0.0, "used_gb": 0.0, "percent": 0.0}


def get_disk_usage():
    target_path = (
        DATA_DIR if os.path.ismount(DATA_DIR) or os.path.exists(DATA_DIR) else "/"
    )
    try:
        stat = os.statvfs(target_path)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        pct = (used / total * 100) if total > 0 else 0
        return {
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "percent": round(pct, 1),
        }
    except Exception:
        return {"total_gb": 0.0, "used_gb": 0.0, "percent": 0.0}


def get_network_bytes():
    # Read actual interfaces bytes from Linux proc namespaces
    try:
        rx = 0
        tx = 0
        with open("/proc/net/dev") as f:
            lines = f.readlines()
        for line in lines[2:]:
            parts = line.split()
            if len(parts) >= 10:
                iface = parts[0].replace(":", "").strip()
                if iface == "lo":
                    continue
                rx += int(parts[1])
                tx += int(parts[9])
        return rx, tx
    except Exception:
        return 0, 0


def format_speed(bytes_sec):
    if bytes_sec < 1024:
        return f"{round(bytes_sec, 1)} B/s"
    elif bytes_sec < 1024 * 1024:
        return f"{round(bytes_sec / 1024, 1)} KB/s"
    else:
        return f"{round(bytes_sec / (1024 * 1024), 1)} MB/s"


def get_system_uptime():
    try:
        if os.name != "nt":
            with open("/proc/uptime") as f:
                uptime_seconds = float(f.readline().split()[0])
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            if hours > 0:
                return f"{hours}h {minutes}m"
            return f"{minutes}m"
    except Exception:
        pass

    # Fallback to server process runtime duration
    try:
        diff = time.time() - START_TIME
        hours = int(diff // 3600)
        minutes = int((diff % 3600) // 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return "—"


def get_local_ip():
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def system_monitor_worker():
    global system_info
    last_cpu = get_cpu_raw()
    last_rx, last_tx = get_network_bytes()

    while True:
        time.sleep(2)
        # Calculate CPU usage
        curr_cpu = get_cpu_raw()
        cpu_pct = 0.0
        if last_cpu and curr_cpu:
            diff_total = curr_cpu["total"] - last_cpu["total"]
            diff_idle = curr_cpu["idle"] - last_cpu["idle"]
            if diff_total > 0:
                cpu_pct = round((1.0 - diff_idle / diff_total) * 100.0, 1)
        last_cpu = curr_cpu

        # Calculate real Tx/Rx network throughput
        curr_rx, curr_tx = get_network_bytes()
        rx_speed = (curr_rx - last_rx) / 2.0 if last_rx > 0 else 0.0
        tx_speed = (curr_tx - last_tx) / 2.0 if last_tx > 0 else 0.0
        last_rx = curr_rx
        last_tx = curr_tx

        # Get RAM and Disk stats
        ram = get_ram_usage()
        disk = get_disk_usage()

        system_info = {
            "cpu": cpu_pct,
            "ram": ram,
            "disk": disk,
            "network": {
                "rx_bytes_sec": rx_speed,
                "tx_bytes_sec": tx_speed,
                "rx_formatted": format_speed(rx_speed),
                "tx_formatted": format_speed(tx_speed),
            },
        }


# Cloudflare Tunnel execution worker
def tunnel_worker():
    global tunnel_process, tunnel_status, tunnel_url

    with tunnel_lock:
        tunnel_status = "connecting"
        tunnel_url = None

    try:
        # Launch cloudflared tunnel pointing to local nginx reverse proxy
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", "http://127.0.0.1:80"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            preexec_fn=None if os.name == "nt" else os.setsid,  # creates process group
        )

        with tunnel_lock:
            tunnel_process = proc

        with open(TUNNEL_PID_FILE, "w") as f:
            f.write(str(proc.pid))

    except Exception as e:
        with tunnel_lock:
            tunnel_status = "error"
        print(f"Error launching cloudflared: {e}")
        return

    # Scan the stdout/stderr stream for the trycloudflare URL
    url_found = False
    for line in iter(proc.stdout.readline, ""):
        if "trycloudflare.com" in line:
            match = re.search(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com", line)
            if match:
                resolved_url = match.group(0)
                with tunnel_lock:
                    tunnel_url = resolved_url
                    tunnel_status = "online"
                url_found = True

                # Write to config
                cfg = load_config()
                cfg["tunnel_url"] = resolved_url
                save_config(cfg)

                # Broadcast URL to Discord Webhook if configured
                webhook_url = cfg.get("discord_webhook")
                if webhook_url:
                    try:
                        import requests

                        payload = {
                            "embeds": [
                                {
                                    "title": "ChimeraStick Server Online 🚀",
                                    "description": "Your bootable ChimeraStick server has established a secure Cloudflare tunnel connection.",
                                    "color": 5814783,  # Cyan decimal
                                    "fields": [
                                        {
                                            "name": "Global Access Address",
                                            "value": f"[{resolved_url}]({resolved_url})",
                                            "inline": False,
                                        },
                                        {
                                            "name": "Local IP Address",
                                            "value": get_local_ip(),
                                            "inline": True,
                                        },
                                        {
                                            "name": "System Uptime",
                                            "value": get_system_uptime(),
                                            "inline": True,
                                        },
                                    ],
                                    "footer": {"text": "ChimeraStick Secure Gateway"},
                                    "timestamp": time.strftime(
                                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                    ),
                                }
                            ]
                        }
                        requests.post(webhook_url, json=payload, timeout=10)
                    except Exception as e:
                        print(f"Failed to post to Discord Webhook: {e}")
                break

        # If the process terminates
        if proc.poll() is not None:
            break

    if not url_found:
        with tunnel_lock:
            if tunnel_status == "connecting":
                tunnel_status = "error"


def stop_tunnel_internal():
    global tunnel_process, tunnel_status, tunnel_url

    with tunnel_lock:
        tunnel_status = "offline"
        tunnel_url = None

        if tunnel_process:
            try:
                if os.name == "nt":
                    tunnel_process.terminate()
                else:
                    # Kill whole process group (cloudflared children) to avoid orphan tunnels
                    os.killpg(os.getpgid(tunnel_process.pid), 15)
                tunnel_process.wait(timeout=2)
            except Exception:
                try:
                    if os.name == "nt":
                        tunnel_process.kill()
                    else:
                        os.killpg(os.getpgid(tunnel_process.pid), 9)
                except Exception:
                    pass
            tunnel_process = None

    cfg = load_config()
    cfg["tunnel_url"] = None
    save_config(cfg)

    # Clean up PID file
    if os.path.exists(TUNNEL_PID_FILE):
        try:
            with open(TUNNEL_PID_FILE) as f:
                pid = int(f.read().strip())
            if os.name == "nt":
                os.kill(pid, 15)
            else:
                os.killpg(os.getpgid(pid), 15)
        except Exception:
            pass
        try:
            os.remove(TUNNEL_PID_FILE)
        except Exception:
            pass


# Routes
@app.route("/")
def index():
    config = load_config()
    if not config["password_hash"]:
        return redirect(url_for("setup"))
    if not session.get("authenticated"):
        return redirect(url_for("login"))

    postgres_active = is_postgres_running()
    website_active = os.path.exists(os.path.join(SHARED_DIR, "website", "index.html"))

    return render_template(
        "index.html",
        tunnel_url=tunnel_url,
        tunnel_status=tunnel_status,
        system_info=system_info,
        discord_webhook=config.get("discord_webhook") or "",
        postgres_status="active" if postgres_active else "inactive",
        website_status="active" if website_active else "inactive",
        website_url="/site/" if website_active else None,
    )


@app.route("/settings", methods=["POST"])
def settings():
    if not session.get("authenticated"):
        return jsonify({"error": "Unauthorized"}), 401

    webhook_url = request.form.get("discord_webhook", "").strip()
    if webhook_url and not (
        webhook_url.startswith("https://discord.com/api/webhooks/")
        or webhook_url.startswith("https://discordapp.com/api/webhooks/")
    ):
        return (
            render_template(
                "base.html",
                content_html="<div class='glass-panel' style='text-align:center;'><h4>Validation Error</h4><p>Invalid Discord Webhook URL structure.</p><a href='/' class='action-btn secondary-btn'>Return to Dashboard</a></div>",
            ),
            400,
        )

    cfg = load_config()
    cfg["discord_webhook"] = webhook_url if webhook_url else None
    save_config(cfg)
    return redirect(url_for("index"))


@app.route("/deploy/postgres", methods=["POST"])
def deploy_postgres():
    if not session.get("authenticated"):
        return jsonify({"error": "Unauthorized"}), 401

    global mock_postgres_running
    action = request.form.get("action")

    if os.name == "nt" or app.config.get("TESTING"):
        if action == "start":
            mock_postgres_running = True
        else:
            mock_postgres_running = False
        return redirect(url_for("index"))

    if action == "start":
        try:
            if shutil.which("pg_ctl") is None:
                subprocess.run(["apk", "add", "--no-cache", "postgresql"], check=True)
        except Exception as e:
            return (
                render_template(
                    "base.html",
                    content_html=f"<div class='glass-panel' style='text-align:center;'><h4>Deployment Failed</h4><p>Failed to install PostgreSQL package: {e}</p><a href='/' class='action-btn secondary-btn'>Return to Dashboard</a></div>",
                ),
                500,
            )

        data_dir = "/media/data/postgres/data"
        log_file = "/media/data/postgres/postgres.log"
        os.makedirs(os.path.dirname(data_dir), exist_ok=True)

        try:
            subprocess.run(
                ["chown", "-R", "postgres:postgres", "/media/data/postgres"],
                check=True,
            )
        except Exception:
            pass

        if not os.path.exists(os.path.join(data_dir, "PG_VERSION")):
            try:
                subprocess.run(
                    ["su", "-", "postgres", "-c", f"pg_ctl initdb -D {data_dir}"],
                    check=True,
                )
            except Exception as e:
                return (
                    render_template(
                        "base.html",
                        content_html=f"<div class='glass-panel' style='text-align:center;'><h4>Deployment Failed</h4><p>Failed to initialize PostgreSQL database cluster: {e}</p><a href='/' class='action-btn secondary-btn'>Return to Dashboard</a></div>",
                    ),
                    500,
                )

        try:
            subprocess.run(
                [
                    "su",
                    "-",
                    "postgres",
                    "-c",
                    f"pg_ctl start -D {data_dir} -l {log_file} -o '-p 5432'",
                ],
                check=True,
            )
        except Exception as e:
            return (
                render_template(
                    "base.html",
                    content_html=f"<div class='glass-panel' style='text-align:center;'><h4>Deployment Failed</h4><p>Failed to start PostgreSQL daemon: {e}</p><a href='/' class='action-btn secondary-btn'>Return to Dashboard</a></div>",
                ),
                500,
            )
    else:
        data_dir = "/media/data/postgres/data"
        try:
            subprocess.run(
                ["su", "-", "postgres", "-c", f"pg_ctl stop -D {data_dir}"], check=True
            )
        except Exception as e:
            print(f"Error stopping postgres: {e}")

    return redirect(url_for("index"))


@app.route("/deploy/sqlite", methods=["POST"])
def deploy_sqlite():
    if not session.get("authenticated"):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        db_filename = f"database_{int(time.time())}.sqlite"
        db_path = os.path.join(SHARED_DIR, db_filename)
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)"
        )
        cursor.execute(
            "INSERT INTO users (name, email) VALUES ('Administrator', 'admin@chimerastick.local')"
        )
        conn.commit()
        conn.close()
    except Exception as e:
        return (
            render_template(
                "base.html",
                content_html=f"<div class='glass-panel' style='text-align:center;'><h4>Deployment Failed</h4><p>Failed to create SQLite database: {e}</p><a href='/' class='action-btn secondary-btn'>Return to Dashboard</a></div>",
            ),
            500,
        )

    return redirect(url_for("files"))


@app.route("/deploy/website", methods=["POST"])
def deploy_website():
    if not session.get("authenticated"):
        return jsonify({"error": "Unauthorized"}), 401

    website_dir = os.path.join(SHARED_DIR, "website")
    os.makedirs(website_dir, exist_ok=True)
    index_file = os.path.join(website_dir, "index.html")

    if not os.path.exists(index_file):
        try:
            template_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>My ChimeraStick Site</title>
    <style>
        body {
            background-color: #060608;
            color: #f4f4f6;
            font-family: system-ui, -apple-system, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
            overflow: hidden;
        }
        .card {
            background: rgba(18, 18, 24, 0.7);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 24px;
            padding: 40px 60px;
            text-align: center;
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
        }
        h1 {
            font-size: 2.5rem;
            font-weight: 700;
            margin-bottom: 15px;
            background: linear-gradient(135deg, #00f2fe, #bf5af2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        p {
            color: #8e8e93;
            font-size: 1.1rem;
            margin-bottom: 25px;
            line-height: 1.6;
        }
        .btn {
            display: inline-block;
            text-decoration: none;
            background: #00f2fe;
            color: #060608;
            font-weight: 600;
            padding: 12px 28px;
            border-radius: 12px;
            box-shadow: 0 4px 15px rgba(0, 242, 254, 0.3);
            transition: all 0.2s;
        }
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(0, 242, 254, 0.4);
        }
    </style>
</head>
<body>
    <div class="card">
        <h1>ChimeraStick Website 🚀</h1>
        <p>Your one-click static website is now live globally on the internet!</p>
        <p style="font-size: 0.9rem; color: #48484a;">Customize this by modifying <code>shared/website/index.html</code> via the file browser.</p>
        <a href="/" class="btn">Return to Control Center</a>
    </div>
</body>
</html>"""
            with open(index_file, "w", encoding="utf-8") as f:
                f.write(template_content)
        except Exception as e:
            return (
                render_template(
                    "base.html",
                    content_html=f"<div class='glass-panel' style='text-align:center;'><h4>Deployment Failed</h4><p>Failed to create index.html: {e}</p><a href='/' class='action-btn secondary-btn'>Return to Dashboard</a></div>",
                ),
                500,
            )

    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    config = load_config()
    if not config["password_hash"]:
        return redirect(url_for("setup"))

    if request.method == "POST":
        password = request.form.get("password", "").encode("utf-8")
        stored_hash = config["password_hash"].encode("utf-8")

        try:
            # Exception-handled bcrypt matching
            if bcrypt.checkpw(password, stored_hash):
                session["authenticated"] = True
                return redirect(url_for("index"))
            else:
                return render_template(
                    "login.html", error="Invalid administrator password"
                )
        except Exception:
            return render_template(
                "login.html", error="Corrupt key configuration. Reinitialize."
            )

    return render_template("login.html")


@app.route("/setup", methods=["GET", "POST"])
def setup():
    config = load_config()
    if config["password_hash"]:
        return redirect(url_for("login"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if len(password) < 8:
            return render_template(
                "setup.html", error="Password must be at least 8 characters long"
            )
        if password != confirm_password:
            return render_template("setup.html", error="Passwords do not match")

        hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode(
            "utf-8"
        )
        config["password_hash"] = hashed
        save_config(config)

        # Hardening: Sync the OS credentials for SSH / local sudo security
        if os.name != "nt":
            try:
                subprocess.run(
                    ["chpasswd"], input=f"chimera:{password}", text=True, check=True
                )
            except Exception as e:
                print(f"OS credentials sync failed: {e}")

        session["authenticated"] = True
        return redirect(url_for("index"))

    return render_template("setup.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/files")
def files():
    if not session.get("authenticated"):
        return redirect(url_for("login"))

    items = []
    try:
        for entry in os.listdir(SHARED_DIR):
            path = os.path.join(SHARED_DIR, entry)
            is_directory = os.path.isdir(path)
            size = os.path.getsize(path) if not is_directory else 0

            size_str = "—"
            if not is_directory:
                if size < 1024:
                    size_str = f"{size} B"
                elif size < 1024 * 1024:
                    size_str = f"{round(size / 1024, 1)} KB"
                else:
                    size_str = f"{round(size / (1024 * 1024), 1)} MB"

            items.append(
                {
                    "name": entry,
                    "is_dir": is_directory,
                    "size_raw": size,
                    "size": size_str,
                }
            )
    except Exception as e:
        print(f"Error listing files: {e}")

    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return render_template("files.html", items=items)


@app.route("/upload", methods=["POST"])
def upload():
    if not session.get("authenticated"):
        return jsonify({"error": "Unauthorized"}), 401

    if "file" not in request.files:
        return redirect(url_for("files"))

    file = request.files["file"]
    if file.filename == "":
        return redirect(url_for("files"))

    # Hardening: Strict upload sanitization and traversal prevention
    filename = re.sub(r"[^a-zA-Z0-9._-]", "_", file.filename)

    # Block hidden configuration file uploads (.env, .ssh, .bashrc)
    if filename.startswith("."):
        filename = "uploaded_" + filename.lstrip(".")

    # Verify free disk space before writing
    if os.name != "nt":
        try:
            stat = os.statvfs(SHARED_DIR)
            free_space = stat.f_bavail * stat.f_frsize
            if free_space < 50 * 1024 * 1024:  # 50MB safeguard
                return "Disk space critically low. Upload rejected.", 400
        except Exception:
            pass

    target_path = os.path.join(SHARED_DIR, filename)

    # If file exists, append timestamp suffix to prevent accidental data overwrites
    if os.path.exists(target_path):
        base, ext = os.path.splitext(filename)
        filename = f"{base}_{int(time.time())}{ext}"
        target_path = os.path.join(SHARED_DIR, filename)

    try:
        file.save(target_path)
    except Exception as e:
        print(f"Upload write error: {e}")

    return redirect(url_for("files"))


@app.route("/download/<path:filename>")
def download(filename):
    if not session.get("authenticated"):
        return redirect(url_for("login"))

    # Enforce strict path traversal prevention
    clean_filename = os.path.basename(filename)
    return send_from_directory(SHARED_DIR, clean_filename, as_attachment=True)


@app.route("/delete/<path:filename>", methods=["POST"])
def delete_file(filename):
    if not session.get("authenticated"):
        return jsonify({"error": "Unauthorized"}), 401

    clean_filename = os.path.basename(filename)
    target_path = os.path.join(SHARED_DIR, clean_filename)

    try:
        if os.path.exists(target_path):
            if os.path.isdir(target_path):
                shutil.rmtree(target_path)
            else:
                os.remove(target_path)
    except Exception as e:
        print(f"Error deleting file {clean_filename}: {e}")

    return redirect(url_for("files"))


@app.route("/enable-tunnel", methods=["POST"])
def enable_tunnel():
    if not session.get("authenticated"):
        return redirect(url_for("login"))

    if tunnel_status not in ["connecting", "online"]:
        t = threading.Thread(target=tunnel_worker, daemon=True)
        t.start()

    return redirect(url_for("index"))


@app.route("/disable-tunnel", methods=["POST"])
def disable_tunnel():
    if not session.get("authenticated"):
        return redirect(url_for("login"))

    stop_tunnel_internal()
    return redirect(url_for("index"))


@app.route("/api/status")
def api_status():
    if not session.get("authenticated"):
        return jsonify({"error": "Unauthorized"}), 401

    tunnel_srv_status = "inactive"
    if tunnel_status == "online":
        tunnel_srv_status = "active"
    elif tunnel_status == "connecting":
        tunnel_srv_status = "connecting"

    postgres_srv_status = "active" if is_postgres_running() else "inactive"
    website_deployed = os.path.exists(os.path.join(SHARED_DIR, "website", "index.html"))
    website_status = "active" if website_deployed else "inactive"

    return jsonify(
        {
            "tunnel_status": tunnel_status,
            "tunnel_url": tunnel_url,
            "system_info": system_info,
            "local_ip": get_local_ip(),
            "uptime": get_system_uptime(),
            "services": {
                "web": "active",
                "app": "active",
                "tunnel": tunnel_srv_status,
                "postgres": postgres_srv_status,
                "website": website_status,
            },
        }
    )


if __name__ == "__main__":
    # Clean old tunnel process
    if os.path.exists(TUNNEL_PID_FILE):
        try:
            with open(TUNNEL_PID_FILE) as f:
                pid = int(f.read().strip())
            if os.name == "nt":
                os.kill(pid, 15)
            else:
                os.killpg(os.getpgid(pid), 15)
        except Exception:
            pass

    # Start monitor daemon
    monitor_thread = threading.Thread(target=system_monitor_worker, daemon=True)
    monitor_thread.start()

    serve(app, host="127.0.0.1", port=8080)
