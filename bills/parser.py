"""
Two-Pass Parser for Bill Text Extraction
=========================================
Uses xAI Grok API with TEXT prompts (not vision/images) for cost-effective extraction.
"""

import os
import json
import logging
import time
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass

from openai import OpenAI

logger = logging.getLogger(__name__)

XAI_API_KEY = os.environ.get("XAI_API_KEY")

REQUIRED_FIELDS = ['account_number', 'billing_period', 'total_kwh', 'total_charges']

PASS_A_SCHEMA = {
    "utility_name": "",
    "account_number": "",
    "meter_number": "",
    "billing_period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
    "total_kwh": None,
    "total_charges": None,
    "amount_due": None,
    "confidence": 0.0
}

PASS_B_SCHEMA = {
    "utility_name": "",
    "account_number": "",
    "meter_number": "",
    "service_address": "",
    "rate_schedule": "",
    "billing_period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
    "due_date": "",
    "total_kwh": None,
    "total_charges": None,
    "amount_due": None,
    "energy_charges": None,
    "demand_charges": None,
    "kwh_on_peak": None,
    "kwh_mid_peak": None,
    "kwh_off_peak": None,
    "kwh_super_off_peak": None,
    "rate_on_peak": None,
    "rate_mid_peak": None,
    "rate_off_peak": None,
    "rate_super_off_peak": None,
    "max_demand_kw": None,
    "confidence": 0.0,
    "meters": []
}


@dataclass
class ParseResult:
    """Result of text parsing."""
    data: Dict[str, Any]
    success: bool
    pass_used: str
    tokens_in: int
    tokens_out: int
    duration_ms: float
    error: Optional[str] = None


class TwoPassParser:
    """
    Two-pass text parser for utility bill extraction.
    
    Pass A: Minimal extraction with low token limit (~500)
    Pass B: Full extraction with more context (if Pass A incomplete)
    
    Uses xAI Grok API with TEXT prompts only - never sends images.
    """
    
    PASS_A_MAX_TOKENS = 500
    PASS_B_MAX_TOKENS = 2000
    MODEL = "grok-3-mini"
    
    def __init__(self):
        """Initialize the parser."""
        self.client = None
    
    def _get_client(self) -> OpenAI:
        """Get or create xAI client."""
        if self.client is None:
            if not XAI_API_KEY:
                raise ValueError("XAI_API_KEY environment variable not set")
            self.client = OpenAI(
                api_key=XAI_API_KEY,
                base_url="https://api.x.ai/v1"
            )
        return self.client
    
    def parse(self, cleaned_text: str, evidence_lines: list = None) -> ParseResult:
        """
        Parse cleaned text using two-pass strategy.
        
        Args:
            cleaned_text: Cleaned text from TextCleaner
            evidence_lines: Optional list of key evidence lines
            
        Returns:
            ParseResult with extracted data and metrics
        """
        start_time = time.time()
        total_tokens_in = 0
        total_tokens_out = 0
        
        pass_a_result = self._pass_a(cleaned_text)
        total_tokens_in += pass_a_result.tokens_in
        total_tokens_out += pass_a_result.tokens_out
        
        if not pass_a_result.success:
            duration_ms = (time.time() - start_time) * 1000
            return ParseResult(
                data={},
                success=False,
                pass_used="A",
                tokens_in=total_tokens_in,
                tokens_out=total_tokens_out,
                duration_ms=duration_ms,
                error=pass_a_result.error
            )
        
        if self._has_required_fields(pass_a_result.data):
            duration_ms = (time.time() - start_time) * 1000
            logger.info("Pass A sufficient, skipping Pass B")
            return ParseResult(
                data=self._normalize_result(pass_a_result.data),
                success=True,
                pass_used="A",
                tokens_in=total_tokens_in,
                tokens_out=total_tokens_out,
                duration_ms=duration_ms
            )
        
        logger.info("Pass A incomplete, running Pass B for more detail")
        pass_b_result = self._pass_b(cleaned_text, evidence_lines)
        total_tokens_in += pass_b_result.tokens_in
        total_tokens_out += pass_b_result.tokens_out
        duration_ms = (time.time() - start_time) * 1000
        
        if pass_b_result.success:
            merged = self._merge_results(pass_a_result.data, pass_b_result.data)
            return ParseResult(
                data=self._normalize_result(merged),
                success=True,
                pass_used="A+B",
                tokens_in=total_tokens_in,
                tokens_out=total_tokens_out,
                duration_ms=duration_ms
            )
        
        return ParseResult(
            data=self._normalize_result(pass_a_result.data),
            success=True,
            pass_used="A",
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            duration_ms=duration_ms,
            error="Pass B failed, using Pass A results"
        )
    
    def _pass_a(self, text: str) -> ParseResult:
        """
        Pass A: Minimal extraction with low token limit.
        Focuses on key fields only.
        """
        prompt = f"""Extract utility bill data from this text. Return ONLY valid JSON, no explanation.

Use this exact schema:
{json.dumps(PASS_A_SCHEMA, indent=2)}

Rules:
- Use null for values you cannot find
- Dates must be YYYY-MM-DD format
- Numbers should be numeric values (no $ or commas)
- confidence: 0.0-1.0 based on how certain you are

TEXT TO ANALYZE:
{text[:8000]}

JSON:"""
        
        return self._call_api(prompt, self.PASS_A_MAX_TOKENS)
    
    def _pass_b(self, text: str, evidence_lines: list = None) -> ParseResult:
        """
        Pass B: Full extraction with more context.
        Used when Pass A is missing required fields.
        """
        evidence_section = ""
        if evidence_lines:
            evidence_section = "\n\nKEY EVIDENCE LINES:\n" + "\n".join(evidence_lines[:30])
        
        prompt = f"""Extract detailed utility bill data from this text. Return ONLY valid JSON, no explanation.

Use this exact schema:
{json.dumps(PASS_B_SCHEMA, indent=2)}

Rules:
- Use null for values you cannot find
- Dates must be YYYY-MM-DD format
- Numbers should be numeric values (no $ or commas)
- For meters array: include meter_number, service_address, kwh, and total_charge per meter
- confidence: 0.0-1.0 based on extraction certainty

UTILITY-SPECIFIC EXTRACTION RULES:

For SCE (Southern California Edison) bills:
- utility_name: "Southern California Edison" or "SCE"
- account_number: 10-12 digit number near top (common: ending in 4369 or 6457)
- rate_schedule: SHORT CODE like "TOU-GS-2-E", "TOU-8-B" (5-15 chars max). Look in Electric Charges section. Long text is NOT rate schedule.
- service_address: Complete address with street, city, state, ZIP
- due_date: Payment due date in MM/DD/YYYY or text format
- TOU data: Extract Time-of-Use periods into specific fields:
  * kwh_on_peak: kWh for "On-Peak" period
  * kwh_mid_peak: kWh for "Mid-Peak" period
  * kwh_off_peak: kWh for "Off-Peak" period
  * kwh_super_off_peak: kWh for "Super Off-Peak" period
  * rate_on_peak: Rate per kWh for On-Peak (as decimal, e.g., 0.25 for $0.25/kWh)
  * rate_mid_peak: Rate per kWh for Mid-Peak
  * rate_off_peak: Rate per kWh for Off-Peak
  * rate_super_off_peak: Rate per kWh for Super Off-Peak

For SDG&E (San Diego Gas & Electric) bills:
- utility_name: "San Diego Gas & Electric" or "SDG&E"
- account_number: Typically 10 digits
- rate_schedule: SHORT CODE like "DR-SES", "AL-TOU", "DG-R", "EV-TOU-5" (5-15 chars max)
- due_date: Payment due date
- TOU data: Extract into kwh_on_peak, kwh_off_peak, kwh_super_off_peak and corresponding rates

For PG&E (Pacific Gas & Electric) bills:
- utility_name: "Pacific Gas & Electric" or "PG&E"
- account_number: 10-12 digits, format XXXX-XXXX-XX
- rate_schedule: SHORT CODE like "E-TOU-C", "A-10", "E-19", "EV2-A" (5-15 chars max)
- due_date: Payment due date
- TOU data: "Peak"=kwh_on_peak, "Part-Peak"=kwh_mid_peak, "Off-Peak"=kwh_off_peak with rates

For LADWP (Los Angeles Department of Water and Power) bills:
- utility_name: "LADWP" or "Los Angeles Department of Water and Power"
- account_number: Use "ACCOUNT NUMBER" from header (NOT "SA #")
- rate_schedule: SHORT CODE like "R-1B", "A-2", "D-1"
- due_date: Payment due date (often labeled "AUTO PAYMENT" date)
- Separate electric charges from water charges (often combined)
- TOU data: "High Peak"=kwh_on_peak, "Low Peak"=kwh_off_peak, "Base"=kwh_super_off_peak with rates

For RPU (Riverside Public Utilities) bills:
- utility_name: "Riverside Public Utilities" or "RPU"
- rate_schedule: SHORT CODE (municipal utility format)

For IID (Imperial Irrigation District) bills:
- utility_name: "Imperial Irrigation District" or "IID"
- rate_schedule: SHORT CODE

For Anaheim Public Utilities bills:
- utility_name: "Anaheim Public Utilities" or "City of Anaheim"
- rate_schedule: SHORT CODE (municipal format)
{evidence_section}

FULL TEXT:
{text[:15000]}

JSON:"""
        
        return self._call_api(prompt, self.PASS_B_MAX_TOKENS)
    
    def _call_api(self, prompt: str, max_tokens: int) -> ParseResult:
        """Call xAI API and parse response."""
        start_time = time.time()
        tokens_in = 0
        tokens_out = 0
        
        try:
            client = self._get_client()
            
            response = client.chat.completions.create(
                model=self.MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a utility bill parser. Return only valid JSON, no markdown or explanation."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                max_tokens=max_tokens,
                temperature=0.0
            )
            
            if hasattr(response, 'usage') and response.usage:
                tokens_in = response.usage.prompt_tokens or 0
                tokens_out = response.usage.completion_tokens or 0
            
            content = response.choices[0].message.content.strip()
            
            if content.startswith("```"):
                lines = content.split('\n')
                json_lines = []
                in_json = False
                for line in lines:
                    if line.startswith("```json"):
                        in_json = True
                        continue
                    elif line.startswith("```"):
                        in_json = False
                        continue
                    if in_json or not line.startswith("```"):
                        json_lines.append(line)
                content = '\n'.join(json_lines)
            
            data = json.loads(content)
            duration_ms = (time.time() - start_time) * 1000
            
            return ParseResult(
                data=data,
                success=True,
                pass_used="",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                duration_ms=duration_ms
            )
            
        except json.JSONDecodeError as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.warning(f"JSON parse error: {e}")
            return ParseResult(
                data={},
                success=False,
                pass_used="",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                duration_ms=duration_ms,
                error=f"JSON parse error: {str(e)}"
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.exception(f"API call failed: {e}")
            return ParseResult(
                data={},
                success=False,
                pass_used="",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                duration_ms=duration_ms,
                error=str(e)
            )
    
    def _has_required_fields(self, data: Dict[str, Any]) -> bool:
        """Check if all required fields are present and valid."""
        account = data.get('account_number')
        if not account or account == "":
            return False
        
        period = data.get('billing_period', {})
        if not isinstance(period, dict):
            return False
        if not period.get('start') or not period.get('end'):
            return False
        if period.get('start') == 'YYYY-MM-DD' or period.get('end') == 'YYYY-MM-DD':
            return False
        
        kwh = data.get('total_kwh')
        if kwh is None:
            return False
        
        charges = data.get('total_charges')
        if charges is None:
            charges = data.get('amount_due')
        if charges is None:
            return False
        
        return True
    
    def _merge_results(self, pass_a: Dict, pass_b: Dict) -> Dict:
        """Merge Pass A and Pass B results, preferring non-null values from Pass B."""
        merged = dict(pass_a)
        
        for key, value in pass_b.items():
            if value is not None and value != "" and value != []:
                if key not in merged or merged[key] is None or merged[key] == "":
                    merged[key] = value
                elif key == 'meters' and isinstance(value, list) and len(value) > 0:
                    merged[key] = value
        
        return merged
    
    def _normalize_result(self, data: Dict) -> Dict:
        """
        Normalize extracted data to match the expected format for save_bill_to_normalized_tables.
        Converts the minimal schema to the full extraction format.
        """
        period = data.get('billing_period', {})
        if isinstance(period, dict):
            period_start = period.get('start')
            period_end = period.get('end')
        else:
            period_start = None
            period_end = None
        
        total_charges = data.get('total_charges')
        if total_charges is None:
            total_charges = data.get('amount_due')
        
        normalized = {
            'success': True,
            'utility_name': data.get('utility_name'),
            'account_number': data.get('account_number'),
            'detailed_data': {
                'customer_account': data.get('account_number'),
                'service_address': data.get('service_address', ''),
                'rate': data.get('rate_schedule', ''),
                'due_date': data.get('due_date', ''),
                'billing_period_start': period_start,
                'billing_period_end': period_end,
                'kwh_total': data.get('total_kwh'),
                'amount_due': total_charges,
                'new_charges': total_charges,
                'energy_charges_total': data.get('energy_charges'),
                'demand_charges_total': data.get('demand_charges'),
                'kwh_on_peak': data.get('kwh_on_peak'),
                'kwh_mid_peak': data.get('kwh_mid_peak'),
                'kwh_off_peak': data.get('kwh_off_peak'),
                'kwh_super_off_peak': data.get('kwh_super_off_peak'),
                'rate_on_peak_per_kwh': data.get('rate_on_peak'),
                'rate_mid_peak_per_kwh': data.get('rate_mid_peak'),
                'rate_off_peak_per_kwh': data.get('rate_off_peak'),
                'rate_super_off_peak_per_kwh': data.get('rate_super_off_peak'),
                'max_demand_kw': data.get('max_demand_kw'),
            },
            'meters': data.get('meters', []),
            'confidence': data.get('confidence', 0.5)
        }
        
        if not normalized['meters'] and data.get('meter_number'):
            normalized['meters'] = [{
                'meter_number': data.get('meter_number'),
                'service_address': data.get('service_address', ''),
                'reads': [{
                    'period_start': period_start,
                    'period_end': period_end,
                    'kwh': data.get('total_kwh'),
                    'total_charge': total_charges
                }]
            }]
        
        return normalized


def normalize_utility_name(raw: str) -> str:
    """
    Normalize utility company names to a canonical form.
    """
    if not raw:
        return "Unknown"
    name = raw.strip().lower()
    
    if "southern california edison" in name or name == "sce":
        return "Southern California Edison"
    if "san diego gas" in name or name == "sdge" or name == "sdg&e":
        return "San Diego Gas & Electric"
    if "los angeles department of water" in name or name == "ladwp":
        return "LADWP"
    if "pacific gas" in name or name == "pge" or name == "pg&e":
        return "Pacific Gas & Electric"
    
    return raw.strip()
