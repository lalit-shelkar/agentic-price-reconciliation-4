"""External tool contracts (spec 08) and their fakes. Shared."""

from .contracts import (
    Agent1Tools,
    Agent2Tools,
    BookingUpdate,
    DraftComms,
    ExtractedClauses,
    NotFound,
    NotificationReceipt,
    ParsedEmail,
    PermissionDenied,
    QuotaExceeded,
    TermSheetDocument,
    ToolError,
    ToolTimeout,
    ToolUnavailable,
    ValidationRejected,
)

__all__ = [
    "Agent1Tools",
    "Agent2Tools",
    "BookingUpdate",
    "DraftComms",
    "ExtractedClauses",
    "NotFound",
    "NotificationReceipt",
    "ParsedEmail",
    "PermissionDenied",
    "QuotaExceeded",
    "TermSheetDocument",
    "ToolError",
    "ToolTimeout",
    "ToolUnavailable",
    "ValidationRejected",
]
