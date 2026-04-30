"""FR-CR-05-116 — Zoom Cloud Recordings ingest pipeline.

Mirrors `app/fireflies/` for Zoom as a second meeting source.
"""
from app.zoom.client import ZoomClient, ZoomRecordingMeta
from app.zoom.pipeline import ZoomPipeline

__all__ = ["ZoomClient", "ZoomRecordingMeta", "ZoomPipeline"]
