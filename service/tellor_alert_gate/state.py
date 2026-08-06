"""SQLite-backed spool, evidence, incident, and delivery state."""

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from .models import DecodedEvent, Finding, utc_now, utc_text


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match_queue (
    record_hash TEXT PRIMARY KEY,
    raw_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    inserted_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS evm_events (
    event_key TEXT PRIMARY KEY,
    block_number INTEGER NOT NULL,
    block_hash TEXT NOT NULL,
    transaction_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    address TEXT NOT NULL,
    name TEXT NOT NULL,
    signature TEXT NOT NULL,
    args_json TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evm_events_name_idx
    ON evm_events(name, address, block_number);
CREATE TABLE IF NOT EXISTS layer_blocks (
    height INTEGER PRIMARY KEY,
    block_hash TEXT NOT NULL UNIQUE,
    block_time_ms INTEGER NOT NULL,
    raw_block_json TEXT NOT NULL,
    raw_results_json TEXT NOT NULL,
    verified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS layer_events (
    event_key TEXT PRIMARY KEY,
    height INTEGER NOT NULL REFERENCES layer_blocks(height),
    block_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    event_ordinal INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    transaction_hash TEXT,
    inserted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS layer_events_type_idx
    ON layer_events(event_type, height);
CREATE TABLE IF NOT EXISTS incidents (
    incident_key TEXT PRIMARY KEY,
    monitor_slug TEXT NOT NULL,
    status TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    context_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_open_idx
    ON incidents(monitor_slug, status);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_key TEXT PRIMARY KEY,
    incident_key TEXT,
    monitor_slug TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_key TEXT PRIMARY KEY,
    incident_key TEXT NOT NULL,
    monitor_slug TEXT NOT NULL,
    status TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    delivered_at TEXT,
    last_error TEXT
);
"""


class StateStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def get_meta(self, key, default=None):
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return default if row is None else row["value"]

    def set_meta(self, key, value):
        self.connection.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.connection.commit()

    def set_metadata(self, values):
        with self.transaction():
            self.connection.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(str(key), str(value)) for key, value in values.items()],
            )

    def ingest_spool(self, spool_path):
        path = Path(spool_path)
        if not path.exists():
            return 0
        cursor = int(self.get_meta("spool_cursor", "0"))
        size = path.stat().st_size
        if size < cursor:
            cursor = 0
        records = []
        new_cursor = cursor
        with path.open("rb") as spool:
            spool.seek(cursor)
            while True:
                start = spool.tell()
                line = spool.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    new_cursor = start
                    break
                new_cursor = spool.tell()
                raw = line[:-1]
                digest = hashlib.sha256(raw).hexdigest()
                try:
                    text = raw.decode("utf-8")
                    json.loads(text)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    text = json.dumps(
                        {
                            "invalid_spool_record_sha256": digest,
                            "reason": "invalid JSON record",
                        }
                    )
                records.append((digest, text, utc_text()))
        with self.transaction():
            self.connection.executemany(
                "INSERT OR IGNORE INTO match_queue"
                "(record_hash,raw_json,inserted_at) VALUES(?,?,?)",
                records,
            )
            self.connection.execute(
                "INSERT INTO metadata(key,value) VALUES('spool_cursor',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_cursor),),
            )
        return len(records)

    def pending_matches(self, limit=25):
        now = utc_text()
        return self.connection.execute(
            "SELECT * FROM match_queue WHERE status='pending' "
            "AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
            "ORDER BY inserted_at LIMIT ?",
            (now, int(limit)),
        ).fetchall()

    def complete_match(self, record_hash):
        self.connection.execute(
            "UPDATE match_queue SET status='complete',completed_at=?,last_error=NULL "
            "WHERE record_hash=?",
            (utc_text(), record_hash),
        )
        self.connection.commit()

    def retry_match(self, record_hash, error, delay_seconds=30):
        next_attempt = utc_text(utc_now() + timedelta(seconds=delay_seconds))
        safe_error = str(error)[:500]
        self.connection.execute(
            "UPDATE match_queue SET attempts=attempts+1,next_attempt_at=?,last_error=? "
            "WHERE record_hash=?",
            (next_attempt, safe_error, record_hash),
        )
        self.connection.commit()

    def record_evm_events(self, events):
        now = utc_text()
        with self.transaction():
            for event in events:
                key = "1:{}:{}".format(event.transaction_hash, event.log_index)
                self.connection.execute(
                    "INSERT OR IGNORE INTO evm_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        event.block_number,
                        event.block_hash,
                        event.transaction_hash,
                        event.log_index,
                        event.address,
                        event.name,
                        event.signature,
                        _json(event.args),
                        now,
                    ),
                )

    def evm_events(self, name=None, address=None):
        clauses = []
        values = []
        if name:
            clauses.append("name=?")
            values.append(name)
        if address:
            clauses.append("address=?")
            values.append(address.lower())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM evm_events" + where + " ORDER BY block_number,log_index",
            values,
        ).fetchall()
        return rows

    def record_layer_block(self, height, block_hash, time_ms, block, results, events):
        now = utc_text()
        with self.transaction():
            existing = self.connection.execute(
                "SELECT block_hash FROM layer_blocks WHERE height=?", (height,)
            ).fetchone()
            if existing is not None and existing["block_hash"] != block_hash:
                raise ValueError("committed Tellor Layer block hash changed")
            self.connection.execute(
                "INSERT OR IGNORE INTO layer_blocks VALUES(?,?,?,?,?,?)",
                (height, block_hash, time_ms, _json(block), _json(results), now),
            )
            for event in events:
                self.connection.execute(
                    "INSERT OR IGNORE INTO layer_events VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        event["event_key"],
                        height,
                        block_hash,
                        event["source"],
                        event["event_ordinal"],
                        event["event_type"],
                        _json(event["attributes"]),
                        event.get("transaction_hash"),
                        now,
                    ),
                )

    def import_bridge_seed(self, seed):
        existing_digest = self.get_meta("bridge_seed_sha256")
        if existing_digest is not None:
            if existing_digest != seed.digest:
                raise ValueError("M4 bridge-ledger seed differs from initialized state")
            return
        existing_rows = self.connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM evm_events) + "
            "(SELECT COUNT(*) FROM layer_blocks) + "
            "(SELECT COUNT(*) FROM layer_events) AS total"
        ).fetchone()["total"]
        if existing_rows:
            raise ValueError("M4 bridge-ledger seed requires an uninitialized event ledger")
        now = utc_text()
        with self.transaction():
            for event in seed.evm_events:
                key = "1:{}:{}".format(event.transaction_hash, event.log_index)
                self.connection.execute(
                    "INSERT INTO evm_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        event.block_number,
                        event.block_hash,
                        event.transaction_hash,
                        event.log_index,
                        event.address,
                        event.name,
                        event.signature,
                        _json(event.args),
                        now,
                    ),
                )
            for block in seed.layer_blocks:
                self.connection.execute(
                    "INSERT INTO layer_blocks VALUES(?,?,?,?,?,?)",
                    (
                        block["height"],
                        block["block_hash"],
                        block["block_time_ms"],
                        _json({"seed_checkpoint": True}),
                        _json({"seed_checkpoint": True}),
                        now,
                    ),
                )
            for event in seed.layer_events:
                self.connection.execute(
                    "INSERT INTO layer_events VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        event["event_key"],
                        event["height"],
                        event["block_hash"],
                        event["source"],
                        event["event_ordinal"],
                        event["event_type"],
                        _json(event["attributes"]),
                        event["transaction_hash"],
                        now,
                    ),
                )
            self.connection.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?)",
                [
                    ("bridge_seed_sha256", seed.digest),
                    (
                        "bridge_seed_layer_height",
                        str(seed.layer_checkpoint_height),
                    ),
                    (
                        "evm_deposit_replayed_through",
                        str(seed.ethereum_checkpoint_number),
                    ),
                ],
            )

    def assert_live_ready(
        self,
        *,
        seed_digest,
        ethereum_confirmed_block,
        latest_layer_height,
    ):
        if self.get_meta("bridge_seed_sha256") != seed_digest:
            raise ValueError("M4 bridge-ledger seed is not imported into runtime state")
        ethereum_cursor = int(self.get_meta("evm_deposit_replayed_through", "-1"))
        if ethereum_cursor < int(ethereum_confirmed_block):
            raise ValueError("M4 Ethereum deposit replay is not caught up")
        evaluated_text = self.get_meta("layer_evaluated_height")
        cursor_text = self.get_meta("layer_mint_cursor")
        next_text = self.get_meta("layer_next_height")
        if evaluated_text is None or cursor_text is None or next_text is None:
            raise ValueError("Tellor Layer replay has not completed an evaluated block")
        evaluated = int(evaluated_text)
        if evaluated < int(latest_layer_height) - 1:
            raise ValueError("Tellor Layer replay is not caught up")
        if int(next_text) != evaluated + 1:
            raise ValueError("Tellor Layer ingestion and evaluation cursors differ")
        try:
            mint_cursor = json.loads(cursor_text)
            mint_height = int(mint_cursor["previous_height"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Tellor Layer minter cursor is invalid") from error
        if mint_height != evaluated:
            raise ValueError("Tellor Layer minter and evaluation cursors differ")
        pending = self.connection.execute(
            "SELECT COUNT(*) AS count FROM match_queue WHERE status='pending'"
        ).fetchone()["count"]
        if pending:
            raise ValueError("unresolved EVM monitor matches remain queued")
        open_incidents = self.connection.execute(
            "SELECT COUNT(*) AS count FROM incidents WHERE status='open'"
        ).fetchone()["count"]
        if open_incidents:
            raise ValueError("open log-only incidents must be reviewed before live delivery")
        unsettled = self.connection.execute(
            "SELECT COUNT(*) AS count FROM deliveries WHERE status!='sent'"
        ).fetchone()["count"]
        if unsettled:
            raise ValueError("unsettled deliveries remain in runtime state")

    def poll_liveness(self, *, max_stale_seconds=300, now=None):
        """Report whether the alert-gate's scheduled evaluation pass is still
        advancing, for use as a genuine process-health signal.

        `last_scheduled_minute` (see `AlertGate.run_once` /
        `AlertGate.process_scheduled` in service.py) is written only after a
        full M9-M11 scheduled evaluation *and* the M3 stale-DataBridge check
        both complete successfully against the configured Ethereum RPC
        providers, once per new wall-clock minute. That makes it a real
        liveness signal rather than a trivial "is the process alive" check:
        a poll loop that is deadlocked (e.g. stuck on the SQLite state file)
        or wedged against a dead RPC stops advancing this value, even though
        the process itself keeps running. A healthcheck that only proved the
        container was up would miss exactly that failure mode.

        Returns `(healthy, age_seconds)`. `age_seconds` is `None` when no
        scheduled pass has completed yet (e.g. shortly after startup, before
        the first pass finishes) -- callers should treat that as unhealthy
        past a reasonable startup grace period (a Docker `start_period` is
        the natural place for that grace), not as an indefinite pass.
        """
        now = utc_now() if now is None else now
        raw = self.get_meta("last_scheduled_minute")
        if raw is None:
            return False, None
        try:
            last_minute = int(raw)
        except (TypeError, ValueError):
            return False, None
        current_minute = int(now.timestamp()) // 60
        age_seconds = max(0, current_minute - last_minute) * 60
        return age_seconds <= max_stale_seconds, age_seconds

    def layer_events(self, event_type=None, height=None):
        clauses = []
        values = []
        if event_type:
            clauses.append("event_type=?")
            values.append(event_type)
        if height is not None:
            clauses.append("height=?")
            values.append(int(height))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.connection.execute(
            "SELECT * FROM layer_events" + where + " ORDER BY height,event_ordinal",
            values,
        ).fetchall()

    def layer_block(self, height):
        return self.connection.execute(
            "SELECT * FROM layer_blocks WHERE height=?", (int(height),)
        ).fetchone()

    def layer_blocks_after(self, height, limit=25):
        return self.connection.execute(
            "SELECT * FROM layer_blocks WHERE height>? ORDER BY height LIMIT ?",
            (int(height), int(limit)),
        ).fetchall()

    def add_evidence(self, key, monitor_slug, payload, incident_key=None):
        self.connection.execute(
            "INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?)",
            (key, incident_key, monitor_slug, _json(payload), utc_text()),
        )
        self.connection.commit()

    def open_incident(self, finding):
        context = finding.as_dict()
        self.connection.execute(
            "INSERT INTO incidents VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(incident_key) DO UPDATE SET "
            "status='open',closed_at=NULL,context_json=excluded.context_json",
            (
                finding.incident_key,
                finding.slug,
                "open",
                finding.first_seen,
                utc_text(),
                None,
                _json(context),
            ),
        )
        self.connection.commit()

    def open_incident_for_slug(self, slug):
        return self.connection.execute(
            "SELECT * FROM incidents WHERE monitor_slug=? AND status='open' "
            "ORDER BY opened_at LIMIT 1",
            (slug,),
        ).fetchone()

    def open_incidents_for_slug(self, slug):
        return self.connection.execute(
            "SELECT * FROM incidents WHERE monitor_slug=? AND status='open' "
            "ORDER BY opened_at",
            (slug,),
        ).fetchall()

    def open_incidents_with_prefix(self, prefix):
        return self.connection.execute(
            "SELECT * FROM incidents WHERE incident_key LIKE ? AND status='open' "
            "ORDER BY opened_at",
            (prefix + "%",),
        ).fetchall()

    def matching_open_incident(self, slug, query_id, timestamp=None, value=None):
        for row in self.open_incidents_for_slug(slug):
            try:
                context = json.loads(row["context_json"])
                evidence = context.get("evidence") or {}
            except (json.JSONDecodeError, TypeError):
                continue
            if str(evidence.get("query_id", "")).lower() != str(query_id).lower():
                continue
            if timestamp is not None and int(evidence.get("report_timestamp", -1)) != int(timestamp):
                continue
            if value is not None and str(evidence.get("value", "")).lower() != str(value).lower():
                continue
            return row
        return None

    def incident(self, incident_key):
        return self.connection.execute(
            "SELECT * FROM incidents WHERE incident_key=?", (incident_key,)
        ).fetchone()

    def close_incident(self, incident_key):
        self.connection.execute(
            "UPDATE incidents SET status='closed',closed_at=? "
            "WHERE incident_key=? AND status='open'",
            (utc_text(), incident_key),
        )
        self.connection.commit()

    def open_dispute(self, query_id, timestamp=None):
        rows = self.connection.execute(
            "SELECT * FROM incidents WHERE monitor_slug='governance-dispute' "
            "AND status='open' ORDER BY opened_at"
        ).fetchall()
        for row in rows:
            context = json.loads(row["context_json"])
            evidence = context.get("evidence") or {}
            if str(evidence.get("query_id", "")).lower() != query_id.lower():
                continue
            if timestamp is None or int(evidence.get("report_timestamp", -1)) == int(
                timestamp
            ):
                return row
        return None

    def delivery(self, delivery_key):
        return self.connection.execute(
            "SELECT * FROM deliveries WHERE delivery_key=?", (delivery_key,)
        ).fetchone()

    def reserve_delivery(self, finding):
        digest = hashlib.sha256(finding.message().encode()).hexdigest()
        try:
            self.connection.execute(
                "INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?)",
                (
                    finding.delivery_key,
                    finding.incident_key,
                    finding.slug,
                    "sending",
                    digest,
                    utc_text(),
                    None,
                    None,
                ),
            )
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delivery_succeeded(self, finding):
        with self.transaction():
            self.connection.execute(
                "UPDATE deliveries SET status='sent',delivered_at=?,last_error=NULL "
                "WHERE delivery_key=?",
                (utc_text(), finding.delivery_key),
            )
            context = finding.as_dict()
            self.connection.execute(
                "INSERT INTO incidents VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(incident_key) DO UPDATE SET "
                "status='open',closed_at=NULL,context_json=excluded.context_json",
                (
                    finding.incident_key,
                    finding.slug,
                    "open",
                    finding.first_seen,
                    utc_text(),
                    None,
                    _json(context),
                ),
            )

    def reclaim_stale_sending(self, delivery_key, older_than_seconds):
        """Reclaim a `sending` reservation abandoned by a dead process.

        Atomically re-stamps `reserved_at` to now, but only if the row is
        still `status='sending'` and its existing `reserved_at` is older
        than `older_than_seconds`. Returns True if this call reclaimed the
        row (the caller should now retry delivery), False if the row is
        missing, no longer `sending`, or not yet stale (a genuinely
        in-flight delivery must be left untouched).
        """
        cutoff = utc_text(utc_now() - timedelta(seconds=older_than_seconds))
        cursor = self.connection.execute(
            "UPDATE deliveries SET reserved_at=? "
            "WHERE delivery_key=? AND status='sending' AND reserved_at<?",
            (utc_text(), delivery_key, cutoff),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def delivery_failed(self, delivery_key, error, ambiguous=False):
        if ambiguous:
            self.connection.execute(
                "UPDATE deliveries SET status='uncertain',last_error=? "
                "WHERE delivery_key=?",
                (str(error)[:500], delivery_key),
            )
        else:
            self.connection.execute(
                "DELETE FROM deliveries WHERE delivery_key=?", (delivery_key,)
            )
        self.connection.commit()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
