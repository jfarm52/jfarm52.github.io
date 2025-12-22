"""
Text Cleaner for Bill Processing
=================================
Cleans and filters normalized text to reduce token count for LLM processing.
"""

import re
import logging
from typing import Tuple, List, Set, Dict
from collections import Counter
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class CleaningResult:
    """Result of text cleaning."""
    cleaned_text: str
    evidence_lines: List[str]
    stats: Dict[str, int]


class TextCleaner:
    """
    Cleans normalized bill text by:
    1. Removing repeating headers/footers
    2. Collapsing whitespace
    3. Filtering to keep only relevant lines with context
    4. Capping final text length
    """
    
    KEYWORDS = [
        r'kwh', r'kw\b', r'total', r'amount', r'due', r'\$\d',
        r'charges?', r'billing', r'period', r'account', r'meter',
        r'service', r'rate', r'demand', r'energy', r'electric',
        r'usage', r'consumption', r'read', r'previous', r'current',
        r'balance', r'payment', r'credit', r'adjustment',
        r'peak', r'off-?peak', r'mid-?peak', r'super\s*off',
        r'summer', r'winter', r'baseline', r'tier',
        r'facility', r'transmission', r'distribution',
        r'generation', r'delivery', r'supply'
    ]
    
    DATE_PATTERNS = [
        r'\d{1,2}/\d{1,2}/\d{2,4}',
        r'\d{1,2}-\d{1,2}-\d{2,4}',
        r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s,]+\d{1,2}',
        r'\d{1,2}[\s]+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*',
    ]
    
    MAX_OUTPUT_CHARS = 20000
    CONTEXT_LINES = 2
    HEADER_FOOTER_THRESHOLD = 3
    MIN_LINE_LENGTH = 5
    
    def __init__(self, max_chars: int = None, context_lines: int = None):
        """
        Initialize the text cleaner.
        
        Args:
            max_chars: Maximum output characters (default 20000)
            context_lines: Lines of context around matches (default 2)
        """
        self.max_chars = max_chars or self.MAX_OUTPUT_CHARS
        self.context_lines = context_lines or self.CONTEXT_LINES
        
        keyword_pattern = '|'.join(self.KEYWORDS)
        date_pattern = '|'.join(self.DATE_PATTERNS)
        self.relevance_pattern = re.compile(
            f'({keyword_pattern}|{date_pattern})',
            re.IGNORECASE
        )
    
    def clean(self, text: str) -> CleaningResult:
        """
        Clean and filter normalized text.
        
        Args:
            text: Raw normalized text from NormalizationService
            
        Returns:
            CleaningResult with cleaned text, evidence lines, and stats
        """
        if not text:
            return CleaningResult(
                cleaned_text="",
                evidence_lines=[],
                stats={"original_chars": 0, "final_chars": 0, "lines_kept": 0}
            )
        
        original_chars = len(text)
        
        text = self._collapse_whitespace(text)
        lines = text.split('\n')
        
        repeating = self._find_repeating_lines(lines)
        lines = [line for line in lines if line.strip() not in repeating]
        
        relevant_indices = self._find_relevant_lines(lines)
        
        kept_lines, evidence_lines = self._extract_with_context(lines, relevant_indices)
        
        cleaned_text = '\n'.join(kept_lines)
        
        if len(cleaned_text) > self.max_chars:
            cleaned_text = cleaned_text[:self.max_chars]
            if '\n' in cleaned_text[self.max_chars - 500:]:
                last_newline = cleaned_text.rfind('\n')
                cleaned_text = cleaned_text[:last_newline]
            cleaned_text += "\n[Truncated...]"
        
        return CleaningResult(
            cleaned_text=cleaned_text,
            evidence_lines=evidence_lines[:50],
            stats={
                "original_chars": original_chars,
                "final_chars": len(cleaned_text),
                "original_lines": len(text.split('\n')),
                "lines_kept": len(kept_lines),
                "repeating_removed": len(repeating),
                "relevant_matches": len(relevant_indices)
            }
        )
    
    def _collapse_whitespace(self, text: str) -> str:
        """Collapse multiple spaces/tabs and normalize line endings."""
        text = re.sub(r'\r\n?', '\n', text)
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()
    
    def _find_repeating_lines(self, lines: List[str]) -> Set[str]:
        """Find lines that appear too frequently (likely headers/footers)."""
        line_counts = Counter(line.strip() for line in lines if len(line.strip()) >= self.MIN_LINE_LENGTH)
        return {
            line for line, count in line_counts.items()
            if count >= self.HEADER_FOOTER_THRESHOLD and len(line) < 100
        }
    
    def _find_relevant_lines(self, lines: List[str]) -> Set[int]:
        """Find indices of lines containing relevant keywords or dates."""
        relevant = set()
        for i, line in enumerate(lines):
            if self.relevance_pattern.search(line):
                relevant.add(i)
        return relevant
    
    def _extract_with_context(self, lines: List[str], relevant_indices: Set[int]) -> Tuple[List[str], List[str]]:
        """Extract relevant lines with surrounding context."""
        if not relevant_indices:
            return lines[:100], []
        
        to_keep = set()
        for idx in relevant_indices:
            start = max(0, idx - self.context_lines)
            end = min(len(lines), idx + self.context_lines + 1)
            for i in range(start, end):
                to_keep.add(i)
        
        kept_lines = []
        evidence_lines = []
        prev_idx = -2
        
        for idx in sorted(to_keep):
            if idx > prev_idx + 1 and kept_lines:
                kept_lines.append("...")
            kept_lines.append(lines[idx])
            if idx in relevant_indices:
                evidence_lines.append(lines[idx].strip())
            prev_idx = idx
        
        return kept_lines, evidence_lines
    
    def extract_key_values(self, text: str) -> Dict[str, List[str]]:
        """
        Extract potential key-value pairs from text.
        Useful for pre-extraction validation.
        
        Returns dict with lists of found values for common bill fields.
        """
        patterns = {
            'dollar_amounts': r'\$[\d,]+\.?\d*',
            'kwh_values': r'[\d,]+\.?\d*\s*k[Ww][Hh]',
            'account_numbers': r'(?:account|acct)[#:\s]*([A-Z0-9\-]+)',
            'dates': r'\d{1,2}/\d{1,2}/\d{2,4}',
            'meter_numbers': r'(?:meter)[#:\s]*([A-Z0-9\-]+)',
        }
        
        results = {}
        for name, pattern in patterns.items():
            matches = re.findall(pattern, text, re.IGNORECASE)
            results[name] = matches[:20]
        
        return results
