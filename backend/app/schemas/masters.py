from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class MasterVersionOut(BaseModel):
    id: uuid.UUID
    version_number: int
    file_kind: Literal["docx", "pdf"]
    size_bytes: int
    sha256: str
    original_filename: str
    extraction_status: Literal["complete", "partial", "failed"]
    extraction_notes: list[str]
    evidence_count: int
    created_at: datetime


class UploadResult(BaseModel):
    master: MasterVersionOut
    duplicate: bool
