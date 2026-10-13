"""
Database module for user management, role-based access, and per-user settings.
Connects to MongoDB Cloud via MONGODB_URI from .env, with an automatic
graceful fallback to local persistent JSON storage if MongoDB is unreachable or unconfigured.
"""
import os
import json
import logging
import threading
from datetime import datetime
from typing import Optional, Dict, List, Any
from pathlib import Path
import bcrypt

from common.paths import get_base_data_dir
from common.runtime_config import get_config_value

logger = logging.getLogger("db")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_DB_CLIENT = None
_DB_INSTANCE = None
_MONGO_CONNECTED = False
_LOCK = threading.RLock()


def _get_local_store_path() -> Path:
    p = get_base_data_dir() / "users_store.json"
    return p


def _read_local_store() -> Dict[str, Any]:
    path = _get_local_store_path()
    if not path.exists():
        return {"users": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Error reading local users store: {e}")
        return {"users": {}}


def _write_local_store(data: Dict[str, Any]):
    path = _get_local_store_path()
    try:
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        logger.error(f"Error saving local users store: {e}")


def get_db():
    """Initializes and returns the MongoDB database or None if using local storage."""
    global _DB_CLIENT, _DB_INSTANCE, _MONGO_CONNECTED

    with _LOCK:
        if _MONGO_CONNECTED and _DB_INSTANCE is not None:
            return _DB_INSTANCE

        mongo_uri = get_config_value("MONGODB_URI", default="").strip("\"' ")
        if not mongo_uri:
            mongo_uri = os.environ.get("MONGODB_URI", "").strip("\"' ")

        db_name = get_config_value("DB_NAME", default="NAUKRI_BOT").strip("\"' ")
        if not db_name:
            db_name = os.environ.get("DB_NAME", "NAUKRI_BOT").strip("\"' ")

        if mongo_uri:
            try:
                import pymongo
                client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=3500)
                # Test connectivity
                client.server_info()
                _DB_CLIENT = client
                _DB_INSTANCE = client[db_name]
                _MONGO_CONNECTED = True
                logger.info(f"Connected to MongoDB Cloud (Database: {db_name})")
                return _DB_INSTANCE
            except Exception as e:
                logger.warning(f"MongoDB connection failed: {e}. Falling back to local storage.")
                _MONGO_CONNECTED = False
                _DB_INSTANCE = None

        return None


def hash_password(plain_password: str) -> str:
    """Hashes a password using bcrypt with salt."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain_password.strip().encode("utf-8"), salt).decode("utf-8")


def verify_password_hash(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plain password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain_password.strip().encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception as e:
        logger.error(f"Error verifying password hash: {e}")
        return False


def get_user(username: str) -> Optional[Dict[str, Any]]:
    """Retrieves a user document by username."""
    u_clean = username.strip().lower()
    db = get_db()
    if db is not None:
        try:
            doc = db.users.find_one({"username": u_clean})
            if doc:
                doc["_id"] = str(doc["_id"])
                return doc
        except Exception as e:
            logger.warning(f"MongoDB get_user error: {e}. Falling back to local.")

    # Local fallback
    store = _read_local_store()
    return store.get("users", {}).get(u_clean)


def create_user(
    username: str,
    plain_password: str,
    role: str = "user",
    delay_seconds: int = 5,
    email: str = ""
) -> Dict[str, Any]:
    """
    Creates a new user with hashed password and initial zero counters.
    Role can be 'admin' or 'user'.
    """
    u_clean = username.strip().lower()
    if not u_clean:
        raise ValueError("Username cannot be empty")
    if len(plain_password.strip()) < 4:
        raise ValueError("Password must be at least 4 characters")

    existing = get_user(u_clean)
    if existing:
        raise ValueError(f"User '{username}' already exists")

    hashed = hash_password(plain_password)
    delay_safe = max(2, int(delay_seconds))

    user_doc = {
        "username": u_clean,
        "password_hash": hashed,
        "role": role if role in ("admin", "user") else "user",
        "delay_seconds": delay_safe,
        "status": "Active",
        "email": email.strip(),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session_metadata": {
            "naukri": {"logged_in": False, "last_verified": None},
            "linkedin": {"logged_in": False, "last_verified": None}
        },
        "sheet_config": {
            "webhook_url": "",
            "sheet_url": "",
            "enabled": False
        }
    }

    db = get_db()
    if db is not None:
        try:
            db.users.update_one({"username": u_clean}, {"$set": user_doc}, upsert=True)
            logger.info(f"Created user '{u_clean}' in MongoDB.")
        except Exception as e:
            logger.warning(f"Failed to create user in MongoDB: {e}. Saving locally.")

    # Always ensure local copy exists as backup
    with _LOCK:
        store = _read_local_store()
        store.setdefault("users", {})[u_clean] = user_doc
        _write_local_store(store)

    return user_doc


def list_users() -> List[Dict[str, Any]]:
    """Returns a list of all users without sensitive password hashes."""
    users_list = []
    db = get_db()
    if db is not None:
        try:
            for doc in db.users.find():
                doc["_id"] = str(doc["_id"])
                doc.pop("password_hash", None)
                users_list.append(doc)
            if users_list:
                return users_list
        except Exception as e:
            logger.warning(f"MongoDB list_users error: {e}")

    # Local fallback
    store = _read_local_store()
    for u, data in store.get("users", {}).items():
        clean_item = dict(data)
        clean_item.pop("password_hash", None)
        users_list.append(clean_item)
    return users_list


def delete_user(username: str) -> bool:
    """Deletes a user account. Admin user cannot be deleted."""
    u_clean = username.strip().lower()
    user = get_user(u_clean)
    if not user:
        return False
    if user.get("role") == "admin":
        raise ValueError("Cannot delete the primary admin account")

    db = get_db()
    if db is not None:
        try:
            db.users.delete_one({"username": u_clean})
        except Exception as e:
            logger.warning(f"MongoDB delete_user error: {e}")

    with _LOCK:
        store = _read_local_store()
        if u_clean in store.get("users", {}):
            del store["users"][u_clean]
            _write_local_store(store)
    return True


def update_user_settings(
    username: str,
    delay_seconds: Optional[int] = None,
    sheet_config: Optional[Dict[str, Any]] = None,
    session_metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """Updates user delay (min safe 2s), Google sheet sync, or session metadata."""
    u_clean = username.strip().lower()
    user = get_user(u_clean)
    if not user:
        return False

    updates = {}
    if delay_seconds is not None:
        updates["delay_seconds"] = max(2, int(delay_seconds))
    if sheet_config is not None:
        current_sheet = user.get("sheet_config", {})
        current_sheet.update(sheet_config)
        updates["sheet_config"] = current_sheet
    if session_metadata is not None:
        current_meta = user.get("session_metadata", {})
        current_meta.update(session_metadata)
        updates["session_metadata"] = current_meta

    if not updates:
        return True

    db = get_db()
    if db is not None:
        try:
            db.users.update_one({"username": u_clean}, {"$set": updates})
        except Exception as e:
            logger.warning(f"MongoDB update user settings error: {e}")

    with _LOCK:
        store = _read_local_store()
        if u_clean in store.get("users", {}):
            store["users"][u_clean].update(updates)
            _write_local_store(store)
    return True


def get_user_settings(username: str) -> Dict[str, Any]:
    """Retrieves user settings including delay and sheet configuration."""
    user = get_user(username)
    if not user:
        return {
            "delay_seconds": 5,
            "sheet_config": {"webhook_url": "", "sheet_url": "", "enabled": False}
        }
    return {
        "delay_seconds": user.get("delay_seconds", 5),
        "sheet_config": user.get("sheet_config", {"webhook_url": "", "sheet_url": "", "enabled": False}),
        "role": user.get("role", "user"),
        "created_at": user.get("created_at", "")
    }


def get_user_profile(username: str) -> Optional[Dict[str, Any]]:
    """Retrieves the profile document for a user."""
    u_clean = username.strip().lower()
    db = get_db()
    if db is not None:
        try:
            doc = db.user_profiles.find_one({"username": u_clean})
            if doc:
                doc["_id"] = str(doc["_id"])
                return doc
        except Exception as e:
            logger.warning(f"MongoDB get_user_profile error: {e}. Checking local.")

    # Local fallback
    store = _read_local_store()
    return store.get("profiles", {}).get(u_clean)


def save_user_profile(username: str, profile_data: Dict[str, Any]) -> bool:
    """Saves or updates profile document for a user."""
    u_clean = username.strip().lower()
    if not u_clean:
        return False

    data = dict(profile_data)
    data["username"] = u_clean
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    db = get_db()
    if db is not None:
        try:
            db.user_profiles.update_one({"username": u_clean}, {"$set": data}, upsert=True)
            logger.info(f"Saved profile for user '{u_clean}' to MongoDB.")
        except Exception as e:
            logger.warning(f"MongoDB save_user_profile error: {e}. Saving locally.")

    with _LOCK:
        store = _read_local_store()
        store.setdefault("profiles", {})[u_clean] = data
        _write_local_store(store)
    return True


def save_user_resume_meta(username: str, filename: str, file_path: str, size_bytes: int) -> bool:
    """Updates resume metadata in user profile."""
    u_clean = username.strip().lower()
    meta = {
        "resume_file_name": filename,
        "resume_path": file_path,
        "resume_size_bytes": size_bytes,
        "resume_uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    db = get_db()
    if db is not None:
        try:
            db.user_profiles.update_one({"username": u_clean}, {"$set": meta}, upsert=True)
        except Exception as e:
            logger.warning(f"MongoDB save_user_resume_meta error: {e}")

    with _LOCK:
        store = _read_local_store()
        prof = store.setdefault("profiles", {}).setdefault(u_clean, {"username": u_clean})
        prof.update(meta)
        _write_local_store(store)
    return True


def is_user_profile_complete(username: str) -> tuple[bool, str, List[str]]:
    """
    Validates whether the user's profile has all required fields and uploaded resume.
    Admin users return (True, "", []) because their configuration is in config/profile.yaml.
    Non-admin users must have experience, notice, CTC, target roles, location, and uploaded resume.
    """
    u_clean = username.strip().lower()
    if not u_clean or u_clean == "admin":
        return True, "", []

    user = get_user(u_clean)
    if user and user.get("role") == "admin":
        return True, "", []

    prof = get_user_profile(u_clean)
    if not prof:
        return False, "Profile has not been created yet.", ["profile_created"]

    missing = []

    # Required personal & location details
    if not prof.get("current_city"):
        missing.append("Current City")

    # Required experience and job search
    if prof.get("total_experience_years") is None or prof.get("total_experience_years") == "":
        missing.append("Total Experience (Years)")

    roles = prof.get("target_roles")
    if not roles or (isinstance(roles, list) and len(roles) == 0):
        missing.append("Target Roles")

    if prof.get("notice_period_days") is None or prof.get("notice_period_days") == "":
        missing.append("Notice Period (Days)")

    if prof.get("current_ctc_lpa") is None or prof.get("current_ctc_lpa") == "":
        missing.append("Current CTC (LPA)")

    if prof.get("expected_ctc_lpa") is None or prof.get("expected_ctc_lpa") == "":
        missing.append("Expected CTC (LPA)")

    # Resume verification (file must be uploaded and exist on disk)
    resume_path = prof.get("resume_path")
    if not resume_path or not Path(resume_path).exists():
        missing.append("Resume PDF Upload")

    if missing:
        msg = f"Missing required fields: {', '.join(missing)}"
        return False, msg, missing

    return True, "", []



def init_db():
    """Initializes DB connection and ensures default Admin user exists."""
    admin_user = get_config_value("ADMIN_USERNAME", default="admin").strip().lower()
    admin_pass = get_config_value("ADMIN_PASSWORD", default="admin123").strip()

    existing_admin = get_user(admin_user)
    if not existing_admin:
        logger.info(f"Seeding default admin user '{admin_user}'...")
        create_user(admin_user, admin_pass, role="admin", delay_seconds=5)
    else:
        logger.info(f"Admin user '{admin_user}' verified.")


# Initialize on import
try:
    init_db()
except Exception as _e:
    logger.warning(f"DB initialization warning: {_e}")
