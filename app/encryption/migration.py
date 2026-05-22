"""
Encryption migration loops.

Each loop is backend-agnostic — they call methods on the active StorageBackend
(via app.db.get_db()) which work identically for SQLite + Supabase + Postgres.

Loops:
    encrypt_all()          — re-encrypt every plaintext sensitive value with the
                             active per-tenant DEK. Idempotent; safe to re-run.
    decrypt_all()          — reverse direction; strip encryption from every row.
    rotate_dek(user_id)    — generate a new DEK version for one tenant and
                             re-encrypt only that tenant's rows.
    rotate_kek()           — re-wrap every DEK in the vault under a new KEK.
                             Row data is never touched. O(tenants), not O(rows).

The vault entry for inline-DB secrets uses a sentinel user_id ("_vault") with
service "_secrets_vault". These rows are SKIPPED — they hold the wrapped DEKs
themselves and must not be encrypted (that would be a chicken-and-egg).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from app.encryption.interface import is_ciphertext
from app.encryption.vault_keys import get_vault_keys, _unwrap

logger = logging.getLogger(__name__)

VAULT_SENTINEL_USER = "_vault"
VAULT_SENTINEL_SERVICE = "_secrets_vault"


def _is_vault_row(row: dict) -> bool:
    return (
        row.get("user_id") == VAULT_SENTINEL_USER
        and row.get("service") == VAULT_SENTINEL_SERVICE
    )


async def _all_users() -> List[str]:
    """Return every user_id we know about (from user_profiles + auth_elements)."""
    from app.db import get_db
    db = _unwrap(get_db())
    raw = db.get_raw_client()
    uids: set = set()
    for table, col in (("user_profiles", "user_id"), ("auth_elements", "user_id")):
        try:
            res = raw.table(table).select(col).execute()
            for row in (res.data or []):
                uid = row.get(col)
                if uid and uid != VAULT_SENTINEL_USER:
                    uids.add(uid)
        except Exception as e:
            logger.warning("Listing %s failed: %s", table, e)
    return sorted(uids)


async def _walk_auth_elements_for_user(user_id: str) -> List[dict]:
    from app.db import get_db
    db = _unwrap(get_db())
    raw = db.get_raw_client()
    try:
        res = (
            raw.table("auth_elements")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )
        return list(res.data or [])
    except Exception as e:
        logger.warning("Listing auth_elements(%s) failed: %s", user_id, e)
        return []


async def _set_secret_ref(row_id: str, new_value: str) -> None:
    from app.db import get_db
    db = _unwrap(get_db())
    raw = db.get_raw_client()
    from datetime import datetime, timezone
    raw.table("auth_elements") \
       .update({"secret_ref": new_value, "updated_at": datetime.now(timezone.utc).isoformat()}) \
       .eq("id", row_id) \
       .execute()


# ── Public loops ───────────────────────────────────────────────────────────


async def encrypt_all() -> dict:
    """
    Walk every user's auth_elements rows; encrypt any plaintext secret_ref.
    Idempotent — rows already in ciphertext form are skipped.
    """
    from app.encryption import get_encryption
    enc = get_encryption()
    if enc.level == "none":
        return {"ok": False, "error": "Encryption level is 'none'; switch to 'field' first."}

    users = await _all_users()
    total = 0
    encrypted = 0
    skipped_vault = 0
    skipped_empty = 0
    errors = 0

    for uid in users:
        rows = await _walk_auth_elements_for_user(uid)
        for row in rows:
            total += 1
            if _is_vault_row(row):
                skipped_vault += 1
                continue
            secret = row.get("secret_ref") or ""
            if not secret:
                skipped_empty += 1
                continue
            if is_ciphertext(secret):
                continue
            try:
                ct = await enc.encrypt(uid, secret)
                await _set_secret_ref(row["id"], ct)
                encrypted += 1
            except Exception as e:
                logger.error("encrypt row %s failed: %s", row.get("id"), e)
                errors += 1

    return {
        "ok": errors == 0,
        "users": len(users),
        "rows_scanned": total,
        "rows_encrypted": encrypted,
        "rows_skipped_vault": skipped_vault,
        "rows_skipped_empty": skipped_empty,
        "errors": errors,
    }


async def decrypt_all() -> dict:
    """
    Reverse of encrypt_all — decrypt every ciphertext row back to plaintext.
    Used before switching the level back to 'none' or before a JSON export.
    """
    from app.encryption import get_encryption
    enc = get_encryption()
    if enc.level == "none":
        return {"ok": False, "error": "Encryption is already disabled."}

    users = await _all_users()
    total = 0
    decrypted = 0
    errors = 0

    for uid in users:
        rows = await _walk_auth_elements_for_user(uid)
        for row in rows:
            total += 1
            if _is_vault_row(row):
                continue
            secret = row.get("secret_ref") or ""
            if not is_ciphertext(secret):
                continue
            try:
                pt = await enc.decrypt(uid, secret)
                if pt == secret:
                    errors += 1
                    continue
                await _set_secret_ref(row["id"], pt)
                decrypted += 1
            except Exception as e:
                logger.error("decrypt row %s failed: %s", row.get("id"), e)
                errors += 1

    return {
        "ok": errors == 0,
        "users": len(users),
        "rows_scanned": total,
        "rows_decrypted": decrypted,
        "errors": errors,
    }


async def rotate_dek(user_id: str) -> dict:
    """
    Generate a new DEK version for this tenant, then re-encrypt all of their
    rows with the new key. The old version is marked retired and purged after
    a successful loop.
    """
    from app.encryption import get_encryption
    enc = get_encryption()
    if enc.level == "none":
        return {"ok": False, "error": "Encryption is disabled."}
    if not user_id:
        return {"ok": False, "error": "user_id is required"}

    vkm = get_vault_keys()
    rotation = await vkm.rotate_dek(user_id)
    old_version = rotation.get("old_version")
    new_version = rotation.get("new_version")

    # Walk and re-encrypt this user's rows.
    rows = await _walk_auth_elements_for_user(user_id)
    n_rewrapped = 0
    errors = 0
    for row in rows:
        if _is_vault_row(row):
            continue
        secret = row.get("secret_ref") or ""
        if not secret:
            continue
        try:
            # Decrypt with whatever version the ciphertext claims (old DEK still
            # available in cache + vault until we purge).
            plain = await enc.decrypt(user_id, secret)
            ct = await enc.encrypt(user_id, plain)
            await _set_secret_ref(row["id"], ct)
            n_rewrapped += 1
        except Exception as e:
            logger.error("rotate_dek row %s failed: %s", row.get("id"), e)
            errors += 1

    # Purge the retired DEK on success.
    if errors == 0 and old_version is not None:
        await vkm.purge_retired_dek(user_id, old_version)

    return {
        "ok": errors == 0,
        "user_id": user_id,
        "old_version": old_version,
        "new_version": new_version,
        "rows_rewrapped": n_rewrapped,
        "errors": errors,
    }


async def rotate_kek() -> dict:
    """Re-wrap every DEK in the vault under a fresh KEK. O(tenants), not O(rows)."""
    vkm = get_vault_keys()
    return await vkm.rotate_kek()
