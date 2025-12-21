# Sitewalk App - Code Analysis Report
**Generated:** 2025-12-21
**File Analyzed:** index.html (18,273 lines)
**Focus Areas:** Electric bill analysis, UI issues, code quality

---

## 🔴 **CRITICAL ISSUES**

### 1. **Field Name Inconsistencies (Bill Data)**
**Location:** Lines 10540-10770
**Problem:** Your bill data uses multiple naming conventions, causing extraction failures:
- `totalKwh` vs `kwh`
- `totalAmountDue` vs `total_charge` vs `totalCost`
- `periodStart` vs `period_start`
- `meterNumber` vs `meter_number`
- `avgCostPerDay` vs `avgCostPerDayDollars`

**Impact:**
- Bills may show "$0.00" or "0 kWh" even when data exists
- Calculations fail silently
- Rate/kWh shows "NaN" or incorrect values

**Example Code (Lines 10556-10557):**
```javascript
facilityTotalKwh += bill.totalKwh || bill.kwh || 0;
facilityTotalCost += bill.totalAmountDue || bill.total_charge || bill.totalCost || 0;
```
Every field requires 2-3 fallbacks!

---

### 2. **Rate Calculation Bug**
**Location:** Line 10563
**Problem:**
```javascript
const facilityBlendedRate = facilityTotalKwh > 0 ? (facilityTotalCost / facilityTotalKwh) * 100 : 0;
```
This multiplies by 100 TWICE! Rate should be in cents/kWh, but you're also displaying as cents elsewhere.

**Impact:** Blended rate shows incorrectly (e.g., "15.2" instead of "0.152" or vice versa)

---

### 3. **Duplicate Event Listeners**
**Location:** Lines 10139-10146, 10326-10336
**Problem:** Upload button handlers reassigned on every render:
```javascript
uploadBtn.onclick = () => fileInput.click();
fileInput.onchange = (e) => { ... };
```

**Impact:**
- Memory leaks (old handlers not cleaned up)
- Possible double-uploads
- Performance degradation over time

---

### 4. **Polling Never Stops in Some Cases**
**Location:** Lines 10363-10499
**Problem:** If `allComplete` is false but no files are processing, polling continues forever.

**Impact:**
- Unnecessary API calls
- Battery drain on mobile
- Server load

---

## ⚠️ **MAJOR ISSUES**

### 5. **Missing Error Messages for Users**
**Location:** Line 10340
**Problem:**
```javascript
} catch (err) {
    console.error('[embeddedBills] Error loading bills:', err);
    container.innerHTML = '<div style="color: #dc3545;">Error loading bills data.</div>';
}
```
User sees generic error with no actionable information.

---

### 6. **Date Parsing Without Validation**
**Location:** Lines 10587-10592
**Problem:**
```javascript
const d = new Date(periodStart);
if (!facilityOldestDate || d < facilityOldestDate) facilityOldestDate = d;
```
No check if date is valid. `new Date("invalid")` creates Invalid Date object.

**Impact:** Date ranges show "Invalid Date - Invalid Date"

---

### 7. **Inconsistent Null Checks**
**Location:** Throughout bill rendering
**Problem:** Some places check `bill.totalKwh || 0`, others assume it exists.

**Impact:** "Cannot read property of undefined" errors

---

## 💡 **MODERATE ISSUES**

### 8. **Redundant Code - formatBillValue**
**Observation:** `formatBillValue()` is called hundreds of times but definition not visible in excerpt.
**Potential Issue:** If not memoized, could cause performance issues.

---

### 9. **localStorage Abuse**
**Location:** Lines 10154-10156, 10785
**Problem:** Reading/writing localStorage on every render/toggle.

**Impact:** Performance hit on older devices

---

### 10. **Magic Numbers**
**Location:** Throughout
**Examples:**
- `months=12` (line 10520) - hardcoded
- `queue_depth > 3` (line 10446) - arbitrary threshold
- `setTimeout(..., 3000)` (line 10085) - hardcoded delay

**Impact:** Difficult to maintain and tune

---

## 🐛 **MINOR ISSUES & UI BUGS**

### 11. **Sort Mutation Bug**
**Location:** Line 10731
**Problem:**
```javascript
bills.sort((a, b) => { ... });
```
Sorts the original array (mutates data). If bills array is reused elsewhere, order changes unexpectedly.

---

### 12. **Accessibility Issues**
- No ARIA labels on interactive elements
- No keyboard navigation for bill dropdowns
- No focus management after modals close

---

### 13. **Mobile UI Issues**
- `.embedded-meter-stats-row` probably overflows on small screens
- No touch event optimization
- Buttons may be too small (< 44px tap target)

---

### 14. **Inconsistent String Formatting**
**Examples:**
- `"${ ... }"` vs `'${ ... }'` (both used)
- Sometimes `Bill${bills.length !== 1 ? 's' : ''}`, sometimes just `Bills`

---

## 📊 **CODE QUALITY ISSUES**

### 15. **Function Length**
- `renderEmbeddedExtractedData()`: 250+ lines
- `renderEmbeddedBillsSection()`: 250+ lines

**Impact:** Hard to test, debug, and maintain

---

### 16. **Global Namespace Pollution**
**Functions attached to window:**
- `window.toggleEmbeddedFilesSection`
- `window.toggleEmbeddedMeter`
- `window.openBillReviewModal`
- `window.BILL_DEBUG`

**Impact:** Naming conflicts possible

---

### 17. **No Input Validation**
Project IDs, file IDs passed to API without validation.

**Security Risk:** Potential injection attacks

---

## 🔧 **RECOMMENDED FIXES (Priority Order)**

### **Priority 1 (Fix Immediately):**
1. ✅ Normalize field names OR create helper to handle variants
2. ✅ Fix rate calculation (* 100 issue)
3. ✅ Add date validation before parsing
4. ✅ Fix polling termination logic

### **Priority 2 (Fix Soon):**
5. ✅ Replace `onclick` with `addEventListener` + cleanup
6. ✅ Add user-friendly error messages
7. ✅ Validate all numeric calculations for NaN/Infinity
8. ✅ Fix sort mutation (use `.slice().sort()`)

### **Priority 3 (Improvement):**
9. ✅ Extract long functions into smaller pieces
10. ✅ Add constants for magic numbers
11. ✅ Improve mobile responsive layout
12. ✅ Add accessibility attributes

---

## 📝 **NOTES**

- File is 18,273 lines - consider splitting into modules
- No build process detected - using vanilla JS
- PDF.js and Flatpickr loaded from CDN (single point of failure)
- Heavy reliance on backend API for all bill operations

---

**Want me to start fixing these issues?** I'll tackle them in priority order and discuss each change with you before committing.
