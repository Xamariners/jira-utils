from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

VALID_STATUSES = ("TODO", "IN_PROGRESS", "DONE", "ERROR")


@dataclass
class AttachmentState:
    attachment_id: str
    project_key: str
    issue_key: str
    issue_summary: str
    attachment_name: str
    size_bytes: int
    created: str
    status: str = "TODO"
    error: Optional[str] = None

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


class StateManager:
    def __init__(self, path: Union[str, Path] = "state/state.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Dict] = {"attachments": {}}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except json.JSONDecodeError as exc:
                raise ValueError(f"State file {self.path} is corrupt: {exc}") from exc
        else:
            self._write()

    def _write(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2))

    def list(self) -> List[AttachmentState]:
        return [
            AttachmentState(**payload)
            for payload in self.data.get("attachments", {}).values()
        ]

    def upsert(self, record: AttachmentState) -> None:
        if record.status not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{record.status}'")
        self.data.setdefault("attachments", {})[record.attachment_id] = asdict(record)
        self._write()

    def update_status(self, attachment_id: str, status: str, error: Optional[str] = None) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'")
        attachments = self.data.setdefault("attachments", {})
        if attachment_id not in attachments:
            raise KeyError(f"Attachment {attachment_id} is not tracked in state.")
        attachments[attachment_id]["status"] = status
        attachments[attachment_id]["error"] = error
        self._write()

    def get(self, attachment_id: str) -> Optional[AttachmentState]:
        payload = self.data.get("attachments", {}).get(attachment_id)
        return AttachmentState(**payload) if payload else None

    def filter_by_status(self, statuses: Iterable[str]) -> List[AttachmentState]:
        status_set = set(statuses)
        return [record for record in self.list() if record.status in status_set]
