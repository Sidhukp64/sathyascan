"""
benchmark_datasets / benchmark_runs — docs/database-schema.md,
decisions.md §2/§11.

Phase 3 note: this infrastructure is built (tables + a minimal recording
helper, app/agent/benchmarking.py) but NOT populated with a real run — this
sandbox has no OCR provider credentials and no curated Malayalam/Tamil/
Hindi/English benchmark image set. decisions.md §11's sequencing rule
("benchmarking happens before final engine selection") is therefore not yet
satisfied; NullOCRProvider remains the only wired provider until a real
benchmark exists. See docs/risks-and-open-questions.md.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BenchmarkDataset(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "benchmark_datasets"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    modality: Mapped[str] = mapped_column(String(20), nullable=False)  # ocr | asr | image_forensics | ...
    language: Mapped[str | None] = mapped_column(String(5), nullable=True)  # ml | ta | hi | en | NULL
    sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_description: Mapped[str | None] = mapped_column(Text, nullable=True)


class BenchmarkRun(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "benchmark_runs"

    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("benchmark_datasets.id"), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(50), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    accuracy_metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # precision/recall/F1/WER/CER
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
