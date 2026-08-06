"""Committed Tellor Layer block ingestion with consecutive-read agreement."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import re

from .models import Unresolved
from .rpc import canonical_hash


RFC3339_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?Z\Z"
)


class LayerIngestor:
    def __init__(self, client, store, start_height):
        self.client = client
        self.store = store
        self.start_height = int(start_height)

    def latest_height(self):
        payload = self.client.status()
        try:
            node = payload["result"]["node_info"]
            sync = payload["result"]["sync_info"]
            if node["network"] != "tellor-1" or sync["catching_up"] is not False:
                raise ValueError("wrong or catching-up chain")
            return int(sync["latest_block_height"])
        except (KeyError, TypeError, ValueError) as error:
            raise Unresolved("Tellor Layer status is incomplete") from error

    def next_height(self):
        stored = self.store.get_meta("layer_next_height")
        return int(stored) if stored is not None else self.start_height

    def ingest_available(self, limit=25):
        latest = self.latest_height()
        height = self.next_height()
        ingested = 0
        while height <= latest and ingested < limit:
            self.ingest_height(height)
            height += 1
            ingested += 1
        return ingested

    def ingest_height(self, height):
        first_block = self.client.block(height)
        first_results = self.client.block_results(height)
        second_block = self.client.block(height)
        second_results = self.client.block_results(height)
        first = _parse_block(height, first_block, first_results)
        second = _parse_block(height, second_block, second_results)
        if (
            first["block_hash"] != second["block_hash"]
            or first["results_hash"] != second["results_hash"]
            or first["time_ms"] != second["time_ms"]
        ):
            raise Unresolved("consecutive Tellor Layer block reads disagree")
        previous = self.store.connection.execute(
            "SELECT block_hash FROM layer_blocks WHERE height=?", (height - 1,)
        ).fetchone()
        if previous is not None and first["last_block_hash"] != previous["block_hash"]:
            raise Unresolved("Tellor Layer committed block continuity failed")
        events = extract_events(height, first_block, first_results)
        self.store.record_layer_block(
            height,
            first["block_hash"],
            first["time_ms"],
            first_block,
            first_results,
            events,
        )
        self.store.set_meta("layer_next_height", height + 1)


def _parse_block(expected_height, block_payload, results_payload):
    try:
        block_result = block_payload["result"]
        block = block_result["block"]
        header = block["header"]
        block_hash = block_result["block_id"]["hash"].upper()
        height = int(header["height"])
        chain_id = header["chain_id"]
        last_hash = (header.get("last_block_id") or {}).get("hash", "").upper()
        result_height = int(results_payload["result"]["height"])
    except (KeyError, TypeError, ValueError) as error:
        raise Unresolved("Tellor Layer block response is incomplete") from error
    if height != expected_height or result_height != expected_height or chain_id != "tellor-1":
        raise Unresolved("Tellor Layer block identity does not match request")
    if not re.fullmatch(r"[0-9A-F]{64}", block_hash):
        raise Unresolved("Tellor Layer block hash is invalid")
    return {
        "block_hash": block_hash,
        "last_block_hash": last_hash,
        "time_ms": rfc3339_milliseconds(header["time"]),
        "results_hash": canonical_hash(results_payload["result"]),
    }


def extract_events(height, block_payload, results_payload):
    result = results_payload["result"]
    txs = (((block_payload.get("result") or {}).get("block") or {}).get("data") or {}).get(
        "txs"
    ) or []
    tx_hashes = []
    for encoded in txs:
        try:
            tx_hashes.append(hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest().upper())
        except (ValueError, TypeError) as error:
            raise Unresolved("Tellor Layer transaction bytes are invalid") from error
    tx_results = result.get("txs_results") or []
    if not isinstance(tx_results, list) or len(tx_results) != len(tx_hashes):
        raise Unresolved(
            "Tellor Layer transaction bytes and result counts differ"
        )
    events = []
    for tx_index, tx_result in enumerate(tx_results):
        if not isinstance(tx_result, dict):
            raise Unresolved("Tellor Layer transaction result is invalid")
        try:
            int(tx_result.get("code", 0))
        except (TypeError, ValueError) as error:
            raise Unresolved("Tellor Layer transaction result code is invalid") from error
        source = "txs_results:{}".format(tx_index)
        tx_hash = tx_hashes[tx_index]
        for ordinal, event in enumerate(tx_result.get("events") or []):
            events.append(_event_record(height, source, ordinal, event, tx_hash))
    for ordinal, event in enumerate(result.get("finalize_block_events") or []):
        events.append(
            _event_record(height, "finalize_block_events", ordinal, event, None)
        )
    return events


def successful_event_rows(block_row, rows):
    """Keep FinalizeBlock events and events from successful committed transactions."""

    try:
        result = json.loads(block_row["raw_results_json"])["result"]
        tx_results = result.get("txs_results") or []
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise Unresolved("stored Tellor Layer block results are invalid") from error
    filtered = []
    for row in rows:
        source = str(row["source"])
        if source == "finalize_block_events":
            filtered.append(row)
            continue
        if not source.startswith("txs_results:"):
            raise Unresolved("stored Tellor Layer event source is invalid")
        try:
            index = int(source.split(":", 1)[1])
            result_row = tx_results[index]
            code = int(result_row.get("code", 0))
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise Unresolved(
                "stored Tellor Layer transaction result is unavailable"
            ) from error
        if code == 0:
            filtered.append(row)
    return filtered


def _event_record(height, source, ordinal, event, transaction_hash):
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise Unresolved("Tellor Layer event is invalid")
    attributes = {}
    for attribute in event.get("attributes") or []:
        if not isinstance(attribute, dict) or not isinstance(attribute.get("key"), str):
            raise Unresolved("Tellor Layer event attribute is invalid")
        key = attribute["key"]
        value = attribute.get("value", "")
        if key in attributes:
            prior = attributes[key]
            attributes[key] = prior + [value] if isinstance(prior, list) else [prior, value]
        else:
            attributes[key] = value
    key = "tellor-1:{}:{}:{}:{}".format(height, source, ordinal, event["type"])
    return {
        "event_key": key,
        "source": source,
        "event_ordinal": ordinal,
        "event_type": event["type"],
        "attributes": attributes,
        "transaction_hash": transaction_hash,
    }


def rfc3339_milliseconds(value):
    match = RFC3339_RE.fullmatch(str(value))
    if match is None:
        raise Unresolved("Tellor Layer block time is not canonical UTC")
    base = datetime.fromisoformat(
        "{}T{}+00:00".format(match.group("date"), match.group("time"))
    )
    fraction = (match.group("fraction") or "")[:3].ljust(3, "0")
    return int(base.timestamp()) * 1000 + int(fraction or 0)


def row_attributes(row):
    try:
        value = json.loads(row["attributes_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise Unresolved("stored Tellor Layer event attributes are invalid") from error
    if not isinstance(value, dict):
        raise Unresolved("stored Tellor Layer event attributes are invalid")
    return value
