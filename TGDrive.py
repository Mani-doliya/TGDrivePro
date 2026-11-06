import asyncio
import json
import hashlib
import mimetypes
import subprocess
import webbrowser
import logging
import math
import os
import re
import shutil
import sqlite3
import sys
try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from telethon import TelegramClient, events, errors
from telethon.tl import functions, types
from telethon.tl.types import (
    DocumentAttributeFilename,
    MessageMediaDocument,
    MessageMediaPhoto,
    PeerChannel,
)

# Optional Drag & Drop Integration
DND_AVAILABLE = False
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    TkinterDnD = object

# ============================================================
# LOGGING CONFIGURATION
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TGDrivePro")

def safe_log(message: str):
    sanitized = re.sub(
        r'(\\+?\\d{10,15}|code=\\d+|password[=: ]+\\S+|(?:api_)?hash[=: ]+\\S+|bot_token[=: ]+\\S+)',
        '[REDACTED]', str(message), flags=re.IGNORECASE
    )
    logger.info(sanitized)

# ============================================================
# CONFIGURATION MANAGEMENT
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _user_data_dir():
    """Return a writable per-user data directory on Windows, macOS and Linux."""
    app_name = "TGDrivePro"
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    target = base / app_name
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


DATA_DIR = _user_data_dir()
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
SESSION_DIR = os.path.join(DATA_DIR, "sessions")
DATABASE_DIR = os.path.join(DATA_DIR, "database")
SESSION = os.path.join(SESSION_DIR, "tdrive_user")
DB_NAME = os.path.join(DATABASE_DIR, "tdrive_user.db")
DOWNLOADS = os.path.join(DATA_DIR, "downloads")
os.makedirs(SESSION_DIR, exist_ok=True)
os.makedirs(DATABASE_DIR, exist_ok=True)
os.makedirs(DOWNLOADS, exist_ok=True)


def migrate_legacy_data():
    """Preserve sessions and database files from older project/data layouts."""
    pairs = [
        (os.path.join(DATA_DIR, "tdrive_user.db"), DB_NAME),
        (os.path.join(BASE_DIR, "tdrive_user.db"), DB_NAME),
        (os.path.join(DATA_DIR, "tdrive_user.session"), SESSION + ".session"),
        (os.path.join(BASE_DIR, "tdrive_user.session"), SESSION + ".session"),
        (os.path.join(DATA_DIR, "tdrive_user.session-journal"), SESSION + ".session-journal"),
        (os.path.join(BASE_DIR, "tdrive_user.session-journal"), SESSION + ".session-journal"),
        (os.path.join(DATA_DIR, "tdrive_user.session-wal"), SESSION + ".session-wal"),
        (os.path.join(BASE_DIR, "tdrive_user.session-wal"), SESSION + ".session-wal"),
        (os.path.join(DATA_DIR, "tdrive_user.session-shm"), SESSION + ".session-shm"),
        (os.path.join(BASE_DIR, "tdrive_user.session-shm"), SESSION + ".session-shm"),
    ]
    for old_path, new_path in pairs:
        if os.path.isfile(old_path) and not os.path.exists(new_path):
            try:
                shutil.copy2(old_path, new_path)
            except OSError as exc:
                safe_log(f"Legacy data migration skipped: {exc}")
    old_downloads = os.path.join(BASE_DIR, "downloads")
    if os.path.isdir(old_downloads) and not os.path.exists(os.path.join(DOWNLOADS, ".migration_done")):
        try:
            shutil.copytree(old_downloads, DOWNLOADS, dirs_exist_ok=True)
            Path(os.path.join(DOWNLOADS, ".migration_done")).touch()
        except OSError as exc:
            safe_log(f"Legacy downloads migration skipped: {exc}")


migrate_legacy_data()


def load_config():
    """Load user configuration, migrating an older project-local config once."""
    candidates = [CONFIG_FILE, os.path.join(BASE_DIR, "config.json")]
    for candidate in candidates:
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if not isinstance(data, dict):
                    raise ValueError("Configuration root must be a JSON object.")
                if candidate != CONFIG_FILE:
                    save_config(data)
                return data
            except Exception as exc:
                safe_log(f"Config load error: {exc}")
                if candidate == CONFIG_FILE:
                    return {}
    return {}


def save_config(data):
    """Write config atomically so an interrupted save cannot corrupt it."""
    temp_file = CONFIG_FILE + ".tmp"
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(temp_file, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_file, CONFIG_FILE)
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
        return True
    except Exception as exc:
        safe_log(f"Config save error: {exc}")
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except OSError:
            pass
        return False


def ensure_config():
    """Show a branded first-run setup window instead of a chain of basic dialogs."""
    cfg = load_config()
    if cfg.get("app_password") == "1234":
        cfg["app_password"] = ""

    required_ok = (
        str(cfg.get("api_id", "")).strip().isdigit()
        and int(cfg.get("api_id", 0) or 0) > 0
        and bool(str(cfg.get("api_hash", "")).strip())
        and str(cfg.get("channel_id", "")).strip().lstrip("-").isdigit()
        and int(cfg.get("channel_id", 0) or 0) != 0
        and bool(str(cfg.get("app_password", "")).strip())
    )
    if required_ok:
        return cfg

    setup = ctk.CTk()
    setup.title("TGDrive Pro — First-time setup")
    setup.geometry("720x790")
    setup.minsize(560, 650)
    setup.configure(fg_color="#060D18")
    setup.resizable(True, True)
    result = {"saved": False}

    header = ctk.CTkFrame(setup, fg_color="#0D1A2B", corner_radius=0)
    header.pack(fill="x")
    ctk.CTkFrame(header, height=4, fg_color="#1984FF", corner_radius=0).pack(fill="x")
    ctk.CTkLabel(
        header, text="TGDrive Pro", text_color="#F5F9FF",
        font=ctk.CTkFont(size=27, weight="bold")
    ).pack(anchor="w", padx=30, pady=(22, 3))
    ctk.CTkLabel(
        header, text="Connect Telegram and create your private app lock",
        text_color="#91A8C1", font=ctk.CTkFont(size=12)
    ).pack(anchor="w", padx=30, pady=(0, 20))

    body = ctk.CTkScrollableFrame(
        setup, fg_color="#081321", corner_radius=14,
        scrollbar_button_color="#204064"
    )
    body.pack(fill="both", expand=True, padx=22, pady=18)

    fields = {}
    field_specs = [
        ("api_id", "Telegram API ID", str(cfg.get("api_id", "")), False,
         "Get it from my.telegram.org"),
        ("api_hash", "Telegram API HASH", str(cfg.get("api_hash", "")), True,
         "Your Telegram API hash"),
        ("channel_id", "Target Channel ID", str(cfg.get("channel_id", "")), False,
         "Example: -1001234567890"),
        ("bot_token", "Bot Token (optional)", str(cfg.get("bot_token", "")), True,
         "Optional bot token"),
        ("app_password", "Create App Lock Password", "", True,
         "At least 8 characters recommended"),
        ("confirm_password", "Confirm App Lock Password", "", True,
         "Re-enter the same password"),
    ]
    for key, label, value, secret, hint in field_specs:
        ctk.CTkLabel(
            body, text=label, text_color="#D7E5F7",
            font=ctk.CTkFont(size=12, weight="bold")
        ).pack(anchor="w", padx=18, pady=(12, 4))
        entry = ctk.CTkEntry(
            body, height=42, corner_radius=9, border_width=1,
            border_color="#2A4D76", fg_color="#0D1A2B",
            text_color="#F5F9FF", placeholder_text=hint,
            show="*" if secret else ""
        )
        entry.pack(fill="x", padx=18)
        if value:
            entry.insert(0, value)
        fields[key] = entry

    note = ctk.CTkLabel(
        body,
        text="Credentials and Telegram session are stored in your user profile, not inside the GitHub project folder. Never upload config.json or session files.",
        text_color="#91A8C1", wraplength=600, justify="left",
        font=ctk.CTkFont(size=11)
    )
    note.pack(anchor="w", padx=18, pady=(18, 8))

    status = ctk.CTkLabel(body, text="", text_color="#F04444", wraplength=600)
    status.pack(anchor="w", padx=18, pady=(4, 8))

    def submit():
        try:
            api_id = int(fields["api_id"].get().strip())
            api_hash = fields["api_hash"].get().strip()
            channel_id = int(fields["channel_id"].get().strip())
            bot_token = fields["bot_token"].get().strip()
            password = fields["app_password"].get()
            confirm = fields["confirm_password"].get()
            if api_id <= 0 or not api_hash or channel_id == 0:
                raise ValueError("API ID, API HASH aur Channel ID sahi se enter karo.")
            if len(password) < 4 or password == "1234":
                raise ValueError("App password minimum 4 characters ka hona chahiye; 1234 allowed nahi hai.")
            if password != confirm:
                raise ValueError("Dono app passwords match nahi kar rahe.")
            new_cfg = {
                "api_id": api_id,
                "api_hash": api_hash,
                "channel_id": channel_id,
                "bot_token": bot_token,
                "app_password": password,
            }
            if not save_config(new_cfg):
                raise OSError("Settings save nahi ho paayi. Folder permissions check karo.")
            cfg.update(new_cfg)
            result["saved"] = True
            setup.destroy()
        except (ValueError, OSError) as exc:
            status.configure(text=str(exc), text_color="#F04444")

    footer = ctk.CTkFrame(setup, fg_color="#0A1626", corner_radius=12)
    footer.pack(fill="x", padx=22, pady=(0, 20))
    ctk.CTkButton(
        footer, text="Exit setup", width=120, height=42,
        fg_color="#142A45", hover_color="#204064",
        command=setup.destroy
    ).pack(side="right", padx=(6, 12), pady=12)
    ctk.CTkButton(
        footer, text="Save & Continue", width=170, height=42,
        fg_color="#1984FF", hover_color="#0F6FE0",
        font=ctk.CTkFont(size=12, weight="bold"),
        command=submit
    ).pack(side="right", padx=6, pady=12)

    setup.bind("<Return>", lambda _event: submit())
    setup.protocol("WM_DELETE_WINDOW", setup.destroy)
    setup.update_idletasks()
    sw, sh = setup.winfo_screenwidth(), setup.winfo_screenheight()
    width, height = min(720, sw - 40), min(790, sh - 80)
    setup.geometry(f"{width}x{height}+{max(0, (sw-width)//2)}+{max(0, (sh-height)//2)}")
    setup.mainloop()
    if not result["saved"]:
        raise SystemExit("TGDrive setup was cancelled.")
    return cfg


cfg = ensure_config()
API_ID = int(cfg.get("api_id", 0) or 0)
API_HASH = str(cfg.get("api_hash", ""))
CHANNEL_ID = int(cfg.get("channel_id", 0) or 0)
BOT_TOKEN = str(cfg.get("bot_token", ""))
APP_PASSWORD = str(cfg.get("app_password", ""))

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# ============================================================
# CATEGORY HELPERS
# ============================================================
CATEGORIES = ["All Files", "Starred", "Recent", "Images", "Videos", "Audio", "Documents", "Others", "Msg"]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".raw", ".cr2", ".nef"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg"}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}


def get_category(filename: str) -> str:
    if not filename:
        return "Others"
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "Images"
    if ext in VIDEO_EXTENSIONS:
        return "Videos"
    if ext in AUDIO_EXTENSIONS:
        return "Audio"
    if ext in DOCUMENT_EXTENSIONS:
        return "Documents"
    # Archives are supported and indexed, but remain in Others because
    # the dedicated Archives navigation button was removed.
    if ext in ARCHIVE_EXTENSIONS:
        return "Others"
    return "Others"


def format_size(size: int) -> str:
    if not size or size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = int(math.floor(math.log(size, 1024)))
    p = math.pow(1024, i)
    s = round(size / p, 2)
    return f"{s} {units[i]}"

def format_speed(bytes_per_sec: float) -> str:
    return f"{format_size(int(bytes_per_sec))}/s"

# ============================================================
# APPLICATION CLASS
# ============================================================

# TGDrive Pro UI palette — inspired by the supplied dark navy/blue reference.
UI_COLORS = {
    "bg": "#060D18",
    "sidebar": "#081321",
    "surface": "#0D1A2B",
    "surface_2": "#142A45",
    "card": "#0F2035",
    "border": "#2A4D76",
    "primary": "#1984FF",
    "primary_hover": "#0F6FE0",
    "success": "#22C981",
    "danger": "#F04444",
    "accent": "#7654F2",
    "text": "#F5F9FF",
    "muted": "#91A8C1",
}


# =========================
# TGDrive Pro CSS-LIKE THEME
# =========================
THEME = {
    "bg": "#060D18",
    "sidebar": "#081321",
    "panel": "#0A1626",
    "card": "#0F2035",
    "card_hover": "#142B48",
    "input": "#0D1A2B",
    "border": "#2A4D76",
    "border_active": "#1984FF",
    "text": "#F5F9FF",
    "text_secondary": "#B9CBE0",
    "muted": "#91A8C1",
    "primary": "#1984FF",
    "primary_hover": "#0F6FE0",
    "success": "#22C981",
    "success_hover": "#18A667",
    "danger": "#F04444",
    "danger_hover": "#D43B37",
    "purple": "#7654F2",
    "purple_hover": "#6340D2",
    "warning": "#F5A623",
    "radius": 12,
    "radius_small": 9,
}

def apply_app_theme():
    """Central theme entry point; equivalent to a CSS variables/theme sheet."""
    try:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
    except Exception:
        pass


# Dialog / Login theme
DIALOG_THEME = {
    "bg": "#060D18",
    "surface": "#102238",
    "border": "#2A4D76",
    "primary": "#1984FF",
    "primary_hover": "#0F6FE0",
    "success": "#22C981",
    "danger": "#F04444",
    "warning": "#F5A623",
    "text": "#F5F9FF",
    "muted": "#91A8C1",
}


# Application-wide background — use this for every root-level surface.
APP_BG = "#060D18"

class App(ctk.CTk if not DND_AVAILABLE else ctk.CTk, TkinterDnD.DnDWrapper if DND_AVAILABLE else object):

    def __init__(self):
        super().__init__()

        self.configure(fg_color="#060D18")
        if DND_AVAILABLE:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
            except Exception as e:
                safe_log(f"Drag and Drop initialization skipped: {e}")

        self.title("TGDrive Pro — Telegram Cloud Management System")
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = min(1280, max(760, screen_w - 60))
        win_h = min(800, max(560, screen_h - 100))
        self.geometry(f"{win_w}x{win_h}")
        self.minsize(min(760, screen_w - 20), min(560, screen_h - 60))

        # Telegram & Auth State
        self.client = None
        self.channel = None
        self.phone = None
        self.phone_code_hash = None
        self.phone_change_code_hash = None
        self.phone_change_number = None
        self.otp_cooldown_until = 0.0
        self.otp_cooldown_job = None
        self.otp_request_inflight = False
        self.otp_verify_inflight = False
        self.connection_status = "Disconnected"

        # Operational State
        self.uploading = False
        self.downloading = False
        self.busy = False
        self.indexing = False
        self._ui_progress_at = 0.0
        self.destroyed = False
        self.current_folder = "/"
        self.current_category = "All Files"
        self.search_query = ""
        self.sort_column = "name"
        self.sort_reverse = False

        self.rows_data = []
        self.selected_ids = set()
        self.selected_msg_ids = self.selected_ids
        self.row_vars = {}
        self._grid_preview_images = {}
        self.view_mode = "grid"
        self._smooth_scroll_job = None
        self._smooth_scroll_target = 0.0
        self._smooth_scroll_current = 0.0
        self._scroll_velocity = 0.0
        self._duplicate_cache = {}
        self._duplicate_set = set()
        self._duplicate_scan_at = 0.0
        self._thumbnail_cache = {}
        self._preview_window = None
        self._current_preview_rows = []
        self._current_preview_index = 0
        self._connect_anim_job = None
        self._connect_started_at = 0.0
        self._connect_cancelled = False

        # Async Loop Isolation
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._loop_worker, daemon=True)
        self.loop_thread.start()

        self.init_db()

        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.show_password_screen()

    def _loop_worker(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_async(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ========================================================
    # DATABASE SYSTEM
    # ========================================================
    def init_db(self):
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    msg_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    size INTEGER DEFAULT 0,
                    category TEXT NOT NULL,
                    date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    folder_path TEXT DEFAULT '/',
                    is_favorite INTEGER DEFAULT 0,
                    last_accessed TIMESTAMP,
                    item_type TEXT DEFAULT 'file',
                    message_text TEXT DEFAULT ''
                )
            """)

            cursor.execute("PRAGMA table_info(files)")
            cols = [info[1] for info in cursor.fetchall()]
            
            if "folder_path" not in cols:
                cursor.execute("ALTER TABLE files ADD COLUMN folder_path TEXT DEFAULT '/'")
            if "is_favorite" not in cols:
                cursor.execute("ALTER TABLE files ADD COLUMN is_favorite INTEGER DEFAULT 0")
            if "date_added" not in cols:
                cursor.execute("ALTER TABLE files ADD COLUMN date_added TIMESTAMP")
            if "last_accessed" not in cols:
                cursor.execute("ALTER TABLE files ADD COLUMN last_accessed TIMESTAMP")
            if "item_type" not in cols:
                cursor.execute("ALTER TABLE files ADD COLUMN item_type TEXT DEFAULT 'file'")
            if "message_text" not in cols:
                cursor.execute("ALTER TABLE files ADD COLUMN message_text TEXT DEFAULT ''")

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_name ON files(name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_category ON files(category)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_folder ON files(folder_path)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_message_text ON files(message_text)")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    folder_path TEXT UNIQUE NOT NULL
                )
            """)
            cursor.execute("INSERT OR IGNORE INTO folders (folder_path) VALUES ('/')")

            conn.commit()

    def save_file_record(self, msg_id: int, name: str, size: int = 0, folder_path: str = "/"):
        if not name or not msg_id:
            return
        cat = get_category(name)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("""
                INSERT INTO files (
                    msg_id, name, size, category, date_added, folder_path,
                    item_type, message_text
                )
                VALUES (?, ?, ?, ?, ?, ?, 'file', '')
                ON CONFLICT(msg_id) DO UPDATE SET
                    name=excluded.name,
                    size=excluded.size,
                    category=excluded.category,
                    item_type='file',
                    message_text=""
            """, (int(msg_id), name, int(size or 0), cat, now, folder_path))
            conn.commit()

    def save_message_record(self, msg_id: int, text_content: str, date_added=None, folder_path: str = "/"):
        if not msg_id:
            return
        clean_text = (text_content or "").strip()
        if not clean_text:
            clean_text = "(empty message)"
        name = f"Message #{int(msg_id)}"
        date_value = date_added or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("""
                INSERT INTO files (
                    msg_id, name, size, category, date_added, folder_path,
                    item_type, message_text
                )
                VALUES (?, ?, 0, 'Msg', ?, ?, 'message', ?)
                ON CONFLICT(msg_id) DO UPDATE SET
                    name=excluded.name,
                    size=0,
                    category='Msg',
                    date_added=excluded.date_added,
                    item_type='message',
                    message_text=excluded.message_text
            """, (int(msg_id), name, date_value, folder_path, clean_text))
            conn.commit()

    def delete_db_records(self, ids):
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute(f"DELETE FROM files WHERE msg_id IN ({placeholders})", tuple(ids))
            conn.commit()

    def toggle_favorite_db(self, msg_id: int):
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("UPDATE files SET is_favorite = NOT is_favorite WHERE msg_id = ?", (msg_id,))
            conn.commit()


    def update_last_accessed(self, msg_id: int):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("UPDATE files SET last_accessed = ? WHERE msg_id = ?", (now, msg_id))
            conn.commit()

    def _parse_advanced_search(self, raw):
        """Parse optional search tokens without breaking normal filename search.

        Supported examples:
          type:image  ext:pdf  msg:resume  link:telegram.org
          size>10mb  size<500mb  after:2026-01-01  before:2026-12-31
        """
        tokens = re.findall(r'(?:"[^"]+"|\S+)', raw or "")
        normal = []
        filters = []
        for token in tokens:
            t = token.strip().strip('"')
            low = t.lower()
            if low.startswith("type:"):
                value = low[5:]
                mapped = {"image": "Images", "images": "Images", "video": "Videos",
                          "videos": "Videos", "audio": "Audio", "document": "Documents",
                          "documents": "Documents", "other": "Others", "others": "Others",
                          "msg": "Msg", "message": "Msg"}
                if value in mapped:
                    filters.append(("category", mapped[value]))
                    continue
            if low.startswith("ext:"):
                ext = low[4:]
                if ext and not ext.startswith("."):
                    ext = "." + ext
                filters.append(("ext", ext))
                continue
            if low.startswith("msg:"):
                filters.append(("msg", t[4:]))
                continue
            if low.startswith("link:"):
                filters.append(("link", t[5:]))
                continue
            m = re.match(r"size(>=|<=|>|<)(\d+(?:\.\d+)?)(kb|mb|gb|b)?$", low)
            if m:
                mult = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, None: 1}
                filters.append(("size", m.group(1), float(m.group(2)) * mult[m.group(3)]))
                continue
            if low.startswith("after:"):
                filters.append(("after", t[6:]))
                continue
            if low.startswith("before:"):
                filters.append(("before", t[7:]))
                continue
            normal.append(t)

        return " ".join(normal), filters

    def get_filtered_rows(self):
        query = """SELECT
            msg_id, name, size, category, date_added, folder_path,
            is_favorite, last_accessed, item_type, message_text
            FROM files WHERE 1=1"""
        params = []

        # All Files intentionally excludes Telegram text/link messages.
        if self.current_category == "All Files":
            query += " AND item_type = 'file'"
        elif self.current_category == "Msg":
            query += " AND item_type = 'message' AND COALESCE(message_text, '') <> ''"
        elif self.current_category == "Starred":
            query += " AND item_type = 'file' AND is_favorite = 1"
        elif self.current_category == "Recent":
            query += """ AND item_type = 'file'
                         AND (date_added >= datetime('now', '-7 days')
                              OR last_accessed >= datetime('now', '-7 days'))"""
        elif self.current_category in ("Images", "Videos", "Audio", "Documents", "Others"):
            query += " AND item_type = 'file' AND category = ?"
            params.append(self.current_category)

        normal_search, filters = self._parse_advanced_search(self.search_query)

        if normal_search:
            q = f"%{normal_search}%"
            query += """ AND (
                name LIKE ? COLLATE NOCASE OR
                category LIKE ? COLLATE NOCASE OR
                message_text LIKE ? COLLATE NOCASE
            )"""
            params.extend([q, q, q])

        for f in filters:
            kind = f[0]
            if kind == "category":
                query += " AND category = ?"
                params.append(f[1])
            elif kind == "ext":
                query += " AND LOWER(name) LIKE ?"
                params.append("%" + f[1])
            elif kind == "msg":
                query += " AND message_text LIKE ? COLLATE NOCASE"
                params.append("%" + f[1] + "%")
            elif kind == "link":
                query += " AND (message_text LIKE ? OR message_text LIKE ?)"
                value = "%" + f[1] + "%"
                params.extend([value, value])
            elif kind == "size":
                op, value = f[1], f[2]
                if op not in (">", "<", ">=", "<="):
                    op = ">"
                query += f" AND size {op} ?"
                params.append(value)
            elif kind == "after":
                query += " AND date_added >= ?"
                params.append(f[1])
            elif kind == "before":
                query += " AND date_added <= ?"
                params.append(f[1])

        allowed_sort = {"name": "name COLLATE NOCASE", "date": "date_added",
                        "type": "category COLLATE NOCASE", "size": "size"}
        sort_sql = allowed_sort.get(self.sort_column, "name COLLATE NOCASE")
        query += f" ORDER BY {sort_sql} {'DESC' if self.sort_reverse else 'ASC'}"

        for attempt in range(3):
            try:
                with sqlite3.connect(DB_NAME, timeout=15) as conn:
                    conn.execute("PRAGMA busy_timeout=15000")
                    return conn.execute(query, params).fetchall()
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or attempt == 2:
                    raise
                time.sleep(0.25 * (attempt + 1))
        return []

    def get_storage_stats(self):
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT category, SUM(size), COUNT(*) FROM files GROUP BY category")
            stats = {}
            total_bytes = 0
            total_files = 0
            for cat, size_sum, count in cursor.fetchall():
                sz = size_sum or 0
                cnt = count or 0
                stats[cat] = [sz, cnt]
                total_bytes += sz
                total_files += cnt
            return total_bytes, total_files, stats

    # ========================================================
    # TELEGRAM CHANNEL RESOLUTION
    # ========================================================
    async def resolve_channel(self):
        try:
            dialogs = await self.client.get_dialogs()
            target_id = abs(CHANNEL_ID)
            if str(CHANNEL_ID).startswith("-100"):
                target_id = int(str(CHANNEL_ID)[4:])

            for d in dialogs:
                ent = d.entity
                if getattr(ent, "id", None) == target_id:
                    return ent
                if hasattr(ent, "id") and int(f"-100{ent.id}") == CHANNEL_ID:
                    return ent
        except Exception as e:
            safe_log(f"Dialog resolution notice: {e}")

        try:
            return await self.client.get_entity(CHANNEL_ID)
        except Exception:
            pass

        try:
            return await self.client.get_entity(PeerChannel(int(str(CHANNEL_ID).replace("-100", ""))))
        except Exception as e:
            raise RuntimeError(
                f"Unable to access channel target (ID: {CHANNEL_ID}).\n"
                "Please verify account admin rights and channel status."
            ) from e

    def set_connection_status(self, status: str):
        self.connection_status = status
        if hasattr(self, "status_indicator"):
            color_map = {
                "Connected": "#2ecc71",
                "Disconnected": "#e74c3c",
                "Reconnecting": "#f39c12",
                "Logging in": "#3498db"
            }
            color = color_map.get(status, "#95a5a6")
            self.after(0, lambda: self.status_indicator.configure(text=f"● {status}", text_color=color))

    # ========================================================
    # AUTHENTICATION & APP LOCK UI
    # ========================================================
    def show_password_screen(self):
        """Premium, compact dark-navy vault login screen."""
        self.clear_root_frame()
        self.f = ctk.CTkFrame(self, fg_color="#060D18")
        self.f.pack(fill="both", expand=True)

        # Subtle top accent.
        top = ctk.CTkFrame(self.f, height=4, fg_color="#1984FF", corner_radius=0)
        top.pack(fill="x", side="top")

        card = ctk.CTkFrame(
            self.f,
            width=500, height=500,
            fg_color="#0D1A2B",
            corner_radius=22,
            border_width=1,
            border_color="#2A4D76"
        )
        card.place(relx=0.5, rely=0.50, anchor="center")
        card.pack_propagate(False)

        brand = ctk.CTkFrame(
            card, width=70, height=70,
            fg_color="#1984FF", corner_radius=19
        )
        brand.pack(pady=(34, 14))
        brand.pack_propagate(False)

        ctk.CTkLabel(
            brand, text="☁",
            text_color="#FFFFFF",
            font=ctk.CTkFont(size=34, weight="bold")
        ).pack(expand=True)

        ctk.CTkLabel(
            card, text="TGDrive Pro",
            text_color="#F5F9FF",
            font=ctk.CTkFont(size=27, weight="bold")
        ).pack()

        ctk.CTkLabel(
            card, text="Telegram Cloud Management",
            text_color="#91A8C1",
            font=ctk.CTkFont(size=13)
        ).pack(pady=(4, 14))

        badge = ctk.CTkFrame(
            card,
            fg_color="#0B2A22",
            corner_radius=14,
            border_width=1,
            border_color="#165A46"
        )
        badge.pack(pady=(0, 22))

        ctk.CTkLabel(
            badge, text="●",
            text_color="#22C981",
            font=ctk.CTkFont(size=11)
        ).pack(side="left", padx=(12, 5), pady=6)

        ctk.CTkLabel(
            badge, text="Encrypted Vault",
            text_color="#BDEFD9",
            font=ctk.CTkFont(size=12, weight="bold")
        ).pack(side="left", padx=(0, 12), pady=6)

        ctk.CTkLabel(
            card, text="App Lock Password",
            text_color="#D7E5F7",
            font=ctk.CTkFont(size=12, weight="bold")
        ).pack(anchor="w", padx=55, pady=(0, 7))

        # Single clean input container — no nested entry border.
        wrap = ctk.CTkFrame(
            card,
            height=52,
            fg_color="#081321",
            corner_radius=11,
            border_width=1,
            border_color="#2B527F"
        )
        wrap.pack(fill="x", padx=55)
        wrap.pack_propagate(False)

        self.pe = ctk.CTkEntry(
            wrap,
            show="•",
            placeholder_text="Enter your password",
            fg_color="transparent",
            border_width=0,
            text_color="#E7F0FA",
            placeholder_text_color="#7188A2",
            font=ctk.CTkFont(size=13)
        )
        self.pe.pack(
            side="left", fill="both", expand=True,
            padx=(14, 4), pady=2
        )
        self.pe.focus()
        self._password_visible = False

        def toggle_password():
            self._password_visible = not self._password_visible
            self.pe.configure(
                show="" if self._password_visible else "•"
            )
            eye.configure(
                text="Hide" if self._password_visible else "Show",
                text_color="#B9CBE0"
            )

        eye = ctk.CTkButton(
            wrap,
            text="Show",
            width=62,
            height=38,
            corner_radius=8,
            fg_color="transparent",
            hover_color="#142A45",
            text_color="#91A8C1",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=toggle_password
        )
        eye.pack(side="right", padx=6)

        ctk.CTkButton(
            card,
            text="Unlock Vault   →",
            height=48,
            corner_radius=11,
            fg_color="#1984FF",
            hover_color="#0F6FE0",
            text_color="#FFFFFF",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.check_pass
        ).pack(fill="x", padx=55, pady=(18, 8))

        ctk.CTkLabel(
            card,
            text="Your saved Telegram session connects automatically",
            text_color="#7188A2",
            font=ctk.CTkFont(size=11)
        ).pack(pady=(8, 0))

        self.pe.bind("<Return>", lambda _: self.check_pass())

    def check_pass(self):
        if self.pe.get() != APP_PASSWORD:
            self.show_themed_info("Security Alert", "Incorrect Master Password.", "error")
            return

        self.f.destroy()

        # If a valid Telegram session already exists, do NOT ask for the phone
        # number or OTP again. Connect directly with the saved session.
        self.set_connection_status("Logging in")
        self.show_session_connecting()
        self.run_async(self._connect_saved_session())

    def show_session_connecting(self):
        """Fast, non-blocking connection screen with real connection stages."""
        self.clear_root_frame()
        self._connect_started_at = time.monotonic()
        self._connect_cancelled = False

        self.f = ctk.CTkFrame(self, fg_color=APP_BG)
        self.f.pack(fill="both", expand=True)

        # Subtle background accents.
        ctk.CTkFrame(self.f, height=3, fg_color="#1984FF", corner_radius=0).pack(fill="x", side="top")
        card = ctk.CTkFrame(
            self.f, width=540, height=455,
            fg_color="#0D1A2B", corner_radius=24,
            border_width=1, border_color="#2A4D76"
        )
        card.place(relx=0.5, rely=0.5, anchor="center")
        card.pack_propagate(False)

        mark = ctk.CTkFrame(card, width=76, height=76, fg_color="#1984FF", corner_radius=21)
        mark.pack(pady=(38, 16))
        mark.pack_propagate(False)
        ctk.CTkLabel(mark, text="☁", text_color="#FFFFFF",
                     font=ctk.CTkFont(size=37, weight="bold")).pack(expand=True)

        ctk.CTkLabel(card, text="Connecting to Telegram",
                     font=ctk.CTkFont(size=23, weight="bold"),
                     text_color="#F5F9FF").pack()
        ctk.CTkLabel(card, text="Restoring your saved secure session",
                     font=ctk.CTkFont(size=12), text_color="#91A8C1").pack(pady=(5, 22))

        self.connect_stage = ctk.CTkLabel(
            card, text="●  Preparing secure connection",
            anchor="w", text_color="#D9E8FA",
            font=ctk.CTkFont(size=13, weight="bold")
        )
        self.connect_stage.pack(fill="x", padx=58, pady=(0, 8))

        self.connect_progress = ctk.CTkProgressBar(
            card, height=7, corner_radius=5,
            progress_color="#1984FF", fg_color="#142A45"
        )
        self.connect_progress.pack(fill="x", padx=58)
        self.connect_progress.set(0.08)

        self.session_status = ctk.CTkLabel(
            card, text="Checking saved Telegram session…",
            font=ctk.CTkFont(size=12), text_color="#91A8C1"
        )
        self.session_status.pack(pady=(13, 15))

        self.connect_spinner = ctk.CTkLabel(
            card, text="·  ·  ·", text_color="#1984FF",
            font=ctk.CTkFont(size=17, weight="bold")
        )
        self.connect_spinner.pack(pady=(0, 12))

        self.connect_hint = ctk.CTkLabel(
            card, text="No phone number or OTP is needed when the saved session is valid.",
            text_color="#7188A2", font=ctk.CTkFont(size=10), wraplength=410
        )
        self.connect_hint.pack(pady=(0, 8))

        self._animate_connection()

    def _animate_connection(self):
        if not hasattr(self, "connect_spinner") or not self.connect_spinner.winfo_exists():
            return
        frames = ["·  ·  ·", "•  ·  ·", "•  •  ·", "•  •  •"]
        elapsed = int(time.monotonic() - self._connect_started_at)
        self.connect_spinner.configure(text=frames[elapsed % len(frames)])
        self._connect_anim_job = self.after(350, self._animate_connection)

    def _set_connect_stage(self, stage, detail, progress):
        def update():
            if hasattr(self, "connect_stage") and self.connect_stage.winfo_exists():
                self.connect_stage.configure(text=f"●  {stage}")
            if hasattr(self, "session_status") and self.session_status.winfo_exists():
                self.session_status.configure(text=detail)
            if hasattr(self, "connect_progress") and self.connect_progress.winfo_exists():
                self.connect_progress.set(max(0.0, min(1.0, progress)))
        self.after(0, update)

    def _stop_connection_animation(self):
        job = getattr(self, "_connect_anim_job", None)
        if job:
            try:
                self.after_cancel(job)
            except Exception:
                pass
            self._connect_anim_job = None

    async def _connect_saved_session(self):
        """Restore Telegram session quickly and report each slow stage."""
        try:
            self._set_connect_stage("Preparing secure connection", "Opening saved session…", 0.12)
            if self.client is None:
                self.client = TelegramClient(
                    SESSION, API_ID, API_HASH,
                    connection_retries=2, retry_delay=1, auto_reconnect=True
                )

            # A bounded connect prevents an indefinite 'Connecting…' screen.
            self._set_connect_stage("Connecting to Telegram", "Establishing secure connection…", 0.30)
            await asyncio.wait_for(self.client.connect(), timeout=15)

            self._set_connect_stage("Checking session", "Verifying saved authorization…", 0.52)
            authorized = await asyncio.wait_for(self.client.is_user_authorized(), timeout=10)
            if not authorized:
                self.set_connection_status("Disconnected")
                self._stop_connection_animation()
                self.after(0, self.show_phone_login)
                return

            self._set_connect_stage("Loading channel", "Opening your configured channel…", 0.72)
            self.channel = await asyncio.wait_for(self.resolve_channel(), timeout=15)

            self._set_connect_stage("Finalizing", "Starting live message updates…", 0.90)
            try:
                self.client.remove_event_handler(self._on_new_message)
            except Exception:
                pass
            self.client.add_event_handler(
                self._on_new_message,
                events.NewMessage(chats=self.channel)
            )

            self.set_connection_status("Connected")
            self._set_connect_stage("Connected", "Telegram session restored successfully.", 1.0)
            self._stop_connection_animation()
            self.after(250, self.show_main_ui)
            self.after(700, self.load_existing_index)

        except asyncio.TimeoutError:
            safe_log("Telegram connection timed out")
            self._stop_connection_animation()
            try:
                if self.client:
                    await self.client.disconnect()
            except Exception:
                pass
            self.client = None
            self.set_connection_status("Disconnected")
            self.after(0, self.show_phone_login)
            self.after(100, lambda: self.login_status.configure(
                text="Telegram connection timed out. Please try again."
            ) if hasattr(self, "login_status") else None)
        except Exception as e:
            safe_log(f"Saved session connection failed: {e}")
            self._stop_connection_animation()
            try:
                if self.client:
                    await self.client.disconnect()
            except Exception:
                pass
            self.client = None
            self.set_connection_status("Disconnected")
            self.after(0, self.show_phone_login)
            self.after(
                100,
                lambda: self.login_status.configure(
                    text="Saved session unavailable. Please sign in again."
                ) if hasattr(self, "login_status") else None
            )

    def show_phone_login(self):
        self.clear_root_frame()
        self.f = ctk.CTkFrame(self, fg_color=APP_BG)
        self.f.pack(fill="both", expand=True)

        ctk.CTkLabel(self.f, text="TGDrive Pro", font=ctk.CTkFont(size=32, weight="bold")).pack(pady=(80, 10))
        ctk.CTkLabel(self.f, text="Sign in with your Telegram Account", font=ctk.CTkFont(size=16)).pack(pady=(0, 25))

        ctk.CTkLabel(self.f, text="Phone Number (International format, e.g. +14155552671)").pack(pady=2)
        self.phone_entry = ctk.CTkEntry(self.f, width=320, height=40, placeholder_text="+14155552671")
        self.phone_entry.pack(pady=8)
        self.phone_entry.focus()
        self.phone_entry.bind("<Return>", lambda _: self.send_code())

        self.send_code_btn = ctk.CTkButton(self.f, text="Send Verification Code", width=320, height=42, command=self.send_code)
        self.send_code_btn.pack(pady=15)

        self.login_status = ctk.CTkLabel(self.f, text="", font=ctk.CTkFont(size=13))
        self.login_status.pack(pady=5)

    def send_code(self):
        phone = self.phone_entry.get().strip()
        if not phone or not phone.startswith("+"):
            self.show_themed_info("Invalid Input", "Please enter a valid phone number with country code (e.g., +1...).", "error")
            return

        self.phone = phone
        self.send_code_btn.configure(state="disabled")
        self.login_status.configure(text="Connecting to Telegram securely…")
        self.set_connection_status("Logging in")

        self.run_async(self._send_code(phone))

    async def _send_code(self, phone: str):
        try:
            if not self.client:
                self.client = TelegramClient(SESSION, API_ID, API_HASH, connection_retries=2, retry_delay=1, auto_reconnect=True)

            await self.client.connect()

            if await self.client.is_user_authorized():
                self.set_connection_status("Connected")
                self.channel = await self.resolve_channel()
                self.client.add_event_handler(self._on_new_message, events.NewMessage(chats=self.channel))

                self.after(0, self.show_main_ui)
                self.after(500, self.load_existing_index)
                return

            sent = await self.client.send_code_request(phone)
            self.phone_code_hash = getattr(sent, "phone_code_hash", None)
            self.otp_verify_inflight = False
            self.otp_request_inflight = False

            self.after(0, self.show_otp_screen)
            self.after(0, lambda: self.start_otp_cooldown(60))

        except errors.FloodWaitError as e:
            wait_time = getattr(e, "seconds", 60)
            self.after(0, lambda: messagebox.showerror("Rate Limit Exceeded", f"Telegram rate limit reached. Try again in {wait_time}s."))
            self.after(0, lambda: self.login_status.configure(text="Rate limited. Please wait."))
            self.after(0, lambda: self.send_code_btn.configure(state="normal"))
        except Exception as e:
            err = str(e)
            safe_log(f"Send code failure: {err}")
            self.after(0, lambda: messagebox.showerror("Authentication Error", err))
            self.after(0, lambda: self.login_status.configure(text="Failed to send code."))
            self.after(0, lambda: self.send_code_btn.configure(state="normal"))

    def show_otp_screen(self):
        self.clear_root_frame()
        self.f = ctk.CTkFrame(self, fg_color=APP_BG)
        self.f.pack(fill="both", expand=True)

        ctk.CTkLabel(self.f, text="Verification Code", font=ctk.CTkFont(size=28, weight="bold")).pack(pady=(70, 10))
        ctk.CTkLabel(self.f, text=f"Code sent to {self.phone}", font=ctk.CTkFont(size=14), text_color="#91A8C1").pack(pady=(0, 20))

        self.otp_entry = ctk.CTkEntry(self.f, width=220, height=40, placeholder_text="OTP Code", font=ctk.CTkFont(size=18, weight="bold"))
        self.otp_entry.pack(pady=10)
        self.otp_entry.focus()
        self.otp_entry.bind("<Return>", lambda _: self.verify_otp())

        self.verify_btn = ctk.CTkButton(self.f, text="Verify & Log In", width=280, height=42, command=self.verify_otp)
        self.verify_btn.pack(pady=10)

        self.resend_btn = ctk.CTkButton(self.f, text="Resend Code", width=280, height=38, fg_color="#d35400", hover_color="#a04000", command=self.resend_otp)
        self.resend_btn.pack(pady=5)

        self.change_phone_btn = ctk.CTkButton(self.f, text="Change Phone Number", width=280, height=32, fg_color="#0A1626", border_width=1, command=self.reset_auth_state)
        self.change_phone_btn.pack(pady=10)

        self.otp_status = ctk.CTkLabel(self.f, text="Enter the code sent to your Telegram app.", font=ctk.CTkFont(size=12))
        self.otp_status.pack(pady=5)

    def reset_auth_state(self):
        self.phone_code_hash = None
        self.phone = None
        self.otp_verify_inflight = False
        self.otp_request_inflight = False
        self.show_phone_login()

    def start_otp_cooldown(self, seconds=60):
        self.otp_cooldown_until = time.monotonic() + seconds
        if self.otp_cooldown_job:
            try:
                self.after_cancel(self.otp_cooldown_job)
            except Exception:
                pass
            self.otp_cooldown_job = None

        if hasattr(self, "resend_btn"):
            self.resend_btn.configure(state="disabled")

        def tick():
            remaining = int(max(0, self.otp_cooldown_until - time.monotonic()))
            if remaining > 0:
                if hasattr(self, "resend_btn") and self.resend_btn.winfo_exists():
                    self.resend_btn.configure(text=f"Resend Code ({remaining}s)")
                self.otp_cooldown_job = self.after(1000, tick)
            else:
                self.otp_cooldown_job = None
                if hasattr(self, "resend_btn") and self.resend_btn.winfo_exists():
                    self.resend_btn.configure(text="Resend Code", state="normal")

        tick()

    def verify_otp(self):
        if self.otp_verify_inflight or self.otp_request_inflight:
            return

        code = self.otp_entry.get().strip().replace(" ", "")
        if not code:
            self.show_themed_info("Missing Information", "Please enter the verification code.", "error")
            return

        self.otp_verify_inflight = True
        if hasattr(self, "verify_btn"):
            self.verify_btn.configure(state="disabled")
        self.otp_status.configure(text="Verifying code...")

        self.run_async(self._verify_otp(code))

    def resend_otp(self):
        if self.otp_request_inflight or self.otp_verify_inflight:
            return

        remaining = int(max(0, self.otp_cooldown_until - time.monotonic()))
        if remaining > 0:
            return

        self.otp_request_inflight = True
        if hasattr(self, "resend_btn"):
            self.resend_btn.configure(state="disabled")
        self.otp_status.configure(text="Requesting new code...")

        self.run_async(self._resend_otp())

    async def _resend_otp(self):
        try:
            sent = await self.client.send_code_request(self.phone)
            self.phone_code_hash = getattr(sent, "phone_code_hash", None)
            self.after(0, lambda: self.otp_entry.delete(0, "end"))
            self.after(0, lambda: self.otp_status.configure(text="New code sent successfully."))
            self.after(0, lambda: self.start_otp_cooldown(60))
        except errors.FloodWaitError as e:
            w = getattr(e, "seconds", 60)
            self.after(0, lambda: self.otp_status.configure(text=f"Rate limit. Try again in {w}s."))
            self.after(0, lambda: self.start_otp_cooldown(w))
        except errors.SendCodeUnavailableError:
            self.after(0, lambda: self.otp_status.configure(text="Resend unavailable. Please try again later."))
        except Exception as e:
            self.after(0, lambda err=str(e): messagebox.showerror("Resend Error", err))
        finally:
            self.after(0, lambda: setattr(self, "otp_request_inflight", False))

    async def _verify_otp(self, code: str):
        try:
            await self.client.sign_in(phone=self.phone, code=code, phone_code_hash=self.phone_code_hash)
            self.set_connection_status("Connected")
            self.channel = await self.resolve_channel()
            self.client.add_event_handler(self._on_new_message, events.NewMessage(chats=self.channel))

            self.after(0, self.show_main_ui)
            self.after(500, self.load_existing_index)

        except errors.SessionPasswordNeededError:
            self.after(0, self.show_2fa_screen)
        except errors.PhoneCodeInvalidError:
            self.after(0, lambda: self.otp_status.configure(text="Invalid OTP. Please enter the latest Telegram code."))
            self.after(0, lambda: self.otp_entry.focus())
        except errors.PhoneCodeExpiredError:
            self.after(0, lambda: self.otp_status.configure(text="Verification code expired. Please request a new code."))
            self.after(0, lambda: self.otp_entry.delete(0, "end"))
        except Exception as e:
            err = str(e)
            safe_log(f"Verification failure: {err}")
            self.after(0, lambda: messagebox.showerror("Authentication Failed", err))
        finally:
            self.after(0, lambda: setattr(self, "otp_verify_inflight", False))
            self.after(0, lambda: self.verify_btn.configure(state="normal") if hasattr(self, "verify_btn") else None)

    def show_2fa_screen(self):
        self.clear_root_frame()
        self.f = ctk.CTkFrame(self, fg_color=APP_BG)
        self.f.pack(fill="both", expand=True)

        ctk.CTkLabel(self.f, text="Two-Step Verification", font=ctk.CTkFont(size=28, weight="bold")).pack(pady=(80, 10))
        ctk.CTkLabel(self.f, text="Enter your Telegram Cloud Password", font=ctk.CTkFont(size=14), text_color="#91A8C1").pack(pady=(0, 20))

        self.twofa_entry = ctk.CTkEntry(self.f, show="*", width=280, height=40, placeholder_text="2FA Password")
        self.twofa_entry.pack(pady=10)
        self.twofa_entry.focus()
        self.twofa_entry.bind("<Return>", lambda _: self.verify_2fa())

        self.twofa_btn = ctk.CTkButton(self.f, text="Unlock & Authenticate", width=280, height=42, command=self.verify_2fa)
        self.twofa_btn.pack(pady=15)

        self.twofa_status = ctk.CTkLabel(self.f, text="")
        self.twofa_status.pack(pady=5)

    def verify_2fa(self):
        pwd = self.twofa_entry.get().strip()
        if not pwd:
            self.show_themed_info("Missing Password", "Please enter your 2FA password.", "error")
            return

        self.twofa_btn.configure(state="disabled")
        self.twofa_status.configure(text="Authenticating 2FA...")
        self.run_async(self._verify_2fa(pwd))

    async def _verify_2fa(self, password: str):
        try:
            await self.client.sign_in(password=password)
            self.set_connection_status("Connected")
            self.channel = await self.resolve_channel()
            self.client.add_event_handler(self._on_new_message, events.NewMessage(chats=self.channel))

            self.after(0, self.show_main_ui)
            self.after(500, self.load_existing_index)
        except Exception as e:
            err = str(e)
            self.after(0, lambda: messagebox.showerror("2FA Failed", "Invalid Cloud Password."))
            self.after(0, lambda: self.twofa_btn.configure(state="normal"))
            self.after(0, lambda: self.twofa_status.configure(text="Authentication failed."))

    # ========================================================
    # MAIN APPLICATION DASHBOARD UI
    # ========================================================
    def show_main_ui(self):
        self.clear_root_frame()

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ------------------- SIDEBAR -------------------
        # Navigation stays scrollable; Storage + Settings remain fixed at the bottom.
        self.sidebar = ctk.CTkFrame(
            self, width=230, corner_radius=0,
            fg_color="#081321", border_width=0
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_columnconfigure(0, weight=1)
        self.sidebar.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            self.sidebar, text="TGDrive Pro",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#F5F9FF"
        ).grid(row=0, column=0, padx=20, pady=(20, 7), sticky="w")

        self.status_indicator = ctk.CTkLabel(
            self.sidebar, text="● Connected",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#22C981"
        )
        self.status_indicator.grid(row=1, column=0, padx=20, pady=(0, 10), sticky="w")

        self.nav_frame = ctk.CTkScrollableFrame(
            self.sidebar,
            corner_radius=0,
            fg_color="#081321",
            scrollbar_button_color="#204064",
            scrollbar_button_hover_color="#2A4D76"
        )
        self.nav_frame.grid(row=2, column=0, padx=8, pady=(0, 6), sticky="nsew")
        self.nav_frame.grid_columnconfigure(0, weight=1)

        nav_items = [
            "All Files", "Starred", "Recent", "Images", "Videos",
            "Audio", "Documents", "Others", "Msg"
        ]
        nav_icons = {
            "All Files": "□",
            "Starred": "☆",
            "Recent": "◷",
            "Images": "▧",
            "Videos": "▷",
            "Audio": "♫",
            "Documents": "▤",
            "Others": "▱",
            "Msg": "▱"
        }
        self.selection_widgets = {}
        self.nav_buttons = {}

        for idx, item in enumerate(nav_items):
            btn = ctk.CTkButton(
                self.nav_frame,
                text=f"{nav_icons[item]}   {item}",
                anchor="w",
                height=42,
                corner_radius=9,
                fg_color="#081321",
                text_color="#D7E5F7",
                hover_color="#142A45",
                border_width=0,
                command=lambda c=item: self.set_category_filter(c)
            )
            btn.grid(row=idx, column=0, padx=2, pady=2, sticky="ew")
            self.nav_buttons[item] = btn

        # Bottom area: Settings first, Storage Summary below it.
        bottom = ctk.CTkFrame(
            self.sidebar, fg_color="#081321", corner_radius=0
        )
        bottom.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 12))
        bottom.grid_columnconfigure(0, weight=1)

        self.storage_frame = ctk.CTkFrame(
            bottom,
            fg_color="#102238",
            corner_radius=12,
            border_width=1,
            border_color="#1D3B5E"
        )
        self.storage_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        ctk.CTkLabel(
            self.storage_frame,
            text="Storage Summary",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#F5F9FF"
        ).pack(padx=12, pady=(10, 2), anchor="w")

        self.storage_lbl = ctk.CTkLabel(
            self.storage_frame, text="Used: 0 B",
            font=ctk.CTkFont(size=11),
            text_color="#91A8C1"
        )
        self.storage_lbl.pack(padx=12, pady=(0, 10), anchor="w")

        self.settings_btn = ctk.CTkButton(
            bottom,
            text="⚙  Settings",
            anchor="w",
            height=40,
            corner_radius=10,
            fg_color="#142A45",
            hover_color="#1B385C",
            text_color="#F5F9FF",
            border_width=1,
            border_color="#2A4D76",
            command=self.show_settings
        )
        self.settings_btn.grid(row=0, column=0, sticky="ew")

        # ------------------- MAIN CONTENT -------------------
        self.main_content = ctk.CTkFrame(self, corner_radius=0, fg_color="#060D18")
        self.main_content.grid(row=0, column=1, sticky="nsew", padx=15, pady=15)
        self.main_content.grid_rowconfigure(2, weight=1)
        self.main_content.grid_columnconfigure(0, weight=1)

        top_bar = ctk.CTkFrame(self.main_content, fg_color="#060D18")
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        # Modern search bar
        search_wrap = ctk.CTkFrame(
            top_bar,
            width=260,
            height=40,
            corner_radius=10,
            fg_color="#0B192B",
            border_width=1,
            border_color="#2B527F"
        )
        search_wrap.pack(side="left", padx=(0, 10))
        search_wrap.pack_propagate(False)

        ctk.CTkLabel(
            search_wrap,
            text="⌕",
            width=34,
            font=ctk.CTkFont(size=22),
            text_color="#9AB3CE"
        ).pack(side="left", padx=(5, 0))

        self.search_entry = ctk.CTkEntry(
            search_wrap,
            placeholder_text="Search files, messages, name or type...",
            height=38,
            fg_color="transparent",
            border_width=0,
            text_color="#EAF3FF",
            placeholder_text_color="#7F96B0",
            font=ctk.CTkFont(size=12)
        )
        self.search_entry.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.search_entry.bind("<KeyRelease>", self.on_search_key)
        # Consistent screenshot-inspired action buttons
        action_style = dict(
            height=42, corner_radius=10, border_width=1,
            font=ctk.CTkFont(size=12, weight="bold")
        )

        ctk.CTkButton(
            top_bar, text="☁  Upload Files", width=142,
            fg_color="#16A765", hover_color="#128A54",
            border_color="#21C477", text_color="#FFFFFF",
            command=self.upload_files, **action_style
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            top_bar, text="⇩  Download", width=125,
            fg_color="#1683F7", hover_color="#0D6ED6",
            border_color="#3196FF", text_color="#FFFFFF",
            command=self.download_selected, **action_style
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            top_bar, text="♜  Delete", width=105,
            fg_color="#F04444", hover_color="#D63838",
            border_color="#FF6666", text_color="#FFFFFF",
            command=self.delete_selected, **action_style
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            top_bar, text="⟳  Refresh", width=108,
            fg_color="#233750", hover_color="#2D4663",
            border_color="#304B6B", text_color="#EAF3FF",
            command=self.refresh_ui, **action_style
        ).pack(side="left", padx=4)

        self.index_btn = ctk.CTkButton(
            top_bar, text="▱  Index All Channel", width=175,
            height=42, corner_radius=10,
            fg_color="#0B192B", hover_color="#142A45",
            border_width=1, border_color="#3A63FF",
            text_color="#EAF3FF",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.start_rebuild_index
        )
        self.index_btn.pack(side="right", padx=4)

        sub_bar = ctk.CTkFrame(self.main_content, fg_color="#060D18")
        sub_bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        # New Separate Sort Dropdowns
        self.sort_order_option = ctk.CTkOptionMenu(sub_bar, values=["Ascending", "Descending"], width=110, height=30, corner_radius=8, fg_color="#1984FF", button_color="#0F6FE0", button_hover_color="#0B5DBE", dropdown_fg_color="#0D1A2B", dropdown_hover_color="#142A45", dropdown_text_color="#F5F9FF", text_color="#FFFFFF", command=self.on_sort_change)
        self.sort_order_option.pack(side="right", padx=3)

        self.sort_field_option = ctk.CTkOptionMenu(sub_bar, values=["Name", "Date modified", "Type", "Size"], width=130, height=30, corner_radius=8, fg_color="#1984FF", button_color="#0F6FE0", button_hover_color="#0B5DBE", dropdown_fg_color="#0D1A2B", dropdown_hover_color="#142A45", dropdown_text_color="#F5F9FF", text_color="#FFFFFF", command=self.on_sort_change)
        self.sort_field_option.pack(side="right", padx=3)

        view_wrap = ctk.CTkFrame(sub_bar, fg_color="transparent")
        view_wrap.pack(side="left", padx=2)
        self.grid_view_btn = ctk.CTkButton(
            view_wrap, text="▦", width=38, height=30, corner_radius=8,
            fg_color="#163B68", hover_color="#1C4A80",
            command=lambda: self.set_view_mode("grid")
        )
        self.grid_view_btn.pack(side="left", padx=2)
        self.list_view_btn = ctk.CTkButton(
            view_wrap, text="☷", width=38, height=30, corner_radius=8,
            fg_color="#0B192B", hover_color="#142A45",
            command=lambda: self.set_view_mode("list")
        )
        self.list_view_btn.pack(side="left", padx=2)

        self.file_container = ctk.CTkScrollableFrame(self.main_content, fg_color="#0A1626")
        self.file_container.grid(row=2, column=0, sticky="nsew")

        if DND_AVAILABLE:
            try:
                self.file_container.drop_target_register(DND_FILES)
                self.file_container.dnd_bind('<<Drop>>', self.on_drag_and_drop)
            except Exception as e:
                safe_log(f"Drag and drop binding failed: {e}")

        self.status_bar = ctk.CTkFrame(self.main_content, height=28, fg_color="#060D18")
        self.status_bar.grid(row=3, column=0, sticky="ew", pady=(5, 0))

        self.prog = ctk.CTkProgressBar(self.status_bar)
        self.prog.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.prog.set(0)

        self.lbl = ctk.CTkLabel(self.status_bar, text="Ready", font=ctk.CTkFont(size=12))
        self.lbl.pack(side="right", padx=5)

        self.set_category_filter("All Files")

    def set_view_mode(self, mode):
        if mode not in ("grid", "list"):
            return
        self.view_mode = mode
        if hasattr(self, "grid_view_btn"):
            self.grid_view_btn.configure(
                fg_color="#163B68" if mode == "grid" else "#0B192B",
                border_width=1 if mode == "grid" else 0,
                border_color="#2B6FB0"
            )
        if hasattr(self, "list_view_btn"):
            self.list_view_btn.configure(
                fg_color="#163B68" if mode == "list" else "#0B192B",
                border_width=1 if mode == "list" else 0,
                border_color="#2B6FB0"
            )
        self.render_files()

    def _focus_search(self, event=None):
        if hasattr(self, "search_entry"):
            self.search_entry.focus_set()
            self.search_entry.select_range(0, "end")
        return "break"

    def _keyboard_preview(self, event=None):
        if len(self.selected_ids) == 1:
            sid = next(iter(self.selected_ids))
            row = next((r for r in self.rows_data if r[0] == sid), None)
            if row:
                self.preview_file(row)
        return "break"

    # ========================================================
    # SELECT ALL & REFRESH LOGIC
    # ========================================================

    def toggle_item_selection(self, msg_id, var=None):
        """Handle one card checkbox."""
        msg_id = int(msg_id)
        selected = bool(var.get()) if var is not None else (
            msg_id in self.selected_msg_ids
        )

        if selected:
            self.selected_msg_ids.add(msg_id)
        else:
            self.selected_msg_ids.discard(msg_id)

        self.update_selected_count()


    def set_item_selected(self, msg_id, selected):
        msg_id = int(msg_id)
        if selected:
            self.selected_msg_ids.add(msg_id)
        else:
            self.selected_msg_ids.discard(msg_id)
        self.refresh_selection_ui()


    def refresh_ui(self):
        if self.busy or self.indexing:
            return
        self.lbl.configure(text="Refreshing index…")
        self.after(20, self.load_existing_index)
        self.after(40, lambda: self.lbl.configure(text="UI refreshed successfully."))

    # ========================================================
    # RENDER & SELECTION ENGINE
    # ========================================================
    def set_category_filter(self, category: str):
        self.current_category = category

        # Clean navy active state — avoid the default gray CustomTkinter selection color.
        for cat, btn in self.nav_buttons.items():
            if cat == category:
                btn.configure(
                    fg_color="#163B68",
                    hover_color="#1C4A80",
                    text_color="#FFFFFF",
                    border_width=1,
                    border_color="#2B6FB0",
                    font=ctk.CTkFont(size=13, weight="bold")
                )
            else:
                btn.configure(
                    fg_color="#081321",
                    hover_color="#142A45",
                    text_color="#D7E5F7",
                    border_width=0,
                    font=ctk.CTkFont(size=13, weight="normal")
                )

        self.render_files()

    def on_search_key(self, event=None):
        self.search_query = self.search_entry.get().strip()
        self.render_files()

    def on_sort_change(self, choice=None):
        field = self.sort_field_option.get()
        order = self.sort_order_option.get()

        if field == "Name":
            self.sort_column = "name"
        elif field == "Date modified":
            self.sort_column = "date"
        elif field == "Type":
            self.sort_column = "type"
        elif field == "Size":
            self.sort_column = "size"

        self.sort_reverse = (order == "Descending")
        self.render_files()

    def load_existing_index(self):
        self.rows_data = self.get_filtered_rows()
        valid_ids = {row[0] for row in self.rows_data}
        self.selected_ids.intersection_update(valid_ids)
        self.render_files()
        self.update_storage_dashboard()

    def update_storage_dashboard(self):
        total_bytes, total_files, _ = self.get_storage_stats()
        if hasattr(self, "storage_lbl"):
            self.storage_lbl.configure(text=f"Indexed: {total_files} files\nUsed: {format_size(total_bytes)}")

    def open_local_file(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self.show_themed_info("Open File", str(e), "error")

    def show_toast(self, message, kind="info", duration=2200):
        """Small non-blocking notification instead of a modal popup."""
        try:
            colors = {
                "info": ("#1984FF", "#EAF3FF"),
                "success": ("#16A765", "#E9FFF5"),
                "warning": ("#C88716", "#FFF5DC"),
                "error": ("#D63D3D", "#FFF0F0"),
            }
            fg, tc = colors.get(kind, colors["info"])
            toast = ctk.CTkFrame(self, fg_color="#102238", corner_radius=10,
                                 border_width=1, border_color=fg)
            toast.place(relx=0.985, rely=0.965, anchor="se")
            ctk.CTkLabel(toast, text=message, text_color=tc,
                         font=ctk.CTkFont(size=11, weight="bold"),
                         wraplength=360).pack(padx=16, pady=10)
            self.after(duration, toast.destroy)
        except Exception:
            pass

    def _open_url(self, url):
        try:
            webbrowser.open(url)
        except Exception as e:
            self.show_themed_info("Link", str(e), "error")

    def _copy_text(self, value):
        try:
            self.clipboard_clear()
            self.clipboard_append(str(value))
            self.update()
            self.show_toast("Copied to clipboard", "success")
        except Exception as e:
            self.show_toast(str(e), "error")

    def show_file_details(self, row):
        msg_id, name, size, category, date_added, folder_path, is_fav, last_accessed, item_type, message_text = row[:10]
        path = self._find_local_file(name, folder_path)
        links = self.extract_links(message_text or "")
        details = [
            f"Name: {name}",
            f"Type: {category}",
            f"Size: {format_size(size)}",
            f"Telegram message ID: {msg_id}",
            f"Date: {date_added or '—'}",
            f"Last accessed: {last_accessed or '—'}",
            f"Starred: {'Yes' if is_fav else 'No'}",
            f"Local: {path or 'Not downloaded'}",
        ]
        if links:
            details.append(f"Links: {len(links)}")
        win = ctk.CTkToplevel(self)
        win.title("File Details")
        win.geometry("560x430")
        win.configure(fg_color="#060D18")
        win.transient(self)
        box = ctk.CTkFrame(win, fg_color="#0D1A2B", corner_radius=14,
                           border_width=1, border_color="#2A4D76")
        box.pack(fill="both", expand=True, padx=14, pady=14)
        ctk.CTkLabel(box, text="File Details", text_color="#F5F9FF",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", padx=20, pady=(18, 8))
        txt = ctk.CTkTextbox(box, fg_color="#081321", border_width=0, text_color="#D7E5F7")
        txt.pack(fill="both", expand=True, padx=20, pady=8)
        txt.insert("1.0", "\n".join(details))
        if message_text:
            txt.insert("end", "\n\nMessage:\n" + message_text)
        txt.configure(state="disabled")
        actions = ctk.CTkFrame(box, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 18))
        ctk.CTkButton(actions, text="Copy Info", width=110,
                      command=lambda: self._copy_text("\n".join(details))).pack(side="left")
        if path:
            ctk.CTkButton(actions, text="Open", width=100,
                          command=lambda: self.open_local_file(path)).pack(side="left", padx=8)
        ctk.CTkButton(actions, text="Close", width=100,
                      fg_color="#142A45", hover_color="#204064",
                      command=win.destroy).pack(side="right")

    def _show_context_menu(self, event, row):
        menu = ctk.CTkToplevel(self)
        menu.overrideredirect(True)
        menu.configure(fg_color="#0D1A2B")
        menu.geometry(f"210x245+{event.x_root}+{event.y_root}")
        menu.transient(self)
        def item(label, cmd):
            ctk.CTkButton(menu, text=label, anchor="w", height=36,
                          corner_radius=7, fg_color="transparent",
                          hover_color="#142A45", text_color="#EAF3FF",
                          command=lambda: (menu.destroy(), cmd())).pack(fill="x", padx=5, pady=2)
        item("Preview", lambda: self.preview_file(row))
        item("Download", lambda: self.download_single(row[0], row[1]))
        item("★  Star / Unstar", lambda: self.toggle_favorite(row[0]))
        item("File Details", lambda: self.show_file_details(row))
        if row[9]:
            item("Copy Message", lambda: self._copy_text(row[9]))
        menu.bind("<FocusOut>", lambda e: menu.destroy())
        menu.focus_force()

    def _duplicate_names(self):
        """Detect local duplicates by SHA-256, with a short-lived scan cache."""
        now = time.time()
        if now - self._duplicate_scan_at < 8:
            return self._duplicate_set

        files = []
        try:
            for root, _, names in os.walk(DOWNLOADS):
                for n in names:
                    path = os.path.join(root, n)
                    try:
                        st = os.stat(path)
                        files.append((path, st.st_size))
                    except OSError:
                        pass
        except Exception:
            return set()

        size_groups = {}
        for path, size in files:
            size_groups.setdefault(size, []).append(path)

        duplicates = set()
        for group in size_groups.values():
            if len(group) < 2:
                continue
            hashed = {}
            for path in group:
                digest = self.calculate_file_hash(path)
                if digest:
                    hashed.setdefault(digest, []).append(path)
            for same in hashed.values():
                if len(same) > 1:
                    duplicates.update(same)

        self._duplicate_set = duplicates
        self._duplicate_scan_at = now
        return duplicates

    def _is_duplicate_local(self, path):
        if not path:
            return False
        key = path
        if key not in self._duplicate_cache:
            self._duplicate_cache[key] = path in self._duplicate_names()
        return self._duplicate_cache[key]

    def _open_with_system(self, path):
        self.update_last_accessed(self._current_preview_rows[self._current_preview_index][0]
                                  if self._current_preview_rows else 0)
        self.open_local_file(path)

    def _preview_text_file(self, path, name):
        win = ctk.CTkToplevel(self)
        win.title(f"Preview — {name}")
        win.geometry("900x650")
        win.configure(fg_color="#060D18")
        box = ctk.CTkFrame(win, fg_color="#0D1A2B", corner_radius=14,
                           border_width=1, border_color="#2A4D76")
        box.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(box, text=name, text_color="#F5F9FF",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", padx=16, pady=(14, 8))
        txt = ctk.CTkTextbox(box, fg_color="#081321", text_color="#D7E5F7",
                             font=ctk.CTkFont(size=12))
        txt.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                txt.insert("1.0", fh.read())
        except Exception as e:
            txt.insert("1.0", str(e))
        txt.configure(state="disabled")

    def _preview_pdf(self, path, name):
        win = ctk.CTkToplevel(self)
        win.title(f"PDF Preview — {name}")
        win.geometry("900x700")
        win.configure(fg_color="#060D18")
        label = ctk.CTkLabel(win, text="Loading PDF…", text_color="#B9CBE0")
        label.pack(expand=True)
        try:
            import fitz
            doc = fitz.open(path)
            page_index = {"value": 0}
            image_holder = {"photo": None}

            def render_page():
                page = doc.load_page(page_index["value"])
                pix = page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False)
                im = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                im.thumbnail((820, 560), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(im)
                image_holder["photo"] = photo
                label.configure(image=photo, text="")
                count.configure(text=f"Page {page_index['value'] + 1} / {len(doc)}")

            label.destroy()
            label = ctk.CTkLabel(win, text="")
            label.pack(fill="both", expand=True, padx=12, pady=8)
            count = ctk.CTkLabel(win, text="", text_color="#91A8C1")
            count.pack(pady=4)
            nav = ctk.CTkFrame(win, fg_color="transparent")
            nav.pack(pady=(0, 12))
            ctk.CTkButton(nav, text="← Previous", command=lambda: (page_index.__setitem__("value", max(0, page_index["value"]-1)), render_page())).pack(side="left", padx=4)
            ctk.CTkButton(nav, text="Next →", command=lambda: (page_index.__setitem__("value", min(len(doc)-1, page_index["value"]+1)), render_page())).pack(side="left", padx=4)
            render_page()
            win.protocol("WM_DELETE_WINDOW", lambda: (doc.close(), win.destroy()))
        except Exception:
            label.configure(text="PDF viewer requires PyMuPDF (fitz).\nUse Open to view it with your default PDF app.",
                             text_color="#91A8C1")
            ctk.CTkButton(win, text="Open PDF", command=lambda: self.open_local_file(path)).pack(pady=15)

    def _preview_media(self, row, path):
        msg_id, name, size, category, date_added, folder_path, is_fav, last_accessed, item_type, message_text = row[:10]
        ext = Path(name).suffix.lower()
        self.update_last_accessed(msg_id)

        if ext == ".txt":
            self._preview_text_file(path, name)
            return
        if ext == ".pdf":
            self._preview_pdf(path, name)
            return
        if category == "Images":
            self._show_image_lightbox(row, path)
            return

        if category == "Videos":
            self._video_player(row, path)
            return

        if category == "Audio":
            self._audio_player(row, path)
            return

        win = ctk.CTkToplevel(self)
        win.title(f"Preview — {name}")
        win.geometry("720x520")
        win.configure(fg_color="#060D18")
        ctk.CTkLabel(
            win, text=name, text_color="#F5F9FF",
            font=ctk.CTkFont(size=16, weight="bold")
        ).pack(pady=(18, 8))
        ctk.CTkLabel(
            win, text=self._grid_icon(category, item_type),
            font=ctk.CTkFont(size=58), text_color="#D7E5F7"
        ).pack(pady=40)
        ctk.CTkLabel(
            win, text=f"{category} • {format_size(size)}",
            text_color="#91A8C1"
        ).pack()
        actions = ctk.CTkFrame(win, fg_color="transparent")
        actions.pack(fill="x", padx=30, pady=20)
        ctk.CTkButton(
            actions, text="Open", command=lambda: self.open_local_file(path)
        ).pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(
            actions, text="Details", command=lambda: self.show_file_details(row)
        ).pack(side="left", fill="x", expand=True, padx=4)

    def _video_player(self, row, path):
        """Lightweight in-app video player when OpenCV is available."""
        name = row[1]
        try:
            import cv2
        except Exception:
            win = ctk.CTkToplevel(self)
            win.title(f"Video Preview — {name}")
            win.geometry("620x300")
            win.configure(fg_color="#060D18")
            ctk.CTkLabel(
                win, text="OpenCV is not installed.\nThe video can still be opened with your system player.",
                text_color="#B9CBE0", font=ctk.CTkFont(size=13)
            ).pack(expand=True, pady=30)
            ctk.CTkButton(win, text="Open Video", command=lambda: self.open_local_file(path)).pack(pady=12)
            return

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            self.show_themed_info("Video Preview", "Unable to open this video.", "error")
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = total / fps if total else 0

        win = ctk.CTkToplevel(self)
        win.title(f"Video Preview — {name}")
        win.geometry("900x650")
        win.configure(fg_color="#050B14")

        image_label = ctk.CTkLabel(win, text="Loading…")
        image_label.pack(fill="both", expand=True, padx=12, pady=(12, 4))
        time_label = ctk.CTkLabel(win, text="00:00 / 00:00", text_color="#91A8C1")
        time_label.pack(pady=3)

        seek = ctk.CTkSlider(win, from_=0, to=max(1, duration), number_of_steps=max(1, min(1000, total)))
        seek.pack(fill="x", padx=22, pady=5)

        controls = ctk.CTkFrame(win, fg_color="transparent")
        controls.pack(pady=(4, 12))
        state = {"playing": True, "closed": False, "photo": None}

        def fmt(sec):
            sec = max(0, int(sec))
            return f"{sec//60:02d}:{sec%60:02d}"

        def draw_frame():
            if state["closed"]:
                return
            ok, frame = cap.read()
            if not ok:
                state["playing"] = False
                return
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            im = Image.fromarray(frame)
            im.thumbnail((850, 500), Image.Resampling.LANCZOS)
            ph = ImageTk.PhotoImage(im)
            state["photo"] = ph
            image_label.configure(image=ph, text="")
            pos = cap.get(cv2.CAP_PROP_POS_FRAMES) / fps
            seek.set(min(duration, pos) if duration else 0)
            time_label.configure(text=f"{fmt(pos)} / {fmt(duration)}")
            if state["playing"]:
                win.after(max(10, int(1000 / fps)), draw_frame)

        def toggle():
            state["playing"] = not state["playing"]
            play_btn.configure(text="Pause" if state["playing"] else "Play")
            if state["playing"]:
                draw_frame()

        def seek_to(value):
            cap.set(cv2.CAP_PROP_POS_MSEC, float(value) * 1000)
            if not state["playing"]:
                ok, frame = cap.read()
                if ok:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    im = Image.fromarray(frame)
                    im.thumbnail((850, 500), Image.Resampling.LANCZOS)
                    ph = ImageTk.PhotoImage(im)
                    state["photo"] = ph
                    image_label.configure(image=ph, text="")
            time_label.configure(text=f"{fmt(float(value))} / {fmt(duration)}")

        seek.configure(command=seek_to)
        play_btn = ctk.CTkButton(
            controls, text="Pause", width=100, command=toggle,
            fg_color="#1984FF", hover_color="#0F6FE0"
        )
        play_btn.pack(side="left", padx=5)
        ctk.CTkButton(
            controls, text="Open", width=100,
            command=lambda: self.open_local_file(path)
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            controls, text="Details", width=100,
            command=lambda: self.show_file_details(row)
        ).pack(side="left", padx=5)

        def close():
            state["closed"] = True
            state["playing"] = False
            try:
                cap.release()
            except Exception:
                pass
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)
        draw_frame()

    def _audio_player(self, row, path):
        """In-app audio controls when pygame is available, otherwise a safe fallback."""
        name, size = row[1], row[2]
        win = ctk.CTkToplevel(self)
        win.title(f"Audio Preview — {name}")
        win.geometry("620x360")
        win.configure(fg_color="#060D18")

        ctk.CTkLabel(
            win, text="♫", font=ctk.CTkFont(size=70, weight="bold"),
            text_color="#1984FF"
        ).pack(pady=(28, 5))
        ctk.CTkLabel(
            win, text=name, text_color="#F5F9FF",
            font=ctk.CTkFont(size=15, weight="bold")
        ).pack(pady=3)
        ctk.CTkLabel(
            win, text=f"Audio • {format_size(size)}",
            text_color="#91A8C1"
        ).pack()

        controls = ctk.CTkFrame(win, fg_color="transparent")
        controls.pack(pady=22)

        try:
            import pygame
            pygame.mixer.init()
            pygame.mixer.music.load(path)
            state = {"paused": False, "closed": False}

            def play():
                if state["paused"]:
                    pygame.mixer.music.unpause()
                    state["paused"] = False
                else:
                    pygame.mixer.music.play()
                status.configure(text="Playing")

            def pause():
                pygame.mixer.music.pause()
                state["paused"] = True
                status.configure(text="Paused")

            def stop():
                pygame.mixer.music.stop()
                state["paused"] = False
                status.configure(text="Stopped")

            ctk.CTkButton(controls, text="Play", width=90, command=play).pack(side="left", padx=4)
            ctk.CTkButton(controls, text="Pause", width=90, command=pause).pack(side="left", padx=4)
            ctk.CTkButton(controls, text="Stop", width=90, command=stop).pack(side="left", padx=4)
            status = ctk.CTkLabel(win, text="Ready", text_color="#91A8C1")
            status.pack()
            def close_audio():
                try:
                    pygame.mixer.music.stop()
                except Exception:
                    pass
                win.destroy()
            win.protocol("WM_DELETE_WINDOW", close_audio)
        except Exception:
            ctk.CTkLabel(
                win,
                text="In-app audio engine is unavailable.\nUse the system player to play this file.",
                text_color="#B9CBE0"
            ).pack(pady=15)
            ctk.CTkButton(
                win, text="Open / Play",
                command=lambda: self.open_local_file(path)
            ).pack(pady=8)

        ctk.CTkButton(
            win, text="Details",
            fg_color="#142A45", hover_color="#204064",
            command=lambda: self.show_file_details(row)
        ).pack(pady=12)

    def _show_image_lightbox(self, row, path):
        rows = [r for r in self.rows_data if r[3] == "Images" and r[8] == "file"]
        try:
            index = next(i for i, r in enumerate(rows) if r[0] == row[0])
        except StopIteration:
            index = 0
        self._current_preview_rows = rows
        self._current_preview_index = index

        win = ctk.CTkToplevel(self)
        self._preview_window = win
        win.title(f"Image Preview — {row[1]}")
        win.geometry("980x720")
        win.configure(fg_color="#050B14")
        image_label = ctk.CTkLabel(win, text="Loading…")
        image_label.pack(fill="both", expand=True, padx=12, pady=10)
        title = ctk.CTkLabel(win, text="", text_color="#D7E5F7",
                             font=ctk.CTkFont(size=12, weight="bold"))
        title.pack()
        controls = ctk.CTkFrame(win, fg_color="transparent")
        controls.pack(pady=12)

        def show(index):
            if not rows:
                return
            index %= len(rows)
            self._current_preview_index = index
            rr = rows[index]
            pp = self._find_local_file(rr[1], rr[5])
            if not pp or Image is None:
                image_label.configure(text="Image is not downloaded locally.", image=None)
                return
            try:
                im = Image.open(pp).convert("RGB")
                im.thumbnail((900, 600), Image.Resampling.LANCZOS)
                ph = ImageTk.PhotoImage(im)
                image_label.configure(image=ph, text="")
                image_label.image = ph
                title.configure(text=f"{rr[1]}   •   {index+1}/{len(rows)}")
                self.update_last_accessed(rr[0])
            except Exception as e:
                image_label.configure(text=str(e), image=None)

        ctk.CTkButton(controls, text="← Previous", width=120,
                      command=lambda: show(self._current_preview_index - 1)).pack(side="left", padx=5)
        ctk.CTkButton(controls, text="Next →", width=120,
                      command=lambda: show(self._current_preview_index + 1)).pack(side="left", padx=5)
        ctk.CTkButton(controls, text="Open", width=100,
                      command=lambda: self.open_local_file(self._find_local_file(rows[self._current_preview_index][1], rows[self._current_preview_rows[self._current_preview_index][5]]))
                      if rows else None).pack(side="left", padx=5)
        ctk.CTkButton(controls, text="Close", width=100, command=win.destroy).pack(side="left", padx=5)
        win.bind("<Left>", lambda e: show(self._current_preview_index - 1))
        win.bind("<Right>", lambda e: show(self._current_preview_index + 1))
        show(index)

    def preview_file(self, row):
        try:
            row = tuple(row[:10])
            msg_id, name, size, category, date_added, folder_path, is_fav, last_accessed, item_type, message_text = row
            if item_type == "message":
                self.show_message_dialog(msg_id, message_text or "")
                return

            path = self._find_local_file(name, folder_path)
            if not path:
                self.show_themed_info(
                    "Preview",
                    f"This file is indexed but not downloaded locally.\n\n{name}\n\nSize: {format_size(size)}\n\nDownload it first to preview it.",
                    "info"
                )
                return

            self._preview_media(row, path)
        except Exception as e:
            self.show_themed_info("Preview Error", str(e), "error")

    def theme_button(self, parent, text, command, variant="primary", **kwargs):
        colors = {
            "primary": (THEME["primary"], THEME["primary_hover"]),
            "success": (THEME["success"], THEME["success_hover"]),
            "danger": (THEME["danger"], THEME["danger_hover"]),
            "purple": (THEME["purple"], THEME["purple_hover"]),
            "secondary": (THEME["panel"], THEME["card_hover"]),
        }
        fg, hover = colors.get(variant, colors["primary"])
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            fg_color=fg,
            hover_color=hover,
            text_color=THEME["text"],
            corner_radius=THEME["radius_small"],
            **kwargs
        )

    def show_themed_confirm(self, title, message, yes_command):
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.geometry("470x260")
        win.resizable(False, False)
        win.configure(fg_color=DIALOG_THEME["bg"])
        win.transient(self)
        win.grab_set()

        panel = ctk.CTkFrame(
            win, fg_color=DIALOG_THEME["surface"],
            corner_radius=16, border_width=1,
            border_color=DIALOG_THEME["border"]
        )
        panel.pack(fill="both", expand=True, padx=12, pady=12)

        ctk.CTkLabel(
            panel, text="?", width=46, height=46, corner_radius=23,
            fg_color=DIALOG_THEME["primary"],
            text_color="#FFFFFF",
            font=ctk.CTkFont(size=23, weight="bold")
        ).pack(pady=(22, 10))

        ctk.CTkLabel(
            panel, text=title,
            text_color=DIALOG_THEME["text"],
            font=ctk.CTkFont(size=17, weight="bold")
        ).pack(pady=(0, 7))

        ctk.CTkLabel(
            panel, text=str(message), wraplength=400, justify="center",
            text_color=DIALOG_THEME["muted"]
        ).pack(padx=24, pady=(0, 16))

        buttons = ctk.CTkFrame(panel, fg_color="#0A1626")
        buttons.pack()

        ctk.CTkButton(
            buttons, text="Cancel", width=110, height=36,
            fg_color=DIALOG_THEME["surface"],
            hover_color="#1A3555",
            border_width=1, border_color=DIALOG_THEME["border"],
            command=win.destroy
        ).pack(side="left", padx=5)

        def confirm():
            win.destroy()
            yes_command()

        ctk.CTkButton(
            buttons, text="Continue", width=110, height=36,
            fg_color=DIALOG_THEME["danger"],
            hover_color="#D43B37",
            command=confirm
        ).pack(side="left", padx=5)

        return win

    def show_themed_info(self, title, message, icon="info"):
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.geometry("470x285")
        win.resizable(False, False)
        win.configure(fg_color=DIALOG_THEME["bg"])
        win.transient(self)
        win.grab_set()

        panel = ctk.CTkFrame(
            win,
            fg_color=DIALOG_THEME["surface"],
            corner_radius=16,
            border_width=1,
            border_color=DIALOG_THEME["border"],
        )
        panel.pack(fill="both", expand=True, padx=12, pady=12)

        icon_map = {
            "info": ("i", DIALOG_THEME["primary"]),
            "success": ("✓", DIALOG_THEME["success"]),
            "error": ("!", DIALOG_THEME["danger"]),
            "warning": ("!", DIALOG_THEME["warning"]),
        }
        glyph, icon_color = icon_map.get(icon, icon_map["info"])

        ctk.CTkLabel(
            panel,
            text=glyph,
            width=48,
            height=48,
            corner_radius=24,
            fg_color=icon_color,
            text_color="#FFFFFF",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).pack(pady=(24, 12))

        ctk.CTkLabel(
            panel,
            text=title,
            text_color=DIALOG_THEME["text"],
            font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(pady=(0, 7))

        ctk.CTkLabel(
            panel,
            text=str(message),
            wraplength=400,
            justify="center",
            text_color=DIALOG_THEME["muted"],
            font=ctk.CTkFont(size=13),
        ).pack(padx=24, pady=(0, 18))

        ctk.CTkButton(
            panel,
            text="OK",
            width=125,
            height=38,
            corner_radius=10,
            fg_color=DIALOG_THEME["primary"],
            hover_color=DIALOG_THEME["primary_hover"],
            command=win.destroy,
        ).pack(pady=(0, 18))

        win.bind("<Return>", lambda event: win.destroy())
        win.bind("<Escape>", lambda event: win.destroy())
        return win

    def _smooth_scroll_step(self, canvas, target=None):
        """Animate scroll position with easing instead of jumping by Tk's wheel delta."""
        try:
            if not canvas.winfo_exists():
                return
            current = canvas.yview()[0]
            if target is not None:
                self._smooth_scroll_target = max(0.0, min(1.0, float(target)))

            target = self._smooth_scroll_target
            distance = target - current

            # Ease-out interpolation. Stop when close enough.
            if abs(distance) < 0.0012:
                canvas.yview_moveto(target)
                self._smooth_scroll_job = None
                return

            next_pos = current + distance * 0.28
            canvas.yview_moveto(max(0.0, min(1.0, next_pos)))
            self._smooth_scroll_job = self.after(10, lambda: self._smooth_scroll_step(canvas))
        except Exception:
            self._smooth_scroll_job = None

    def _smooth_mousewheel(self, event, canvas):
        """Windows/macOS/Linux friendly smooth wheel handler."""
        try:
            if getattr(event, "delta", 0):
                # Windows commonly reports multiples of 120.
                units = -event.delta / 120.0
            elif getattr(event, "num", None) == 4:
                units = -1.0
            elif getattr(event, "num", None) == 5:
                units = 1.0
            else:
                return "break"

            current = canvas.yview()[0]
            # Convert wheel movement to normalized canvas movement.
            scrollregion = canvas.bbox("all")
            visible_h = max(1, canvas.winfo_height())
            content_h = max(visible_h, (scrollregion[3] - scrollregion[1]) if scrollregion else visible_h)
            step = (visible_h / content_h) * 0.17
            target = current + units * step

            self._smooth_scroll_target = max(0.0, min(1.0, target))

            if self._smooth_scroll_job is None:
                self._smooth_scroll_job = self.after(
                    10, lambda: self._smooth_scroll_step(canvas)
                )
            return "break"
        except Exception:
            return "break"

    def _install_smooth_scroll(self, widget):
        """Bind wheel events to the scrollable canvas used by CTkScrollableFrame."""
        try:
            canvas = getattr(widget, "_parent_canvas", None)
            if canvas is None:
                canvas = getattr(widget, "_canvas", None)
            if canvas is None:
                return

            # Store the canvas so future dynamically created cards can use it too.
            self._scroll_canvas = canvas

            for target in (canvas, widget):
                target.bind("<MouseWheel>",
                            lambda e, c=canvas: self._smooth_mousewheel(e, c),
                            add="+")
                target.bind("<Button-4>",
                            lambda e, c=canvas: self._smooth_mousewheel(e, c),
                            add="+")
                target.bind("<Button-5>",
                            lambda e, c=canvas: self._smooth_mousewheel(e, c),
                            add="+")
        except Exception:
            pass

    def render_files(self):
        self.selection_widgets = {}
        if not hasattr(self, "file_container"):
            return
        for widget in self.file_container.winfo_children():
            widget.destroy()
        self.rows_data = self.get_filtered_rows()
        self.row_vars = {}
        if not self.rows_data:
            ctk.CTkLabel(self.file_container, text="No files found in current context.", font=ctk.CTkFont(size=14), text_color="#91A8C1").pack(pady=60)
            return
        if self.view_mode == "list":
            self._render_list_view()
        else:
            self._render_grid_view()

    def _find_local_file(self, name, folder_path=""):
        candidates = [
            os.path.join(DOWNLOADS, name or ""),
            os.path.join(folder_path or DOWNLOADS, name or ""),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return None

    def _get_grid_preview(self, name, category, folder_path="", item_type="file", max_size=(220, 135)):
        """Return a PIL image for the grid card when a local preview is possible."""
        if Image is None or item_type == "message":
            return None

        path = self._find_local_file(name, folder_path)
        if not path:
            return None
        try:
            cache_key = (os.path.abspath(path), os.path.getmtime(path), max_size)
        except OSError:
            return None
        if cache_key in self._thumbnail_cache:
            return self._thumbnail_cache[cache_key].copy()

        ext = os.path.splitext(name or "")[1].lower()
        if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
            try:
                with Image.open(path) as src:
                    im = src.convert("RGB")
                    im.thumbnail(max_size, Image.Resampling.LANCZOS)
                    result = im.copy()
                self._thumbnail_cache[cache_key] = result
                return result.copy()
            except Exception as e:
                safe_log(f"Image thumbnail failed for {name}: {e}")
                return None

        # Video decoding is intentionally deferred to the Preview window so a large
        # grid never stalls while opening/scrolling. This keeps loading responsive.
        return None

    def _make_grid_preview_widget(self, parent, name, category, folder_path, item_type):
        preview = self._get_grid_preview(name, category, folder_path, item_type)
        if preview is not None:
            try:
                photo = ImageTk.PhotoImage(preview)
                label = ctk.CTkLabel(parent, text="", image=photo, height=145)
                label.image = photo
                self._grid_preview_images[name] = photo
                return label
            except Exception:
                pass

        preview_height = 92 if item_type == "message" else 125
        icon_size = 42 if item_type == "message" else 42
        return ctk.CTkLabel(
            parent,
            text=self._grid_icon(category, item_type),
            font=ctk.CTkFont(size=icon_size),
            height=preview_height,
            text_color="#D7E5F7"
        )

    def _grid_icon(self, category, item_type):
        if item_type == "message":
            return "💬"
        return {"Images":"🖼️", "Videos":"🎬", "Audio":"🎵", "Documents":"📄", "Others":"📦"}.get(category, "📁")

    def _render_grid_view(self):
        grid = ctk.CTkFrame(self.file_container, fg_color="#0A1626")
        grid.pack(fill="both", expand=True, padx=4, pady=4)
        width = max(700, self.file_container.winfo_width())
        columns = max(2, min(5, width // 285))
        for col in range(columns):
            grid.grid_columnconfigure(col, weight=1, uniform="file_card")

        for idx, (msg_id, name, size, category, date_added, folder_path, is_fav, _, item_type, message_text) in enumerate(self.rows_data):
            r, c = divmod(idx, columns)
            card = ctk.CTkFrame(grid, corner_radius=14, border_width=1, fg_color="#0F2035", border_color="#2A4D76")
            card.grid(row=r, column=c, sticky="nsew", padx=6, pady=6)
            card.grid_columnconfigure(0, weight=1)

            var = ctk.BooleanVar(value=msg_id in self.selected_ids)
            self.row_vars[msg_id] = var
            def on_chk(mid=msg_id, v=var):
                if v.get(): self.selected_ids.add(mid)
                else: self.selected_ids.discard(mid)

            top = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0, height=38)
            top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8,2))
            ctk.CTkCheckBox(top, text="", width=22, variable=var, command=on_chk).pack(side="left")
            ctk.CTkButton(top, text="★" if is_fav else "☆", width=30, height=26, fg_color="transparent", hover_color="#142A45", text_color="#f1c40f" if is_fav else "#91A8C1", command=lambda m=msg_id: self.toggle_favorite(m)).pack(side="right")
            preview_widget = self._make_grid_preview_widget(
                card, name, category, folder_path, item_type
            )
            preview_widget.grid(row=1, column=0, sticky="nsew", padx=8, pady=(4,2))

            if item_type == "message":
                preview = re.sub(r"\s+", " ", (message_text or "")).strip()
                if len(preview)>70: preview=preview[:67]+"..."
                title = "🔗 Link Message" if ("http://" in (message_text or "") or "https://" in (message_text or "")) else "💬 Text Message"
                subtitle = preview or "(empty message)"
            else:
                title = name if len(name)<=27 else name[:24]+"..."
                subtitle = f"{category}  •  {format_size(size)}"
            ctk.CTkLabel(card,text=title,anchor="center",justify="center",font=ctk.CTkFont(size=13,weight="bold")).grid(row=2,column=0,sticky="ew",padx=8,pady=(2,1))
            ctk.CTkLabel(card,text=subtitle,anchor="center",justify="center",wraplength=190,text_color="#B9CBE0",font=ctk.CTkFont(size=10)).grid(row=3,column=0,sticky="ew",padx=8,pady=(0,2))
            meta_text = str(date_added)[:16] if date_added else "—"
            local_path = self._find_local_file(name, folder_path) if item_type == "file" else None
            if local_path and self._is_duplicate_local(local_path):
                meta_text += "   •   Duplicate"
            ctk.CTkLabel(card,text=meta_text,font=ctk.CTkFont(size=9),text_color="#91A8C1").grid(row=4,column=0,pady=(0,4))
            actions=ctk.CTkFrame(card,fg_color="#0A1626"); actions.grid(row=5,column=0,sticky="ew",padx=8,pady=(0,9))
            if item_type == "message":
                ctk.CTkButton(actions,text="View",height=30,corner_radius=8,fg_color="#1984FF",hover_color="#0F6FE0",command=lambda m=msg_id,t=message_text:self.show_message_dialog(m,t)).pack(side="left",fill="x",expand=True,padx=(0,3))
            else:
                ctk.CTkButton(
                    actions, text="Preview", height=30, corner_radius=8, fg_color="#1984FF", hover_color="#0F6FE0",
                    command=lambda row=(msg_id, name, size, category, date_added, folder_path, is_fav, _, item_type, message_text): self.preview_file(row)
                ).pack(side="left", fill="x", expand=True, padx=(0,3))
                ctk.CTkButton(
                    actions, text="Download", height=28,
                    command=lambda m=msg_id,n=name:self.download_single(m,n)
                ).pack(side="left", fill="x", expand=True, padx=(3,0))

            for w in (card, preview_widget, top):
                w.bind("<Button-3>", lambda e, rr=(msg_id, name, size, category, date_added, folder_path, is_fav, _, item_type, message_text): self._show_context_menu(e, rr))

    def _render_list_view(self):
        list_frame = ctk.CTkFrame(self.file_container, fg_color="#0A1626")
        list_frame.pack(fill="both", expand=True, padx=4, pady=4)
        headers = [("Name", 0), ("Type", 1), ("Size", 2), ("Date", 3), ("Actions", 4)]
        for label, col in headers:
            ctk.CTkLabel(list_frame, text=label, text_color="#91A8C1",
                         font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=col, sticky="w", padx=10, pady=8)
        for r_idx, row in enumerate(self.rows_data, 1):
            msg_id, name, size, category, date_added, folder_path, is_fav, last_accessed, item_type, message_text = row
            bg = "#0F2035" if r_idx % 2 else "#0D1A2B"
            ctk.CTkLabel(list_frame, text=("★ " if is_fav else "") + name,
                         text_color="#F5F9FF", anchor="w").grid(row=r_idx, column=0, sticky="ew", padx=10, pady=6)
            ctk.CTkLabel(list_frame, text=category, text_color="#B9CBE0",
                         anchor="w").grid(row=r_idx, column=1, sticky="w", padx=10)
            ctk.CTkLabel(list_frame, text=format_size(size), text_color="#91A8C1",
                         anchor="w").grid(row=r_idx, column=2, sticky="w", padx=10)
            ctk.CTkLabel(list_frame, text=str(date_added or "—")[:16], text_color="#91A8C1",
                         anchor="w").grid(row=r_idx, column=3, sticky="w", padx=10)
            act = ctk.CTkFrame(list_frame, fg_color="transparent")
            act.grid(row=r_idx, column=4, padx=8, pady=3)
            ctk.CTkButton(act, text="Preview" if item_type == "file" else "View", width=80, height=28,
                          command=lambda rr=row: self.preview_file(rr)).pack(side="left", padx=2)
            ctk.CTkButton(act, text="⋮", width=34, height=28, fg_color="#142A45",
                          hover_color="#204064",
                          command=lambda rr=row: self._show_context_menu(
                              type("E", (), {"x_root": self.winfo_pointerx(), "y_root": self.winfo_pointery()})(), rr)).pack(side="left", padx=2)

    @staticmethod
    def extract_links(text_content):
        if not text_content:
            return []
        return re.findall(r'https?://[^\s<>\"\']+', text_content)

    @staticmethod
    def file_extension(name):
        return os.path.splitext(name or "")[1].lower()

    @staticmethod
    def calculate_file_hash(path, chunk_size=1024 * 1024):
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()
        except Exception:
            return ""


    def show_message_dialog(self, msg_id: int, message_text: str):
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Channel Message #{msg_id}")
        dialog.geometry("850x650")
        dialog.transient(self)
        dialog.grab_set()

        full_text = message_text or "(empty message)"
        links = self.extract_links(full_text)

        ctk.CTkLabel(
            dialog, text=f"💬 Channel Message #{msg_id}",
            font=ctk.CTkFont(size=18, weight="bold")
        ).pack(padx=20, pady=(18, 8), anchor="w")

        if links:
            ctk.CTkLabel(
                dialog, text=f"🔗 Links ({len(links)})",
                font=ctk.CTkFont(size=12, weight="bold")
            ).pack(padx=20, pady=(0, 4), anchor="w")

            for url in links:
                line = ctk.CTkFrame(dialog, fg_color="#0A1626")
                line.pack(fill="x", padx=20, pady=2)
                ctk.CTkLabel(
                    line, text=url, anchor="w"
                ).pack(side="left", fill="x", expand=True)
                ctk.CTkButton(
                    line, text="Open", width=65,
                    command=lambda u=url: webbrowser.open(u)
                ).pack(side="right", padx=2)
                ctk.CTkButton(
                    line, text="Copy", width=65,
                    command=lambda u=url: (dialog.clipboard_clear(), dialog.clipboard_append(u))
                ).pack(side="right", padx=2)

        ctk.CTkLabel(
            dialog, text="Full Message",

            font=ctk.CTkFont(size=12, weight="bold")
        ).pack(padx=20, pady=(12, 4), anchor="w")

        box = ctk.CTkTextbox(dialog, wrap="word")
        box.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        box.insert("1.0", full_text)
        box.configure(state="disabled")

        actions = ctk.CTkFrame(dialog, fg_color="#0A1626")
        actions.pack(fill="x", padx=20, pady=(0, 15))
        ctk.CTkButton(
            actions, text="Copy Full Message", width=150,
            command=lambda: (dialog.clipboard_clear(), dialog.clipboard_append(full_text))
        ).pack(side="left")
        ctk.CTkButton(
            actions, text="Close", width=100, command=dialog.destroy
        ).pack(side="right")


    # ========================================================
    # FAVORITES & FOLDERS
    # ========================================================
    def toggle_favorite(self, msg_id: int):
        self.toggle_favorite_db(msg_id)
        self.render_files()


    # ========================================================
    # UPLOAD ENGINE
    # ========================================================
    def on_drag_and_drop(self, event):
        files = self.tk.splitlist(event.data)
        if files:
            self._start_upload_pipeline(files)

    def upload_files(self):
        if self.uploading or self.busy:
            return
        paths = filedialog.askopenfilenames(title="Select files to upload")
        if paths:
            self._start_upload_pipeline(paths)

    def _start_upload_pipeline(self, paths):
        if self.uploading or self.downloading or self.busy:
            return
        self.uploading = True
        self.busy = True
        self.prog.set(0)
        self.lbl.configure(text=f"Preparing {len(paths)} upload(s)…")
        self.run_async(self._upload_worker(paths))

    async def _upload_one(self, path, idx, total_files, semaphore):
        async with semaphore:
            if not os.path.isfile(path):
                return 0
            filename = os.path.basename(path)
            file_size = os.path.getsize(path)
            start_time = time.time()
            last_ui = [0.0]

            def progress_cb(current, total):
                if not total:
                    return
                now = time.time()
                # Do not flood Tk with thousands of callbacks for large files.
                if now - last_ui[0] < 0.12 and current < total:
                    return
                last_ui[0] = now
                elapsed = max(0.001, now - start_time)
                speed = current / elapsed
                eta = (total - current) / speed if speed > 0 else 0
                fraction = max(0.0, min(1.0, current / total))
                self.after(0, lambda f=fraction: self.prog.set(f))
                self.after(0, lambda i=idx, n=total_files, fn=filename, sp=speed, et=eta:
                           self.lbl.configure(text=f"Uploading {i}/{n}: {fn}  •  {format_speed(sp)}  •  ETA {int(et)}s"))

            try:
                sent = await self.client.send_file(
                    self.channel, path, force_document=True,
                    progress_callback=progress_cb
                )
                msgs = sent if isinstance(sent, list) else [sent]
                for msg in msgs:
                    if getattr(msg, "id", None):
                        self.save_file_record(msg.id, filename, file_size, self.current_folder)
                return 1
            except Exception as e:
                safe_log(f"Upload failure for {filename}: {e}")
                self.after(0, lambda fn=filename, err=str(e):
                           self.show_toast(f"Upload failed: {fn} — {err}", "error", 3500))
                return 0

    async def _upload_worker(self, paths):
        total_files = len(paths)
        # A small parallelism keeps the network busy without hammering Telegram.
        concurrency = min(3, max(1, total_files))
        semaphore = asyncio.Semaphore(concurrency)
        tasks = [self._upload_one(path, idx, total_files, semaphore)
                 for idx, path in enumerate(paths, 1)]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        completed = sum(results)
        self.uploading = False
        self.busy = False
        self.after(0, lambda: self.prog.set(0))
        self.after(0, lambda: self.lbl.configure(text=f"Uploaded {completed}/{total_files} file(s)."))
        self.after(0, lambda: self.show_toast(f"Upload complete • {completed}/{total_files} files", "success"))
        self.after(0, self.load_existing_index)

    # ========================================================
    # DOWNLOAD ENGINE
    # ========================================================
    def download_single(self, msg_id: int, name: str):
        if self.uploading or self.downloading or self.busy:
            return
        self.downloading = True
        self.busy = True
        self.prog.set(0)
        self.lbl.configure(text=f"Preparing download: {name}…")
        self.run_async(self._download_worker([msg_id]))

    def download_selected(self):
        if self.uploading or self.downloading or self.busy:
            return
        row_map = {row[0]: row for row in self.rows_data}
        ids = [msg_id for msg_id in self.selected_ids
               if row_map.get(msg_id) and row_map[msg_id][8] == "file"]
        if not ids:
            messagebox.showwarning("No File Selected", "Select one or more files. Text messages are view-only.")
            return
        self.downloading = True
        self.busy = True
        self.prog.set(0)
        self.lbl.configure(text=f"Preparing {len(ids)} download(s)…")
        self.run_async(self._download_worker(ids))

    async def _download_one(self, msg, row, idx, total, semaphore):
        async with semaphore:
            msg_id = getattr(msg, "id", None)
            name = row[1] if row else f"file_{msg_id}"
            save_path = os.path.join(DOWNLOADS, name)
            if os.path.exists(save_path):
                stem, suffix = Path(name).stem, Path(name).suffix
                c = 1
                while os.path.exists(save_path):
                    save_path = os.path.join(DOWNLOADS, f"{stem}_({c}){suffix}")
                    c += 1

            start_time = time.time()
            last_ui = [0.0]
            def progress_cb(current, total_bytes):
                if not total_bytes:
                    return
                now = time.time()
                if now - last_ui[0] < 0.12 and current < total_bytes:
                    return
                last_ui[0] = now
                elapsed = max(0.001, now - start_time)
                speed = current / elapsed
                fraction = max(0.0, min(1.0, current / total_bytes))
                self.after(0, lambda f=fraction: self.prog.set(f))
                self.after(0, lambda i=idx, n=total, fn=name, sp=speed:
                           self.lbl.configure(text=f"Downloading {i}/{n}: {fn}  •  {format_speed(sp)}"))
            try:
                if msg and msg.media:
                    await self.client.download_media(msg, file=save_path, progress_callback=progress_cb)
                    self.update_last_accessed(msg_id)
                    return 1
            except Exception as e:
                safe_log(f"Download failure {msg_id}: {e}")
                self.after(0, lambda fn=name, err=str(e):
                           self.show_toast(f"Download failed: {fn} — {err}", "error", 3500))
            return 0

    async def _download_worker(self, ids):
        total = len(ids)
        row_map = {row[0]: row for row in self.rows_data}
        file_ids = [mid for mid in ids if row_map.get(mid, (None,)*9)[8] == "file"]
        completed = 0
        # Fetch message metadata in one Telegram request instead of one request per file.
        try:
            messages = await self.client.get_messages(self.channel, ids=file_ids)
        except Exception as e:
            safe_log(f"Bulk message fetch failed: {e}")
            messages = []
        if not isinstance(messages, list):
            messages = [messages] if messages else []
        by_id = {getattr(m, "id", None): m for m in messages}

        concurrency = min(3, max(1, len(file_ids)))
        semaphore = asyncio.Semaphore(concurrency)
        tasks = [self._download_one(by_id.get(mid), row_map.get(mid), idx, len(file_ids), semaphore)
                 for idx, mid in enumerate(file_ids, 1) if by_id.get(mid)]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=False)
            completed = sum(results)

        self.downloading = False
        self.busy = False
        self.after(0, lambda: self.prog.set(0))
        self.after(0, lambda: self.lbl.configure(text=f"Downloaded {completed}/{len(file_ids)} file(s)."))
        if completed:
            self.after(0, lambda: self.show_toast(f"Download complete • {completed} files", "success"))

    # ========================================================
    # DELETION ENGINE
    # ========================================================
    def delete_selected(self):
        ids = list(self.selected_ids)
        if not ids:
            messagebox.showwarning("No Selection", "Please select items to delete.")
            return

        if not messagebox.askyesno("Confirm Deletion", f"Permanently delete {len(ids)} selected file(s) from Telegram and index?"):
            return

        self.run_async(self._delete_worker(ids))

    async def _delete_worker(self, ids):
        try:
            await self.client.delete_messages(self.channel, ids)
            self.delete_db_records(ids)
            self.selected_ids.clear()
            self.after(0, self.load_existing_index)
            self.after(0, lambda: messagebox.showinfo("Deletion Complete", f"Successfully removed {len(ids)} file(s)."))
        except Exception as e:
            err = str(e)
            self.after(0, lambda: messagebox.showerror("Delete Error", err))

    # ========================================================
    # NEW MESSAGE AUTOMATIC LISTENER
    # ========================================================
    async def _on_new_message(self, event):
        try:
            msg = event.message
            if not msg:
                return

            if not msg.media and getattr(msg, "message", None):
                date_str = msg.date.strftime("%Y-%m-%d %H:%M:%S") if getattr(msg, "date", None) else None
                self.save_message_record(msg.id, msg.message, date_str, "/")
                self.after(0, self.load_existing_index)
                return

            if not msg.media:
                return

            name = None
            size = 0

            if isinstance(msg.media, MessageMediaDocument) and msg.document:
                for attr in msg.document.attributes:
                    if isinstance(attr, DocumentAttributeFilename):
                        name = attr.file_name
                        break
                size = getattr(msg.document, "size", 0) or 0
                if not name:
                    name = f"doc_{msg.id}"
            elif isinstance(msg.media, MessageMediaPhoto):
                name = f"photo_{msg.id}.jpg"

            if name:
                self.save_file_record(msg.id, name, size, "/")
                caption = (getattr(msg, 'message', '') or '').strip()
                if caption:
                    with sqlite3.connect(DB_NAME) as conn:
                        conn.execute("UPDATE files SET message_text = ? WHERE msg_id = ?", (caption, int(msg.id)))
                        conn.commit()
                self.after(0, self.load_existing_index)
        except Exception as e:
            safe_log(f"Live event index notice: {e}")

    # ========================================================
    # ATOMIC REBUILD INDEX ENGINE
    # ========================================================
    def start_rebuild_index(self):
        if self.indexing:
            self.show_themed_info("Indexing", "Indexing is already running. Please wait for it to finish.", "info")
            return
        if not self.client or not self.channel:
            messagebox.showwarning("Not Connected", "Connect to Telegram and select the channel first.")
            return
        self.indexing=True
        if hasattr(self,"index_btn"): self.index_btn.configure(state="disabled", text="Indexing...")
        self.prog.set(0)
        self.run_async(self.rebuild_index())

    async def rebuild_index(self):
        if not self.client or not self.channel:
            self.indexing=False
            return
        staging=sqlite3.connect(":memory:")
        try:
            self.after(0,lambda:self.lbl.configure(text="Scanning full Telegram channel (files + old text messages)..."))
            preserved={}
            with sqlite3.connect(DB_NAME,timeout=60) as old_conn:
                old_conn.execute("PRAGMA busy_timeout=60000")
                for row in old_conn.execute("SELECT msg_id, folder_path, is_favorite, last_accessed FROM files").fetchall():
                    preserved[row[0]]=row[1:]
            staging.execute("""CREATE TABLE IF NOT EXISTS temp_files (msg_id INTEGER PRIMARY KEY,name TEXT NOT NULL,size INTEGER DEFAULT 0,category TEXT NOT NULL,date_added TIMESTAMP,folder_path TEXT DEFAULT '/',is_favorite INTEGER DEFAULT 0,last_accessed TIMESTAMP,item_type TEXT DEFAULT 'file',message_text TEXT DEFAULT '')""")
            count=file_count=message_count=0
            async for msg in self.client.iter_messages(self.channel,limit=None,reverse=True):
                if not msg: continue
                old_meta=preserved.get(msg.id,("/",0,None)); folder_path=old_meta[0] or "/"; is_favorite=int(old_meta[1] or 0); last_accessed=old_meta[2]
                date_str=msg.date.strftime("%Y-%m-%d %H:%M:%S") if getattr(msg,"date",None) else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if not msg.media and getattr(msg,"message",None):
                    staging.execute("""INSERT OR REPLACE INTO temp_files (msg_id,name,size,category,date_added,folder_path,is_favorite,last_accessed,item_type,message_text) VALUES (?, ?, 0, 'Msg', ?, ?, ?, ?, 'message', ?)""",(msg.id,f"Message #{msg.id}",date_str,folder_path,is_favorite,last_accessed,msg.message))
                    message_count+=1; count+=1
                elif msg.media:
                    name=None; size=0
                    if getattr(msg,"document",None):
                        size=getattr(msg.document,"size",0) or 0
                        for attr in getattr(msg.document,"attributes",[]):
                            if hasattr(attr,"file_name") and attr.file_name: name=attr.file_name; break
                        if not name:
                            mime=getattr(msg.document,"mime_type","") or ""
                            name=f"video_{msg.id}.mp4" if "video" in mime else f"audio_{msg.id}.mp3" if "audio" in mime else f"image_{msg.id}.jpg" if "image" in mime else f"document_{msg.id}.bin"
                    elif getattr(msg,"photo",None):
                        name=f"photo_{msg.id}.jpg"
                        sizes=getattr(msg.photo,"sizes",[])
                        if sizes: size=getattr(sizes[-1],"size",0) or 0
                    if name:
                        staging.execute("""INSERT OR REPLACE INTO temp_files (msg_id,name,size,category,date_added,folder_path,is_favorite,last_accessed,item_type,message_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'file', ?)""",(msg.id,name,size,get_category(name),date_str,folder_path,is_favorite,last_accessed,(getattr(msg, 'message', '') or '').strip()))
                        file_count+=1; count+=1
                if count and count%20==0:
                    self.after(0,lambda c=count,f=file_count,m=message_count:self.lbl.configure(text=f"Indexed {c} items ({f} files, {m} messages)..."))
            staging.commit()
            with sqlite3.connect(DB_NAME,timeout=60) as primary_conn:
                primary_conn.execute("PRAGMA busy_timeout=60000")
                primary_conn.execute("BEGIN IMMEDIATE")
                try:
                    rows=staging.execute("SELECT msg_id,name,size,category,date_added,folder_path,is_favorite,last_accessed,item_type,message_text FROM temp_files").fetchall()
                    primary_conn.execute("DELETE FROM files")
                    primary_conn.executemany("INSERT INTO files (msg_id,name,size,category,date_added,folder_path,is_favorite,last_accessed,item_type,message_text) VALUES (?,?,?,?,?,?,?,?,?,?)",rows)
                    primary_conn.commit()
                except Exception:
                    primary_conn.rollback(); raise
            self.after(0,self.load_existing_index); self.after(0,lambda:self.prog.set(1))
            self.after(0,lambda c=count,f=file_count,m=message_count:messagebox.showinfo("Index Complete",f"Indexed {c} Telegram items.\nFiles: {f}\nText/Links: {m}"))
        except Exception as e:
            err=str(e); safe_log(f"Rebuild failed: {err}")
            self.after(0,lambda:messagebox.showerror("Index Failed",f"Index rebuild error:\n{err}"))
        finally:
            try: staging.close()
            except Exception: pass
            self.indexing=False
            self.after(0,self._index_finished_ui)

    def _index_finished_ui(self):
        if hasattr(self,"index_btn"):
            self.index_btn.configure(state="normal",text="Index All Channel")

    # ========================================================
    # SETTINGS / ACCOUNT MANAGEMENT
    # ========================================================
    def start_phone_number_change(self, parent_window=None, phone_var=None):
        """Start Telegram's official change-phone verification flow."""
        win = ctk.CTkToplevel(self)
        win.title("Change Telegram Number")
        win.geometry("500x330")
        win.resizable(False, False)
        win.configure(fg_color="#060D18")
        win.transient(parent_window or self)
        win.grab_set()

        panel = ctk.CTkFrame(
            win, fg_color="#102238", corner_radius=16,
            border_width=1, border_color="#2A4D76"
        )
        panel.pack(fill="both", expand=True, padx=14, pady=14)

        ctk.CTkLabel(
            panel, text="📱  Change Telegram Number",
            font=ctk.CTkFont(size=19, weight="bold"),
            text_color="#F5F9FF"
        ).pack(pady=(24, 6))

        ctk.CTkLabel(
            panel,
            text="Enter the new number in international format.",
            text_color="#91A8C1"
        ).pack(pady=(0, 16))

        new_phone_var = ctk.StringVar(value=self.phone or "")
        entry = ctk.CTkEntry(
            panel, textvariable=new_phone_var, width=360, height=42,
            placeholder_text="+14155552671"
        )
        entry.pack(pady=4)
        entry.focus()

        status = ctk.CTkLabel(panel, text="", text_color="#91A8C1")
        status.pack(pady=8)

        def send_code():
            new_phone = new_phone_var.get().strip().replace(" ", "")
            if not re.fullmatch(r"\+\d{7,15}", new_phone):
                status.configure(text="Enter a valid international phone number.", text_color="#F04444")
                return
            if self.phone and new_phone == self.phone:
                status.configure(text="This is already your current number.", text_color="#F5A623")
                return
            status.configure(text="Sending verification code…", text_color="#91A8C1")
            send_btn.configure(state="disabled")
            self.run_async(self._send_phone_change_code(new_phone, win, status, send_btn, phone_var))

        send_btn = ctk.CTkButton(
            panel, text="Send Verification Code", width=220, height=40,
            fg_color="#1984FF", hover_color="#0F6FE0", command=send_code
        )
        send_btn.pack(pady=8)

        ctk.CTkButton(
            panel, text="Cancel", width=120, height=36,
            fg_color="#142A45", hover_color="#204064",
            border_width=1, border_color="#2A4D76",
            command=win.destroy
        ).pack(pady=(2, 18))

        entry.bind("<Return>", lambda _e: send_code())

    async def _send_phone_change_code(self, new_phone, win, status, send_btn, phone_var):
        try:
            if not self.client or not await self.client.is_user_authorized():
                raise RuntimeError("Telegram session is not connected.")

            result = await self.client(
                functions.account.SendChangePhoneCodeRequest(
                    phone_number=new_phone,
                    settings=types.CodeSettings()
                )
            )
            self.phone_change_number = new_phone
            self.phone_change_code_hash = result.phone_code_hash

            self.after(
                0,
                lambda: self._show_phone_change_code_dialog(
                    win, status, send_btn, phone_var
                )
            )
        except Exception as e:
            err = str(e)
            self.after(
                0,
                lambda: (
                    status.configure(text=err, text_color="#F04444"),
                    send_btn.configure(state="normal")
                )
            )

    def _show_phone_change_code_dialog(self, parent_win, status, send_btn, phone_var):
        code_win = ctk.CTkToplevel(parent_win)
        code_win.title("Verify New Number")
        code_win.geometry("500x320")
        code_win.resizable(False, False)
        code_win.configure(fg_color="#060D18")
        code_win.transient(parent_win)
        code_win.grab_set()

        panel = ctk.CTkFrame(
            code_win, fg_color="#102238", corner_radius=16,
            border_width=1, border_color="#2A4D76"
        )
        panel.pack(fill="both", expand=True, padx=14, pady=14)

        ctk.CTkLabel(
            panel, text="Enter Telegram Code",
            font=ctk.CTkFont(size=19, weight="bold"),
            text_color="#F5F9FF"
        ).pack(pady=(24, 6))

        ctk.CTkLabel(
            panel,
            text=f"Telegram sent a code to {self.phone_change_number}",
            text_color="#91A8C1"
        ).pack(pady=(0, 15))

        code_var = ctk.StringVar()
        code_entry = ctk.CTkEntry(
            panel, textvariable=code_var, width=220, height=42,
            placeholder_text="12345", justify="center"
        )
        code_entry.pack(pady=5)
        code_entry.focus()

        code_status = ctk.CTkLabel(panel, text="", text_color="#91A8C1")
        code_status.pack(pady=7)

        def verify():
            code = code_var.get().strip().replace(" ", "")
            if not code:
                code_status.configure(text="Enter the Telegram verification code.", text_color="#F04444")
                return
            verify_btn.configure(state="disabled", text="Updating…")
            self.run_async(
                self._complete_phone_number_change(
                    code, code_win, parent_win, code_status, verify_btn, phone_var
                )
            )

        verify_btn = ctk.CTkButton(
            panel, text="Verify & Update Number", width=220, height=40,
            fg_color="#22C981", hover_color="#18A667", command=verify
        )
        verify_btn.pack(pady=7)

        ctk.CTkButton(
            panel, text="Cancel", width=120, height=34,
            fg_color="#142A45", hover_color="#204064",
            command=code_win.destroy
        ).pack(pady=(0, 16))

        code_entry.bind("<Return>", lambda _e: verify())

    async def _complete_phone_number_change(
        self, code, code_win, parent_win, status, verify_btn, phone_var
    ):
        try:
            if not self.client or not self.phone_change_code_hash:
                raise RuntimeError("Phone change session expired. Please request a new code.")

            await self.client(
                functions.account.ChangePhoneRequest(
                    phone_number=self.phone_change_number,
                    phone_code_hash=self.phone_change_code_hash,
                    phone_code=code
                )
            )

            me = await self.client.get_me()
            number = getattr(me, "phone", None)
            display = f"+{number}" if number and not str(number).startswith("+") else (number or self.phone_change_number)

            self.phone = display
            self.phone_change_number = None
            self.phone_change_code_hash = None

            if phone_var is not None:
                self.after(0, lambda: phone_var.set(display))

            self.after(0, code_win.destroy)
            self.after(0, lambda: self.show_themed_info(
                "Number Updated",
                f"Telegram number updated successfully.\n\n{display}",
                "success"
            ))
        except Exception as e:
            err = str(e)
            self.after(
                0,
                lambda: (
                    status.configure(text=err, text_color="#F04444"),
                    verify_btn.configure(state="normal", text="Verify & Update Number")
                )
            )

    def show_settings(self):
        """Polished settings modal for account, Telegram API and security."""
        global API_ID, API_HASH, CHANNEL_ID, BOT_TOKEN, APP_PASSWORD

        win = ctk.CTkToplevel(self)
        win.title("TGDrive Pro — Settings")
        win.geometry("760x760")
        win.minsize(700, 680)
        win.configure(fg_color="#060D18")
        win.transient(self)
        win.grab_set()

        # Settings content starts directly here; the redundant Settings banner/header was removed.
        scroll = ctk.CTkScrollableFrame(
            win, fg_color="#081321",
            corner_radius=14,
            scrollbar_button_color="#204064",
            scrollbar_button_hover_color="#2A4D76"
        )
        scroll.pack(fill="both", expand=True, padx=18, pady=(18, 4))

        def section(parent, title, subtitle):
            box = ctk.CTkFrame(
                parent, fg_color="#0D1A2B",
                corner_radius=14, border_width=1,
                border_color="#2A4D76"
            )
            box.pack(fill="x", padx=4, pady=6)
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=18, pady=(14, 2))
            ctk.CTkLabel(
                row, text=title,
                font=ctk.CTkFont(size=15, weight="bold"),
                text_color="#F5F9FF"
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=subtitle,
                font=ctk.CTkFont(size=10),
                text_color="#7188A2"
            ).pack(side="right")
            return box

        def field(parent, label, var, secret=False, placeholder=""):
            ctk.CTkLabel(
                parent, text=label,
                text_color="#B9CBE0",
                font=ctk.CTkFont(size=11, weight="bold")
            ).pack(anchor="w", padx=18, pady=(9, 4))
            ent = ctk.CTkEntry(
                parent, textvariable=var, height=38,
                fg_color="#081321",
                border_color="#2A4D76",
                text_color="#F5F9FF",
                placeholder_text=placeholder,
                show="*" if secret else ""
            )
            ent.pack(fill="x", padx=18, pady=(0, 3))
            return ent

        # Account
        account = section(scroll, "Telegram Account", "CONNECTED ACCOUNT")
        phone_var = ctk.StringVar(value=self.phone or "Loading...")
        phone_entry = ctk.CTkEntry(
            account, textvariable=phone_var, height=40,
            fg_color="#081321", border_color="#2A4D76",
            text_color="#F5F9FF", state="readonly"
        )
        phone_entry.pack(fill="x", padx=18, pady=(8, 8))

        account_row = ctk.CTkFrame(account, fg_color="transparent")
        account_row.pack(fill="x", padx=18, pady=(0, 8))
        ctk.CTkButton(
            account_row, text="✎  Edit Number",
            width=140, height=35, corner_radius=9,
            fg_color="#1984FF", hover_color="#0F6FE0",
            command=lambda: self.start_phone_number_change(win, phone_var)
        ).pack(side="left")
        ctk.CTkLabel(
            account_row,
            text=f"●  {self.connection_status}",
            text_color="#22C981",
            font=ctk.CTkFont(size=11, weight="bold")
        ).pack(side="left", padx=14)

        # API
        api_frame = section(scroll, "Telegram Connection", "API / CHANNEL")
        ctk.CTkLabel(
            api_frame,
            text=f"Channel ID: {CHANNEL_ID}   •   Session: {'Saved' if os.path.exists(SESSION + '.session') else 'Not saved'}",
            text_color="#7188A2",
            font=ctk.CTkFont(size=10)
        ).pack(anchor="w", padx=18, pady=(0, 4))
        api_id_var = ctk.StringVar(value=str(API_ID))
        api_hash_var = ctk.StringVar(value=API_HASH)
        channel_var = ctk.StringVar(value=str(CHANNEL_ID))
        token_var = ctk.StringVar(value=BOT_TOKEN)

        # Two-column layout for compact settings.
        api_grid = ctk.CTkFrame(api_frame, fg_color="transparent")
        api_grid.pack(fill="x", padx=0, pady=(0, 7))
        api_grid.grid_columnconfigure(0, weight=1)
        api_grid.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(api_grid, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew")
        right = ctk.CTkFrame(api_grid, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")

        field(left, "API ID", api_id_var, placeholder="12345678")
        field(left, "Channel ID", channel_var, placeholder="-1001234567890")
        field(right, "API HASH", api_hash_var, True, "API hash")
        field(right, "Channel / Bot Token", token_var, True, "Optional")

        # Security
        sec = section(scroll, "App Security", "PASSWORD")
        current_var = ctk.StringVar()
        new_var = ctk.StringVar()
        confirm_var = ctk.StringVar()
        field(sec, "Current password", current_var, True)
        field(sec, "New password", new_var, True, "Minimum 4 characters")
        field(sec, "Confirm new password", confirm_var, True)
        ctk.CTkLabel(
            sec,
            text="Leave all three password fields empty to keep your current password.",
            text_color="#7188A2", font=ctk.CTkFont(size=10)
        ).pack(anchor="w", padx=18, pady=(4, 12))

        async def load_account():
            try:
                if self.client and await self.client.is_user_authorized():
                    me = await self.client.get_me()
                    number = getattr(me, "phone", None)
                    display = f"+{number}" if number and not str(number).startswith("+") else (number or "Not available")
                    self.phone = display
                    self.after(0, lambda: phone_var.set(display))
            except Exception as e:
                safe_log(f"Settings account lookup failed: {e}")
        if self.client:
            self.run_async(load_account())

        def save_settings():
            global API_ID, API_HASH, CHANNEL_ID, BOT_TOKEN, APP_PASSWORD
            try:
                new_api_id = int(api_id_var.get().strip())
                new_api_hash = api_hash_var.get().strip()
                new_channel = int(channel_var.get().strip())
                new_token = token_var.get().strip()

                if new_api_id <= 0 or not new_api_hash or new_channel == 0:
                    raise ValueError("API ID, API HASH and Channel ID are required.")

                if new_var.get() or confirm_var.get() or current_var.get():
                    if current_var.get() != APP_PASSWORD:
                        raise ValueError("Current app password is incorrect.")
                    if len(new_var.get()) < 4 or new_var.get() == "1234":
                        raise ValueError("New password must be at least 4 characters and cannot be 1234.")
                    if new_var.get() != confirm_var.get():
                        raise ValueError("New password and confirmation do not match.")
                    APP_PASSWORD = new_var.get()

                old_api = (API_ID, API_HASH)
                old_channel = CHANNEL_ID
                API_ID, API_HASH, CHANNEL_ID, BOT_TOKEN = (
                    new_api_id, new_api_hash, new_channel, new_token
                )

                cfg = load_config()
                cfg.update({
                    "api_id": API_ID,
                    "api_hash": API_HASH,
                    "channel_id": CHANNEL_ID,
                    "bot_token": BOT_TOKEN,
                    "app_password": APP_PASSWORD
                })
                if not save_config(cfg):
                    raise OSError("Settings could not be saved. Check write permissions.")

                if old_channel != CHANNEL_ID and self.client and self.client.is_connected():
                    self.run_async(self._apply_channel_change())

                win.destroy()
                self.show_themed_info(
                    "Settings Saved",
                    "Your settings were updated successfully.",
                    "success"
                )
            except Exception as e:
                self.show_themed_info("Settings Error", str(e), "error")

        footer = ctk.CTkFrame(win, fg_color="#0A1626", corner_radius=12)
        footer.pack(fill="x", padx=18, pady=(8, 18))

        ctk.CTkButton(
            footer, text="⇥  Logout",
            width=120, height=40, corner_radius=10,
            fg_color="#6B2BC4", hover_color="#5821A7",
            border_width=1, border_color="#8746E8",
            command=self.logout_telegram
        ).pack(side="left", padx=10, pady=10)

        ctk.CTkButton(
            footer, text="Cancel",
            width=110, height=40, corner_radius=9,
            fg_color="#142A45", hover_color="#204064",
            border_width=1, border_color="#2A4D76",
            command=win.destroy
        ).pack(side="right", padx=5, pady=10)

        ctk.CTkButton(
            footer, text="Save Changes",
            width=140, height=40, corner_radius=9,
            fg_color="#1984FF", hover_color="#0F6FE0",
            command=save_settings
        ).pack(side="right", padx=10, pady=10)

        # Center the modal on the application window.
        win.update_idletasks()
        try:
            x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 2
            win.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

    async def _apply_channel_change(self):
        try:
            self.channel = await self.resolve_channel()
            try:
                self.client.remove_event_handler(self._on_new_message)
            except Exception:
                pass
            self.client.add_event_handler(self._on_new_message, events.NewMessage(chats=self.channel))
            self.after(0, self.refresh_ui)
        except Exception as e:
            safe_log(f"Channel settings apply failed: {e}")
            self.after(0, lambda err=str(e): self.show_themed_info("Channel Error", err, "error"))

    # ========================================================
    # TELEGRAM LOGOUT & CLEAN SHUTDOWN
    # ========================================================
    def logout_telegram(self):
        self.show_themed_confirm(
            "Confirm Logout",
            "Disconnect this Telegram account and invalidate the saved session?\nYour local index will remain safe.",
            lambda: self.run_async(self._logout_worker())
        )

    async def _logout_worker(self):
        try:
            if self.client:
                try:
                    await self.client.log_out()
                except Exception:
                    pass
                try:
                    await self.client.disconnect()
                except Exception:
                    pass

            self.client = None
            self.channel = None

            for s_path in [SESSION, SESSION + ".session"]:
                if os.path.exists(s_path):
                    try:
                        os.remove(s_path)
                    except Exception as e:
                        safe_log(f"Session cleanup failed: {e}")

            self.after(0, self.show_phone_login)
            self.set_connection_status("Disconnected")
        except Exception as e:
            err = str(e)
            self.after(0, lambda: messagebox.showerror("Logout Error", err))

    def clear_root_frame(self):
        for widget in self.winfo_children():
            widget.destroy()

    def close_app(self):
        if self.destroyed:
            return
        self.destroyed = True

        async def shutdown():
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            self.loop.stop()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        except Exception:
            pass
        self.destroy()

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()
