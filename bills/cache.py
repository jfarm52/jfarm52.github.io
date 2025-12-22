"""
Cache Service for Bill Extraction Results
==========================================
Provides content-addressed caching to avoid re-processing identical bills.
"""

import hashlib
import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

CACHE_VERSION = "v1"


class CacheService:
    """
    Content-addressed cache for bill extraction results.
    
    Uses SHA256 hash of (normalized_text + version_tag) as cache key.
    Stores results in PostgreSQL via bills_db.
    """
    
    def __init__(self, version: str = None):
        """
        Initialize the cache service.
        
        Args:
            version: Version tag for cache invalidation (default: CACHE_VERSION)
        """
        self.version = version or CACHE_VERSION
    
    def compute_hash(self, normalized_text: str) -> str:
        """
        Compute cache hash for normalized text.
        
        Args:
            normalized_text: The normalized text content
            
        Returns:
            SHA256 hex digest (64 chars)
        """
        content = f"{self.version}:{normalized_text}"
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def get_cached_result(self, text_hash: str) -> Optional[Dict[str, Any]]:
        """
        Look up cached extraction result by hash.
        
        Args:
            text_hash: SHA256 hash from compute_hash()
            
        Returns:
            Cached parse result dict if exists, None otherwise
        """
        try:
            from bills_db import get_cached_result_by_hash
            result = get_cached_result_by_hash(text_hash)
            if result:
                logger.info(f"Cache hit for hash {text_hash[:12]}...")
                return result
            return None
        except ImportError:
            logger.warning("bills_db not available, cache disabled")
            return None
        except Exception as e:
            logger.warning(f"Cache lookup failed: {e}")
            return None
    
    def save_result(
        self,
        file_id: int,
        text_hash: str,
        normalized_text: str,
        parse_result: Dict[str, Any],
        metrics: Dict[str, Any]
    ) -> bool:
        """
        Save extraction result to cache.
        
        Args:
            file_id: Database ID of the bill file
            text_hash: SHA256 hash of normalized text
            normalized_text: The normalized text content
            parse_result: Extracted bill data
            metrics: Processing metrics (timing, tokens, etc)
            
        Returns:
            True if saved successfully
        """
        try:
            from bills_db import save_cache_entry
            save_cache_entry(
                file_id=file_id,
                normalized_hash=text_hash,
                normalized_text=normalized_text,
                parse_result=parse_result,
                metrics=metrics
            )
            logger.info(f"Cached result for file {file_id}, hash {text_hash[:12]}...")
            return True
        except ImportError:
            logger.warning("bills_db not available, cache disabled")
            return False
        except Exception as e:
            logger.warning(f"Cache save failed: {e}")
            return False
    
    def check_and_get(self, normalized_text: str) -> tuple[str, Optional[Dict[str, Any]]]:
        """
        Compute hash and check cache in one call.
        
        Args:
            normalized_text: The normalized text content
            
        Returns:
            Tuple of (hash, cached_result or None)
        """
        text_hash = self.compute_hash(normalized_text)
        cached = self.get_cached_result(text_hash)
        return text_hash, cached
    
    def invalidate(self, file_id: int) -> bool:
        """
        Invalidate cache entry for a file.
        
        Args:
            file_id: Database ID of the bill file
            
        Returns:
            True if invalidated successfully
        """
        try:
            from bills_db import invalidate_cache_for_file
            invalidate_cache_for_file(file_id)
            logger.info(f"Invalidated cache for file {file_id}")
            return True
        except ImportError:
            logger.warning("bills_db not available")
            return False
        except Exception as e:
            logger.warning(f"Cache invalidation failed: {e}")
            return False


def build_metrics(
    method: str,
    duration_ms: float,
    tokens_in: int = 0,
    tokens_out: int = 0,
    pages: int = 1,
    char_count: int = 0,
    cache_hit: bool = False,
    **extra
) -> Dict[str, Any]:
    """
    Build a standardized metrics dict for cache storage.
    
    Args:
        method: Extraction method used (pdf_native, pdf_ocr, etc)
        duration_ms: Processing time in milliseconds
        tokens_in: LLM input tokens (if applicable)
        tokens_out: LLM output tokens (if applicable)
        pages: Number of pages processed
        char_count: Character count of normalized text
        cache_hit: Whether this was a cache hit
        **extra: Additional metrics
        
    Returns:
        Standardized metrics dict
    """
    return {
        "method": method,
        "duration_ms": round(duration_ms, 2),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "pages": pages,
        "char_count": char_count,
        "cache_hit": cache_hit,
        "timestamp": datetime.utcnow().isoformat(),
        "cache_version": CACHE_VERSION,
        **extra
    }
