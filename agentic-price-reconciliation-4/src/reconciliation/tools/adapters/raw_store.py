"""Storage for raw inbound message bodies.

`ParsedEmail` deliberately carries no body field (`tools/contracts.py`): the body
stays here and only an opaque `raw_ref` travels forward, which is what bounds
prompt-injection surface (spec 05 G3 / spec 06 G5) and satisfies data
minimisation (spec 06 G6). Anything that genuinely needs the original text — a
gate-2 reviewer, an auditor reconstructing a decision — resolves the ref against
this store, which in production is access-controlled.

`RawRecord` also keeps the model id and prompt version that read each message.
That is how "which model extracted this price, under which prompt" stays
answerable without adding a field to `ParsedEmail` (a SHARED schema) — the audit
entry references the message, the message references the extraction provenance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RawRecord:
    """One stored inbound message plus its extraction provenance."""

    message_id: str
    sender: str
    received_at: datetime
    body: str
    #: Set once an extraction has run over this body.
    extracted_by_model: str | None = None
    prompt_version: str | None = None


@runtime_checkable
class RawMessageStore(Protocol):
    """Where raw bodies live. Implementations must be access-controlled."""

    def put(self, record: RawRecord) -> str:
        """Store a body, returning the `raw_ref` that stands in for it."""
        ...

    def get(self, raw_ref: str) -> RawRecord: ...

    def record_extraction(
        self, raw_ref: str, *, model_id: str, prompt_version: str
    ) -> None:
        """Attach extraction provenance to an already-stored body."""
        ...


class RawMessageNotFound(KeyError):
    """No stored body for that ref."""


@dataclass
class InMemoryRawMessageStore:
    """For tests and the demo. Loses everything on exit, by design."""

    records: dict[str, RawRecord] = field(default_factory=dict)

    def put(self, record: RawRecord) -> str:
        raw_ref = f"raw://{record.message_id}"
        self.records[raw_ref] = record
        return raw_ref

    def get(self, raw_ref: str) -> RawRecord:
        try:
            return self.records[raw_ref]
        except KeyError:
            raise RawMessageNotFound(raw_ref) from None

    def record_extraction(
        self, raw_ref: str, *, model_id: str, prompt_version: str
    ) -> None:
        record = self.get(raw_ref)
        self.records[raw_ref] = RawRecord(
            message_id=record.message_id,
            sender=record.sender,
            received_at=record.received_at,
            body=record.body,
            extracted_by_model=model_id,
            prompt_version=prompt_version,
        )


@dataclass
class FileRawMessageStore:
    """One JSON file per message under `root`.

    A stand-in for the real access-controlled store (OPEN QUESTION 6 in
    `requirements.md` §6 covers which system that is). Filenames are derived from
    the message id with path separators stripped, so a hostile `message_id`
    cannot write outside `root`.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, message_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in message_id)
        return self.root / f"{safe}.json"

    def put(self, record: RawRecord) -> str:
        payload = {
            "message_id": record.message_id,
            "sender": record.sender,
            "received_at": record.received_at.isoformat(),
            "body": record.body,
            "extracted_by_model": record.extracted_by_model,
            "prompt_version": record.prompt_version,
        }
        self._path(record.message_id).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        return f"raw://{record.message_id}"

    def get(self, raw_ref: str) -> RawRecord:
        message_id = raw_ref.removeprefix("raw://")
        path = self._path(message_id)
        if not path.exists():
            raise RawMessageNotFound(raw_ref)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return RawRecord(
            message_id=payload["message_id"],
            sender=payload["sender"],
            received_at=datetime.fromisoformat(payload["received_at"]),
            body=payload["body"],
            extracted_by_model=payload.get("extracted_by_model"),
            prompt_version=payload.get("prompt_version"),
        )

    def record_extraction(
        self, raw_ref: str, *, model_id: str, prompt_version: str
    ) -> None:
        record = self.get(raw_ref)
        self.put(
            RawRecord(
                message_id=record.message_id,
                sender=record.sender,
                received_at=record.received_at,
                body=record.body,
                extracted_by_model=model_id,
                prompt_version=prompt_version,
            )
        )


def utc_now() -> datetime:
    return datetime.now(UTC)
