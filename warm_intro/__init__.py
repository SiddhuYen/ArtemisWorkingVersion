"""Warm-introduction pathfinding over public professional information."""

from .client import (
    PathfinderError,
    PathResult,
    RefusalError,
    TruncatedError,
    UnparseableError,
    UsageRecord,
    find_path,
)
from .config import PathfinderConfig, Pricing
from .discovery import DISCOVERY_SYSTEM_PROMPT, DiscoveryResult, find_people
from .parsing import JSONExtractionError, extract_json_object
from .prompt import SYSTEM_PROMPT
from .schema import SchemaError, validate_and_repair

__version__ = "0.1.0"

__all__ = [
    "find_path",
    "find_people",
    "DiscoveryResult",
    "DISCOVERY_SYSTEM_PROMPT",
    "PathResult",
    "PathfinderConfig",
    "Pricing",
    "UsageRecord",
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
