import os
import re
import csv
import io
import base64
import datetime
import dropbox
from flask import Blueprint, request, jsonify

upload_bp = Blueprint("upload_bp", __name__)

APP_KEY = os.environ["DROPBOX_APP_KEY"]
APP_SECRET = os.environ["DROPBOX_APP_SECRET"]
REFRESH_TOKEN = os.environ["DROPBOX_REFRESH_TOKEN"]

BASE_PATH = "/1. CES/1. FRIGITEK/1. FRIGITEK ANALYSIS/SiteWalk Exports"
PP_ROOT = "/1. CES/1. FRIGITEK/1. FRIGITEK ANALYSIS/2. PENDING Proposals"

# Map app utility names to actual Dropbox folder names
UTILITY_FOLDER_MAP = {
    "LADWP": "1. LADWP",
    "VERNON": "2 VERNON",
    "SCE": "3. SCE (Edison)",
    "SDGE": "5. SDGE",
    "RPU": "6. RPU",
    "PG&E": "7. PG&E",
    "ANAHEIM": "4b. Anaheim",
    "TID": "4a. TID",
    "HAWAII ENERGY": "8. Hawaii Energy",
    "NV ENERGY": "NV Energy",
    "PILGRIM": "9. Pilgrim",
    "AZUSA LIGHT & WATER": "Azusa Light & Water",
    "CHICAGO": "Chicago",
    "EVERSOURCE - BOSTON": "EVERSOURCE-BOSTON",
    "FPL - FL": "FPL - FL",
    "IID": "IID",
    "MPD ELECTRIC - CAROLINAS": "MPD Electric - Carolinas",
    "PSEG": "PSEG",
    "RMP - UTAH": "RMP–Utah",
}

def get_dbx():
    return dropbox.Dropbox(
        oauth2_refresh_token=REFRESH_TOKEN,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
    )

def detect_device(user_agent):
    """Classify device from User-Agent header"""
    if not user_agent:
        return "Other"
    ua_lower = user_agent.lower()
    if "iphone" in ua_lower or "ipad" in ua_lower or "ipod" in ua_lower:
        if "crios" in ua_lower:
            return "iOS Chrome"
        else:
            return "iOS Safari"
    elif "safari" in ua_lower:
        return "Desktop Safari"
    elif "chrome" in ua_lower:
        return "Desktop Chrome"
    return "Other"

def sanitize_name(name):
    """Sanitize folder name: trim whitespace, replace / and \ with -"""
    return name.strip().replace("/", "-").replace("\\", "-")

def normalize(name):
    """Normalize utility name for comparison: strip numbers/prefixes and extra chars"""
    s = name.upper() if name else ""
    s = re.sub(r"\([^)]*\)", "", s)  # drop anything in parentheses
    s = re.sub(r"^\d+[.\s]*", "", s)  # strip leading numbers and "1." / "2 "
    s = re.sub(r"\s+", " ", s).strip()  # collapse spaces
    return s

def list_folders_under_path(dbx, path):
    """List immediate subfolders under a Dropbox path"""
    try:
        result = dbx.files_list_folder(path)
        folders = []
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata):
                folders.append(entry.name)
        return folders
    except dropbox.exceptions.ApiError:
        return []

def pick_utility_folder_name(utility_name_short, existing_folders):
    """
    Map app utility name to Dropbox folder name using three-tier fallback:
    1. Explicit map lookup
    2. Normalized matching against existing folders
    3. Last resort: return the short name as-is
    """
    if not utility_name_short or utility_name_short == "Unknown":
        return None
    
    key = utility_name_short.strip().upper()
    
    # 1) Explicit map first
    if key in UTILITY_FOLDER_MAP:
        return UTILITY_FOLDER_MAP[key]
    
    # 2) Fallback: try to match by normalization against existing subfolders under PP_ROOT
    target = normalize(key)
    for folder in existing_folders:
        if normalize(folder) == target:
            return folder
    
    # 3) Last resort: just use the short name
    return utility_name_short.strip()

def format_date_no_leading_zeros(date_obj):
    """
    Format a datetime object as M.D.YY (no leading zeros on month/day).
    Examples: December 1, 2025 → "12.1.25", December 10, 2025 → "12.10.25"
    """
    month = date_obj.month
    day = date_obj.day
    year = str(date_obj.year)[-2:]
    return f"{month}.{day}.{year}"

def extract_evap_cond_names(csv_data):
    """
    Parse CSV data and extract evaporator and condenser room/unit names.
    Handles both old format (label row + header row) and new format (label+header combined).
    Old: "Evaporators" (label row) -> "Zone,Sheet,Name,..." (header) -> data rows
    New: "Evaporators,Zone,Sheet,Name,..." (label+header combined) -> data rows
    Returns: (evap_names_set, cond_names_set)
    """
    evap_names = set()
    cond_names = set()
    
    try:
        # Decode CSV from bytes
        if isinstance(csv_data, bytes):
            csv_text = csv_data.decode('utf-8')
        else:
            csv_text = csv_data
        
        lines = csv_text.strip().split('\n')
        rows = []
        
        # Parse all rows using CSV reader
        for line in lines:
            line = line.strip()
            if not line:
                rows.append([])
                continue
            try:
                reader = csv.reader(io.StringIO(line))
                for row in reader:
                    rows.append([cell.strip().strip('"') for cell in row])
                    break
            except:
                rows.append([])
        
        # Process rows to find Evaporators and Condensers sections
        i = 0
        while i < len(rows):
            row = rows[i]
            if not row:
                i += 1
                continue
            
            first_col = row[0] if row else ''
            
            # Check if this row starts a section (Evaporators or Condensers)
            if first_col in ('Evaporators', 'Condensers'):
                section = 'evap' if first_col == 'Evaporators' else 'cond'
                header_row = None
                name_col_index = -1
                data_start_row = -1
                
                # Check if this is NEW format: "Evaporators,Zone,Name,..." (label+header in one row)
                if len(row) > 1 and 'Name' in row:
                    # This row IS the header (new format)
                    header_row = row
                    name_col_index = row.index('Name')
                    data_start_row = i + 1
                    print(f"[dropbox] Found {first_col} section (new format, merged row) at line {i}")
                
                # Otherwise, OLD format: next row should be the header
                elif i + 1 < len(rows):
                    next_row = rows[i + 1]
                    if next_row and 'Name' in next_row:
                        # Next row IS the header (old format)
                        header_row = next_row
                        name_col_index = next_row.index('Name')
                        data_start_row = i + 2
                        print(f"[dropbox] Found {first_col} section (old format, separate rows) at line {i}")
                
                # If we found a header, collect data rows
                if header_row is not None and name_col_index >= 0 and data_start_row >= 0:
                    # Process data rows starting after header
                    for j in range(data_start_row, len(rows)):
                        data_row = rows[j]
                        if not data_row:
                            break
                        
                        # Stop if we encounter another section header row (Evaporators or Condensers in first column)
                        first_col_of_data_row = data_row[0] if data_row else ''
                        if first_col_of_data_row in ('Evaporators', 'Condensers'):
                            break
                        
                        # Get Name column value
                        if name_col_index < len(data_row):
                            room_name = data_row[name_col_index]
                        else:
                            room_name = ''
                        
                        # Stop if Name is empty
                        if not room_name or not room_name.strip():
                            break
                        
                        # Add name to the appropriate set
                        if section == 'evap':
                            evap_names.add(room_name)
                        else:
                            cond_names.add(room_name)
                    
                    print(f"[dropbox] Collected {section} names: {evap_names if section == 'evap' else cond_names}")
            
            i += 1
        
        print(f"[dropbox] Final extracted evaps: {evap_names}, conds: {cond_names}")
        return evap_names, cond_names
    
    except Exception as e:
        print(f"[dropbox] Error parsing CSV for room names: {e}")
        import traceback
        traceback.print_exc()
        return set(), set()

def create_folder_idempotent(dbx, path):
    """Create folder if it doesn't exist, ignore conflict errors"""
    try:
        dbx.files_create_folder_v2(path, autorename=False)
    except dropbox.exceptions.ApiError as e:
        if e.error.is_path() and e.error.get_path().is_conflict():
            pass  # Folder already exists
        else:
            raise

def check_file_exists(dbx, path):
    """Check if a file exists in Dropbox"""
    try:
        dbx.files_get_metadata(path)
        return True
    except dropbox.exceptions.ApiError as e:
        if e.error.is_path() and e.error.get_path().is_not_found():
            return False
        raise

@upload_bp.route("/upload", methods=["POST"])
def upload_csv():
    """
    Uploads CSV to both SiteWalk Exports and Pending Proposals trees.
    Creates per-room folders for evaporators and condensers in Pending Proposals.
    Returns structured response with separate statuses for each.
    Implements retry-safe logic: if SiteWalk file already exists, skip re-upload.
    """
    sitewalk_result = {"ok": False, "path": None}
    pending_result = {"ok": False, "path": None, "error": None}
    
    try:
        # Detect device
        user_agent = request.headers.get("User-Agent", "")
        device = detect_device(user_agent)
        
        # Extract form data
        if request.files and 'file' in request.files:
            file = request.files['file']
            data = file.read()
            filename = request.form.get('filename', '')
            company = request.form.get('company', 'Unknown')
            utility = request.form.get('utility', 'Unknown')
            street_address = request.form.get('street_address', 'Unknown')
        else:
            data = request.get_data()
            company = request.headers.get("X-Company", "Unknown")
            utility = "Unknown"
            street_address = "Unknown"
            filename = ''
        
        if not data:
            print(f"[export] ERROR: empty request body | device=\"{device}\"")
            return jsonify({
                "ok": False,
                "sitewalk": sitewalk_result,
                "pending": pending_result
            }), 400
        
        # Generate filename if not provided
        if not filename:
            ts = datetime.datetime.utcnow().strftime("%m.%d.%y_%H.%M.%S")
            safe_company = "".join(c for c in company if c not in "\\/:*?\"<>|").strip()
            filename = f"Data_Collection_{safe_company}_{ts}.csv"
        
        dbx = get_dbx()
        
        # ===== SITEWALK EXPORTS UPLOAD (existing behavior) =====
        print(f"[export] Start export | customer=\"{company}\" | device=\"{device}\"")
        
        company_folder_name = sanitize_name(company)
        sitewalk_company_folder = f"{BASE_PATH}/{company_folder_name}"
        sitewalk_file_path = f"{sitewalk_company_folder}/{filename}"
        
        # Check if SiteWalk file already exists (retry detection)
        sitewalk_file_exists = check_file_exists(dbx, sitewalk_file_path)
        
        if not sitewalk_file_exists:
            print(f"[dropbox] Uploading to SiteWalk Exports: {sitewalk_file_path}")
            
            # Create company folder
            try:
                create_folder_idempotent(dbx, sitewalk_company_folder)
            except Exception as e:
                print(f"[dropbox] ERROR creating SiteWalk company folder: {e}")
                return jsonify({
                    "ok": False,
                    "sitewalk": sitewalk_result,
                    "pending": pending_result
                }), 500
            
            # Upload to SiteWalk Exports
            try:
                dbx.files_upload(
                    data,
                    sitewalk_file_path,
                    mode=dropbox.files.WriteMode.overwrite,
                )
                sitewalk_result["ok"] = True
                sitewalk_result["path"] = sitewalk_file_path
                print(f"[dropbox] SUCCESS SiteWalk upload to {sitewalk_file_path}")
            except Exception as e:
                print(f"[dropbox] ERROR SiteWalk upload: {e}")
                return jsonify({
                    "ok": False,
                    "sitewalk": sitewalk_result,
                    "pending": pending_result
                }), 500
        else:
            # File already exists - this is a retry for Pending Proposals only
            print(f"[dropbox] SiteWalk file already exists (retry detected), skipping re-upload")
            sitewalk_result["ok"] = True
            sitewalk_result["path"] = sitewalk_file_path
        
        # ===== PENDING PROPOSALS UPLOAD (new behavior) =====
        print(f"[dropbox] Starting Pending Proposals upload flow")
        
        try:
            # List existing utility folders under PP_ROOT
            existing_utility_folders = list_folders_under_path(dbx, PP_ROOT)
            
            # Get mapped utility folder name
            utility_folder_name = pick_utility_folder_name(utility, existing_utility_folders)
            
            if not utility_folder_name:
                # FALLBACK: Missing utility - create structure under SiteWalk Exports / [Customer Name]
                print(f"[dropbox] INFO: Utility '{utility}' is missing, using fallback folder structure")
                
                pp_company = sanitize_name(company)
                pp_street = sanitize_name(street_address)
                
                # Extract date from filename and convert to M.D.YY format (no leading zeros)
                parts = filename.split('_')
                if len(parts) >= 2:
                    old_date_str = parts[-2]
                    try:
                        old_date = datetime.datetime.strptime(old_date_str, "%m.%d.%y")
                        date_part = format_date_no_leading_zeros(old_date)
                    except:
                        date_part = format_date_no_leading_zeros(datetime.datetime.utcnow())
                else:
                    date_part = format_date_no_leading_zeros(datetime.datetime.utcnow())
                
                # Build fallback folder hierarchy under SiteWalk Exports / [Customer Name]
                # Address folder without utility suffix
                fallback_customer_folder = f"{BASE_PATH}/{pp_company}"
                fallback_address_folder = f"{fallback_customer_folder}/{pp_street}"
                fallback_customer_docs = f"{fallback_customer_folder}/Customer Docs"
                fallback_old = f"{fallback_customer_folder}/Old"
                fallback_perf_guarantee = f"{fallback_customer_folder}/Performance guarantee"
                fallback_photos = f"{fallback_address_folder}/Photos_{date_part}"
                fallback_evaps = f"{fallback_photos}/Evaporators"
                fallback_conds = f"{fallback_photos}/Condensers"
                
                # Extract evaporator and condenser names from CSV
                evap_names, cond_names = extract_evap_cond_names(data)
                
                # Create all necessary folders (idempotent)
                fallback_folders = [
                    fallback_customer_folder,
                    fallback_address_folder,
                    fallback_customer_docs,
                    fallback_old,
                    fallback_perf_guarantee,
                    fallback_photos,
                    fallback_evaps,
                    fallback_conds
                ]
                
                # Add per-room/per-unit folders for evaporators and condensers
                for evap_name in evap_names:
                    if evap_name.strip():
                        safe_evap_name = sanitize_name(evap_name)
                        fallback_folders.append(f"{fallback_evaps}/{safe_evap_name}")
                
                for cond_name in cond_names:
                    if cond_name.strip():
                        safe_cond_name = sanitize_name(cond_name)
                        fallback_folders.append(f"{fallback_conds}/{safe_cond_name}")
                
                try:
                    for folder_path in fallback_folders:
                        create_folder_idempotent(dbx, folder_path)
                    
                    # Upload CSV to fallback address folder
                    fallback_file_path = f"{fallback_address_folder}/{filename}"
                    print(f"[dropbox] Uploading to fallback location: {fallback_file_path}")
                    
                    dbx.files_upload(
                        data,
                        fallback_file_path,
                        mode=dropbox.files.WriteMode.overwrite,
                    )
                    
                    pending_result["ok"] = True
                    pending_result["path"] = fallback_file_path
                    print(f"[dropbox] SUCCESS fallback upload to {fallback_file_path}")
                except Exception as e:
                    print(f"[dropbox] ERROR fallback upload: {e}")
                    pending_result["ok"] = False
                    pending_result["error"] = str(e)
            else:
                # Sanitize names for folder paths (minimal: trim, replace / and \)
                pp_company = sanitize_name(company)
                pp_street = sanitize_name(street_address)
                
                # Extract date from filename and convert to M.D.YY format (no leading zeros)
                # filename is like: Data_Collection_CompanyName_MM.DD.YY_HH.MM.SS.csv
                parts = filename.split('_')
                if len(parts) >= 2:
                    # Parse MM.DD.YY from filename and convert to M.D.YY
                    old_date_str = parts[-2]
                    try:
                        old_date = datetime.datetime.strptime(old_date_str, "%m.%d.%y")
                        date_part = format_date_no_leading_zeros(old_date)
                    except:
                        # Fallback: use current date if parsing fails
                        date_part = format_date_no_leading_zeros(datetime.datetime.utcnow())
                else:
                    # Fallback: use current date
                    date_part = format_date_no_leading_zeros(datetime.datetime.utcnow())
                
                # Build folder hierarchy: PP_ROOT/<utility_folder>/<company>/...
                pp_utility_folder = f"{PP_ROOT}/{utility_folder_name}"
                pp_customer_folder = f"{pp_utility_folder}/{pp_company}"
                pp_address_folder = f"{pp_customer_folder}/{pp_street}_{utility.upper()}"
                pp_customer_docs = f"{pp_customer_folder}/Customer Docs"
                pp_old = f"{pp_customer_folder}/Old"
                pp_perf_guarantee = f"{pp_customer_folder}/Performance guarantee"
                pp_photos = f"{pp_address_folder}/Photos_{date_part}"
                pp_evaps = f"{pp_address_folder}/Photos_{date_part}/Evaporators"
                pp_conds = f"{pp_address_folder}/Photos_{date_part}/Condensers"
                
                # Extract evaporator and condenser names from CSV
                evap_names, cond_names = extract_evap_cond_names(data)
                
                # Create all necessary folders (idempotent)
                folders_to_create = [
                    pp_utility_folder,
                    pp_customer_folder,
                    pp_address_folder,
                    pp_customer_docs,
                    pp_old,
                    pp_perf_guarantee,
                    pp_photos,
                    pp_evaps,
                    pp_conds
                ]
                
                # Add per-room/per-unit folders for evaporators and condensers
                for evap_name in evap_names:
                    if evap_name.strip():
                        safe_evap_name = sanitize_name(evap_name)
                        folders_to_create.append(f"{pp_evaps}/{safe_evap_name}")
                
                for cond_name in cond_names:
                    if cond_name.strip():
                        safe_cond_name = sanitize_name(cond_name)
                        folders_to_create.append(f"{pp_conds}/{safe_cond_name}")
                
                for folder_path in folders_to_create:
                    try:
                        create_folder_idempotent(dbx, folder_path)
                    except Exception as e:
                        print(f"[dropbox] ERROR creating folder {folder_path}: {e}")
                        pending_result["ok"] = False
                        pending_result["error"] = f"Folder creation failed: {str(e)}"
                        raise
                
                # Upload CSV to Pending Proposals
                pp_file_path = f"{pp_address_folder}/{filename}"
                print(f"[dropbox] Uploading to Pending Proposals: {pp_file_path}")
                
                dbx.files_upload(
                    data,
                    pp_file_path,
                    mode=dropbox.files.WriteMode.overwrite,
                )
                
                pending_result["ok"] = True
                pending_result["path"] = pp_file_path
                print(f"[dropbox] SUCCESS Pending Proposals upload to {pp_file_path}")
            
        except Exception as e:
            print(f"[dropbox] ERROR Pending Proposals: {e}")
            pending_result["ok"] = False
            pending_result["error"] = str(e)
        
        # Determine overall success: both uploads must succeed
        overall_ok = sitewalk_result["ok"] and pending_result["ok"]
        
        print(f"[dropbox] Upload complete | sitewalk.ok={sitewalk_result['ok']} | pending.ok={pending_result['ok']} | overall.ok={overall_ok}")
        
        return jsonify({
            "ok": overall_ok,
            "sitewalk": sitewalk_result,
            "pending": pending_result
        }), 200
    
    except Exception as e:
        error_msg = str(e)
        print(f"[dropbox] ERROR (unhandled): {error_msg}")
        return jsonify({
            "ok": False,
            "sitewalk": sitewalk_result,
            "pending": pending_result
        }), 500


@upload_bp.route("/upload-photo", methods=["POST"])
def upload_photo():
    """
    Upload a single photo to Dropbox in the correct project folder structure.
    
    Form fields:
    - file: The photo file (required)
    - company: Customer name (required)
    - utility: Utility company name (optional, for folder path)
    - street_address: Street address (required)
    - room_name: Room/unit name for assigned photos (optional)
    - section: 'evap' or 'cond' (required if room_name provided)
    - photo_filename: Original filename or timestamp-based name (optional)
    - visit_date: Date of site visit in MM.DD.YY format (optional)
    
    Returns JSON with upload status and path.
    """
    result = {"ok": False, "path": None, "error": None}
    
    try:
        # Check for file
        if 'file' not in request.files:
            return jsonify({"ok": False, "error": "No file provided"}), 400
        
        file = request.files['file']
        if not file or not file.filename:
            return jsonify({"ok": False, "error": "Empty file"}), 400
        
        photo_data = file.read()
        if not photo_data:
            return jsonify({"ok": False, "error": "Empty file data"}), 400
        
        # Extract form data
        company = request.form.get('company', 'Unknown')
        utility = request.form.get('utility', '')
        street_address = request.form.get('street_address', 'Unknown')
        room_name = request.form.get('room_name', '')
        section = request.form.get('section', '')  # 'evap' or 'cond'
        photo_filename = request.form.get('photo_filename', '')
        visit_date = request.form.get('visit_date', '')
        
        # Generate filename if not provided
        if not photo_filename:
            ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
            ext = file.filename.rsplit('.', 1)[-1] if '.' in file.filename else 'jpg'
            photo_filename = f"photo_{ts}.{ext}"
        
        # Parse visit date or use current date
        if visit_date:
            try:
                date_obj = datetime.datetime.strptime(visit_date, "%m.%d.%y")
                date_part = format_date_no_leading_zeros(date_obj)
            except:
                date_part = format_date_no_leading_zeros(datetime.datetime.utcnow())
        else:
            date_part = format_date_no_leading_zeros(datetime.datetime.utcnow())
        
        dbx = get_dbx()
        
        # List existing utility folders to determine correct path
        existing_utility_folders = list_folders_under_path(dbx, PP_ROOT)
        utility_folder_name = pick_utility_folder_name(utility, existing_utility_folders) if utility else None
        
        # Sanitize names
        pp_company = sanitize_name(company)
        pp_street = sanitize_name(street_address)
        
        # Determine base path based on utility presence
        if utility_folder_name:
            # Normal path: PP_ROOT/<utility>/<company>/<address_utility>/Photos_<date>
            base_path = f"{PP_ROOT}/{utility_folder_name}/{pp_company}/{pp_street}_{utility.upper()}/Photos_{date_part}"
        else:
            # Fallback path: BASE_PATH/<company>/<address>/Photos_<date>
            base_path = f"{BASE_PATH}/{pp_company}/{pp_street}/Photos_{date_part}"
        
        # Determine subfolder based on assignment
        if room_name and room_name.strip() and section:
            safe_room_name = sanitize_name(room_name.strip())
            if not safe_room_name:
                safe_room_name = "Unnamed_Room"
            if section == 'evap':
                photo_folder = f"{base_path}/Evaporators/{safe_room_name}"
            elif section == 'cond':
                photo_folder = f"{base_path}/Condensers/{safe_room_name}"
            else:
                photo_folder = f"{base_path}/Unassigned"
        else:
            # Unassigned photos
            photo_folder = f"{base_path}/Unassigned"
        
        # Create folder if needed
        try:
            create_folder_idempotent(dbx, photo_folder)
        except Exception as e:
            print(f"[dropbox] Error creating photo folder {photo_folder}: {e}")
        
        # Upload photo
        photo_path = f"{photo_folder}/{photo_filename}"
        print(f"[dropbox] Uploading photo to: {photo_path}")
        
        try:
            dbx.files_upload(
                photo_data,
                photo_path,
                mode=dropbox.files.WriteMode.add,  # Use 'add' to avoid overwriting
                autorename=True  # Auto-rename if conflict
            )
            result["ok"] = True
            result["path"] = photo_path
            print(f"[dropbox] SUCCESS photo upload to {photo_path}")
        except Exception as e:
            print(f"[dropbox] ERROR uploading photo: {e}")
            result["error"] = str(e)
        
        return jsonify(result), 200 if result["ok"] else 500
    
    except Exception as e:
        error_msg = str(e)
        print(f"[dropbox] ERROR photo upload: {error_msg}")
        return jsonify({"ok": False, "error": error_msg}), 500


@upload_bp.route("/upload-photos-batch", methods=["POST"])
def upload_photos_batch():
    """
    Upload multiple photos to Dropbox in a single request.
    
    Expects JSON body with:
    - company: Customer name
    - utility: Utility company name
    - street_address: Street address
    - visit_date: Date of visit in MM.DD.YY format
    - photos: Array of photo objects, each with:
        - data: Base64 encoded photo data
        - filename: Photo filename
        - room_name: Room/unit name (optional, null for unassigned)
        - section: 'evap' or 'cond' (required if room_name provided)
    
    Returns JSON with results for each photo.
    """
    results = {"ok": True, "uploaded": [], "failed": []}
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({"ok": False, "error": "No JSON data provided"}), 400
        
        company = data.get('company', 'Unknown')
        utility = data.get('utility', '')
        street_address = data.get('street_address', 'Unknown')
        visit_date = data.get('visit_date', '')
        photos = data.get('photos', [])
        
        if not photos:
            return jsonify({"ok": True, "message": "No photos to upload", "uploaded": [], "failed": []}), 200
        
        # Parse visit date
        if visit_date:
            try:
                date_obj = datetime.datetime.strptime(visit_date, "%m.%d.%y")
                date_part = format_date_no_leading_zeros(date_obj)
            except:
                date_part = format_date_no_leading_zeros(datetime.datetime.utcnow())
        else:
            date_part = format_date_no_leading_zeros(datetime.datetime.utcnow())
        
        dbx = get_dbx()
        
        # Determine base paths
        existing_utility_folders = list_folders_under_path(dbx, PP_ROOT)
        utility_folder_name = pick_utility_folder_name(utility, existing_utility_folders) if utility else None
        
        pp_company = sanitize_name(company)
        pp_street = sanitize_name(street_address)
        
        if utility_folder_name:
            base_path = f"{PP_ROOT}/{utility_folder_name}/{pp_company}/{pp_street}_{utility.upper()}/Photos_{date_part}"
        else:
            base_path = f"{BASE_PATH}/{pp_company}/{pp_street}/Photos_{date_part}"
        
        # Create base folders
        for folder in [f"{base_path}/Evaporators", f"{base_path}/Condensers", f"{base_path}/Unassigned"]:
            try:
                create_folder_idempotent(dbx, folder)
            except:
                pass
        
        # Upload each photo
        for photo in photos:
            try:
                photo_data_b64 = photo.get('data', '')
                filename = photo.get('filename', f"photo_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.jpg")
                room_name = photo.get('room_name', '')
                section = photo.get('section', '')
                
                # Decode base64 data
                if ',' in photo_data_b64:
                    # Handle data URL format: "data:image/jpeg;base64,..."
                    photo_data_b64 = photo_data_b64.split(',', 1)[1]
                
                photo_bytes = base64.b64decode(photo_data_b64)
                
                # Determine folder
                if room_name and room_name.strip() and section:
                    safe_room_name = sanitize_name(room_name.strip())
                    if not safe_room_name:
                        safe_room_name = "Unnamed_Room"
                    if section == 'evap':
                        photo_folder = f"{base_path}/Evaporators/{safe_room_name}"
                    elif section == 'cond':
                        photo_folder = f"{base_path}/Condensers/{safe_room_name}"
                    else:
                        photo_folder = f"{base_path}/Unassigned"
                else:
                    photo_folder = f"{base_path}/Unassigned"
                
                # Create room folder if needed
                try:
                    create_folder_idempotent(dbx, photo_folder)
                except:
                    pass
                
                # Upload
                photo_path = f"{photo_folder}/{filename}"
                dbx.files_upload(
                    photo_bytes,
                    photo_path,
                    mode=dropbox.files.WriteMode.add,
                    autorename=True
                )
                
                results["uploaded"].append({
                    "filename": filename,
                    "path": photo_path,
                    "room_name": room_name
                })
                
            except Exception as e:
                print(f"[dropbox] ERROR uploading photo {photo.get('filename', 'unknown')}: {e}")
                results["failed"].append({
                    "filename": photo.get('filename', 'unknown'),
                    "error": str(e)
                })
                results["ok"] = False
        
        print(f"[dropbox] Batch upload complete: {len(results['uploaded'])} succeeded, {len(results['failed'])} failed")
        return jsonify(results), 200
    
    except Exception as e:
        error_msg = str(e)
        print(f"[dropbox] ERROR batch photo upload: {error_msg}")
        return jsonify({"ok": False, "error": error_msg, "uploaded": [], "failed": []}), 500
