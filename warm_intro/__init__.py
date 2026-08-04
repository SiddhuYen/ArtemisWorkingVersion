"""Warm-introduction pathfinding over public professional information."""

from .client import (
    PathfinderError,
    PathResult,
    RefusalError,
    TruncatedError,
    UnparseableError,
    UsageRecord,
    find_path,
    verify_existing,
)
from .config import PathfinderConfig, Pricing, VerifyConfig
from .parsing import JSONExtractionError, extract_json_object
from .prompt import SYSTEM_PROMPT
from .schema import SchemaError, validate_and_repair
from .verify import UrlCheck, verify_hops

__version__ = "0.1.0"

__all__ = [
    "find_path",
    "verify_existing",
    "PathResult",
    "PathfinderConfig",
    "VerifyConfig",
    "Pricing",
    "UsageRecord",
    "UrlCheck",
    "verify_hops",
    "validate_and_repair",
    "extract_json_object",
    "SYSTEM_PROMPT",
    "PathfinderError",
    "RefusalError",
    "TruncatedError",
    "UnparseableError",
    "SchemaError",
    "JSONExtractionError",
    "__version__",
]
