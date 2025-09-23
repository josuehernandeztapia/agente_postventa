import hashlib
import os
from pathlib import Path
from typing import Any, Optional

POSTGRES_URL = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

def _conn():
    if not POSTGRES_URL:
        return None
    try:
        import psycopg
        return psycopg.connect(POSTGRES_URL)
    except Exception:
        return None

def ensure_schema() -> bool:
    """Create minimal schema if not exists. Returns True if executed or already fine."""
    con = _conn()
    if not con:
        return False
    try:
        p = ROOT_DIR / 'schema.sql'
        sql = ''
        with p.open('r', encoding='utf-8') as f:
            sql = f.read()
        with con:
            with con.cursor() as cur:
                cur.execute(sql)
        return True
    except Exception:
        try:
            con.close()
        except Exception:
            pass
        return False

def create_case(source: str, client_id: Optional[str] = None, contract_id: Optional[str] = None) -> Optional[str]:
    con = _conn()
    if not con:
        return None
    try:
        ensure_schema()
        with con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO cases (source, client_id, contract_id) VALUES (%s,%s,%s) RETURNING id",
                    (source, client_id, contract_id)
                )
                row = cur.fetchone()
                return str(row[0]) if row else None
    finally:
        try:
            con.close()
        except Exception:
            pass

def upsert_case_meta(case_id: str, **fields: Any) -> bool:
    con = _conn()
    if not con:
        return False
    try:
        ensure_schema()
        sets = []
        vals = []
        for k, v in fields.items():
            sets.append(f"{k}=%s")
            vals.append(v)
        vals.append(case_id)
        sql = f"UPDATE cases SET {', '.join(sets)}, updated_at=now() WHERE id=%s"
        with con:
            with con.cursor() as cur:
                cur.execute(sql, vals)
        return True
    except Exception:
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass

def _attachment_hash(url: str, meta: Optional[dict]) -> str:
    base = (url or "").strip()
    if not base:
        return hashlib.sha256(b"" ).hexdigest()
    extra = ""
    if isinstance(meta, dict):
        for key in ("hash", "object_key", "etag", "checksum"):
            val = meta.get(key)
            if val:
                extra = f"|{val}"
                break
    payload = (base + extra).encode("utf-8", "ignore")
    return hashlib.sha256(payload).hexdigest()


def add_attachment(case_id: str, kind: str, url: str, ocr: Optional[dict] = None, meta: Optional[dict] = None) -> dict | bool:
    con = _conn()
    if not con:
        return False
    try:
        ensure_schema()
        url_hash = None
        if isinstance(meta, dict) and meta.get("hash"):
            url_hash = str(meta.get("hash"))
        if not url_hash:
            url_hash = _attachment_hash(url, meta)
        inserted = None
        with con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO case_attachments (case_id, kind, url, ocr, meta, url_hash)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (case_id, url_hash) DO NOTHING
                    RETURNING id
                    """,
                    (case_id, kind, url, ocr, meta, url_hash)
                )
                inserted = cur.fetchone()
        return {"inserted": bool(inserted), "url_hash": url_hash}
    except Exception:
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass

def add_action(case_id: str, action: str, payload: Optional[dict] = None, created_by: Optional[str] = None) -> bool:
    con = _conn()
    if not con:
        return False
    try:
        ensure_schema()
        with con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO case_actions (case_id, action, payload, created_by) VALUES (%s,%s,%s,%s)",
                    (case_id, action, payload, created_by)
                )
        return True
    except Exception:
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass

def get_case(case_id: str) -> Optional[dict]:
    con = _conn()
    if not con:
        return None
    try:
        with con:
            with con.cursor() as cur:
                cur.execute("SELECT id, source, client_id, contract_id, vin, plate, odo_km, falla_tipo, falla_subtipo, warranty_status, status, created_at, updated_at FROM cases WHERE id=%s", (case_id,))
                row = cur.fetchone()
                if not row:
                    return None
                cols = ["id","source","client_id","contract_id","vin","plate","odo_km","falla_tipo","falla_subtipo","warranty_status","status","created_at","updated_at"]
                return {k:v for k,v in zip(cols, row)}
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass

def list_attachments(case_id: str) -> list[dict]:
    con = _conn()
    if not con:
        return []
    try:
        with con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    SELECT kind, url, ocr, meta, created_at, url_hash
                    FROM case_attachments
                    WHERE case_id=%s
                    ORDER BY created_at DESC
                    """,
                    (case_id,)
                )
                rows = cur.fetchall() or []
                out = []
                cols = ["kind","url","ocr","meta","created_at","url_hash"]
                for r in rows:
                    item = {k: v for k, v in zip(cols, r)}
                    ts = item.get("created_at")
                    if hasattr(ts, "isoformat"):
                        item["created_at"] = ts.isoformat()
                    out.append(item)
                return out
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass
