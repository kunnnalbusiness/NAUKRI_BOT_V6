"""
MongoDB Connection Manager, Index Initializer, and Persistent Store.
Provides connection pooling, Atlas network access guidance, and automatic graceful
fallback to local persistent disk storage when MongoDB is unconfigured or unreachable.
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any
import pymongo
from pymongo.errors import ServerSelectionTimeoutError, ConfigurationError

from backend.app.core.config import settings
from backend.app.core.security import hash_password, encrypt_text, decrypt_text

logger = logging.getLogger("db.mongo")

_DB_CLIENT: Optional[pymongo.MongoClient] = None
_DB_INSTANCE: Optional[Any] = None
_MONGO_CONNECTED: bool = False
_LOCK = threading.RLock()


def get_local_store_path() -> Path:
    p = settings.DATA_DIR / "users_store.json"
    return p


def _read_local_store() -> Dict[str, Any]:
    path = get_local_store_path()
    if not path.exists():
        return {"users": {}, "profiles": {}, "sessions": {}, "activity": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Error reading local store: {e}")
        return {"users": {}, "profiles": {}, "sessions": {}, "activity": []}


def _write_local_store(data: Dict[str, Any]):
    path = get_local_store_path()
    try:
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        logger.error(f"Error saving local store: {e}")


def get_db():
    """Initializes and returns the MongoDB database or None if using local storage."""
    global _DB_CLIENT, _DB_INSTANCE, _MONGO_CONNECTED

    with _LOCK:
        if _MONGO_CONNECTED and _DB_INSTANCE is not None:
            return _DB_INSTANCE

        mongo_uri = settings.MONGODB_URI
        if mongo_uri:
            try:
                client = pymongo.MongoClient(
                    mongo_uri,
                    serverSelectionTimeoutMS=4000,
                    connectTimeoutMS=4000,
                    maxPoolSize=20,
                    minPoolSize=2,
                    retryWrites=True,
                )
                # Test connectivity
                client.server_info()
                _DB_CLIENT = client
                _DB_INSTANCE = client[settings.DB_NAME]
                _MONGO_CONNECTED = True
                logger.info(
                    f"Successfully connected to MongoDB Atlas (Database: {settings.DB_NAME})"
                )
                _ensure_indexes(_DB_INSTANCE)
                return _DB_INSTANCE
            except (ServerSelectionTimeoutError, ConfigurationError) as e:
                logger.error(
                    "=========================================================================\n"
                    "MONGODB CONNECTION NOTICE:\n"
                    f"Could not connect to MongoDB URI. Error: {e}\n"
                    "If using MongoDB Atlas on Render, make sure to add IP '0.0.0.0/0' to\n"
                    "MongoDB Atlas -> Network Access -> IP Access List.\n"
                    "========================================================================="
                )
                if settings.ENV == "production":
                    raise RuntimeError(
                        f"CRITICAL: Failed to connect to MongoDB Atlas in production environment ({e}). "
                        "Local JSON fallback is strictly disabled in production. "
                        "Verify MONGODB_URI and IP Access List (0.0.0.0/0) in Atlas."
                    )
                logger.warning(
                    "Falling back to local persistent disk storage (/data/users_store.json) for development/local mode."
                )
                _MONGO_CONNECTED = False
                _DB_INSTANCE = None
            except Exception as e:
                if settings.ENV == "production":
                    raise RuntimeError(
                        f"CRITICAL: MongoDB error in production mode: {e}"
                    )
                logger.warning(
                    f"MongoDB connection failed: {e}. Falling back to local persistent store."
                )
                _MONGO_CONNECTED = False
                _DB_INSTANCE = None
        else:
            if settings.ENV == "production":
                raise RuntimeError(
                    "CRITICAL: MONGODB_URI is required in production mode. Local JSON fallback disabled."
                )

        return None


# ---------------------------------------------------------------------------
# GridFS Resume File Storage (Persistent across restarts)
# ---------------------------------------------------------------------------


def save_resume_gridfs(username: str, filename: str, content: bytes) -> Optional[str]:
    """Saves PDF resume bytes into MongoDB GridFS (resilient to ephemeral disk wipes)."""
    db = get_db()
    if db is None:
        return None
    try:
        import gridfs

        fs = gridfs.GridFS(db, collection="resumes_fs")
        for old in fs.find({"username": username}):
            fs.delete(old._id)
        file_id = fs.put(
            content, filename=filename, username=username, uploaded_at=datetime.utcnow()
        )
        logger.info(
            f"Saved resume for user '{username}' into MongoDB GridFS (ID: {file_id})"
        )
        return str(file_id)
    except Exception as e:
        logger.warning(f"Failed to store resume in GridFS: {e}")
        return None


def get_resume_gridfs(username: str) -> Optional[tuple]:
    """Retrieves PDF resume bytes and filename from MongoDB GridFS."""
    db = get_db()
    if db is None:
        return None
    try:
        import gridfs

        fs = gridfs.GridFS(db, collection="resumes_fs")
        doc = fs.find_one({"username": username}, sort=[("uploaded_at", -1)])
        if doc:
            return doc.filename, doc.read()
        return None
    except Exception as e:
        logger.warning(f"Failed to read resume from GridFS: {e}")
        return None


def _ensure_indexes(db):
    """Creates database indexes for fast queries and uniqueness constraints."""
    try:
        db.users.create_index("username", unique=True)
        db.user_profiles.create_index("username", unique=True)
        db.sessions.create_index([("username", 1), ("site", 1)], unique=True)
        db.activity.create_index([("username", 1), ("timestamp", -1)])
        logger.info("MongoDB collection indexes verified.")
    except Exception as e:
        logger.warning(f"Index creation warning: {e}")


# ---------------------------------------------------------------------------
# User Data Access
# ---------------------------------------------------------------------------


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
            logger.warning(f"MongoDB get_user error: {e}. Falling back to local store.")

    store = _read_local_store()
    return store.get("users", {}).get(u_clean)


def create_user(
    username: str,
    plain_password: str,
    role: str = "user",
    delay_seconds: int = 5,
    email: str = "",
) -> Dict[str, Any]:
    """Creates a new user with hashed password and initial zero counters."""
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
        "total_applied": 0,
        "session_metadata": {
            "naukri": {"logged_in": False, "last_verified": None},
            "linkedin": {"logged_in": False, "last_verified": None},
        },
        "sheet_config": {"webhook_url": "", "sheet_url": "", "enabled": False},
    }

    # Ensure complete clean slate for newly created user across disk, db, and memory
    try:
        from common.paths import get_cache_path, get_session_path, RESUMES_DIR
        # Clean local cache file
        cache_path = get_cache_path(".applied_cache.json", username=u_clean)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"applied_keys": [], "history": []}, indent=2), encoding="utf-8")

        # Clean old session files on disk
        for site in ("naukri", "linkedin"):
            sp = get_session_path(site, username=u_clean)
            if sp.exists():
                sp.unlink()

        # Clean old resume files on disk
        resume_p = RESUMES_DIR / f"{u_clean}_resume.pdf"
        if resume_p.exists():
            resume_p.unlink()

        # Clear in-memory session cache
        from common.login_helper import _SESSION_CACHE, _get_cache_key
        for site in ("naukri", "linkedin"):
            _SESSION_CACHE.pop(_get_cache_key(site, username=u_clean), None)

        # Clear engine manager user tracking
        from common.engine_manager import engine_manager
        engine_manager.reset_daily_limit("naukri", username=u_clean)
        engine_manager.reset_daily_limit("linkedin", username=u_clean)
        with engine_manager._lock:
            if u_clean in engine_manager._user_applied_counts:
                engine_manager._user_applied_counts[u_clean] = {"naukri": 0, "linkedin": 0}
            if engine_manager._engine_active_user.get("naukri") == u_clean:
                engine_manager._engine_active_user["naukri"] = None
            if engine_manager._engine_active_user.get("linkedin") == u_clean:
                engine_manager._engine_active_user["linkedin"] = None
    except Exception as _ce:
        logger.debug(f"User '{u_clean}' initialization notice: {_ce}")

    db = get_db()
    if db is not None:
        try:
            db.users.update_one({"username": u_clean}, {"$set": user_doc}, upsert=True)
            db.user_profiles.delete_one({"username": u_clean})
            db.sessions.delete_many({"username": u_clean})
            db.activity.delete_many({"username": u_clean})
            try:
                import gridfs
                fs = gridfs.GridFS(db, collection="resumes_fs")
                for old in fs.find({"username": u_clean}):
                    fs.delete(old._id)
            except Exception:
                pass
            logger.info(f"Created user '{u_clean}' in MongoDB.")
        except Exception as e:
            logger.warning(
                f"Failed to create user in MongoDB: {e}. Saving to local store."
            )

    with _LOCK:
        store = _read_local_store()
        store.setdefault("users", {})[u_clean] = user_doc
        if u_clean in store.get("profiles", {}):
            del store["profiles"][u_clean]
        for s in ("naukri", "linkedin"):
            store.get("sessions", {}).pop(f"{u_clean}_{s}", None)
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

    store = _read_local_store()
    for _, data in store.get("users", {}).items():
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
    if user.get("role") == "admin" or u_clean == settings.ADMIN_USERNAME:
        raise ValueError("Cannot delete the primary admin account")

    # Clean disk files
    try:
        from common.paths import get_cache_path, get_session_path, RESUMES_DIR
        cache_path = get_cache_path(".applied_cache.json", username=u_clean)
        if cache_path.exists():
            cache_path.unlink()
        for site in ("naukri", "linkedin"):
            sp = get_session_path(site, username=u_clean)
            if sp.exists():
                sp.unlink()
        resume_p = RESUMES_DIR / f"{u_clean}_resume.pdf"
        if resume_p.exists():
            resume_p.unlink()

        from common.login_helper import _SESSION_CACHE, _get_cache_key
        for site in ("naukri", "linkedin"):
            _SESSION_CACHE.pop(_get_cache_key(site, username=u_clean), None)

        from common.engine_manager import engine_manager
        engine_manager.reset_daily_limit("naukri", username=u_clean)
        engine_manager.reset_daily_limit("linkedin", username=u_clean)
        with engine_manager._lock:
            if u_clean in engine_manager._user_applied_counts:
                del engine_manager._user_applied_counts[u_clean]
            if engine_manager._engine_active_user.get("naukri") == u_clean:
                engine_manager._engine_active_user["naukri"] = None
            if engine_manager._engine_active_user.get("linkedin") == u_clean:
                engine_manager._engine_active_user["linkedin"] = None
    except Exception as _de:
        logger.debug(f"User cleanup notice: {_de}")

    db = get_db()
    if db is not None:
        try:
            db.users.delete_one({"username": u_clean})
            db.user_profiles.delete_one({"username": u_clean})
            db.sessions.delete_many({"username": u_clean})
            db.activity.delete_many({"username": u_clean})
            try:
                import gridfs
                fs = gridfs.GridFS(db, collection="resumes_fs")
                for old in fs.find({"username": u_clean}):
                    fs.delete(old._id)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"MongoDB delete_user error: {e}")

    with _LOCK:
        store = _read_local_store()
        if u_clean in store.get("users", {}):
            del store["users"][u_clean]
        if u_clean in store.get("profiles", {}):
            del store["profiles"][u_clean]
        for s in ("naukri", "linkedin"):
            store.get("sessions", {}).pop(f"{u_clean}_{s}", None)
        _write_local_store(store)
    return True


def update_user_settings(
    username: str,
    delay_seconds: Optional[int] = None,
    stream_fps: Optional[int] = None,
    sheet_config: Optional[Dict[str, Any]] = None,
    session_metadata: Optional[Dict[str, Any]] = None,
    increment_applied: Optional[int] = None,
) -> bool:
    """Updates user delay (min safe 2s), stream FPS (1-60), Google sheet sync, or session metadata."""
    u_clean = username.strip().lower()
    user = get_user(u_clean)
    if not user:
        return False

    updates = {}
    if delay_seconds is not None:
        updates["delay_seconds"] = max(2, int(delay_seconds))
    if stream_fps is not None:
        updates["stream_fps"] = max(1, min(60, int(stream_fps)))
    if sheet_config is not None:
        current_sheet = user.get("sheet_config", {})
        current_sheet.update(sheet_config)
        updates["sheet_config"] = current_sheet
    if session_metadata is not None:
        current_meta = user.get("session_metadata", {})
        current_meta.update(session_metadata)
        updates["session_metadata"] = current_meta
    if increment_applied:
        updates["total_applied"] = user.get("total_applied", 0) + increment_applied

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
    """Retrieves user settings including delay, stream FPS, and sheet configuration."""
    user = get_user(username)
    if not user:
        return {
            "delay_seconds": 5,
            "stream_fps": 15,
            "sheet_config": {"webhook_url": "", "sheet_url": "", "enabled": False},
            "role": "user",
            "total_applied": 0,
        }
    return {
        "delay_seconds": user.get("delay_seconds", 5),
        "stream_fps": user.get("stream_fps", 15),
        "sheet_config": user.get(
            "sheet_config", {"webhook_url": "", "sheet_url": "", "enabled": False}
        ),
        "role": user.get("role", "user"),
        "total_applied": user.get("total_applied", 0),
        "created_at": user.get("created_at", ""),
    }


# ---------------------------------------------------------------------------
# Profile Data Access
# ---------------------------------------------------------------------------


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
            logger.warning(
                f"MongoDB get_user_profile error: {e}. Checking local store."
            )

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
            db.user_profiles.update_one(
                {"username": u_clean}, {"$set": data}, upsert=True
            )
            logger.info(f"Saved profile for user '{u_clean}' to MongoDB.")
        except Exception as e:
            logger.warning(
                f"MongoDB save_user_profile error: {e}. Saving to local store."
            )

    with _LOCK:
        store = _read_local_store()
        store.setdefault("profiles", {})[u_clean] = data
        _write_local_store(store)
    return True


def save_user_resume_meta(
    username: str, filename: str, file_path: str, size_bytes: int
) -> bool:
    """Updates resume metadata in user profile."""
    u_clean = username.strip().lower()
    meta = {
        "resume_file_name": filename,
        "resume_path": file_path,
        "resume_size_bytes": size_bytes,
        "resume_uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    db = get_db()
    if db is not None:
        try:
            db.user_profiles.update_one(
                {"username": u_clean}, {"$set": meta}, upsert=True
            )
        except Exception as e:
            logger.warning(f"MongoDB save_user_resume_meta error: {e}")

    with _LOCK:
        store = _read_local_store()
        prof = store.setdefault("profiles", {}).setdefault(
            u_clean, {"username": u_clean}
        )
        prof.update(meta)
        _write_local_store(store)
    return True


def is_user_profile_complete(username: str) -> tuple[bool, str, List[str]]:
    """Validates whether the user's profile has all required fields and uploaded resume."""
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
    if not prof.get("current_city"):
        missing.append("Current City")

    if (
        prof.get("total_experience_years") is None
        or prof.get("total_experience_years") == ""
    ):
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

    resume_path = prof.get("resume_path")
    if not resume_path or not Path(resume_path).exists():
        missing.append("Resume PDF Upload")

    if missing:
        msg = f"Missing required fields: {', '.join(missing)}"
        return False, msg, missing

    return True, "", []


# ---------------------------------------------------------------------------
# Encrypted Session Cloud Storage (Prevents Session Loss on Render Restart)
# ---------------------------------------------------------------------------


def save_encrypted_session(username: str, site: str, session_json_str: str) -> bool:
    """Encrypts and backs up browser session cookies to MongoDB."""
    u_clean = username.strip().lower()
    encrypted = encrypt_text(session_json_str)

    data = {
        "username": u_clean,
        "site": site.lower(),
        "encrypted_payload": encrypted,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    db = get_db()
    if db is not None:
        try:
            db.sessions.update_one(
                {"username": u_clean, "site": site.lower()}, {"$set": data}, upsert=True
            )
            return True
        except Exception as e:
            logger.warning(f"Error backing up session to MongoDB: {e}")

    with _LOCK:
        store = _read_local_store()
        key = f"{u_clean}_{site.lower()}"
        store.setdefault("sessions", {})[key] = data
        _write_local_store(store)
    return True


def get_decrypted_session(username: str, site: str) -> Optional[str]:
    """Retrieves and decrypts browser session cookies from MongoDB."""
    u_clean = username.strip().lower()
    db = get_db()
    if db is not None:
        try:
            doc = db.sessions.find_one({"username": u_clean, "site": site.lower()})
            if doc and doc.get("encrypted_payload"):
                return decrypt_text(doc["encrypted_payload"])
        except Exception as e:
            logger.warning(f"Error reading session from MongoDB: {e}")

    store = _read_local_store()
    key = f"{u_clean}_{site.lower()}"
    saved = store.get("sessions", {}).get(key)
    if saved and saved.get("encrypted_payload"):
        return decrypt_text(saved["encrypted_payload"])
    return None


def init_db():
    """Initializes DB connection, ensures indexes, and creates default Admin user."""
    admin_user = settings.ADMIN_USERNAME
    admin_pass = settings.ADMIN_PASSWORD

    existing_admin = get_user(admin_user)
    if not existing_admin:
        logger.info(f"Seeding primary admin user '{admin_user}'...")
        create_user(admin_user, admin_pass, role="admin", delay_seconds=5)
    else:
        logger.info(f"Primary admin user '{admin_user}' verified.")


# Initialize DB on module import
try:
    init_db()
except Exception as _e:
    logger.warning(f"Initial DB check warning: {_e}")
