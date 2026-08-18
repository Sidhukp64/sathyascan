"""
Import every model here so Base.metadata is fully populated for Alembic
autogenerate/migrations and for tests that call Base.metadata.create_all().
"""

from app.db.base import Base
from app.models.admin_user import AdminUser
from app.models.analysis import Analysis
from app.models.analysis_session import AnalysisSession
from app.models.appeal import Appeal
from app.models.audit_log import AuditLog
from app.models.benchmark import BenchmarkDataset, BenchmarkRun
from app.models.claim import Claim
from app.models.dashboard_account import DashboardAccount
from app.models.evidence import Evidence
from app.models.explore_claim_cluster import ExploreClaimCluster
from app.models.media_attachment import MediaAttachment
from app.models.media_forensics_result import MediaForensicsResult
from app.models.moderation_report import ModerationReport
from app.models.notification import Notification
from app.models.otp_verification import OtpVerification
from app.models.safety_gate_event import SafetyGateEvent
from app.models.scheduled_check import ScheduledCheck
from app.models.source_credibility import SourceCredibilityRegistry
from app.models.transcript import Transcript
from app.models.url_safety_scan import UrlSafetyScan
from app.models.usage_ledger import UsageLedger
from app.models.user import User
from app.models.video_frame import VideoFrame

__all__ = [
    "Base",
    "User",
    "Analysis",
    "Claim",
    "Evidence",
    "SourceCredibilityRegistry",
    "UsageLedger",
    "MediaAttachment",
    "SafetyGateEvent",
    "MediaForensicsResult",
    "BenchmarkDataset",
    "BenchmarkRun",
    "UrlSafetyScan",
    "Transcript",
    "VideoFrame",
    "DashboardAccount",
    "OtpVerification",
    "AuditLog",
    "ScheduledCheck",
    "Notification",
    "AnalysisSession",
    "ExploreClaimCluster",
    "AdminUser",
    "Appeal",
    "ModerationReport",
]
