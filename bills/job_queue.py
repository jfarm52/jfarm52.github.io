"""
Job Queue for Bill Processing
==============================
Manages async bill processing jobs with status tracking.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, Any, Callable, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class JobState(Enum):
    """Possible states for a bill processing job."""
    QUEUED = "queued"
    EXTRACTING_TEXT = "extracting_text"
    OCR = "ocr"
    CLEANING = "cleaning"
    CACHED_HIT = "cached_hit"
    PARSING_PASS_A = "parsing_pass_a"
    PARSING_PASS_B = "parsing_pass_b"
    DONE = "done"
    FAILED = "failed"


@dataclass
class JobStatus:
    """Status information for a processing job."""
    file_id: int
    state: JobState
    progress: float  # 0.0 to 1.0
    message: str = ""
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "file_id": self.file_id,
            "state": self.state.value,
            "progress": self.progress,
            "message": self.message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
        }


class JobQueue:
    """
    Manages bill processing jobs using a thread pool.
    
    Features:
    - ThreadPoolExecutor with configurable max_workers
    - Job state tracking for frontend polling
    - Completion callbacks
    - Thread-safe status updates
    """
    
    STATE_PROGRESS = {
        JobState.QUEUED: 0.0,
        JobState.EXTRACTING_TEXT: 0.1,
        JobState.OCR: 0.2,
        JobState.CLEANING: 0.3,
        JobState.CACHED_HIT: 0.9,
        JobState.PARSING_PASS_A: 0.5,
        JobState.PARSING_PASS_B: 0.7,
        JobState.DONE: 1.0,
        JobState.FAILED: 1.0,
    }
    
    def __init__(self, max_workers: int = 4):
        """
        Initialize the job queue.
        
        Args:
            max_workers: Maximum concurrent processing threads
        """
        self.max_workers = max_workers
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix='bill_processor'
        )
        self._jobs: Dict[int, JobStatus] = {}
        self._futures: Dict[int, Future] = {}
        self._lock = threading.Lock()
        self._callbacks: Dict[int, Callable] = {}
    
    def submit(
        self,
        file_id: int,
        process_func: Callable[..., Dict[str, Any]],
        *args,
        on_complete: Optional[Callable[[int, Dict[str, Any]], None]] = None,
        **kwargs
    ) -> bool:
        """
        Submit a bill processing job.
        
        Args:
            file_id: Database ID of the bill file
            process_func: Function to call for processing
            *args: Arguments to pass to process_func
            on_complete: Optional callback when job completes
            **kwargs: Keyword arguments for process_func
            
        Returns:
            True if job was submitted, False if already queued
        """
        with self._lock:
            if file_id in self._jobs and self._jobs[file_id].state not in (JobState.DONE, JobState.FAILED):
                logger.warning(f"Job {file_id} already in progress")
                return False
            
            now = datetime.utcnow()
            self._jobs[file_id] = JobStatus(
                file_id=file_id,
                state=JobState.QUEUED,
                progress=0.0,
                message="Queued for processing",
                started_at=now,
                updated_at=now
            )
            
            if on_complete:
                self._callbacks[file_id] = on_complete
        
        def wrapped_process():
            try:
                result = process_func(file_id, self, *args, **kwargs)
                self._on_job_complete(file_id, result)
                return result
            except Exception as e:
                logger.exception(f"Job {file_id} failed")
                self._on_job_failed(file_id, str(e))
                raise
        
        future = self.executor.submit(wrapped_process)
        with self._lock:
            self._futures[file_id] = future
        
        logger.info(f"Submitted job for file {file_id}")
        return True
    
    def update_state(
        self,
        file_id: int,
        state: JobState,
        message: str = "",
        progress: Optional[float] = None
    ) -> None:
        """
        Update job state. Call this from within the processing function.
        
        Args:
            file_id: Database ID of the bill file
            state: New job state
            message: Status message for frontend
            progress: Optional progress override (0.0-1.0)
        """
        with self._lock:
            if file_id not in self._jobs:
                logger.warning(f"Cannot update unknown job {file_id}")
                return
            
            job = self._jobs[file_id]
            job.state = state
            job.message = message
            job.updated_at = datetime.utcnow()
            job.progress = progress if progress is not None else self.STATE_PROGRESS.get(state, 0.5)
            
            logger.debug(f"Job {file_id}: {state.value} ({job.progress:.0%}) - {message}")
    
    def get_status(self, file_id: int) -> Optional[JobStatus]:
        """
        Get current status of a job.
        
        Args:
            file_id: Database ID of the bill file
            
        Returns:
            JobStatus if job exists, None otherwise
        """
        with self._lock:
            return self._jobs.get(file_id)
    
    def get_status_dict(self, file_id: int) -> Optional[Dict[str, Any]]:
        """
        Get current status as a JSON-serializable dict.
        
        Args:
            file_id: Database ID of the bill file
            
        Returns:
            Status dict if job exists, None otherwise
        """
        status = self.get_status(file_id)
        return status.to_dict() if status else None
    
    def _on_job_complete(self, file_id: int, result: Dict[str, Any]) -> None:
        """Handle job completion."""
        with self._lock:
            if file_id in self._jobs:
                job = self._jobs[file_id]
                job.state = JobState.DONE
                job.progress = 1.0
                job.message = "Processing complete"
                job.completed_at = datetime.utcnow()
                job.result = result
            
            callback = self._callbacks.pop(file_id, None)
        
        if callback:
            try:
                callback(file_id, result)
            except Exception as e:
                logger.exception(f"Callback failed for job {file_id}")
        
        logger.info(f"Job {file_id} completed successfully")
    
    def _on_job_failed(self, file_id: int, error: str) -> None:
        """Handle job failure."""
        with self._lock:
            if file_id in self._jobs:
                job = self._jobs[file_id]
                job.state = JobState.FAILED
                job.progress = 1.0
                job.message = f"Failed: {error}"
                job.error = error
                job.completed_at = datetime.utcnow()
            
            self._callbacks.pop(file_id, None)
        
        logger.error(f"Job {file_id} failed: {error}")
    
    def is_processing(self, file_id: int) -> bool:
        """Check if a job is currently processing."""
        with self._lock:
            if file_id not in self._jobs:
                return False
            return self._jobs[file_id].state not in (JobState.DONE, JobState.FAILED)
    
    def cancel(self, file_id: int) -> bool:
        """
        Attempt to cancel a job.
        
        Args:
            file_id: Database ID of the bill file
            
        Returns:
            True if cancelled, False if not possible
        """
        with self._lock:
            future = self._futures.get(file_id)
            if future and not future.done():
                cancelled = future.cancel()
                if cancelled:
                    if file_id in self._jobs:
                        self._jobs[file_id].state = JobState.FAILED
                        self._jobs[file_id].error = "Cancelled"
                    logger.info(f"Job {file_id} cancelled")
                    return True
        return False
    
    def get_active_count(self) -> int:
        """Get count of active (non-completed) jobs."""
        with self._lock:
            return sum(
                1 for job in self._jobs.values()
                if job.state not in (JobState.DONE, JobState.FAILED)
            )
    
    def cleanup_old_jobs(self, max_age_seconds: int = 3600) -> int:
        """
        Remove completed job records older than max_age_seconds.
        
        Returns:
            Number of jobs cleaned up
        """
        cutoff = datetime.utcnow()
        cleaned = 0
        
        with self._lock:
            to_remove = []
            for file_id, job in self._jobs.items():
                if job.state in (JobState.DONE, JobState.FAILED):
                    if job.completed_at:
                        age = (cutoff - job.completed_at).total_seconds()
                        if age > max_age_seconds:
                            to_remove.append(file_id)
            
            for file_id in to_remove:
                del self._jobs[file_id]
                self._futures.pop(file_id, None)
                cleaned += 1
        
        if cleaned:
            logger.info(f"Cleaned up {cleaned} old job records")
        
        return cleaned
    
    def shutdown(self, wait: bool = True) -> None:
        """
        Shutdown the executor.
        
        Args:
            wait: Whether to wait for pending jobs to complete
        """
        logger.info("Shutting down job queue...")
        self.executor.shutdown(wait=wait)


_global_queue: Optional[JobQueue] = None
_queue_lock = threading.Lock()


def get_job_queue(max_workers: int = 4) -> JobQueue:
    """
    Get or create the global job queue singleton.
    
    Args:
        max_workers: Maximum concurrent workers (only used on first call)
        
    Returns:
        The global JobQueue instance
    """
    global _global_queue
    with _queue_lock:
        if _global_queue is None:
            _global_queue = JobQueue(max_workers=max_workers)
        return _global_queue
