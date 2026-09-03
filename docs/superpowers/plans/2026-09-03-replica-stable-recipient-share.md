# Replica-Stable Recipient Share Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve primary-generated recipient mailbox URLs when they are served by replicas with independent `SECRET_KEY` values.

**Architecture:** Store one opaque `recipient_share_segment` on each owning account. Backfill existing primary accounts with their legacy HMAC segment, replicate the field in protocol v3 account payloads, and resolve public routes only against the persisted value.

**Tech Stack:** Python 3, Flask, SQLite, unittest/pytest, existing encrypted cluster replication.

---

### Task 1: Lock The Cross-Node Regression

**Files:**
- Modify: `tests/test_public_mailbox_messages_api.py`

- [ ] **Step 1: Write the failing cross-node route test**

Create isolated primary and replica modules with distinct `SECRET_KEY` values. Seed a recipient link on the primary, capture its `/show/` segment, build a real snapshot, apply it to the replica, mark the replica ready, and request the primary URL from the replica test client.

```python
def test_primary_show_url_resolves_on_replica_with_independent_secret(self):
    # Seed primary, apply its snapshot to this replica, then assert GET /show/... == 200.
    # Also assert the replica account stores the exact primary share segment.
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/test_public_mailbox_messages_api.py -q -k primary_show_url_resolves_on_replica_with_independent_secret
```

Expected: FAIL with `404` and `链接不存在`, because the replica recomputes the segment with its own key.

### Task 2: Persist And Migrate Account Share Identity

**Files:**
- Modify: `outlook_web/segments/01_bootstrap.py`
- Modify: `outlook_web/segments/13_routes_recipient_links.py`
- Modify: `tests/test_recipient_mail_links.py`

- [ ] **Step 1: Write schema and compatibility tests**

Assert `accounts.recipient_share_segment` exists, two recipient links under one account use the same stored segment, new segments match base64url syntax, and an old database row is backfilled with the legacy `build_recipient_link_share_segment(account_id)` value.

- [ ] **Step 2: Run the focused tests and verify RED**

```powershell
python -m pytest tests/test_recipient_mail_links.py -q -k "recipient_share_segment or schema_has_recipient_link_constraints"
```

Expected: FAIL because the account column and persistent allocation do not exist.

- [ ] **Step 3: Implement the schema migration and allocation**

Add the column to new and existing account schemas:

```sql
recipient_share_segment TEXT NOT NULL DEFAULT ''
```

After primary replication triggers are installed, backfill only accounts with recipient links and an empty value using the legacy HMAC calculation. On an upgraded replica, clear `cluster_replica_state.last_success_at` when the column is first added so the next sync is a full snapshot.

In recipient-link creation, allocate `secrets.token_urlsafe(32)` only when the owning account has no segment. URL serialization reads this stored value. Route resolution selects and compares the persisted value rather than calling the HMAC builder.

- [ ] **Step 4: Run the focused tests and verify GREEN**

```powershell
python -m pytest tests/test_recipient_mail_links.py tests/test_public_mailbox_messages_api.py -q -k "recipient_share_segment or primary_show_url_resolves_on_replica_with_independent_secret or public_show_and_query_routes_use_shared_segment"
```

Expected: all selected tests PASS.

### Task 3: Extend Cluster Protocol Account Payloads

**Files:**
- Modify: `outlook_web/cluster/storage.py`
- Modify: `tests/test_public_mailbox_messages_api.py`

- [ ] **Step 1: Add failing snapshot, increment, and validation assertions**

Assert account payloads contain `recipient_share_segment`, replica upserts persist it, malformed values are rejected, and the protocol constant is `3`.

- [ ] **Step 2: Run cluster-focused tests and verify RED**

```powershell
python -m pytest tests/test_public_mailbox_messages_api.py -q -k "snapshot or increment or protocol or recipient_share_segment"
```

Expected: FAIL because protocol v2 account payloads omit the field.

- [ ] **Step 3: Implement protocol v3 serialization and application**

Add `recipient_share_segment` to `_ACCOUNT_PAYLOAD_KEYS`, account serialization queries, validation, snapshot/increment SELECT lists, and replica account INSERT/UPDATE statements. Validate with:

```python
re.fullmatch(r"[A-Za-z0-9_-]{32,128}", recipient_share_segment)
```

Allow an empty value only for accounts that have never owned a recipient link. Set `CLUSTER_PROTOCOL_VERSION = 3`.

- [ ] **Step 4: Run cluster-focused tests and verify GREEN**

```powershell
python -m pytest tests/test_public_mailbox_messages_api.py -q -k "snapshot or increment or protocol or recipient_share_segment"
```

Expected: all selected tests PASS.

### Task 4: Full Verification And Delivery

**Files:**
- Modify: `docs/deployment.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Document the coordinated protocol-v3 upgrade**

State that primary and replica nodes must both run the new version; upgraded replicas force a fresh snapshot, and independent `SECRET_KEY` values remain required.

- [ ] **Step 2: Run syntax, diff, and full test verification**

```powershell
python -m compileall -q outlook_web web_outlook_app.py
python -m pytest -q
git diff --check
git status --short
```

Expected: compile exit `0`, all tests PASS, no whitespace errors, and only planned files changed.

- [ ] **Step 3: Commit using the repository Lore protocol**

Commit the implementation with `Constraint`, `Rejected`, `Confidence`, `Scope-risk`, `Directive`, `Tested`, and `Not-tested` trailers that record the coordinated cluster upgrade and verification evidence.
