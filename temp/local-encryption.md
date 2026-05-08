# Local Encryption Vault — webAgent

## Architecture Overview

Per-user AES-256-GCM encryption with a master key protecting the vault.
All encryption lives **inside `local.db`** (two vault tables). Master key is
kept outside the DB (`.env`, env var, or typed at startup).

```
.env: VAULT_KEY=<64-char hex>
        │
        ▼
┌──────────────────────────────────────────────────┐
│  VaultManager (in process memory)                │
│                                                  │
│  vault_user_keys table (inside local.db):        │
│    user_alice → AES-256 key  (encrypted by master)│
│    user_bob   → AES-256 key  (encrypted by master)│
│                                                  │
│  vault_secrets table (inside local.db):          │
│    telegram/bot_token        (encrypted by master)│
│    twilio/auth_token         (encrypted by master)│
│    supabase/service_key      (encrypted by master)│
└──────────────┬───────────────────────────────────┘
               │
               ▼ Data tables (same local.db)
┌──────────────────────────────────────────────────┐
│  interactions.content  → !ENC!... (AES-256-GCM)   │
│  memories.*             → !ENC!...                │
│  context_documents.*    → !ENC!...                │
│  session_summaries.*    → !ENC!...                │
│  memory_timeline.*      → !ENC!...                │
└──────────────────────────────────────────────────┘
```

## Key Hierarchy

```
VAULT_KEY (master, from env/keychain)
    │  AES-256-GCM decrypt
    ▼
User's AES-256 key (random per user, stored in vault_user_keys)
    │  AES-256-GCM decrypt
    ▼
User data (interactions, memories, context, etc.)
```

**Why two layers instead of one?**
- Master key rotation → re-wrap only user keys (~48 bytes each), not all data
- User key rotation → one user affected, others untouched
- Crypto-deletion → delete one row from vault_user_keys, user's data gone forever

## Files

| File | What |
|------|------|
| `app/security/crypto.py` | AES-256-GCM primitives, `!ENC!` prefix format |
| `app/security/vault.py` | VaultManager class |
| `app/security/__init__.py` | Package exports |
| `app/db/local.py` | Modified: encrypt/decrypt in all read/write methods |
| `app/db/__init__.py` | Modified: init_vault(), get_vault(), vault auto-attach |
| `app/main.py` | Modified: startup reads VAULT_KEY from env |
| `.env.example` | VAULT_KEY docs added |
| `requirements.txt` | `cryptography>=42.0.0` added |

## Storage Format

Encrypted fields use prefix + base64:

```
!ENC! + base64(nonce(12 bytes) + AES-GCM ciphertext)
```

Legacy plaintext fields pass through unchanged (no migration needed).

## What Gets Encrypted vs Plaintext

| Table | Encrypted | Plaintext (queryable) |
|-------|-----------|----------------------|
| `interactions` | content, input, metadata | id, session_id, role, tool_name, created_at |
| `session_summaries` | summary, title | session_id, message_count |
| `memories` | compiled_truth, timeline, frontmatter | slug, page_type, user_id |
| `context_documents` | content, title | context_type, tags, agent_id |
| `memory_timeline` | summary, detail | event_date, source |
| `vault_user_keys` | (already encrypted by master) | user_id |
| `vault_secrets` | (already encrypted by master) | name |
| `sessions` | — | title (plaintext for session list queries) |
| `memory_chunks` | — | chunk_text (FTS5 search compatibility) |

## App Secrets (Telegram, Twilio, Supabase, etc.)

Vault stores app-wide service credentials encrypted by the master key:

```python
# Store
vault.set_secret("telegram/bot_token", "123456:ABC-DEF...")

# Retrieve
token = vault.get_secret("telegram/bot_token")

# List
vault.list_secrets()  # ["telegram/bot_token", ...]
```

These are separate from user keys — one key domain for all service configs.

## Master Key Lifecycle

### Startup

Three sources, checked in order:

1. **`VAULT_KEY` env var** — set in `.env` or system env
2. **Auto-generated** — if neither exists, generates a random 256-bit key and logs it
3. **Terminal prompt** *(planned)* — future option for no-disk security

### Rotation

```python
new_key = generate_key()
vault.rotate_master_key(new_key)
```

- Re-wraps all user keys with new master
- Re-wraps all vault secrets with new master
- User data keys untouched (decrypted with original user key, not master)
- Cache cleared — next read re-decrypts from vault tables

## User Key Management

### Create

```python
key = vault.get_or_create_user_key("alice")
```

Automatically on first data write for that user. Random 256-bit key generated,
encrypted with master key, stored in `vault_user_keys`.

### Rotation

```python
old_key = vault.rotate_user_key("alice")
# old_key returned so caller can re-encrypt data in background
```

User key changes. All previous data encrypted with old key must be
re-encrypted with new key (background batch).

### Crypto-Deletion

```python
vault.delete_user_key("alice")
```

Removes the wrapped key from `vault_user_keys`. Alice's user data in
`interactions`, `memories`, `context_documents` becomes **permanently
unrecoverable** — even with master key, even from backups containing the
old vault table. This is true cryptographic deletion.

## Threat Model

| Attack | Mitigation |
|--------|-----------|
| `local.db` leaked (git push, backup, file traversal) | All content is AES-256-GCM ciphertext. Useless without master key. |
| `local.db` + `vault_user_keys` leaked | User keys are themselves encrypted by master key. Still need master key. |
| `local.db` + `.env` (containing VAULT_KEY) leaked | Full compromise. Key is in env file — same attack surface as any file-based secret. |
| SQL injection reads another user's rows | Different user key → can't decrypt. Ciphertext only. |
| Server RCE / memory dump | Full compromise. Keys are in server memory to serve legitimate requests. Inevitable. |
| User requests data deletion | Delete their vault_user_keys entry → truly unrecoverable. |
| Master key lost | All user keys + vault secrets permanently unrecoverable. Complete data loss by design. |

## Key Rotation Procedures

### Master Key

```python
from app.security import VaultManager
from app.security.crypto import generate_key

vault = VaultManager("app/db/local.db", old_master_key)
new_master = generate_key()
vault.rotate_master_key(new_master)
# Now update .env with new_master.hex()
```

Cost: O(n) where n = users + secrets. ~2ms per entry. Blazing fast.

### User Key

```python
# Generate new key, get old key for data re-encryption
old_key = vault.rotate_user_key("alice")

# Re-encrypt all alice's data (background task)
for each of alice's interactions:
    plaintext = decrypt_str(old_key, stored)
    new_stored = encrypt_str(new_key, plaintext)
    write new_stored back

# Old key can now be garbage collected
```

Cost: O(m) where m = alice's rows. Heavier but only affects one user.

## Integration Points

Vault attaches to the `LocalBackend` via `set_vault()`:

```python
db = LocalBackend("app/db/local.db")
db.set_vault(vault)

# All subsequent reads/writes are transparently encrypted/decrypted
await db.insert_interaction(user_id, session_id, "user", "secret")  # encrypts
interactions = await db.fetch_interactions(user_id, session_id)      # decrypts
```

The `StorageBackend` interface is unchanged — encryption is an internal
implementation detail of `LocalBackend`. The `SupabaseBackend` is unaffected.

## FTS5 Search Limitation

`memory_chunks.chunk_text` is NOT encrypted (stays plaintext) because FTS5
full-text search runs at the SQLite level, before the app can decrypt.

**Practical impact:** `memory_search()` finds results by slug + title (plaintext
searchable). Content is returned decrypted. For most use cases this is
sufficient — users search by title or slug, not by random content snippets.

If you need encrypted FTS5, you'd need searchable encryption (complex) or
decrypt-all-and-search-in-Python (slow for large corpuses).

## Startup Flow in main.py

```
1. Server starts
2. Read VAULT_KEY from environment
3. If empty: generate key, log warning
4. init_vault(VAULT_KEY_bytes)
5. get_db() auto-attaches vault to LocalBackend
6. Server accepts requests → all reads/writes transparently encrypted
```

## Vault API Reference

```python
# crypto.py
encrypt_str(key, plaintext)    → "!ENC!..." string
decrypt_str(key, stored)       → plaintext string (or passthrough if no prefix)
generate_key()                 → 32 random bytes

# vault.py
VaultManager(db_path, master_key)

# User keys
.get_or_create_user_key(user_id)     → bytes (cached)
.get_user_key_or_none(user_id)       → bytes | None
.rotate_user_key(user_id)            → bytes (old key)
.delete_user_key(user_id)            → bool

# App secrets
.set_secret(name, value)             → None
.get_secret(name)                    → str | None
.delete_secret(name)                 → bool
.list_secrets()                      → [str]
.list_secrets_with_preview()         → [{"name": str, "preview": str}]

# Master
.rotate_master_key(new_key)          → {"users_rewrapped": n, "secrets_rewrapped": n}

# Diagnostics
.get_stats()                         → {"users": n, "secrets": n, "cache_entries": n}
.list_user_ids()                     → [str]
```
