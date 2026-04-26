"""
Database module - JSON-based storage for sessions, members, and settings.
"""
import json
import os
import time

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

MEMBERS_FILE = os.path.join(DATA_DIR, "members.json")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
FLOOD_FILE = os.path.join(DATA_DIR, "flood_status.json")
ADDED_FILE = os.path.join(DATA_DIR, "added_users.txt")


def load_json(path, default=None):
    if default is None:
        default = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Members ──────────────────────────────────────
def get_members():
    return load_json(MEMBERS_FILE, [])


def save_members(members):
    save_json(MEMBERS_FILE, members)


def clear_members():
    save_json(MEMBERS_FILE, [])


def get_member_count():
    return len(get_members())


# ── Accounts ─────────────────────────────────────
def get_accounts():
    """Return list of registered phone numbers from session files."""
    import glob
    files = glob.glob(os.path.join(SESSIONS_DIR, "*.session"))
    return [os.path.basename(f).replace(".session", "") for f in files]


def get_account_status(phone):
    """Check if an account is flood-restricted."""
    flood = load_json(FLOOD_FILE, {})
    key = phone.replace("+", "")
    exp = flood.get(key, 0)
    if exp > time.time():
        remaining = int(exp - time.time())
        h = remaining // 3600
        m = (remaining % 3600) // 60
        return f"⏳ محظور ({h}س {m}د)"
    return "✅ جاهز"


def set_flood(phone, seconds):
    flood = load_json(FLOOD_FILE, {})
    flood[phone.replace("+", "")] = time.time() + seconds
    save_json(FLOOD_FILE, flood)


def reset_flood(phone):
    flood = load_json(FLOOD_FILE, {})
    key = phone.replace("+", "")
    if key in flood:
        del flood[key]
        save_json(FLOOD_FILE, flood)


def is_flooded(phone):
    flood = load_json(FLOOD_FILE, {})
    exp = flood.get(phone.replace("+", ""), 0)
    return exp > time.time()


# ── Added users tracking ─────────────────────────
def get_added_users():
    if os.path.exists(ADDED_FILE):
        with open(ADDED_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def mark_added(user_id):
    with open(ADDED_FILE, "a") as f:
        f.write(f"{user_id}\n")


def clear_added():
    if os.path.exists(ADDED_FILE):
        os.remove(ADDED_FILE)


# ── Proxy Management ─────────────────────────────
PROXIES_FILE = os.path.join(DATA_DIR, "proxies.json")

def get_proxies():
    return load_json(PROXIES_FILE, {})

def save_proxy(phone, proxy_string):
    """Save a proxy for a specific phone number. Format: ip:port:user:pass or ip:port"""
    proxies = get_proxies()
    proxies[phone.replace("+", "")] = proxy_string
    save_json(PROXIES_FILE, proxies)

def get_proxy(phone):
    """Retrieve the proxy dictionary for Telethon if it exists."""
    proxies = get_proxies()
    proxy_str = proxies.get(phone.replace("+", ""))
    if not proxy_str:
        return None
        
    parts = proxy_str.split(":")
    if len(parts) == 2:
        return {
            'proxy_type': 'socks5',
            'addr': parts[0],
            'port': int(parts[1])
        }
    elif len(parts) == 4:
        return {
            'proxy_type': 'socks5',
            'addr': parts[0],
            'port': int(parts[1]),
            'username': parts[2],
            'password': parts[3]
        }
    return None

def clear_proxies():
    save_json(PROXIES_FILE, {})
