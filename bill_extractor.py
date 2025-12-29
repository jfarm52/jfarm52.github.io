"""
Bill Extractor Module - Uses xAI Grok 4 for PDF bill data extraction
Supports SCE, LADWP, and other utility bills with detailed TOU breakdown
"""

import os
import json
import base64
try:
    import pymupdf as fitz  # PyMuPDF 1.26+
except ImportError:
    import fitz  # PyMuPDF legacy
from openai import OpenAI

XAI_API_KEY = os.environ.get("XAI_API_KEY")

def get_xai_client():
    """Get xAI client instance using OpenAI-compatible API"""
    if not XAI_API_KEY:
        raise ValueError("XAI_API_KEY environment variable not set")
    return OpenAI(
        api_key=XAI_API_KEY,
        base_url="https://api.x.ai/v1"
    )

def pdf_to_images(file_path, max_pages=10):
    """Convert PDF pages to base64-encoded images for vision API"""
    images = []
    try:
        doc = fitz.open(file_path)
        for page_num in range(min(len(doc), max_pages)):
            page = doc[page_num]
            mat = fitz.Matrix(150/72, 150/72)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            b64_img = base64.b64encode(img_bytes).decode('utf-8')
            images.append(b64_img)
        doc.close()
    except Exception as e:
        print(f"[bill_extractor] Error converting PDF to images: {e}")
    return images


def file_to_images(file_path, max_pages=10):
    """
    Convert a file (PDF or image) to base64-encoded images for vision API.
    Returns list of tuples: (base64_data, mime_type) for proper data URL construction.
    """
    ext = os.path.splitext(file_path)[1].lower()
    
    mime_map = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.heic': 'image/heic',
        '.bmp': 'image/bmp',
        '.tiff': 'image/tiff'
    }
    
    if ext == '.pdf':
        images = pdf_to_images(file_path, max_pages)
        return [(img, 'image/png') for img in images]
    elif ext in mime_map:
        try:
            with open(file_path, 'rb') as f:
                img_bytes = f.read()
                b64_img = base64.b64encode(img_bytes).decode('utf-8')
                return [(b64_img, mime_map[ext])]
        except Exception as e:
            print(f"[bill_extractor] Error reading image file: {e}")
            return []
    else:
        print(f"[bill_extractor] Unsupported file type: {ext}")
        return []


def clean_numeric(val):
    """
    Clean a numeric value by stripping $ and commas.
    Returns float or None if parsing fails.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace('$', '').replace(',', '').strip())
    except (ValueError, TypeError):
        return None


def parse_dollar_rate(raw):
    """
    Parse a dollar rate string to float.
    Handles formats like: "$0.77194", "0.33583", "0.20657/kWh", "$0.20657 USD"
    Returns rate in dollars/kWh as a float.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    
    cleaned = (
        str(raw)
        .replace('$', '')
        .replace('USD', '')
        .replace('/kWh', '')
        .replace('kWh', '')
        .strip()
    )
    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize_utility_name(raw):
    """
    Normalize utility company names to a canonical form.
    This prevents duplicate accounts when the LLM returns different variations.
    """
    if not raw:
        return "Unknown"
    name = raw.strip().lower()
    
    # SCE aliases
    if "southern california edison" in name or name == "sce":
        return "Southern California Edison"
    
    # SDG&E aliases
    if "san diego gas" in name or name == "sdge" or name == "sdg&e":
        return "San Diego Gas & Electric"
    
    # LADWP aliases
    if "los angeles department of water" in name or name == "ladwp":
        return "LADWP"
    
    # PG&E aliases
    if "pacific gas" in name or name == "pge" or name == "pg&e":
        return "Pacific Gas & Electric"
    
    # Return original with proper casing if no match
    return raw.strip()


def save_bill_to_normalized_tables(file_id, project_id, extracted_data):
    """
    Save extracted bill data to the normalized bills and bill_tou_periods tables.
    This is called after successful extraction.
    Idempotent: deletes existing bills for this file_id before inserting new ones.
    """
    # DEBUG: Log what we received
    print(f"\n{'='*80}")
    print(f"[bill_extractor] save_bill_to_normalized_tables called for file_id={file_id}")
    print(f"[bill_extractor] extracted_data keys: {list(extracted_data.keys())}")
    print(f"[bill_extractor] extracted_data: {extracted_data}")
    print(f"{'='*80}\n")

    try:
        from bills_db import (
            upsert_utility_account, upsert_utility_meter,
            insert_bill, insert_bill_tou_period, delete_bills_for_file,
            update_bill_file_review_status
        )

        # Delete any existing bills for this file to prevent duplicates on re-extraction
        delete_bills_for_file(file_id)

        # Merge detailed_data into top-level for easier access
        # The extraction returns nested structure with detailed_data containing most values
        detailed = extracted_data.get('detailed_data', {})

        # Helper to get value from either top-level or detailed_data
        def get_val(*keys):
            for key in keys:
                val = extracted_data.get(key)
                if val is not None:
                    return val
                val = detailed.get(key)
                if val is not None:
                    return val
            return None
        
        utility_name = normalize_utility_name(get_val('utility_name'))
        account_number = get_val('customer_account', 'account_number')
        
        if not utility_name or not account_number:
            print(f"[bill_extractor] Cannot save to normalized tables: missing utility_name or account_number")
            return False
        
        # Get or create account
        account_id = upsert_utility_account(project_id, utility_name, account_number)
        
        # Get meter info
        meters = extracted_data.get('meters', [])
        service_address = get_val('service_address', '')
        rate_schedule = get_val('rate', 'rate_schedule', '')

        # VALIDATE AI EXTRACTIONS: Reject bad data to force regex fallback
        # Rate schedules should be SHORT CODES like "TOU-GS-2-E", NOT long disclaimer text
        if rate_schedule:
            # Check for common disclaimer phrases that indicate wrong extraction
            bad_phrases = ['contact', 'commission', 'safety', 'disconnected',
                          'for more information', 'please', 'may', 'ensure',
                          'service is', 'you may', 'reasons', 'public utilities']
            has_bad_phrase = any(phrase in rate_schedule.lower() for phrase in bad_phrases)
            is_too_long = len(rate_schedule) > 25  # Rate codes are typically 5-20 chars

            if has_bad_phrase or is_too_long:
                print(f"[bill_extractor] Rejecting bad rate_schedule from AI: '{rate_schedule[:60]}...'")
                rate_schedule = ''  # Force regex fallback

        # Service address should be reasonably complete (not just partial)
        # Force regex fallback if address is suspiciously short (likely incomplete)
        service_address_original = service_address
        if service_address and len(service_address) < 20:
            print(f"[bill_extractor] Service address seems incomplete (< 20 chars): '{service_address}' - will try regex fallback")
            service_address = ''  # Force regex to try finding a better one

        # UNIVERSAL regex fallback for missing fields (works for ALL utilities)
        raw_text = extracted_data.get('_raw_text', '')

        if raw_text:
            import re

            # Fallback: Extract rate schedule from text patterns (ALL utilities)
            if not rate_schedule or rate_schedule.strip() == '':
                rate_patterns = [
                    r'Rate\s*Schedule\s*[:\-]?\s*([A-Z0-9\-]+(?:\s[A-Z0-9\-]+)?)',  # SHORT CODES: "TOU-GS-2-E", "EV2-A"
                    r'RATE\s*SCHEDULE\s*[:\-]?\s*([A-Z0-9\-]+(?:\s[A-Z0-9\-]+)?)',  # Uppercase variant
                    r'Rate\s*Plan\s*[:\-]?\s*([A-Z0-9\-]+(?:\s[A-Z0-9\-]+)?)',      # PG&E, SDG&E use "Rate Plan"
                    r'Tariff\s*[:\-]?\s*([A-Z0-9\-]+(?:\s[A-Z0-9\-]+)?)',           # Some utilities use "Tariff"
                    r'Service\s*Class\s*[:\-]?\s*([A-Z0-9\-]+)',                    # Alternative labeling
                    r'Schedule\s*[:\-]?\s*([A-Z0-9\-]+(?:\s[A-Z0-9\-]+)?)',         # Just "Schedule:"
                ]
                for pattern in rate_patterns:
                    match = re.search(pattern, raw_text, re.MULTILINE)
                    if match:
                        rate_schedule = match.group(1).strip()
                        # Validate it's a reasonable rate code (5-25 chars, alphanumeric with hyphens)
                        if 3 <= len(rate_schedule) <= 25 and not any(word in rate_schedule.lower() for word in ['contact', 'please', 'may', 'service']):
                            print(f"[bill_extractor] Regex fallback extracted rate_schedule: {rate_schedule}")
                            break
                        else:
                            rate_schedule = ''  # Invalid, keep searching

            # Fallback: Extract service_address from text patterns (ALL utilities)
            if not service_address or service_address.strip() == '':
                address_patterns = [
                    r'SERVICE\s*ADDRESS[:\-]?\s*(.{10,100})',        # "SERVICE ADDRESS" label
                    r'Service\s*Location[:\-]?\s*(.{10,100})',       # Alternative label
                    r'Premise\s*Address[:\-]?\s*(.{10,100})',        # SDG&E uses this
                    r'Site\s*Address[:\-]?\s*(.{10,100})',           # Some utilities
                    # Generic street address pattern (matches any valid US address)
                    r'(\d{2,5}\s+[A-Z][A-Za-z\s]+(?:Street|ST|Avenue|AVE|Boulevard|BLVD|Road|RD|Drive|DR|Lane|LN|Way|WAY|Court|CT|Place|PL|Circle|CIR|Parkway|PKY)[^\n]{0,50})',
                ]
                for pattern in address_patterns:
                    match = re.search(pattern, raw_text, re.IGNORECASE)
                    if match:
                        addr_text = match.group(1)
                        # Stop at newline or common delimiters
                        addr_text = re.split(r'\n|POD-ID|BILLING|ACCOUNT|METER', addr_text, maxsplit=1)[0]
                        service_address = addr_text.strip()
                        print(f"[bill_extractor] Regex fallback extracted service_address: {service_address}")
                        break

                # If regex didn't find anything and we had an original short address, restore it
                if (not service_address or service_address.strip() == '') and service_address_original:
                    service_address = service_address_original
                    print(f"[bill_extractor] Regex found no address, keeping original: {service_address}")

        # Get billing period
        period_start = get_val('billing_period_start')
        period_end = get_val('billing_period_end')
        due_date = get_val('due_date')

        # VALIDATE due_date: Reject "N/A" or invalid values
        if due_date and (due_date.upper() == 'N/A' or due_date.upper() == 'NA' or due_date == 'None'):
            print(f"[bill_extractor] Rejecting invalid due_date from AI: '{due_date}'")
            due_date = None  # Force regex fallback

        # UNIVERSAL regex fallback for due_date (ALL utilities)
        if not due_date and raw_text:
            import re
            due_patterns = [
                r'Due\s*Date\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})',                    # "Due Date: 12/31/2024"
                r'Due\s*Date\s*[:\-]?\s*(\w+\s+\d{1,2},?\s+\d{4})',                  # "Due Date: Dec 31, 2024"
                r'Payment\s*Due\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})',                 # "Payment Due: ..."
                r'Payment\s*Due\s*[:\-]?\s*(\w+\s+\d{1,2},?\s+\d{4})',               # "Payment Due: Dec 31, 2024"
                r'AUTO\s*PAYMENT\s*[:\-]?\s*(\w+\s+\d{1,2},?\s+\d{4})',              # LADWP: "AUTO PAYMENT Dec 12, 2025"
                r'AUTO\s*PAYMENT\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})',                # LADWP numeric format
                r'Pay\s*By\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})',                      # "Pay By: ..."
                r'Pay\s*By\s*[:\-]?\s*(\w+\s+\d{1,2},?\s+\d{4})',                    # "Pay By: Dec 31, 2024"
                r'DUE\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})',                           # "DUE: 12/31/2024"
                r'DUE\s*[:\-]?\s*(\w+\s+\d{1,2},?\s+\d{4})',                         # "DUE: Dec 31, 2024"
            ]
            for pattern in due_patterns:
                match = re.search(pattern, raw_text, re.IGNORECASE)
                if match:
                    due_date = match.group(1).strip()
                    print(f"[bill_extractor] Regex fallback extracted due_date: {due_date}")
                    break

        # GENERIC TOU (Time-of-Use) extraction fallback for ALL utilities
        # This works for LADWP, SCE, PG&E, SDG&E, and other utilities with TOU pricing
        raw_text = extracted_data.get('_raw_text', '')
        tou_breakdown_from_regex = []

        if raw_text:
            import re

            # Check if bill has TOU data by looking for TOU keywords
            has_tou_keywords = bool(re.search(
                r'\b(TOU|Time[\s\-]*of[\s\-]*Use|Peak|High[\s\-]*Peak|Low[\s\-]*Peak|On[\s\-]*Peak|Mid[\s\-]*Peak|Off[\s\-]*Peak|Super[\s\-]*Off[\s\-]*Peak|Base[\s\-]*Period)\b',
                raw_text,
                re.IGNORECASE
            ))

            if has_tou_keywords:
                print(f"[bill_extractor] Detected TOU keywords in bill text - attempting regex extraction")

                # Generic TOU patterns that match most utility bill formats
                # Pattern 1: "Period Name" followed by kWh value and optionally cost
                # Examples:
                #   "High Peak 1,234 kWh $123.45"
                #   "On-Peak 5,678 $567.89"
                #   "Mid Peak Energy 2,345 kWh"
                tou_patterns = [
                    # Pattern: [Period Name] [number with commas] kWh [optional: $ cost]
                    r'(High[\s\-]*Peak|Low[\s\-]*Peak|Base|On[\s\-]*Peak|Mid[\s\-]*Peak|Off[\s\-]*Peak|Super[\s\-]*Off[\s\-]*Peak)[\s:]+([\d,]+\.?\d*)\s*kWh(?:\s+\$?([\d,]+\.?\d*))?',

                    # Pattern: Rate table format - "Period | kWh | Rate | Cost"
                    r'(High[\s\-]*Peak|Low[\s\-]*Peak|Base|On[\s\-]*Peak|Mid[\s\-]*Peak|Off[\s\-]*Peak|Super[\s\-]*Off[\s\-]*Peak)[^\d\n]{0,20}([\d,]+\.?\d*)[^\d\n]{0,20}\$?([\d,]+\.?\d*)[^\d\n]{0,20}\$?([\d,]+\.?\d*)',

                    # Pattern: Compact format - "Period: number kWh @ $rate = $cost"
                    r'(High[\s\-]*Peak|Low[\s\-]*Peak|Base|On[\s\-]*Peak|Mid[\s\-]*Peak|Off[\s\-]*Peak|Super[\s\-]*Off[\s\-]*Peak)[:\s]+([\d,]+\.?\d*)\s*kWh\s*@?\s*\$?([\d\.]+)\s*=?\s*\$?([\d,]+\.?\d*)',
                ]

                matches = []
                for pattern in tou_patterns:
                    for match in re.finditer(pattern, raw_text, re.IGNORECASE):
                        period_name = match.group(1).strip()
                        kwh_str = match.group(2).replace(',', '').strip()

                        # Try to get cost from capture group 3 or 4
                        cost_str = None
                        rate_str = None
                        if len(match.groups()) >= 3 and match.group(3):
                            # Could be rate or cost - check if group 4 exists
                            if len(match.groups()) >= 4 and match.group(4):
                                # Group 3 is rate, group 4 is cost
                                rate_str = match.group(3).replace(',', '').strip()
                                cost_str = match.group(4).replace(',', '').strip()
                            else:
                                # Group 3 is cost
                                cost_str = match.group(3).replace(',', '').strip()

                        try:
                            kwh = float(kwh_str)
                            cost = float(cost_str) if cost_str else None
                            rate = float(rate_str) if rate_str else None

                            # Normalize period name for consistency
                            period_normalized = ' '.join(period_name.split()).title()

                            # Check if we already have this period (avoid duplicates)
                            existing = next((m for m in matches if m['period'] == period_normalized), None)
                            if not existing:
                                tou_entry = {
                                    'period': period_normalized,
                                    'kwh': kwh,
                                    'rate': rate,
                                    'estimated_cost': cost
                                }
                                matches.append(tou_entry)
                                print(f"[bill_extractor] Regex TOU extraction: {period_normalized} = {kwh} kWh" + (f" @ ${rate}/kWh = ${cost}" if rate and cost else ""))
                        except (ValueError, TypeError) as e:
                            print(f"[bill_extractor] Failed to parse TOU values: {e}")
                            continue

                if matches:
                    tou_breakdown_from_regex = matches
                    print(f"[bill_extractor] Generic TOU regex extraction found {len(matches)} periods")
                else:
                    print(f"[bill_extractor] TOU keywords detected but no TOU data extracted via regex")
            else:
                print(f"[bill_extractor] No TOU keywords detected - skipping TOU extraction")

        # Get totals from detailed_data or top-level
        total_kwh = get_val('kwh_total', 'total_kwh')
        total_amount = get_val('amount_due', 'total_amount_due', 'total_owed', 'new_charges')
        
        # Get charge breakdown
        energy_charges = get_val('energy_charges_total', 'energy_charges')
        demand_charges = get_val('demand_charges_total', 'total_facilities_demand_charge', 'demand_charges')
        taxes = get_val('taxes_total', 'taxes', 'total_taxes')
        other_charges = get_val('other_charges_total', 'other_charges')
        
        total_kwh = clean_numeric(total_kwh)
        total_amount = clean_numeric(total_amount)
        energy_charges = clean_numeric(energy_charges)
        demand_charges = clean_numeric(demand_charges)
        taxes = clean_numeric(taxes)
        other_charges = clean_numeric(other_charges)
        
        # Extract TOU data from extracted_data using get_val helper
        # Try various key patterns for TOU kWh values
        # LADWP uses "High Peak" = On Peak, "Low Peak" = Off Peak
        tou_on_kwh = clean_numeric(get_val('kwh_on_peak', 'on_peak_kwh', 'tou_on_kwh', 'tou_high_peak_kwh'))
        tou_mid_kwh = clean_numeric(get_val('kwh_mid_peak', 'mid_peak_kwh', 'tou_mid_kwh'))
        tou_off_kwh = clean_numeric(get_val('kwh_off_peak', 'off_peak_kwh', 'tou_off_kwh', 'tou_low_peak_kwh'))
        
        # Extract TOU rate values (LADWP High Peak = On Peak, Low Peak = Off Peak)
        tou_on_rate = parse_dollar_rate(get_val('rate_on_peak_per_kwh', 'on_peak_rate', 'tou_on_rate_dollars', 'tou_high_peak_rate'))
        tou_mid_rate = parse_dollar_rate(get_val('rate_mid_peak_per_kwh', 'mid_peak_rate', 'tou_mid_rate_dollars'))
        tou_off_rate = parse_dollar_rate(get_val('rate_off_peak_per_kwh', 'off_peak_rate', 'tou_off_rate_dollars', 'tou_low_peak_rate'))
        
        # Extract Super Off-Peak TOU data (also check for LADWP "Base" usage)
        tou_super_off_kwh = clean_numeric(get_val('kwh_super_off_peak', 'super_off_peak_kwh', 'tou_super_off_kwh', 'tou_base_kwh'))
        tou_super_off_rate = parse_dollar_rate(get_val('rate_super_off_peak_per_kwh', 'super_off_peak_rate', 'tou_super_off_rate_dollars', 'tou_base_rate'))
        
        # Try to get TOU costs directly from extraction (LADWP provides these)
        tou_on_cost = clean_numeric(get_val('tou_high_peak_cost', 'tou_on_cost'))
        tou_mid_cost = clean_numeric(get_val('tou_mid_cost'))
        tou_off_cost = clean_numeric(get_val('tou_low_peak_cost', 'tou_off_cost'))
        tou_super_off_cost = clean_numeric(get_val('tou_base_cost', 'tou_super_off_cost'))
        
        # === DIAGNOSTIC LOGGING: PROVE WHAT DATA EXISTS ===
        print(f"\n[bill_extractor] ===== LADWP EXTRACTION DIAGNOSTICS =====")
        print(f"[bill_extractor] utility_name: {utility_name}")
        print(f"[bill_extractor] account_number: {account_number}")
        print(f"[bill_extractor] billing_period: {period_start} to {period_end}")
        print(f"[bill_extractor] total_kwh: {total_kwh}")
        print(f"[bill_extractor] total_amount: {total_amount}")
        print(f"[bill_extractor] rate_schedule: {rate_schedule}")
        print(f"[bill_extractor] due_date: {due_date}")
        print(f"[bill_extractor] --- RAW EXTRACTION VALUES ---")
        print(f"[bill_extractor] RAW rate: {detailed.get('rate')}")
        print(f"[bill_extractor] RAW due_date: {detailed.get('due_date')}")
        print(f"[bill_extractor] RAW service_address: {detailed.get('service_address')}")
        print(f"[bill_extractor] --- TOU DATA ---")
        print(f"[bill_extractor] tou_on_kwh: {tou_on_kwh}")
        print(f"[bill_extractor] tou_mid_kwh: {tou_mid_kwh}")
        print(f"[bill_extractor] tou_off_kwh: {tou_off_kwh}")
        print(f"[bill_extractor] tou_super_off_kwh: {tou_super_off_kwh}")
        print(f"[bill_extractor] tou_on_rate: {tou_on_rate}")
        print(f"[bill_extractor] tou_off_rate: {tou_off_rate}")
        print(f"[bill_extractor] tou_on_cost: {tou_on_cost}")
        print(f"[bill_extractor] tou_off_cost: {tou_off_cost}")
        print(f"[bill_extractor] --- RAW EXTRACTED DATA KEYS ---")
        print(f"[bill_extractor] top-level keys: {list(extracted_data.keys())}")
        print(f"[bill_extractor] detailed_data keys: {list(detailed.keys())}")
        # Check for tou_breakdown array
        tou_breakdown_raw = extracted_data.get('tou_breakdown') or detailed.get('tou_breakdown')
        print(f"[bill_extractor] tou_breakdown array: {tou_breakdown_raw}")
        print(f"[bill_extractor] ===== END DIAGNOSTICS =====\n")
        
        # Compute TOU costs if not provided directly but we have rate and kWh
        if tou_on_cost is None and tou_on_kwh is not None and tou_on_rate is not None:
            tou_on_cost = round(tou_on_kwh * tou_on_rate, 2)
        
        if tou_mid_cost is None and tou_mid_kwh is not None and tou_mid_rate is not None:
            tou_mid_cost = round(tou_mid_kwh * tou_mid_rate, 2)
        
        if tou_off_cost is None and tou_off_kwh is not None and tou_off_rate is not None:
            tou_off_cost = round(tou_off_kwh * tou_off_rate, 2)
        
        if tou_super_off_cost is None and tou_super_off_kwh is not None and tou_super_off_rate is not None:
            tou_super_off_cost = round(tou_super_off_kwh * tou_super_off_rate, 2)
        
        # Water charges are extracted separately in water_charges_total
        # The extraction prompt already asks the LLM to return electric-only values
        # in the main fields, so we do NOT subtract water from totals here
        water_charges = clean_numeric(get_val('water_charges_total'))
        if water_charges is not None and water_charges > 0:
            print(f"[bill_extractor] LADWP water charges reported separately: ${water_charges}")
        
        # LADWP-specific validation: due_date and rate_schedule are HARD-REQUIRED
        missing_fields = []
        review_status = 'approved'
        if utility_name == "LADWP":
            if not due_date or due_date.strip() == '':
                missing_fields.append("due_date")
                print(f"[bill_extractor] LADWP bill missing required field: due_date")
            if not rate_schedule or rate_schedule.strip() == '':
                missing_fields.append("rate_schedule")
                print(f"[bill_extractor] LADWP bill missing required field: rate_schedule")
            if missing_fields:
                review_status = 'needs_review'
                print(f"[bill_extractor] LADWP bill flagged for review - missing: {missing_fields}")
        
        # Extract service_type from AI extraction (defaults to 'electric')
        service_type = get_val('service_type') or 'electric'
        if service_type not in ('electric', 'water', 'gas', 'combined'):
            service_type = 'electric'
        print(f"[bill_extractor] service_type: {service_type}")
        
        # Get TOU periods for the bill_tou_periods table
        # Try AI extraction first, then fallback to regex extraction
        tou_rates = extracted_data.get('tou_rates', [])
        if not tou_rates:
            tou_rates = extracted_data.get('tou_breakdown', [])

        # Fallback to regex-extracted TOU data if AI didn't find any
        if not tou_rates and tou_breakdown_from_regex:
            tou_rates = tou_breakdown_from_regex
            print(f"[bill_extractor] Using regex-extracted TOU data ({len(tou_rates)} periods)")
        
        # If we have meter-specific data, create a bill per meter
        if meters and len(meters) > 0:
            for meter_data in meters:
                meter_number = meter_data.get('meter_number') or meter_data.get('meter_id', 'Unknown')
                meter_service_address = meter_data.get('service_address') or service_address
                meter_id = upsert_utility_meter(account_id, meter_number, meter_service_address)
                
                # Use meter-specific values if available
                # Check both direct meter fields and reads[0] (newer structure)
                m_kwh = clean_numeric(meter_data.get('kwh_total'))
                m_amount = clean_numeric(meter_data.get('total_charge'))

                # If not found at top level of meter, check reads[0]
                reads = meter_data.get('reads', [])
                if reads and len(reads) > 0:
                    first_read = reads[0]
                    if m_kwh is None:
                        m_kwh = clean_numeric(first_read.get('kwh'))
                    if m_amount is None:
                        m_amount = clean_numeric(first_read.get('total_charge'))
                    # Also get period from reads if not set
                    if not period_start:
                        period_start = first_read.get('period_start')
                    if not period_end:
                        period_end = first_read.get('period_end')

                # CRITICAL: Check if this meter has kWh BEFORE fallback to total
                # Water/fire/gas meters have no kWh - skip them entirely
                print(f"[bill_extractor] DEBUG: meter {meter_number} - m_kwh={m_kwh} (type={type(m_kwh)})")
                if m_kwh is None or m_kwh == 0:
                    print(f"[bill_extractor] Skipping non-electric meter {meter_number} - no kWh data")
                    continue

                # Only fall back to totals for electric meters that passed the kWh check
                if m_amount is None:
                    m_amount = total_amount

                bill_id = insert_bill(
                    bill_file_id=file_id,
                    account_id=account_id,
                    meter_id=meter_id,
                    utility_name=utility_name,
                    service_address=service_address,
                    rate_schedule=rate_schedule,
                    period_start=period_start,
                    period_end=period_end,
                    total_kwh=m_kwh,
                    total_amount_due=m_amount,
                    energy_charges=energy_charges,
                    demand_charges=demand_charges,
                    other_charges=other_charges,
                    taxes=taxes,
                    tou_on_kwh=tou_on_kwh,
                    tou_mid_kwh=tou_mid_kwh,
                    tou_off_kwh=tou_off_kwh,
                    tou_super_off_kwh=tou_super_off_kwh,
                    tou_on_rate_dollars=tou_on_rate,
                    tou_mid_rate_dollars=tou_mid_rate,
                    tou_off_rate_dollars=tou_off_rate,
                    tou_super_off_rate_dollars=tou_super_off_rate,
                    tou_on_cost=tou_on_cost,
                    tou_mid_cost=tou_mid_cost,
                    tou_off_cost=tou_off_cost,
                    tou_super_off_cost=tou_super_off_cost,
                    due_date=due_date,
                    service_type=service_type
                )
                
                # Insert TOU periods
                for tou in tou_rates:
                    period = tou.get('period') or tou.get('period_name', 'Unknown')
                    kwh = clean_numeric(tou.get('kwh'))
                    rate = parse_dollar_rate(tou.get('rate') or tou.get('rate_per_kwh'))
                    est_cost = clean_numeric(tou.get('estimated_cost') or tou.get('est_cost'))
                    
                    if kwh is not None:
                        insert_bill_tou_period(bill_id, period, kwh, rate, est_cost)
                
                print(f"[bill_extractor] Saved bill {bill_id} for meter {meter_number} - kwh={m_kwh}, amount=${m_amount}")
        else:
            # No meter info - create single bill with default meter
            meter_id = upsert_utility_meter(account_id, 'Primary', service_address)
            
            bill_id = insert_bill(
                bill_file_id=file_id,
                account_id=account_id,
                meter_id=meter_id,
                utility_name=utility_name,
                service_address=service_address,
                rate_schedule=rate_schedule,
                period_start=period_start,
                period_end=period_end,
                total_kwh=total_kwh,
                total_amount_due=total_amount,
                energy_charges=energy_charges,
                demand_charges=demand_charges,
                other_charges=other_charges,
                taxes=taxes,
                tou_on_kwh=tou_on_kwh,
                tou_mid_kwh=tou_mid_kwh,
                tou_off_kwh=tou_off_kwh,
                tou_super_off_kwh=tou_super_off_kwh,
                tou_on_rate_dollars=tou_on_rate,
                tou_mid_rate_dollars=tou_mid_rate,
                tou_off_rate_dollars=tou_off_rate,
                tou_super_off_rate_dollars=tou_super_off_rate,
                tou_on_cost=tou_on_cost,
                tou_mid_cost=tou_mid_cost,
                tou_off_cost=tou_off_cost,
                tou_super_off_cost=tou_super_off_cost,
                due_date=due_date,
                service_type=service_type
            )
            
            # Insert TOU periods
            for tou in tou_rates:
                period = tou.get('period') or tou.get('period_name', 'Unknown')
                kwh = clean_numeric(tou.get('kwh'))
                rate = parse_dollar_rate(tou.get('rate') or tou.get('rate_per_kwh'))
                est_cost = clean_numeric(tou.get('estimated_cost') or tou.get('est_cost'))
                
                if kwh is not None:
                    insert_bill_tou_period(bill_id, period, kwh, rate, est_cost)
            
            print(f"[bill_extractor] Saved bill {bill_id} (single meter) - kwh={total_kwh}, amount=${total_amount}")
        
        # Update bill file review_status in database if LADWP has missing fields
        if missing_fields and len(missing_fields) > 0:
            update_bill_file_review_status(file_id, 'needs_review')
            print(f"[bill_extractor] Updated bill file {file_id} review_status to 'needs_review' - missing: {missing_fields}")

        # Cleanup: Delete ALL empty accounts in project (happens when all meters are filtered)
        from bills_db import delete_all_empty_accounts
        delete_all_empty_accounts(project_id)

        # Return boolean for backward compatibility
        return True
    except Exception as e:
        print(f"[bill_extractor] Error saving to normalized tables: {e}")
        import traceback
        traceback.print_exc()
        return False


def flatten_grok_response(raw_result):
    """
    Flatten nested Grok response into a flat dictionary.
    Grok sometimes returns nested structure like:
    {"ACCOUNT INFO": {...}, "BILLING PERIOD": {...}, "USAGE (kWh)": {...}}
    This function flattens it to a single level.
    """
    if not isinstance(raw_result, dict):
        return raw_result
    
    section_keys = [
        "ACCOUNT INFO", "BILLING PERIOD", "AMOUNTS", "USAGE (kWh)", "USAGE",
        "CHARGES BREAKDOWN", "DEMAND", "TOU RATES", "USAGE HISTORY", "LINE ITEMS",
        "METERS", "account_info", "billing_period", "amounts", "usage", 
        "charges_breakdown", "demand", "tou_rates", "usage_history", "line_items"
    ]
    
    has_nested_sections = any(key in raw_result for key in section_keys)
    
    if not has_nested_sections:
        return raw_result
    
    flat = {}
    
    for key, value in raw_result.items():
        if isinstance(value, dict):
            for inner_key, inner_value in value.items():
                flat[inner_key] = inner_value
        elif isinstance(value, list):
            normalized_key = key.lower().replace(" ", "_").replace("(", "").replace(")", "")
            flat[normalized_key] = value
        else:
            flat[key] = value
    
    return flat


def extract_bill_data(file_path, progress_callback=None, training_hints=None, annotated_images=None):
    """
    Extract utility bill data from PDF using xAI Grok 4 vision
    
    Args:
        file_path: Path to the PDF file
        progress_callback: Optional callback function(progress_value, status_message=None)
        training_hints: Optional list of past corrections for this utility
        annotated_images: Optional list of base64-encoded annotated images
    
    Returns a comprehensive JSON structure with detailed bill breakdown
    """
    def notify_progress(value, message=None):
        if progress_callback:
            try:
                progress_callback(value, message)
            except Exception as e:
                print(f"[bill_extractor] Progress callback error: {e}")
    
    print(f"[bill_extractor] Processing: {file_path}")
    
    notify_progress(0.1, "Converting file to images")
    
    image_tuples = file_to_images(file_path)
    if not image_tuples:
        return {
            "success": False,
            "utility_name": None,
            "account_number": None,
            "meters": [],
            "error": "Could not read file"
        }
    
    print(f"[bill_extractor] Converted {len(image_tuples)} page(s)/image(s) for processing")
    
    notify_progress(0.3, "File converted to images")
    
    try:
        client = get_xai_client()
        
        training_hints_text = ""
        if training_hints and len(training_hints) > 0:
            hints_list = []
            for hint in training_hints[:20]:
                field = hint.get('field_type', 'unknown')
                value = hint.get('corrected_value', '')
                meter = hint.get('meter_number', '')
                period_start = hint.get('period_start_date', '')
                period_end = hint.get('period_end_date', '')
                
                hint_desc = f"- {field}: correct value is '{value}'"
                if meter:
                    hint_desc += f" for meter {meter}"
                if period_start and period_end:
                    hint_desc += f" (period {period_start} to {period_end})"
                hints_list.append(hint_desc)
            
            training_hints_text = """

CORRECTION HINTS (based on past user corrections for this utility):
""" + "\n".join(hints_list)
        
        extraction_prompt = """You are an expert commercial electric-bill parser for the SiteWalk field app.
You MUST respond with STRICT valid JSON and nothing else. No explanations, no markdown.

Analyze this electric bill and return ONLY JSON with these keys:

ACCOUNT INFO:
- customer_account: string (main customer account number - IMPORTANT: For LADWP bills, use the "ACCOUNT NUMBER" from the bill header, NOT the "SA #" which is a Service Agreement number)
- service_account: string (service/meter account number - For LADWP, this is the SA# or Service Agreement number)
- service_address: string (full address)
- pod_id: string (POD-ID if shown)
- utility_name: string (e.g., "SCE", "Southern California Edison", "LADWP", "PG&E")
- rate: string (rate schedule name, e.g., "TOU-GS-2-E")
- rate_schedule: string (rate schedule code from electric charges section)
- rotating_outage_group: string (if shown)

BILLING PERIOD:
- bill_prepared_date: string (YYYY-MM-DD)
- billing_period_start: string (YYYY-MM-DD)
- billing_period_end: string (YYYY-MM-DD)
- days_in_period: number
- due_date: string (YYYY-MM-DD)

AMOUNTS:
- amount_due: number (total amount due)
- previous_balance: number
- payment_received: number (most recent payment, positive number)
- balance_forward: number
- new_charges: number
- total_owed: number

USAGE (kWh) - ELECTRIC ONLY (exclude water):
- kwh_total: number (total kWh for billing period - ELECTRIC ONLY, do NOT include water usage)
- kwh_on_peak: number (on-peak kWh, also called "High Peak" on LADWP bills)
- kwh_mid_peak: number (mid-peak kWh if applicable)
- kwh_off_peak: number (off-peak kWh, also called "Low Peak" or "Base" on LADWP bills)
- reactive_kvarh: number (reactive usage if shown)
- daily_avg_kwh: number (average daily kWh)

CHARGES BREAKDOWN - ELECTRIC ONLY (exclude water charges):
- energy_charges_total: number (total ELECTRIC energy charges in dollars - EXCLUDE any water charges)
- demand_charges_total: number (total demand charges in dollars)
- other_charges_total: number (customer charges, wildfire fund, etc. - EXCLUDE water-related charges)
- taxes_total: number (all taxes for electric only)
- water_charges_total: number (total water charges if present - report separately, do NOT include in electric totals)

DEMAND (kW):
- max_demand_kw: number (maximum demand reached)
- max_demand_threshold_kw: number (threshold/limit if shown)
- max_demand_on_peak_kw: number
- max_demand_mid_peak_kw: number
- max_demand_off_peak_kw: number

RATES PER kWh (extract from rate details):
- rate_on_peak_per_kwh: number (blended on-peak/high-peak rate in dollars)
- rate_mid_peak_per_kwh: number (blended mid-peak rate in dollars)
- rate_off_peak_per_kwh: number (blended off-peak/low-peak rate in dollars)
- rate_delivery_on_peak: number (delivery portion)
- rate_delivery_mid_peak: number
- rate_delivery_off_peak: number
- rate_generation_on_peak: number (generation portion)
- rate_generation_mid_peak: number
- rate_generation_off_peak: number

TOU BREAKDOWN (Time of Use - required for LADWP and SCE):
- tou_high_peak_kwh: number (High Peak kWh usage - LADWP terminology)
- tou_high_peak_cost: number (High Peak cost in dollars)
- tou_high_peak_rate: number (High Peak rate per kWh)
- tou_low_peak_kwh: number (Low Peak kWh usage - LADWP terminology)
- tou_low_peak_cost: number (Low Peak cost in dollars)
- tou_low_peak_rate: number (Low Peak rate per kWh)
- tou_base_kwh: number (Base kWh if applicable)
- tou_base_cost: number (Base cost in dollars)
- tou_base_rate: number (Base rate per kWh)

SERVICE TYPE DETECTION:
- service_type: string - REQUIRED. Detect what type of utility bill this is:
  - "electric" = electric-only bill (most common for SCE, PG&E)
  - "water" = water-only bill
  - "gas" = gas-only bill  
  - "combined" = combined electric and water bill (common for LADWP)
  For LADWP bills: Check if bill includes BOTH electric charges AND water charges. If both are present, use "combined". If only electric section is present, use "electric". If only water section is present, use "water".

OTHER:
- rate_schedule: string (rate schedule code from electric charges section)
- service_voltage: string (e.g., "240 volts")

LINE ITEMS (array of all itemized charges):
- line_items: array of objects with:
  - category: string ("delivery", "generation", "other", "tax", "water")
  - label: string (charge description)
  - calc: string (calculation shown, e.g., "17,829 kWh x $0.00595")
  - amount: number (dollar amount, negative for credits)
  - is_water_charge: boolean (true if this line item is for water service)

USAGE HISTORY (13 months if available):
- usage_history: array of objects with:
  - month: string (e.g., "Sep '23", "Oct '24")
  - kwh: number
  - days: number (billing days)
  - avg_kwh_per_day: number

METERS (for multi-meter bills):
- meters: array with:
  - meter_number: string
  - service_address: string
  - reads: array with period_start, period_end, kwh, total_charge

LADWP-SPECIFIC INSTRUCTIONS:
1. ACCOUNT NUMBER: Use the "ACCOUNT NUMBER" from the bill header (usually near top). DO NOT use "SA #" (Service Agreement) as the customer_account - that goes in service_account.
2. WATER vs ELECTRIC: LADWP bills often include both water AND electric charges. You MUST separate them:
   - Include ONLY electric kWh in kwh_total
   - Include ONLY electric charges in energy_charges_total
   - Put water charges separately in water_charges_total
   - Mark water line items with is_water_charge: true
3. TOU (Time of Use): Look in the "Electric Charges" section for:
   - "High Peak" = on-peak (use tou_high_peak_kwh, tou_high_peak_cost, tou_high_peak_rate)
   - "Low Peak" = off-peak (use tou_low_peak_kwh, tou_low_peak_cost, tou_low_peak_rate)
   - "Base" = base usage if present
4. RATE SCHEDULE: Extract from electric charges section (e.g., "Rate Schedule: R-1B")
5. DUE DATE: Look for "DUE DATE" or "Payment Due" on the front page

SCE-SPECIFIC INSTRUCTIONS:
1. UTILITY NAME: Southern California Edison bills may show "SCE" or "Southern California Edison" in the header. ALWAYS use "Southern California Edison" or "SCE" for utility_name, NEVER "LADWP" or other utilities.
2. ACCOUNT NUMBER: SCE account numbers are typically 10-12 digits. Look for "Account Number" or "Acct#" near the top of the bill. Common SCE accounts in this project are 4369 and 6457. DO NOT confuse account numbers with meter numbers or POD IDs.
3. METER DATA: SCE bills show electric meter information in the usage section. Look for:
   - Meter Number (usually format: E-XXXXXXX or similar)
   - Service Address for each meter
   - kWh usage per meter per billing period
   Each meter should have non-zero kWh values if it's an active electric meter.
4. TOU (Time of Use): SCE uses "On-Peak", "Mid-Peak", "Off-Peak", and sometimes "Super Off-Peak" periods. Map these to:
   - On-Peak → kwh_on_peak, rate_on_peak_per_kwh
   - Mid-Peak → kwh_mid_peak, rate_mid_peak_per_kwh
   - Off-Peak → kwh_off_peak, rate_off_peak_per_kwh
   - Super Off-Peak → use tou fields if present
5. RATE SCHEDULE: SCE rate schedules are SHORT CODES like "TOU-GS-2-E", "TOU-8-B", "TOU-GS-1-E". Look in these locations:
   - Near the account information header
   - In the "Electric Charges" section header
   - Below the service address
   IMPORTANT: The rate schedule is a SHORT code (usually 5-15 characters), NOT a long description. If you find long text, it's NOT the rate schedule.
6. DUE DATE: SCE bills show "Payment Due" or "Due Date" near the amount due. Look for format MM/DD/YYYY or "Mon DD, YYYY". Extract as YYYY-MM-DD.
7. SERVICE ADDRESS: Extract the complete service address including street, city, state, and ZIP code if visible.
8. SERVICE TYPE: SCE bills are typically "electric" only (not combined with water like LADWP).
9. IDENTITY CHECK: Before finalizing, verify the utility_name matches what's actually shown on the bill. If the bill header says "Southern California Edison" or "SCE", utility_name MUST be "Southern California Edison" or "SCE", NOT "LADWP".

Use null for any field you cannot confidently extract. Amounts should be numbers (no $ signs or commas).""" + training_hints_text
        
        content = [
            {
                "type": "text",
                "text": extraction_prompt
            }
        ]
        
        for i, (img_b64, mime_type) in enumerate(image_tuples):
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{img_b64}",
                    "detail": "high"
                }
            })
        
        if annotated_images:
            for i, ann_img_b64 in enumerate(annotated_images):
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{ann_img_b64}",
                        "detail": "high"
                    }
                })
            print(f"[bill_extractor] Added {len(annotated_images)} annotated image(s)")
        
        print(f"[bill_extractor] Sending {len(image_tuples)} page(s) to Grok 4 vision...")
        
        notify_progress(0.6, "Analyzing bill with Grok AI...")
        
        import time
        start_time = time.time()
        
        response = client.chat.completions.create(
            model="grok-4",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert commercial electric-bill parser. You MUST respond with STRICT valid JSON only. No explanations, no markdown, no prose."
                },
                {
                    "role": "user",
                    "content": content
                }
            ],
            temperature=0
        )
        
        end_time = time.time()
        elapsed = end_time - start_time
        
        result_text = response.choices[0].message.content
        print(f"[bill_extractor] Grok 4 API call took {elapsed:.2f} seconds")
        print(f"[bill_extractor] Got response from Grok 4: {result_text[:500]}...")
        
        notify_progress(0.9, "Structuring extracted data")
        
        clean_text = result_text.strip()
        if clean_text.startswith("```json"):
            clean_text = clean_text[7:]
        if clean_text.startswith("```"):
            clean_text = clean_text[3:]
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3]
        clean_text = clean_text.strip()
        
        raw_result = json.loads(clean_text)
        
        result = flatten_grok_response(raw_result)
        
        utility_name = result.get("utility_name")
        account_number = result.get("customer_account") or result.get("account_number")
        meters = result.get("meters", [])
        kwh_total = result.get("kwh_total")
        
        has_valid_data = (
            utility_name and 
            account_number and 
            kwh_total is not None
        )
        
        if has_valid_data:
            print(f"[bill_extractor] Successfully extracted: {utility_name}, account {account_number}, {kwh_total} kWh")
            notify_progress(1.0, "Extraction complete")
            return {
                "success": True,
                "utility_name": utility_name,
                "account_number": account_number,
                "meters": meters,
                "detailed_data": result,
                "error": None
            }
        else:
            missing = []
            if not utility_name:
                missing.append("utility_name")
            if not account_number:
                missing.append("account_number")
            if kwh_total is None:
                missing.append("kwh_total")
            error_msg = f"Missing: {', '.join(missing)}" if missing else "Unknown extraction error"
            print(f"[bill_extractor] Incomplete extraction: {error_msg}")
            print(f"[bill_extractor] Result keys: {list(result.keys())}")
            notify_progress(1.0, "Extraction complete with issues")
            return {
                "success": False,
                "utility_name": utility_name,
                "account_number": account_number,
                "meters": meters,
                "detailed_data": result,
                "error": error_msg
            }
            
    except json.JSONDecodeError as e:
        print(f"[bill_extractor] JSON parse error: {e}")
        print(f"[bill_extractor] Raw response: {result_text[:1000] if 'result_text' in dir() else 'N/A'}")
        return {
            "success": False,
            "utility_name": None,
            "account_number": None,
            "meters": [],
            "error": f"Failed to parse AI response: {e}"
        }
    except Exception as e:
        print(f"[bill_extractor] Error: {e}")
        return {
            "success": False,
            "utility_name": None,
            "account_number": None,
            "meters": [],
            "error": str(e)
        }


def compute_missing_fields(extracted_data):
    """
    Compute which required fields are missing from extracted bill data.
    
    Required fields checked:
    - Bill-level: utility_name, account_number, total_kwh, total_amount_due
    - Per meter: meter_number, service_address
    - Per read: period_start, period_end, kwh, total_charge
    
    Args:
        extracted_data: dict with extraction results (may include 'detailed_data' key)
    
    Returns:
        List of objects like: [{"field": "total_charge", "label": "Total Charge", "reason": "Value is missing"}]
    """
    missing = []
    
    if not extracted_data:
        return [{"field": "no_extraction_data", "label": "Extraction Data", "reason": "No extraction data available"}]
    
    # Handle both raw extraction format and detailed_data wrapper
    data = extracted_data.get('detailed_data', extracted_data)
    
    # Helper to add missing field
    def add_missing(field, label, reason):
        missing.append({"field": field, "label": label, "reason": reason})
    
    # Bill-level required fields
    utility_name = data.get('utility_name')
    account_number = data.get('customer_account') or data.get('account_number')
    total_kwh = data.get('kwh_total') or data.get('total_kwh')
    total_amount = data.get('amount_due') or data.get('total_amount_due')
    
    if not utility_name:
        add_missing("utility_name", "Utility Name", "Value is missing")
    if not account_number:
        add_missing("account_number", "Account Number", "Value is missing")
    if total_kwh is None:
        add_missing("total_kwh", "Total kWh", "Value is missing or zero")
    elif total_kwh == 0:
        add_missing("total_kwh", "Total kWh", "Value is zero")
    if total_amount is None:
        add_missing("total_amount_due", "Total Charge", "Value is missing")
    elif total_amount == 0:
        add_missing("total_amount_due", "Total Charge", "Value is zero")
    
    # Check rate_schedule (optional but important)
    rate_schedule = data.get('rate') or data.get('rate_schedule')
    if not rate_schedule:
        add_missing("rate_schedule", "Rate Schedule", "Value is missing")
    
    # Check meters
    meters = data.get('meters', [])
    if not meters:
        # No meter array - check if we have billing period at top level
        period_start = data.get('billing_period_start')
        period_end = data.get('billing_period_end')
        
        if not period_start:
            add_missing("period_start", "Period Start", "Value is missing")
        if not period_end:
            add_missing("period_end", "Period End", "Value is missing")
    else:
        # Check each meter
        for i, meter in enumerate(meters):
            meter_number = meter.get('meter_number') or meter.get('meter_id')
            service_address = meter.get('service_address')
            
            meter_label = meter_number or f"Meter {i+1}"
            
            if not meter_number:
                add_missing(f"meter_{i+1}_meter_number", f"Meter {i+1} Number", "Meter number is missing")
            if not service_address:
                add_missing(f"meter_{i+1}_service_address", f"{meter_label} Service Address", "Service address is missing")
            
            # Check reads for this meter
            reads = meter.get('reads', [])
            if not reads:
                add_missing(f"meter_{i+1}_no_reads", f"{meter_label} Reads", "No billing reads found")
            else:
                for j, read in enumerate(reads):
                    period_start = read.get('period_start')
                    period_end = read.get('period_end')
                    kwh = read.get('kwh')
                    total_charge = read.get('total_charge')
                    
                    if not period_start:
                        add_missing(f"meter_{i+1}_read_{j+1}_period_start", f"{meter_label} Read {j+1} Period Start", "Period start date is missing")
                    if not period_end:
                        add_missing(f"meter_{i+1}_read_{j+1}_period_end", f"{meter_label} Read {j+1} Period End", "Period end date is missing")
                    if kwh is None:
                        add_missing(f"meter_{i+1}_read_{j+1}_kwh", f"{meter_label} Read {j+1} kWh", "kWh value is missing")
                    if total_charge is None:
                        add_missing(f"meter_{i+1}_read_{j+1}_total_charge", f"{meter_label} Read {j+1} Total Charge", "Total charge is missing")
    
    return missing


def extract_bill_data_text_based(file_id, job_queue, file_path, project_id):
    """Text-based bill extraction using normalization pipeline."""
    from bills import NormalizationService, TextCleaner, CacheService
    from bills.parser import TwoPassParser
    from bills.job_queue import JobState
    from bills.cache import build_metrics
    from bills_db import update_file_processing_status
    import time
    
    start_time = time.time()
    print(f"[bill_extractor] Starting text-based extraction for file {file_id}")
    
    job_queue.update_state(file_id, JobState.EXTRACTING_TEXT, "Extracting text from file")
    normalizer = NormalizationService()
    norm_result = normalizer.normalize(file_path)
    
    if not norm_result.success:
        print(f"[bill_extractor] Normalization failed: {norm_result.error}")
        update_file_processing_status(file_id, 'failed', {"error": norm_result.error})
        return {"success": False, "error": norm_result.error}
    
    print(f"[bill_extractor] Extracted {len(norm_result.text)} chars via {norm_result.metadata.get('method')}")
    
    job_queue.update_state(file_id, JobState.CLEANING, "Cleaning and filtering text")
    cleaner = TextCleaner()
    clean_result = cleaner.clean(norm_result.text)
    print(f"[bill_extractor] Cleaned text: {clean_result.stats}")
    
    cache = CacheService()
    text_hash, cached = cache.check_and_get(clean_result.cleaned_text)
    
    if cached:
        job_queue.update_state(file_id, JobState.CACHED_HIT, "Using cached result")
        print(f"[bill_extractor] Cache hit for hash {text_hash[:12]}")
        result = cached['parse_result']
        # Add cleaned text to result for regex fallback extraction
        result['_raw_text'] = clean_result.cleaned_text
        save_bill_to_normalized_tables(file_id, project_id, result)
        update_file_processing_status(file_id, 'complete', cached.get('metrics', {}))
        return result
    
    job_queue.update_state(file_id, JobState.PARSING_PASS_A, "Parsing with AI (Pass A)")
    parser = TwoPassParser()
    parse_result = parser.parse(clean_result.cleaned_text, clean_result.evidence_lines)
    
    if parse_result.pass_used == "A+B":
        job_queue.update_state(file_id, JobState.PARSING_PASS_B, "Extended parsing (Pass B)")
    
    duration_ms = (time.time() - start_time) * 1000
    print(f"[bill_extractor] Parsing complete: pass={parse_result.pass_used}, success={parse_result.success}")
    
    metrics = build_metrics(
        method=norm_result.metadata.get('method', 'unknown'),
        duration_ms=duration_ms,
        tokens_in=parse_result.tokens_in,
        tokens_out=parse_result.tokens_out,
        pages=norm_result.metadata.get('pages', 1),
        char_count=len(clean_result.cleaned_text),
        cache_hit=False,
        pass_used=parse_result.pass_used
    )
    
    if parse_result.success:
        cache.save_result(file_id, text_hash, clean_result.cleaned_text, parse_result.data, metrics)
        # Add cleaned text to data for regex fallback extraction
        parse_result.data['_raw_text'] = clean_result.cleaned_text
        save_bill_to_normalized_tables(file_id, project_id, parse_result.data)
        update_file_processing_status(file_id, 'complete', metrics)
        print(f"[bill_extractor] Extraction complete for file {file_id}")
        return parse_result.data
    else:
        update_file_processing_status(file_id, 'failed', metrics)
        print(f"[bill_extractor] Extraction failed: {parse_result.error}")
        return {"success": False, "error": parse_result.error}
