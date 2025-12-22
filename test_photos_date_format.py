#!/usr/bin/env python3
"""
Unit test for Photos folder date formatting (M.D.YY format, no leading zeros).
Tests the format_date_no_leading_zeros function from backend_upload_to_dropbox.py
"""

import sys
import datetime
from backend_upload_to_dropbox import format_date_no_leading_zeros

def test_date_formatting():
    """Test that dates are formatted as M.D.YY with no leading zeros"""
    
    test_cases = [
        # (datetime_obj, expected_format)
        (datetime.datetime(2025, 12, 1), "12.1.25"),      # December 1 → 12.1.25
        (datetime.datetime(2025, 12, 10), "12.10.25"),    # December 10 → 12.10.25
        (datetime.datetime(2025, 1, 1), "1.1.25"),        # January 1 → 1.1.25
        (datetime.datetime(2025, 1, 31), "1.31.25"),      # January 31 → 1.31.25
        (datetime.datetime(2025, 2, 28), "2.28.25"),      # February 28 → 2.28.25
        (datetime.datetime(2024, 3, 5), "3.5.24"),        # March 5, 2024 → 3.5.24
        (datetime.datetime(2024, 10, 15), "10.15.24"),    # October 15, 2024 → 10.15.24
        (datetime.datetime(2000, 1, 1), "1.1.00"),        # January 1, 2000 → 1.1.00
        (datetime.datetime(2099, 12, 31), "12.31.99"),    # December 31, 2099 → 12.31.99
    ]
    
    failed = []
    passed = 0
    
    print("=" * 60)
    print("Testing Photos folder date formatting (M.D.YY format)")
    print("=" * 60)
    
    for date_obj, expected in test_cases:
        result = format_date_no_leading_zeros(date_obj)
        status = "✓ PASS" if result == expected else "✗ FAIL"
        
        if result != expected:
            failed.append({
                'date': date_obj,
                'expected': expected,
                'got': result
            })
        else:
            passed += 1
        
        print(f"{status}: {date_obj.strftime('%Y-%m-%d')} → {result} (expected: {expected})")
    
    print("=" * 60)
    print(f"Results: {passed}/{len(test_cases)} tests passed")
    print("=" * 60)
    
    if failed:
        print("\n❌ FAILURES:")
        for failure in failed:
            print(f"  {failure['date'].strftime('%Y-%m-%d')}: expected '{failure['expected']}', got '{failure['got']}'")
        return 1
    else:
        print("\n✅ All tests passed!")
        return 0

if __name__ == '__main__':
    sys.exit(test_date_formatting())
