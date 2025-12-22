"""
Refrigeration Data Collection - Flask Backend
==============================================

OVERVIEW:
This Flask backend provides persistent storage and cross-device access for refrigeration
site walk data. It uses a simple JSON file-based storage system with user buckets.

CROSS-DEVICE BEHAVIOR:
- All devices currently use X-User-Id: "default" (no real authentication yet)
- All saved projects are stored in a shared pool under the "default" user
- Creating a project on Device A makes it immediately available on Device B
- This is intentional for single-user/single-company deployment

ACCURATE UNIT COUNTS:
- Backend correctly sums the 'room-count' field from entries (NOT card count)
- A single card with room-count=5 represents 5 units, not 1 unit
- This matches the frontend's calculation logic

DATA STRUCTURE:
- projects_data.json: {user_id: {project_id: project_data}}
- Each project includes: siteData, entries, photos, _metadata
- _metadata: {project_id, owner_id, customer, name, created_at, saved_at, updated_at}

API ENDPOINTS FOR PROJECT MANAGEMENT:
- POST /api/projects/create - Create new empty project with name
- GET /api/projects - List all projects (includes name field)
- GET /api/projects/<project_id> - Fetch single project by id
- PUT /api/projects/<project_id> - Update existing project
- POST /api/projects/duplicate/<project_id> - Duplicate a project
- DELETE /api/data/<project_id> - Delete a project

KNOWN LIMITATIONS:
- No real authentication - all devices share "default" user pool
- No per-user isolation - admin role sees all projects
- No real-time sync - clients must poll /api/projects
- File-based storage - not suitable for high concurrency

FUTURE ENHANCEMENTS:
- Add real authentication (OAuth, JWT)
- Per-user project isolation
- WebSocket for real-time sync
- Database backend (PostgreSQL)
"""

# ==============================================================================
# CRITICAL: Safe print wrapper - MUST be FIRST before any imports
# Prevents >50KB objects from crashing Replit chat service
# ==============================================================================
import sys
import builtins

_MAX_PRINT_BYTES = 50 * 1024  # 50KB limit

_original_print = builtins.print

def _safe_print(*args, **kwargs):
    """Safe print wrapper that truncates large objects"""
    safe_args = []
    for arg in args:
        try:
            if isinstance(arg, (dict, list)):
                import json
                s = json.dumps(arg, default=str)
                if len(s) > _MAX_PRINT_BYTES:
                    if isinstance(arg, dict):
                        keys = list(arg.keys())[:10]
                        safe_args.append(f"[OMITTED] dict size={len(s)} keys={keys}...")
                    else:
                        safe_args.append(f"[OMITTED] list size={len(s)} len={len(arg)}")
                    continue
            elif isinstance(arg, str) and len(arg) > _MAX_PRINT_BYTES:
                safe_args.append(f"[OMITTED] str size={len(arg)}")
                continue
            elif isinstance(arg, bytes) and len(arg) > _MAX_PRINT_BYTES:
                safe_args.append(f"[OMITTED] bytes size={len(arg)}")
                continue
        except Exception:
            pass
        safe_args.append(arg)
    return _original_print(*safe_args, **kwargs)

builtins.print = _safe_print
print("[PRINT] Safe print wrapper active - max per-arg: 50KB")

import traceback

def log_exception(exc_type, exc_value, exc_tb):
    print(f"[FATAL] Uncaught exception: {exc_type.__name__}: {exc_value}")
    traceback.print_exception(exc_type, exc_value, exc_tb)
    
sys.excepthook = log_exception

from flask import Flask, send_file, jsonify, request, redirect
from flask_cors import CORS
import json
import os
from datetime import datetime
import uuid
import requests
import dropbox
from dropbox.exceptions import ApiError, AuthError
from concurrent.futures import ThreadPoolExecutor
import threading

# Thread pool for parallel bill processing (max 3 concurrent extractions)
bill_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix='bill_processor')

# Lock for thread-safe access to stored_data
data_lock = threading.Lock()

app = Flask(__name__)
CORS(app)

# ==================================================================================
# RESPONSE SIZE LIMITER - BLOCK payloads > 1MB (dev) or > 2MB (prod)
# ==================================================================================
import os as _os_for_env
_IS_PRODUCTION = _os_for_env.environ.get('REPLIT_DEPLOYMENT', '') != ''
MAX_RESPONSE_SIZE_MB = 2 if _IS_PRODUCTION else 1
MAX_RESPONSE_SIZE_BYTES = MAX_RESPONSE_SIZE_MB * 1024 * 1024

@app.after_request
def check_response_size(response):
    """Block JSON responses that exceed size limit - prevents Replit crash"""
    if response.content_type and 'application/json' in response.content_type:
        content_length = response.content_length or len(response.get_data())
        endpoint = request.endpoint or request.path
        size_kb = content_length / 1024
        
        # Log size for every JSON response (route + bytes)
        print(f"[API] {request.method} {request.path} bytes={content_length}")
        
        if content_length > MAX_RESPONSE_SIZE_BYTES:
            size_mb = content_length / (1024 * 1024)
            # DO NOT include payload in error - only size + route
            print(f"[BLOCKED] Response too large: {endpoint} = {size_mb:.2f}MB (limit: {MAX_RESPONSE_SIZE_MB}MB)")
            # Return error response instead of giant payload
            error_response = jsonify({
                'error': 'Response too large',
                'route': endpoint,
                'size_mb': round(size_mb, 2),
                'limit_mb': MAX_RESPONSE_SIZE_MB
            })
            error_response.status_code = 413
            return error_response
    return response

@app.before_request
def redirect_to_custom_domain():
    """Redirect Replit URLs to custom domain in production only."""
    if os.environ.get('REPLIT_DEPLOYMENT'):
        host = request.host.lower()
        if 'replit.app' in host or 'replit.dev' in host:
            new_url = 'https://sitewalk.carlislenergy.com' + request.full_path
            if new_url.endswith('?'):
                new_url = new_url[:-1]
            return redirect(new_url, code=301)

from backend_upload_to_dropbox import upload_bp
app.register_blueprint(upload_bp)

DATA_FILE = 'projects_data.json'
USERS_FILE = 'users.json'
AUTOSAVE_FILE = 'autosave_data.json'
DELETED_FILE = 'deleted_projects.json'

# Retention period for deleted projects (30 days)
DELETED_RETENTION_DAYS = 30

# Dropbox configuration
DROPBOX_ROOT_PATH = "/1. CES/1. FRIGITEK/1. FRIGITEK ANALYSIS/SiteWalk Exports"

# Global progress tracker for bill extraction, keyed by file_id
# Structure: { file_id: { 'status': 'pending'|'extracting'|'ok'|'needs_review', 'progress': 0.0-1.0, 'updated_at': timestamp } }
extraction_progress = {}

def cleanup_old_progress_entries():
    """Remove progress entries older than 5 minutes"""
    import time
    current_time = time.time()
    expired_keys = [
        fid for fid, data in extraction_progress.items()
        if current_time - data.get('updated_at', 0) > 300  # 5 minutes
    ]
    for fid in expired_keys:
        del extraction_progress[fid]

# Cache for refreshed access token
_dropbox_token_cache = {"access_token": None, "expires_at": None}

def get_dropbox_access_token():
    """
    Get a fresh Dropbox access token using OAuth refresh token flow.
    
    Uses DROPBOX_REFRESH_TOKEN + DROPBOX_APP_KEY + DROPBOX_APP_SECRET 
    to obtain a short-lived access token that auto-renews.
    
    Falls back to DROPBOX_ACCESS_TOKEN if refresh token not available.
    """
    global _dropbox_token_cache
    
    # Check if we have a cached token that's still valid (with 60s buffer)
    if _dropbox_token_cache["access_token"] and _dropbox_token_cache["expires_at"]:
        from datetime import datetime, timedelta
        if datetime.now() < _dropbox_token_cache["expires_at"] - timedelta(seconds=60):
            print("[dropbox] Using cached access token")
            return _dropbox_token_cache["access_token"]
    
    # Try refresh token flow first
    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    app_key = os.getenv("DROPBOX_APP_KEY")
    app_secret = os.getenv("DROPBOX_APP_SECRET")
    
    if refresh_token and app_key and app_secret:
        try:
            print("[dropbox] Refreshing access token using OAuth...")
            resp = requests.post(
                "https://api.dropboxapi.com/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": app_key,
                    "client_secret": app_secret
                },
                timeout=15
            )
            
            if resp.ok:
                data = resp.json()
                access_token = data.get("access_token")
                expires_in = data.get("expires_in", 14400)  # Default 4 hours
                
                if access_token:
                    from datetime import datetime, timedelta
                    _dropbox_token_cache["access_token"] = access_token
                    _dropbox_token_cache["expires_at"] = datetime.now() + timedelta(seconds=expires_in)
                    print(f"[dropbox] Got fresh access token, expires in {expires_in}s")
                    return access_token
            else:
                print(f"[dropbox] Token refresh failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"[dropbox] Token refresh error: {e}")
    
    # Fall back to static access token
    token = os.getenv("DROPBOX_ACCESS_TOKEN")
    if not token:
        token = os.getenv("DROPBOX_ACCESS_TC")
    
    if token:
        print(f"[dropbox] Using static access token, length={len(token)}")
        return token
    
    print("[dropbox] ERROR: No Dropbox credentials available")
    return None

# Legacy function name for compatibility
def get_dropbox_token():
    return get_dropbox_access_token()

# Cache for Dropbox account info
_dropbox_account_cache = {"email": None, "name": None, "fetched": False}

def get_dropbox_account_info():
    """
    Get the current Dropbox account info (email, display name).
    Caches the result to avoid repeated API calls.
    """
    global _dropbox_account_cache
    
    # Return cached result if already fetched
    if _dropbox_account_cache["fetched"]:
        return _dropbox_account_cache
    
    token = get_dropbox_access_token()
    if not token:
        print("[dropbox] No token configured - cannot get account info")
        _dropbox_account_cache["fetched"] = True
        return _dropbox_account_cache
    
    try:
        resp = requests.post(
            "https://api.dropboxapi.com/2/users/get_current_account",
            headers={
                "Authorization": f"Bearer {token}"
            },
            data="null",
            timeout=10
        )
        resp.raise_for_status()
        info = resp.json()
        
        email = info.get("email", "unknown")
        display_name = info.get("name", {}).get("display_name", "unknown")
        
        print(f"[dropbox] Current account: {email} ({display_name})")
        
        _dropbox_account_cache = {
            "email": email,
            "name": display_name,
            "fetched": True
        }
        return _dropbox_account_cache
        
    except Exception as e:
        print(f"[dropbox] Error getting account info: {e}")
        _dropbox_account_cache["fetched"] = True
        return _dropbox_account_cache

# Fetch Dropbox account info at startup
if get_dropbox_access_token():
    get_dropbox_account_info()

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                # Migrate old structure to new user-based structure if needed
                if data and not any(isinstance(v, dict) and 'role' not in v for v in data.values()):
                    # Already in new format or empty
                    return data
                # Old format: {project_id: project_data}
                # New format: {user_id: {project_id: project_data}}
                if data and all(isinstance(v, dict) and '_metadata' in v for v in data.values()):
                    # Migrate: move all projects under 'default' user
                    migrated = {'default': data}
                    save_data(migrated)
                    return migrated
                return data
        except:
            return {}
    return {}

def save_data(data):
    import copy
    with data_lock:
        # Deep copy to prevent dictionary changed size during iteration
        data_copy = copy.deepcopy(data)
    with open(DATA_FILE, 'w') as f:
        json.dump(data_copy, f, indent=2)

def load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    # Default: create admin user
    return {
        'default': {
            'user_id': 'default',
            'display_name': 'Carlisle Energy',
            'role': 'admin',
            'created_at': datetime.now().isoformat()
        }
    }

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

def load_deleted_projects():
    """Load deleted projects from JSON file.
    Structure: {user_id: {project_id: {...project_data..., '_deleted_at': timestamp}}}
    """
    if os.path.exists(DELETED_FILE):
        try:
            with open(DELETED_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_deleted_projects(data):
    """Save deleted projects to JSON file."""
    import copy
    with data_lock:
        data_copy = copy.deepcopy(data)
    with open(DELETED_FILE, 'w') as f:
        json.dump(data_copy, f, indent=2)

def cleanup_expired_deleted_projects():
    """Remove projects deleted more than DELETED_RETENTION_DAYS ago.
    Called on app startup.
    """
    global deleted_projects
    from datetime import datetime, timedelta
    
    cutoff_date = datetime.now() - timedelta(days=DELETED_RETENTION_DAYS)
    removed_count = 0
    
    for user_id in list(deleted_projects.keys()):
        projects = deleted_projects[user_id]
        for project_id in list(projects.keys()):
            project = projects[project_id]
            deleted_at_str = project.get('_deleted_at')
            if deleted_at_str:
                try:
                    deleted_at = datetime.fromisoformat(deleted_at_str.replace('Z', '+00:00').replace('+00:00', ''))
                    if deleted_at < cutoff_date:
                        del projects[project_id]
                        removed_count += 1
                        print(f"[cleanup] Permanently removed expired project {project_id}")
                except:
                    pass
        # Remove empty user buckets
        if not projects:
            del deleted_projects[user_id]
    
    if removed_count > 0:
        save_deleted_projects(deleted_projects)
        print(f"[cleanup] Removed {removed_count} expired deleted projects")

stored_data = load_data()
users_db = load_users()
deleted_projects = load_deleted_projects()

# Ensure users file exists
if not os.path.exists(USERS_FILE):
    save_users(users_db)

# Cleanup expired deleted projects on startup
cleanup_expired_deleted_projects()

@app.route('/')
def index():
    response = send_file('index.html')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/health')
def health_check():
    """Health check endpoint for deployment monitoring.
    
    Returns immediately without database dependency to ensure
    Gunicorn workers stay alive during startup.
    """
    return jsonify({'status': 'ok', 'service': 'sitewalk'}), 200

@app.route('/static/<path:filename>')
def serve_static(filename):
    # Serve static files from static directory
    from flask import send_from_directory
    return send_from_directory('static', filename)

@app.route('/api/data', methods=['GET', 'POST'])
def handle_data():
    # Get user context from headers (default to admin user for backward compatibility)
    user_id = request.headers.get('X-User-Id', 'default')
    user_role = request.headers.get('X-User-Role', 'admin')
    
    if request.method == 'POST':
        data = request.get_json()
        if data:
            provided_project_id = data.get('project_id')
            customer = data.get('siteData', {}).get('customer', 'Unknown')
            timestamp = datetime.now().isoformat()
            
            # Log client action and version headers for debugging
            client_action = request.headers.get('X-Client-Action', 'unknown')
            client_version = request.headers.get('X-Client-Version', 'unknown')
            print(f"[projects] CREATE request: X-Client-Action={client_action}, X-Client-Version={client_version}, projectId={provided_project_id or 'NEW'}")
            
            # Current build version - used to detect stale clients
            CURRENT_BUILD_ID = '2025-12-17-v1'
            
            # Build mismatch detection - prompt old clients to refresh
            if client_version != 'unknown' and client_version != CURRENT_BUILD_ID:
                print(f"[projects] BUILD MISMATCH: client={client_version}, server={CURRENT_BUILD_ID}")
                return jsonify({
                    'status': 'error',
                    'message': 'Client out of date, please refresh the page.',
                    'code': 'BUILD_MISMATCH',
                    'client_version': client_version,
                    'server_version': CURRENT_BUILD_ID
                }), 409
            
            # HARD PROTECTION: Only allow explicit create actions
            # Valid actions: 'create_new', 'duplicate', 'import'
            # Rejected: 'autosave', 'unknown', missing headers
            allowed_create_actions = ['create_new', 'duplicate', 'import']
            if client_action not in allowed_create_actions:
                print(f"[projects] REJECTED: Invalid X-Client-Action={client_action} for POST. Must be one of {allowed_create_actions}")
                return jsonify({
                    'status': 'error',
                    'message': 'Cannot create project. Please refresh the page.',
                    'code': 'CREATE_ACTION_INVALID',
                    'received_action': client_action,
                    'allowed_actions': allowed_create_actions
                }), 409
            
            # IDEMPOTENCY KEY: Prevent duplicate project creation from retries/replays
            idempotency_key = request.headers.get('Idempotency-Key')
            if not idempotency_key:
                print(f"[projects] REJECTED: Missing Idempotency-Key header for POST")
                return jsonify({
                    'status': 'error',
                    'message': 'Missing Idempotency-Key header. Please refresh the page.',
                    'code': 'IDEMPOTENCY_KEY_MISSING'
                }), 409
            
            # Check if this idempotency key was already used (simple in-memory cache)
            # In production, this should be Redis-backed or database-backed
            if not hasattr(app, '_idempotency_cache'):
                app._idempotency_cache = {}
            
            # Clean old entries (keep last 1000)
            if len(app._idempotency_cache) > 1000:
                # Remove oldest half
                sorted_keys = sorted(app._idempotency_cache.keys(), key=lambda k: app._idempotency_cache[k]['timestamp'])
                for key in sorted_keys[:500]:
                    del app._idempotency_cache[key]
            
            if idempotency_key in app._idempotency_cache:
                cached = app._idempotency_cache[idempotency_key]
                print(f"[projects] IDEMPOTENCY HIT: Key {idempotency_key} already used for project {cached['project_id']}")
                # Return the original response (idempotent behavior)
                return jsonify({
                    'status': 'success',
                    'project_id': cached['project_id'],
                    'message': 'Project already created (idempotent)',
                    'idempotent': True
                }), 200
            
            # Check if this is an update to existing project (preserve created_at and name)
            existing_metadata = {}
            is_update = False
            found_owner = None
            
            if provided_project_id:
                # Look for the project in current user's data
                if user_id in stored_data and provided_project_id in stored_data[user_id]:
                    existing_metadata = stored_data[user_id][provided_project_id].get('_metadata', {})
                    is_update = True
                    found_owner = user_id
                else:
                    # Also check other users for admin
                    for uid, projects in stored_data.items():
                        if provided_project_id in projects:
                            existing_metadata = projects[provided_project_id].get('_metadata', {})
                            is_update = True
                            found_owner = uid
                            break
                
                # CRITICAL FIX: If project_id was provided but NOT found, reject the request
                # This prevents duplicate creation from stale localStorage IDs
                if not is_update:
                    print(f"[api/data] REJECTED unknown project_id: {provided_project_id}")
                    return jsonify({
                        'status': 'error',
                        'message': 'Project not found - ID may be stale',
                        'code': 'PROJECT_NOT_FOUND',
                        'provided_id': provided_project_id
                    }), 404
            
            # Generate new project ID only if none was provided (new project creation)
            project_id = provided_project_id or str(uuid.uuid4())
            
            # Set _metadata.name: use request name, then existing metadata name, then customer
            project_name = data.get('name') or existing_metadata.get('name') or customer
            
            # Preserve created_at if this is an update, otherwise use current timestamp
            created_at = existing_metadata.get('created_at') if is_update else timestamp
            
            # Add metadata with name, created_at, updated_at
            # saved_at = when data was persisted, updated_at = when content changed
            data['_metadata'] = {
                'project_id': project_id,
                'owner_id': user_id,
                'customer': customer,
                'name': project_name,
                'created_at': created_at,
                'saved_at': timestamp,
                'updated_at': timestamp
            }
            
            # Ensure user bucket exists
            if user_id not in stored_data:
                stored_data[user_id] = {}
            
            # Store project under user
            stored_data[user_id][project_id] = data
            save_data(stored_data)
            
            # Store idempotency key to prevent duplicate creates
            if idempotency_key:
                app._idempotency_cache[idempotency_key] = {
                    'project_id': project_id,
                    'timestamp': timestamp,
                    'user_id': user_id
                }
                print(f"[projects] IDEMPOTENCY STORED: Key {idempotency_key} -> project {project_id}")
            
            return jsonify({
                'status': 'success',
                'message': 'Project saved',
                'project_id': project_id,
                'owner_id': user_id,
                'name': project_name
            }), 200
        return jsonify({'status': 'error', 'message': 'No data provided'}), 400
    
    else:
        # GET request - retrieve specific project
        project_id = request.args.get('project')
        if not project_id:
            return jsonify({'error': 'project_id required'}), 400
        
        # Admin can access any project, technician only their own
        if user_role == 'admin':
            # Search all users for this project
            for uid, projects in stored_data.items():
                if project_id in projects:
                    return jsonify(projects[project_id])
        else:
            # Technician - only their projects
            if user_id in stored_data and project_id in stored_data[user_id]:
                return jsonify(stored_data[user_id][project_id])
        
        return jsonify({'error': 'Project not found'}), 404

@app.route('/api/projects')
def list_projects():
    """List projects with optional pagination.
    
    Query params:
        limit: Max projects to return (default: 25, max: 100)
        offset: Number of projects to skip (default: 0)
    
    Returns:
        Array of project summaries (minimal fields only)
    """
    # Get user context from headers
    user_id = request.headers.get('X-User-Id', 'default')
    user_role = request.headers.get('X-User-Role', 'admin')
    
    # Pagination params with sensible defaults
    try:
        limit = min(int(request.args.get('limit', 25)), 100)  # Max 100
        offset = max(int(request.args.get('offset', 0)), 0)
    except ValueError:
        limit, offset = 25, 0
    
    projects = []
    
    if user_role == 'admin':
        # Admin sees all projects from all users
        for uid, user_projects in stored_data.items():
            for pid, data in user_projects.items():
                owner_name = users_db.get(uid, {}).get('display_name', uid)
                metadata = data.get('_metadata', {})
                
                # FIXED: Sum the room-count field instead of counting cards
                evap_count = sum(int(e.get('room-count', 0)) for e in data.get('entries', []) if e.get('section') == 'evap')
                cond_count = sum(int(e.get('room-count', 0)) for e in data.get('entries', []) if e.get('section') == 'cond')
                
                projects.append({
                    'id': pid,
                    'owner_id': uid,
                    'owner_name': owner_name,
                    'customer': metadata.get('customer') or data.get('siteData', {}).get('customer', 'Unknown'),
                    'name': metadata.get('name') or metadata.get('customer') or data.get('siteData', {}).get('customer', 'Unknown'),
                    'created_at': metadata.get('created_at'),
                    'updated_at': metadata.get('updated_at'),
                    'saved_at': metadata.get('saved_at'),
                    'evap_count': evap_count,
                    'cond_count': cond_count,
                    'city': data.get('siteData', {}).get('city'),
                    'state': data.get('siteData', {}).get('state')
                })
    else:
        # Technician sees only their own projects
        if user_id in stored_data:
            for pid, data in stored_data[user_id].items():
                metadata = data.get('_metadata', {})
                
                # FIXED: Sum the room-count field instead of counting cards
                evap_count = sum(int(e.get('room-count', 0)) for e in data.get('entries', []) if e.get('section') == 'evap')
                cond_count = sum(int(e.get('room-count', 0)) for e in data.get('entries', []) if e.get('section') == 'cond')
                
                projects.append({
                    'id': pid,
                    'owner_id': user_id,
                    'customer': metadata.get('customer') or data.get('siteData', {}).get('customer', 'Unknown'),
                    'name': metadata.get('name') or metadata.get('customer') or data.get('siteData', {}).get('customer', 'Unknown'),
                    'created_at': metadata.get('created_at'),
                    'updated_at': metadata.get('updated_at'),
                    'saved_at': metadata.get('saved_at'),
                    'evap_count': evap_count,
                    'cond_count': cond_count,
                    'city': data.get('siteData', {}).get('city'),
                    'state': data.get('siteData', {}).get('state')
                })
    
    # Sort by saved_at descending (most recent first)
    projects.sort(key=lambda x: x.get('saved_at') or '', reverse=True)
    
    # Apply pagination
    total = len(projects)
    projects = projects[offset:offset + limit]
    
    # Return with pagination metadata
    return jsonify({
        'items': projects,
        'total': total,
        'limit': limit,
        'offset': offset,
        'hasMore': offset + limit < total
    })

@app.route('/api/data/<project_id>', methods=['DELETE'])
def delete_project(project_id):
    """Move project to deleted archive instead of permanent deletion.
    
    IDEMPOTENT: Returns success even if project already deleted or doesn't exist.
    This prevents ghost projects from appearing when delete fails.
    """
    global deleted_projects
    
    user_id = request.headers.get('X-User-Id', 'default')
    user_role = request.headers.get('X-User-Role', 'admin')
    
    # First check if already in deleted_projects (idempotent - already deleted)
    already_deleted = False
    if user_role == 'admin':
        for uid, user_projects in deleted_projects.items():
            if project_id in user_projects:
                already_deleted = True
                break
    else:
        if user_id in deleted_projects and project_id in deleted_projects[user_id]:
            already_deleted = True
    
    if already_deleted:
        print(f"[delete] Project {project_id} already in deleted archive (idempotent success)")
        return jsonify({
            'status': 'success',
            'message': 'Project already deleted',
            'project_id': project_id,
            'idempotent': True
        }), 200
    
    # Try to find and move to deleted
    archived = False
    owner_id = None
    project_data = None
    
    if user_role == 'admin':
        for uid, user_projects in stored_data.items():
            if project_id in user_projects:
                project_data = user_projects[project_id]
                del user_projects[project_id]
                owner_id = uid
                archived = True
                break
    else:
        if user_id in stored_data and project_id in stored_data[user_id]:
            project_data = stored_data[user_id][project_id]
            del stored_data[user_id][project_id]
            owner_id = user_id
            archived = True
    
    if archived and project_data:
        project_data['_deleted_at'] = datetime.now().isoformat() + 'Z'
        
        if owner_id not in deleted_projects:
            deleted_projects[owner_id] = {}
        deleted_projects[owner_id][project_id] = project_data
        
        save_data(stored_data)
        save_deleted_projects(deleted_projects)
        
        print(f"[delete] Project {project_id} moved to deleted archive")
        return jsonify({
            'status': 'success',
            'message': 'Project moved to Recently Deleted',
            'project_id': project_id,
            'owner_id': owner_id
        }), 200
    else:
        # Idempotent: project doesn't exist anywhere = success (already gone)
        print(f"[delete] Project {project_id} not found anywhere (idempotent success)")
        return jsonify({
            'status': 'success',
            'message': 'Project not found (already deleted)',
            'project_id': project_id,
            'idempotent': True
        }), 200


@app.route('/api/deleted-projects', methods=['GET'])
def list_deleted_projects():
    """List all deleted projects with days remaining until permanent deletion."""
    user_id = request.headers.get('X-User-Id', 'default')
    user_role = request.headers.get('X-User-Role', 'admin')
    
    from datetime import datetime, timedelta
    
    projects = []
    now = datetime.now()
    
    if user_role == 'admin':
        for uid, user_projects in deleted_projects.items():
            for pid, data in user_projects.items():
                deleted_at_str = data.get('_deleted_at', '')
                try:
                    deleted_at = datetime.fromisoformat(deleted_at_str.replace('Z', ''))
                    days_elapsed = (now - deleted_at).days
                    days_remaining = max(0, DELETED_RETENTION_DAYS - days_elapsed)
                except:
                    deleted_at = now
                    days_remaining = DELETED_RETENTION_DAYS
                
                metadata = data.get('_metadata', {})
                projects.append({
                    'id': pid,
                    'owner_id': uid,
                    'name': metadata.get('name') or metadata.get('customer') or 'Untitled',
                    'customer': metadata.get('customer') or data.get('siteData', {}).get('customer', ''),
                    'deleted_at': deleted_at_str,
                    'days_remaining': days_remaining,
                    'city': data.get('siteData', {}).get('city'),
                    'state': data.get('siteData', {}).get('state')
                })
    else:
        if user_id in deleted_projects:
            for pid, data in deleted_projects[user_id].items():
                deleted_at_str = data.get('_deleted_at', '')
                try:
                    deleted_at = datetime.fromisoformat(deleted_at_str.replace('Z', ''))
                    days_elapsed = (now - deleted_at).days
                    days_remaining = max(0, DELETED_RETENTION_DAYS - days_elapsed)
                except:
                    deleted_at = now
                    days_remaining = DELETED_RETENTION_DAYS
                
                metadata = data.get('_metadata', {})
                projects.append({
                    'id': pid,
                    'owner_id': user_id,
                    'name': metadata.get('name') or metadata.get('customer') or 'Untitled',
                    'customer': metadata.get('customer') or data.get('siteData', {}).get('customer', ''),
                    'deleted_at': deleted_at_str,
                    'days_remaining': days_remaining,
                    'city': data.get('siteData', {}).get('city'),
                    'state': data.get('siteData', {}).get('state')
                })
    
    projects.sort(key=lambda x: x.get('deleted_at') or '', reverse=True)
    return jsonify(projects)


@app.route('/api/deleted-projects/<project_id>/restore', methods=['POST'])
def restore_deleted_project(project_id):
    """Restore a deleted project back to active projects.
    
    IDEMPOTENT: Returns success if project already active or doesn't exist.
    """
    global deleted_projects
    
    user_id = request.headers.get('X-User-Id', 'default')
    user_role = request.headers.get('X-User-Role', 'admin')
    
    # Check if already in active projects (idempotent)
    already_active = False
    if user_role == 'admin':
        for uid, user_projects in stored_data.items():
            if project_id in user_projects:
                already_active = True
                break
    else:
        if user_id in stored_data and project_id in stored_data[user_id]:
            already_active = True
    
    if already_active:
        return jsonify({
            'status': 'success',
            'message': 'Project already active',
            'project_id': project_id,
            'idempotent': True
        }), 200
    
    restored = False
    owner_id = None
    project_data = None
    
    if user_role == 'admin':
        for uid, user_projects in deleted_projects.items():
            if project_id in user_projects:
                project_data = user_projects[project_id]
                del user_projects[project_id]
                owner_id = uid
                restored = True
                break
    else:
        if user_id in deleted_projects and project_id in deleted_projects[user_id]:
            project_data = deleted_projects[user_id][project_id]
            del deleted_projects[user_id][project_id]
            owner_id = user_id
            restored = True
    
    if restored and project_data:
        if '_deleted_at' in project_data:
            del project_data['_deleted_at']
        
        project_data['_metadata']['updated_at'] = datetime.now().isoformat()
        
        if owner_id not in stored_data:
            stored_data[owner_id] = {}
        stored_data[owner_id][project_id] = project_data
        
        save_data(stored_data)
        save_deleted_projects(deleted_projects)
        
        return jsonify({
            'status': 'success',
            'message': 'Project restored successfully',
            'project_id': project_id,
            'owner_id': owner_id
        }), 200
    else:
        # Idempotent: not found = success (nothing to restore)
        return jsonify({
            'status': 'success',
            'message': 'Project not found in deleted archive',
            'project_id': project_id,
            'idempotent': True
        }), 200


@app.route('/api/deleted-projects/<project_id>', methods=['DELETE'])
def permanently_delete_project(project_id):
    """Permanently delete a project from the archive (cannot be undone).
    
    IDEMPOTENT: Returns success if project doesn't exist.
    """
    global deleted_projects
    
    user_id = request.headers.get('X-User-Id', 'default')
    user_role = request.headers.get('X-User-Role', 'admin')
    
    deleted = False
    owner_id = None
    
    if user_role == 'admin':
        for uid, user_projects in deleted_projects.items():
            if project_id in user_projects:
                del user_projects[project_id]
                owner_id = uid
                deleted = True
                break
    else:
        if user_id in deleted_projects and project_id in deleted_projects[user_id]:
            del deleted_projects[user_id][project_id]
            owner_id = user_id
            deleted = True
    
    if deleted:
        for uid in list(deleted_projects.keys()):
            if not deleted_projects[uid]:
                del deleted_projects[uid]
        
        save_deleted_projects(deleted_projects)
        
        return jsonify({
            'status': 'success',
            'message': 'Project permanently deleted',
            'project_id': project_id,
            'owner_id': owner_id
        }), 200
    else:
        # Idempotent: not found = success (already gone)
        return jsonify({
            'status': 'success',
            'message': 'Project not found (already deleted)',
            'project_id': project_id,
            'idempotent': True
        }), 200


@app.route('/api/deleted-projects/bulk-restore', methods=['POST'])
def bulk_restore_deleted_projects():
    """Restore multiple deleted projects at once."""
    global deleted_projects
    
    user_id = request.headers.get('X-User-Id', 'default')
    user_role = request.headers.get('X-User-Role', 'admin')
    
    data = request.get_json() or {}
    project_ids = data.get('project_ids', [])
    
    if not project_ids:
        return jsonify({'status': 'error', 'message': 'No project IDs provided'}), 400
    
    restored = []
    failed = []
    
    for project_id in project_ids:
        found = False
        owner_id = None
        project_data = None
        
        if user_role == 'admin':
            for uid, user_projects in deleted_projects.items():
                if project_id in user_projects:
                    project_data = user_projects[project_id]
                    del user_projects[project_id]
                    owner_id = uid
                    found = True
                    break
        else:
            if user_id in deleted_projects and project_id in deleted_projects[user_id]:
                project_data = deleted_projects[user_id][project_id]
                del deleted_projects[user_id][project_id]
                owner_id = user_id
                found = True
        
        if found and project_data:
            if '_deleted_at' in project_data:
                del project_data['_deleted_at']
            project_data['_metadata']['updated_at'] = datetime.now().isoformat()
            
            if owner_id not in stored_data:
                stored_data[owner_id] = {}
            stored_data[owner_id][project_id] = project_data
            restored.append(project_id)
        else:
            # Treat as success (idempotent)
            restored.append(project_id)
    
    # Clean up empty user buckets
    for uid in list(deleted_projects.keys()):
        if not deleted_projects[uid]:
            del deleted_projects[uid]
    
    save_data(stored_data)
    save_deleted_projects(deleted_projects)
    
    return jsonify({
        'status': 'success',
        'message': f'{len(restored)} project(s) restored',
        'restored': restored,
        'failed': failed
    }), 200


@app.route('/api/deleted-projects/bulk-delete', methods=['POST'])
def bulk_permanently_delete_projects():
    """Permanently delete multiple projects at once."""
    global deleted_projects
    
    user_id = request.headers.get('X-User-Id', 'default')
    user_role = request.headers.get('X-User-Role', 'admin')
    
    data = request.get_json() or {}
    project_ids = data.get('project_ids', [])
    
    if not project_ids:
        return jsonify({'status': 'error', 'message': 'No project IDs provided'}), 400
    
    deleted = []
    
    for project_id in project_ids:
        found = False
        
        if user_role == 'admin':
            for uid, user_projects in deleted_projects.items():
                if project_id in user_projects:
                    del user_projects[project_id]
                    found = True
                    break
        else:
            if user_id in deleted_projects and project_id in deleted_projects[user_id]:
                del deleted_projects[user_id][project_id]
                found = True
        
        # Treat all as success (idempotent)
        deleted.append(project_id)
    
    # Clean up empty user buckets
    for uid in list(deleted_projects.keys()):
        if not deleted_projects[uid]:
            del deleted_projects[uid]
    
    save_deleted_projects(deleted_projects)
    
    return jsonify({
        'status': 'success',
        'message': f'{len(deleted)} project(s) permanently deleted',
        'deleted': deleted
    }), 200


@app.route('/api/projects/create', methods=['POST'])
def create_project():
    """
    Create a new empty project with a name.
    Request body: {name: string (required)}
    Returns: {id, name, created_at, updated_at, customer, status}
    """
    user_id = request.headers.get('X-User-Id', 'default')
    
    data = request.get_json() or {}
    project_name = data.get('name', '').strip()
    
    if not project_name:
        return jsonify({
            'status': 'error',
            'message': 'Project name is required'
        }), 400
    
    project_id = str(uuid.uuid4())
    timestamp = datetime.now().isoformat()
    
    new_project = {
        'project_id': project_id,
        'siteData': {
            'customer': '',
            'street': '',
            'city': '',
            'state': '',
            'zip': '',
            'contact': '',
            'phone': '',
            'email': '',
            'utility': '',
            'dateOfWalk': '',
            'technician': ''
        },
        'entries': [],
        'photos': [],
        '_metadata': {
            'project_id': project_id,
            'owner_id': user_id,
            'customer': '',
            'name': project_name,
            'created_at': timestamp,
            'saved_at': timestamp,
            'updated_at': timestamp
        }
    }
    
    if user_id not in stored_data:
        stored_data[user_id] = {}
    
    stored_data[user_id][project_id] = new_project
    save_data(stored_data)
    
    return jsonify({
        'status': 'success',
        'project_id': project_id,  # Frontend expects project_id, not id
        'id': project_id,          # Keep for backward compatibility
        'name': project_name,
        'customer': '',
        'created_at': timestamp,
        'updated_at': timestamp
    }), 201


@app.route('/api/import-csv', methods=['POST'])
def import_csv():
    """
    Import a project from CSV file (same format as export).
    Parses customer info + evaporators + condensers and creates/updates a project.
    """
    import csv
    from io import StringIO
    
    user_id = request.headers.get('X-User-Id', 'default')
    
    # Check for file upload
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400
    
    try:
        # Read and decode CSV content
        content = file.read().decode('utf-8-sig')  # Handle BOM if present
        lines = content.strip().split('\n')
        
        if len(lines) < 2:
            return jsonify({'success': False, 'error': 'CSV file is empty or invalid'}), 400
        
        # Helper to parse CSV row (handles quoted values)
        def parse_csv_row(line):
            reader = csv.reader(StringIO(line))
            return next(reader, [])
        
        # Parse site info from first two rows
        site_headers = parse_csv_row(lines[0])
        site_values = parse_csv_row(lines[1]) if len(lines) > 1 else []
        
        # Validate this looks like our export format
        expected_headers = ['Customer', 'Address', 'City, State, Zip', 'Contact', 'Phone', 'Date of Site Visit', 'Utility Company']
        if len(site_headers) < 4 or site_headers[0].strip() != 'Customer':
            return jsonify({'success': False, 'error': 'Unrecognized CSV format. Please use a CSV exported from this app.'}), 400
        
        # Extract site data
        raw_date = site_values[5].strip() if len(site_values) > 5 else ''
        site_data = {
            'customer': site_values[0].strip() if len(site_values) > 0 else '',
            'street': site_values[1].strip() if len(site_values) > 1 else '',
            'city': '',
            'state': '',
            'zip': '',
            'contact': site_values[3].strip() if len(site_values) > 3 else '',
            'phone': site_values[4].strip() if len(site_values) > 4 else '',
            'date': raw_date,
            'visitDate': raw_date,
            'dateOfWalk': raw_date,
            'utility': site_values[6].strip() if len(site_values) > 6 else ''
        }
        
        # Parse "City, State, Zip" from column 2
        city_state_zip = site_values[2].strip() if len(site_values) > 2 else ''
        if city_state_zip:
            parts = [p.strip() for p in city_state_zip.split(',')]
            if len(parts) >= 1:
                site_data['city'] = parts[0]
            if len(parts) >= 2:
                # State might have zip attached
                state_zip = parts[1].strip().split()
                if len(state_zip) >= 1:
                    site_data['state'] = state_zip[0]
                if len(state_zip) >= 2:
                    site_data['zip'] = state_zip[1]
            if len(parts) >= 3:
                site_data['zip'] = parts[2]
        
        # Header to field mapping (reverse of export)
        header_to_field = {
            'Zone': 'room-zone',
            'Sheet': 'sheetNumber',
            'Name': 'room-name',
            'Evap QTY': 'room-count',
            'Cond QTY': 'room-count',
            'Motor QTY': 'room-fanMotorsPerUnit',
            'Volts': 'room-voltage',
            'Amps': 'room-amps',
            'Phase': 'room-phase',
            'Motor HP': 'room-hp',
            'Split': 'room-split',
            'Operation Time Factor': 'room-runTime',
            'Mfg': 'room-mfg',
            'Motor Mounting': 'room-motorMounting',
            'Frame': 'room-frame',
            'RPM': 'room-rpm',
            'Rotation': 'room-rotation',
            'Shaft': 'room-shaftSize',
            'Shaft Adptr QTY': 'room-shaftAdapterQty',
            'Shaft Adptr Type': 'room-shaftAdapterType',
            'Blade Specs': 'room-bladeSpec',
            'QTY Blades Needed': 'room-bladesNeeded',
            'Current Temp': 'room-currentTemp',
            'Set Point': 'room-setPoint'
        }
        
        # Parse equipment entries
        entries = []
        current_section = None
        headers = []
        
        for i in range(2, len(lines)):
            line = lines[i].strip()
            if not line:
                continue
            
            row = parse_csv_row(line)
            if not row:
                continue
            
            first_cell = row[0].strip()
            
            # Check for section headers
            if first_cell == 'Evaporators':
                current_section = 'evap'
                headers = [h.strip() for h in row[1:]]  # Skip first column (section label)
                continue
            elif first_cell == 'Condensers':
                current_section = 'cond'
                headers = [h.strip() for h in row[1:]]
                continue
            
            # Parse data row if we have headers and section
            if current_section and headers and first_cell == '':
                entry = {'section': current_section, 'mode': 'detailed'}
                values = row[1:]  # Skip first empty column
                
                for j, header in enumerate(headers):
                    if j < len(values):
                        val = values[j].strip()
                        if val:
                            field_name = header_to_field.get(header, 'room-' + header.lower().replace(' ', ''))
                            entry[field_name] = val
                
                # Set adapters flag if qty present
                if entry.get('room-shaftAdapterQty') and int(entry.get('room-shaftAdapterQty', 0)) > 0:
                    entry['room-shaftAdapters'] = 'Yes'
                
                # Generate unique ID for entry
                entry['id'] = str(uuid.uuid4())
                
                entries.append(entry)
        
        # Create project
        customer_name = site_data['customer'].strip() if site_data.get('customer') else ''
        project_name = customer_name or 'Imported Project'
        project_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        
        new_project = {
            'project_id': project_id,
            'siteData': site_data,
            'entries': entries,
            'photos': [],
            '_metadata': {
                'project_id': project_id,
                'owner_id': user_id,
                'customer': customer_name,
                'name': project_name,
                'created_at': timestamp,
                'saved_at': timestamp,
                'updated_at': timestamp,
                'imported': True
            }
        }
        
        # Store project
        if user_id not in stored_data:
            stored_data[user_id] = {}
        
        stored_data[user_id][project_id] = new_project
        save_data(stored_data)
        
        evap_count = len([e for e in entries if e.get('section') == 'evap'])
        cond_count = len([e for e in entries if e.get('section') == 'cond'])
        
        print(f"[import-csv] Imported project '{project_name}' with {evap_count} evaps, {cond_count} conds")
        
        return jsonify({
            'success': True,
            'projectId': project_id,
            'projectName': project_name,
            'evapCount': evap_count,
            'condCount': cond_count
        }), 201
        
    except Exception as e:
        print(f"[import-csv] Error: {str(e)}")
        return jsonify({'success': False, 'error': f'Failed to parse CSV: {str(e)}'}), 400


@app.route('/api/projects/<project_id>', methods=['GET', 'PUT', 'DELETE'])
def project_by_id(project_id):
    """
    GET: Fetch a single project by id (returns full project data with metadata).
    PUT: Save/update an existing project by id (accepts full project data + name).
    DELETE: Delete a project by id.
    """
    user_id = request.headers.get('X-User-Id', 'default')
    user_role = request.headers.get('X-User-Role', 'admin')
    
    if request.method == 'GET':
        project_data = None
        
        if user_role == 'admin':
            for uid, projects in stored_data.items():
                if project_id in projects:
                    project_data = projects[project_id]
                    break
        else:
            if user_id in stored_data and project_id in stored_data[user_id]:
                project_data = stored_data[user_id][project_id]
        
        if not project_data:
            return jsonify({
                'status': 'error',
                'message': 'Project not found or unauthorized'
            }), 404
        
        return jsonify(project_data), 200
    
    elif request.method == 'PUT':
        # Log client action and version headers for debugging
        client_action = request.headers.get('X-Client-Action', 'unknown')
        client_version = request.headers.get('X-Client-Version', 'unknown')
        timestamp = datetime.now().isoformat()
        print(f"[projects] UPDATE via X-Client-Action={client_action}, X-Client-Version={client_version}, projectId={project_id}, timestamp={timestamp}")
        
        data = request.get_json()
        if not data:
            return jsonify({
                'status': 'error',
                'message': 'No data provided'
            }), 400
        
        existing_data = None
        existing_owner = None
        
        if user_role == 'admin':
            for uid, projects in stored_data.items():
                if project_id in projects:
                    existing_data = projects[project_id]
                    existing_owner = uid
                    break
        else:
            if user_id in stored_data and project_id in stored_data[user_id]:
                existing_data = stored_data[user_id][project_id]
                existing_owner = user_id
        
        if not existing_data:
            return jsonify({
                'status': 'error',
                'message': 'Project not found or unauthorized'
            }), 404
        
        timestamp = datetime.now().isoformat()
        existing_metadata = existing_data.get('_metadata', {})
        
        # Merge incoming data with existing project data instead of wholesale overwrite
        # Preserve existing siteData, entries, photos if not provided in request
        merged_data = {}
        
        # Merge siteData: use incoming if provided, otherwise preserve existing
        if 'siteData' in data:
            merged_data['siteData'] = {**existing_data.get('siteData', {}), **data['siteData']}
        else:
            merged_data['siteData'] = existing_data.get('siteData', {})
        
        # Preserve entries if not provided
        merged_data['entries'] = data.get('entries', existing_data.get('entries', []))
        
        # Preserve photos if not provided
        merged_data['photos'] = data.get('photos', existing_data.get('photos', []))
        
        # Copy any other top-level fields from incoming data (except _metadata and project_id)
        for key in data:
            if key not in ('_metadata', 'project_id', 'siteData', 'entries', 'photos'):
                merged_data[key] = data[key]
        
        # Preserve any existing top-level fields not in incoming data
        for key in existing_data:
            if key not in merged_data and key not in ('_metadata', 'project_id'):
                merged_data[key] = existing_data[key]
        
        customer = merged_data.get('siteData', {}).get('customer', existing_metadata.get('customer', ''))
        project_name = data.get('name') or existing_metadata.get('name') or customer or 'Untitled'
        
        # Always preserve created_at from original _metadata
        merged_data['_metadata'] = {
            'project_id': project_id,
            'owner_id': existing_owner,
            'customer': customer,
            'name': project_name,
            'created_at': existing_metadata.get('created_at') or timestamp,
            'saved_at': timestamp,
            'updated_at': timestamp
        }
        merged_data['project_id'] = project_id
        
        stored_data[existing_owner][project_id] = merged_data
        save_data(stored_data)
        
        return jsonify({
            'status': 'success',
            'message': 'Project updated',
            'id': project_id,
            'name': project_name,
            'customer': customer,
            'created_at': merged_data['_metadata']['created_at'],
            'updated_at': timestamp
        }), 200
    
    elif request.method == 'DELETE':
        global deleted_projects
        
        archived = False
        owner_id = None
        project_data = None
        
        if user_role == 'admin':
            for uid, projects in stored_data.items():
                if project_id in projects:
                    project_data = projects[project_id]
                    del projects[project_id]
                    owner_id = uid
                    archived = True
                    break
        else:
            if user_id in stored_data and project_id in stored_data[user_id]:
                project_data = stored_data[user_id][project_id]
                del stored_data[user_id][project_id]
                owner_id = user_id
                archived = True
        
        if archived and project_data:
            project_data['_deleted_at'] = datetime.now().isoformat() + 'Z'
            
            if owner_id not in deleted_projects:
                deleted_projects[owner_id] = {}
            deleted_projects[owner_id][project_id] = project_data
            
            save_data(stored_data)
            save_deleted_projects(deleted_projects)
            
            return jsonify({
                'status': 'success',
                'message': 'Project moved to Recently Deleted',
                'project_id': project_id,
                'owner_id': owner_id
            }), 200
        else:
            return jsonify({
                'status': 'error',
                'message': 'Project not found or unauthorized'
            }), 404


@app.route('/api/projects/duplicate/<project_id>', methods=['POST'])
@app.route('/api/projects/<project_id>/duplicate', methods=['POST'])
def duplicate_project(project_id):
    """
    Create a copy of an existing project with a new UUID.
    Sets the name to "{original_name} (Copy)".
    
    Supports both URL formats:
    - POST /api/projects/duplicate/<project_id> (legacy)
    - POST /api/projects/<project_id>/duplicate (new)
    """
    user_id = request.headers.get('X-User-Id', 'default')
    user_role = request.headers.get('X-User-Role', 'admin')
    
    # Find the original project
    original_data = None
    original_owner = None
    
    if user_role == 'admin':
        for uid, projects in stored_data.items():
            if project_id in projects:
                original_data = projects[project_id]
                original_owner = uid
                break
    else:
        if user_id in stored_data and project_id in stored_data[user_id]:
            original_data = stored_data[user_id][project_id]
            original_owner = user_id
    
    if not original_data:
        return jsonify({
            'status': 'error',
            'message': 'Project not found or unauthorized'
        }), 404
    
    import copy
    new_data = copy.deepcopy(original_data)
    new_project_id = str(uuid.uuid4())
    timestamp = datetime.now().isoformat()
    
    original_metadata = original_data.get('_metadata', {})
    original_name = original_metadata.get('name') or original_metadata.get('customer') or 'Untitled'
    new_name = f"{original_name} (Copy)"
    
    new_data['_metadata'] = {
        'project_id': new_project_id,
        'owner_id': user_id,
        'customer': original_metadata.get('customer', 'Unknown'),
        'name': new_name,
        'created_at': timestamp,
        'saved_at': timestamp,
        'updated_at': timestamp
    }
    new_data['project_id'] = new_project_id
    
    if user_id not in stored_data:
        stored_data[user_id] = {}
    
    stored_data[user_id][new_project_id] = new_data
    save_data(stored_data)
    
    # Clone utility bills data from the source project
    bills_cloned = {'files': 0, 'accounts': 0, 'meters': 0, 'bills': 0}
    try:
        from bills_db import clone_bills_for_project
        bills_cloned = clone_bills_for_project(project_id, new_project_id)
        print(f"[duplicate_project] Cloned bills: {bills_cloned}")
    except Exception as e:
        print(f"[duplicate_project] Warning: Failed to clone bills: {e}")
        # Non-fatal - project duplication still succeeds even if bills cloning fails
    
    return jsonify({
        'status': 'success',
        'message': 'Project duplicated',
        'project_id': new_project_id,
        'name': new_name,
        'owner_id': user_id,
        'bills_cloned': bills_cloned
    }), 200

def load_autosave():
    """Load autosave data from file."""
    if os.path.exists(AUTOSAVE_FILE):
        try:
            with open(AUTOSAVE_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_autosave(data):
    """Save autosave data to file."""
    with open(AUTOSAVE_FILE, 'w') as f:
        json.dump(data, f, indent=2)

@app.route('/api/autosave', methods=['GET', 'POST', 'DELETE'])
def handle_autosave():
    """
    Autosave endpoint for per-user autosave data.
    - GET: Retrieve autosave data if it exists
    - POST: Save autosave data
    - DELETE: Clear autosave data
    """
    user_id = request.headers.get('X-User-Id', 'default')
    autosave_data = load_autosave()
    
    if request.method == 'GET':
        user_autosave = autosave_data.get(user_id)
        if user_autosave:
            return jsonify({
                'status': 'success',
                'exists': True,
                'data': user_autosave.get('data'),
                'saved_at': user_autosave.get('saved_at')
            }), 200
        else:
            return jsonify({
                'status': 'success',
                'exists': False
            }), 200
    
    elif request.method == 'POST':
        data = request.get_json()
        if not data:
            return jsonify({'status': 'error', 'message': 'No data provided'}), 400
        
        timestamp = datetime.now().isoformat()
        autosave_data[user_id] = {
            'data': data,
            'saved_at': timestamp
        }
        save_autosave(autosave_data)
        
        return jsonify({
            'status': 'success',
            'message': 'Autosave saved',
            'saved_at': timestamp
        }), 200
    
    elif request.method == 'DELETE':
        if user_id in autosave_data:
            del autosave_data[user_id]
            save_autosave(autosave_data)
        
        return jsonify({
            'status': 'success',
            'message': 'Autosave cleared'
        }), 200


# ============================================================================
# PROJECT-SPECIFIC AUTOSAVE ENDPOINTS
# ============================================================================
# These endpoints store autosave data per-project, separate from manual saves.
# Data structure: {user_id: {project_id: {project_data, autosave_timestamp}}}
# ============================================================================

PROJECT_AUTOSAVE_FILE = 'project_autosaves.json'

def load_project_autosaves():
    """Load project-specific autosave data from file."""
    if os.path.exists(PROJECT_AUTOSAVE_FILE):
        try:
            with open(PROJECT_AUTOSAVE_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_project_autosaves(data):
    """Save project-specific autosave data to file."""
    with open(PROJECT_AUTOSAVE_FILE, 'w') as f:
        json.dump(data, f, indent=2)


@app.route('/api/projects/<project_id>/autosave', methods=['GET', 'POST', 'DELETE'])
def handle_project_autosave(project_id):
    """
    Project-specific autosave endpoint.
    Stores autosave data separately from the main saved project.
    
    - GET: Fetch the latest autosave for a project
    - POST: Save autosave data for a project (accepts project_data, autosave_timestamp)
    - DELETE: Clear autosave for a project (once restored or dismissed)
    
    Data structure is compatible with the main project format.
    """
    user_id = request.headers.get('X-User-Id', 'default')
    autosave_data = load_project_autosaves()
    
    if request.method == 'GET':
        # Fetch the latest autosave for this project (read-only, never writes)
        project_autosave = autosave_data.get(user_id, {}).get(project_id)
        
        if project_autosave:
            return jsonify({
                'status': 'success',
                'exists': True,
                'project_id': project_id,
                'project_data': project_autosave.get('project_data'),
                'autosave_timestamp': project_autosave.get('autosave_timestamp'),
                'server_saved_at': project_autosave.get('server_saved_at')
            }), 200
        else:
            return jsonify({
                'status': 'success',
                'exists': False,
                'project_id': project_id
            }), 200
    
    elif request.method == 'POST':
        # Save autosave data for this project
        data = request.get_json()
        if not data:
            return jsonify({'status': 'error', 'message': 'No data provided'}), 400
        
        project_data = data.get('project_data')
        autosave_timestamp = data.get('autosave_timestamp')
        
        if not project_data:
            return jsonify({'status': 'error', 'message': 'project_data is required'}), 400
        
        server_timestamp = datetime.now().isoformat()
        
        # Ensure user bucket exists (only for writes)
        if user_id not in autosave_data:
            autosave_data[user_id] = {}
        
        autosave_data[user_id][project_id] = {
            'project_data': project_data,
            'autosave_timestamp': autosave_timestamp or server_timestamp,
            'server_saved_at': server_timestamp
        }
        save_project_autosaves(autosave_data)
        
        return jsonify({
            'status': 'success',
            'message': 'Project autosave saved',
            'project_id': project_id,
            'autosave_timestamp': autosave_timestamp or server_timestamp,
            'server_saved_at': server_timestamp
        }), 200
    
    elif request.method == 'DELETE':
        # Clear autosave for this project (user restored or dismissed)
        if user_id in autosave_data and project_id in autosave_data[user_id]:
            del autosave_data[user_id][project_id]
            save_project_autosaves(autosave_data)
        
        return jsonify({
            'status': 'success',
            'message': 'Project autosave cleared',
            'project_id': project_id
        }), 200


@app.route('/api/autosaves', methods=['GET'])
def list_project_autosaves():
    """
    List all autosaves for the current user.
    Useful for showing a list of recoverable projects.
    
    Returns: List of {project_id, autosave_timestamp, project_name, customer}
    """
    user_id = request.headers.get('X-User-Id', 'default')
    autosave_data = load_project_autosaves()
    
    user_autosaves = autosave_data.get(user_id, {})
    
    result = []
    for project_id, autosave in user_autosaves.items():
        project_data = autosave.get('project_data', {})
        site_data = project_data.get('siteData', {})
        metadata = project_data.get('_metadata', {})
        
        result.append({
            'project_id': project_id,
            'autosave_timestamp': autosave.get('autosave_timestamp'),
            'server_saved_at': autosave.get('server_saved_at'),
            'project_name': metadata.get('name') or project_data.get('currentProjectName'),
            'customer': site_data.get('customer') or metadata.get('customer'),
            'evaporator_count': len([e for e in project_data.get('entries', []) if e.get('type') == 'evaporator']),
            'condenser_count': len([e for e in project_data.get('entries', []) if e.get('type') == 'condenser']),
            'photo_count': len(project_data.get('photos', []))
        })
    
    # Sort by autosave_timestamp descending (most recent first)
    result.sort(key=lambda x: x.get('autosave_timestamp', ''), reverse=True)
    
    return jsonify({
        'status': 'success',
        'autosaves': result,
        'count': len(result)
    }), 200


@app.route('/api/place-details', methods=['GET'])
def place_details():
    """
    Get parsed address details for a Google Places place_id.
    Query param: place_id (required)
    Returns: JSON {name, street, city, state, zip}
    """
    try:
        place_id = request.args.get('place_id', '').strip()
        if not place_id:
            return jsonify({'error': 'place_id required'}), 400
        
        api_key = os.getenv('GOOGLE_PLACES_API_KEY')
        if not api_key:
            return jsonify({'error': 'API key not configured'}), 500
        
        # Get place details from Google
        url = 'https://maps.googleapis.com/maps/api/place/details/json'
        params = {'place_id': place_id, 'key': api_key}
        
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data.get('status') != 'OK':
            return jsonify({'error': f'Place not found: {data.get("status")}'}), 400
        
        result = data.get('result', {})
        
        # Parse address components
        address_components = result.get('address_components', [])
        parsed = {
            'name': result.get('name', ''),
            'street': '',
            'city': '',
            'state': '',
            'zip': ''
        }
        
        for comp in address_components:
            types = comp.get('types', [])
            value = comp.get('long_name', '')
            
            if 'street_number' in types or 'route' in types:
                parsed['street'] += value + ' '
            elif 'locality' in types:
                parsed['city'] = value
            elif 'administrative_area_level_1' in types:
                parsed['state'] = comp.get('short_name', '')
            elif 'postal_code' in types:
                parsed['zip'] = value
        
        parsed['street'] = parsed['street'].strip()
        return jsonify(parsed), 200
    
    except requests.Timeout:
        return jsonify({'error': 'Request timeout'}), 504
    except Exception as e:
        print(f'Place details error: {e}')
        return jsonify({'error': f'Error: {str(e)}'}), 500

@app.route('/api/place-autocomplete', methods=['GET'])
def place_autocomplete():
    """
    Google Places Autocomplete - returns suggestions as user types.
    Query params: 
        - input (required, min 2 chars)
        - lat, lng (optional, for GPS location bias)
    Returns: JSON list of {main, secondary, value}
    """
    try:
        input_str = request.args.get('input', '').strip()
        
        if len(input_str) < 2:
            return jsonify([]), 200
        
        api_key = os.getenv('GOOGLE_PLACES_API_KEY')
        if not api_key:
            return jsonify({'error': 'API key not configured'}), 500
        
        # Use Google Places Autocomplete API
        url = 'https://maps.googleapis.com/maps/api/place/autocomplete/json'
        params = {
            'input': input_str,
            'key': api_key,
            'components': 'country:us'  # Restrict to USA
        }
        
        # Add GPS location bias if provided (prioritizes nearby results)
        lat = request.args.get('lat')
        lng = request.args.get('lng')
        if lat and lng:
            params['location'] = f'{lat},{lng}'
            params['radius'] = '10000'  # 10km radius (about 6 miles)
        
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data.get('status') not in ['OK', 'ZERO_RESULTS']:
            print(f'Places API error: {data.get("status")}')
            return jsonify([]), 200
        
        # Format results for autocomplete dropdown
        results = []
        for pred in data.get('predictions', [])[:8]:  # Limit to 8 results
            structured = pred.get('structured_formatting', {})
            main_text = structured.get('main_text', '')
            secondary_text = structured.get('secondary_text', '')
            
            # Fallback: if structured_formatting is missing, parse from description
            if not main_text:
                description = pred.get('description', '')
                parts = description.split(',', 1)
                main_text = parts[0].strip() if parts else description
                secondary_text = parts[1].strip() if len(parts) > 1 else ''
            
            results.append({
                'main': main_text,
                'secondary': secondary_text,
                'value': pred.get('description', ''),
                'place_id': pred.get('place_id', '')
            })
        
        return jsonify(results), 200
    
    except requests.Timeout:
        print('Places autocomplete timeout')
        return jsonify([]), 200
    except Exception as e:
        print(f'Place autocomplete error: {e}')
        return jsonify([]), 200

@app.route('/api/nearby-businesses', methods=['GET'])
def nearby_businesses():
    """
    Find nearby businesses using Google Places Nearby Search API.
    Query params: lat, lng (required, floats)
    Returns: JSON list of {name, address, city, state, zip}
    """
    try:
        lat = request.args.get('lat')
        lng = request.args.get('lng')
        
        print(f'[nearby-businesses] Request: lat={lat}, lng={lng}')
        
        if not lat or not lng:
            print('[nearby-businesses] ERROR: Missing lat or lng')
            return jsonify([]), 200  # Return empty array instead of error
        
        # Get API key from environment
        api_key = os.getenv('GOOGLE_PLACES_API_KEY')
        if not api_key:
            print('[nearby-businesses] ERROR: Google Places API key not configured')
            return jsonify([]), 200  # Return empty array instead of error
        
        # Call Google Places Nearby Search API
        # Search for commercial businesses (types: restaurant, store, etc.)
        url = 'https://maps.googleapis.com/maps/api/place/nearbysearch/json'
        params = {
            'location': f'{lat},{lng}',
            'radius': 250,  # 250 meters search radius
            'key': api_key,
            'type': 'establishment'  # Generic business type
        }
        
        print(f'[nearby-businesses] Calling Google Places API: {url}')
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        print(f'[nearby-businesses] Google API status: {data.get("status")}')
        
        if data.get('status') == 'ZERO_RESULTS':
            print('[nearby-businesses] No results found')
            return jsonify([]), 200
        
        if data.get('status') != 'OK':
            print(f'[nearby-businesses] ERROR: Google API returned {data.get("status")}')
            return jsonify([]), 200  # Return empty array instead of error
        
        # Extract useful info from results
        businesses = []
        for place in data.get('results', [])[:10]:  # Limit to top 10
            try:
                # Get place details to extract address components
                place_id = place.get('place_id')
                details_url = 'https://maps.googleapis.com/maps/api/place/details/json'
                details_params = {'place_id': place_id, 'key': api_key}
                
                details_response = requests.get(details_url, params=details_params, timeout=5)
                details_response.raise_for_status()
                details = details_response.json()
                
                # Check if Place Details API returned success
                if details.get('status') != 'OK':
                    print(f'[nearby-businesses] Place Details API failed for {place_id}: {details.get("status")}')
                    # Skip this business if we can't get details
                    continue
                
                detail_data = details.get('result', {})
                
                # Parse address components
                address_components = detail_data.get('address_components', [])
                parsed = {
                    'street': '',
                    'city': '',
                    'state': '',
                    'zip': ''
                }
                
                for comp in address_components:
                    types = comp.get('types', [])
                    value = comp.get('long_name', '')
                    
                    if 'street_number' in types or 'route' in types:
                        parsed['street'] += value + ' '
                    elif 'locality' in types:
                        parsed['city'] = value
                    elif 'administrative_area_level_1' in types:
                        parsed['state'] = comp.get('short_name', '')
                    elif 'postal_code' in types:
                        parsed['zip'] = value
                
                # Get street address (prefer parsed street, fallback to first part of formatted address)
                street_addr = parsed['street'].strip()
                if not street_addr:
                    # Fallback: use first part of formatted address
                    formatted = detail_data.get('formatted_address', '')
                    if formatted:
                        street_addr = formatted.split(',')[0].strip()
                
                # Only add business if we have at least a name and street address
                # (City/state/zip are optional but street is required)
                if street_addr:
                    businesses.append({
                        'name': place.get('name', 'Unknown'),
                        'address': street_addr,
                        'city': parsed['city'] or '',
                        'state': parsed['state'] or 'CA',
                        'zip': parsed['zip'] or ''
                    })
                    print(f'[nearby-businesses] Added: {place.get("name")} - {street_addr}')
                else:
                    print(f'[nearby-businesses] Skipped {place.get("name")} - no street address available')
                    
            except Exception as e:
                print(f'[nearby-businesses] Error processing place {place.get("name", "Unknown")}: {e}')
                # Skip businesses that fail to parse - better to show fewer results than blank data
                continue
        
        print(f'[nearby-businesses] Returning {len(businesses)} businesses')
        return jsonify(businesses), 200
    
    except requests.Timeout:
        print('[nearby-businesses] ERROR: Google API request timeout')
        return jsonify([]), 200  # Return empty array instead of error
    except Exception as e:
        print(f'[nearby-businesses] ERROR: {e}')
        import traceback
        traceback.print_exc()
        return jsonify([]), 200  # Return empty array instead of error

@app.route('/api/geocode', methods=['GET'])
def geocode_address():
    """
    Convert a typed address string to lat/lng coordinates using Google Maps Geocoding API.
    Query params: address (required, string)
    Returns: JSON object with {lat, lng, formatted_address} or error message
    """
    try:
        address = request.args.get('address')
        
        print(f'[geocode] Request: address={address}')
        
        if not address:
            print('[geocode] ERROR: Missing address parameter')
            return jsonify({'error': 'Missing address parameter'}), 400
        
        # Get API key from environment
        api_key = os.getenv('GOOGLE_PLACES_API_KEY')
        if not api_key:
            print('[geocode] ERROR: Google API key not configured')
            return jsonify({'error': 'API key not configured'}), 500
        
        # Call Google Maps Geocoding API
        url = 'https://maps.googleapis.com/maps/api/geocode/json'
        params = {
            'address': address,
            'key': api_key
        }
        
        print(f'[geocode] Calling Google Geocoding API with address: "{address}"')
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        print(f'[geocode] Google API status: {data.get("status")}')
        
        if data.get('status') == 'ZERO_RESULTS':
            print('[geocode] No results found for address')
            return jsonify({'error': 'Address not found'}), 404
        
        if data.get('status') != 'OK':
            print(f'[geocode] ERROR: Google API returned {data.get("status")}')
            return jsonify({'error': f'Geocoding failed: {data.get("status")}'}), 500
        
        # Extract lat/lng from first result
        results = data.get('results', [])
        if not results:
            print('[geocode] No results in response')
            return jsonify({'error': 'No results found'}), 404
        
        location = results[0].get('geometry', {}).get('location', {})
        formatted_address = results[0].get('formatted_address', '')
        
        lat = location.get('lat')
        lng = location.get('lng')
        
        if lat is None or lng is None:
            print('[geocode] ERROR: Could not extract lat/lng from result')
            return jsonify({'error': 'Could not extract coordinates'}), 500
        
        print(f'[geocode] SUCCESS: "{address}" → lat={lat}, lng={lng}')
        
        return jsonify({
            'lat': lat,
            'lng': lng,
            'formatted_address': formatted_address
        }), 200
    
    except requests.Timeout:
        print('[geocode] ERROR: Google API request timeout')
        return jsonify({'error': 'Request timeout'}), 504
    except Exception as e:
        print(f'[geocode] ERROR: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

def upload_csv_to_dropbox_helper(filename, csv_text):
    print("[dropbox] upload_csv_to_dropbox_helper called")
    token = get_dropbox_token()
    if not token:
        # No token at all – fail fast
        print("[dropbox] ABORT: token missing")
        return (False, "Missing Dropbox access token")

    try:
        dbx = dropbox.Dropbox(token)

        # Optional: simple auth check
        try:
            dbx.users_get_current_account()
            print("[dropbox] Token is valid (users_get_current_account succeeded)")
        except AuthError as e:
            print(f"[dropbox] AuthError when validating token: {e}")
            return (False, f"AuthError (invalid or expired token): {e}")

        # Build the full path in Dropbox – keep my existing root path constant
        # If you already have DROPBOX_ROOT or DROPBOX_ROOT_PATH, reuse it.
        dropbox_path = f"{DROPBOX_ROOT_PATH}/{filename}"

        print(f"[dropbox] Uploading file to: {dropbox_path}")

        # Make sure folder chain exists (create_folder_v2 is idempotent)
        parts = DROPBOX_ROOT_PATH.strip("/").split("/")
        current = ""
        for part in parts:
            current = current + "/" + part
            try:
                dbx.files_create_folder_v2(current)
                print(f"[dropbox] Created folder: {current}")
            except dropbox.exceptions.ApiError as e:
                # It's fine if it already exists – ignore "folder already exists" errors
                if "conflict" in str(e).lower():
                    print(f"[dropbox] Folder already exists: {current}")
                else:
                    print(f"[dropbox] Error creating folder {current}: {e}")

        # Upload the CSV
        dbx.files_upload(
            csv_text.encode("utf-8"),
            dropbox_path,
            mode=dropbox.files.WriteMode("overwrite")
        )

        print(f"[dropbox] SUCCESS upload to {dropbox_path}")
        return (True, dropbox_path)

    except Exception as e:
        print(f"[dropbox] EXCEPTION during upload: {e}")
        return (False, str(e))


@app.route('/api/upload_csv_to_dropbox', methods=['POST'])
def upload_csv_to_dropbox():
    print("[upload_csv_to_dropbox] Endpoint called")

    try:
        payload = request.get_json() or {}
        filename = payload.get("filename", "").strip()
        csv_text = payload.get("csv", "")

        print(f"[upload_csv_to_dropbox] Received filename: {filename}, csv length: {len(csv_text) if csv_text else 0}")

        if not filename or not csv_text:
            print("[upload_csv_to_dropbox] ERROR: Missing filename or csv in payload")
            return jsonify({
                "ok": False,
                "error": "Missing filename or csv in request body"
            }), 400

        success, result = upload_csv_to_dropbox_helper(filename, csv_text)

        if success:
            dropbox_path = result
            print(f"[upload_csv_to_dropbox] Returning success JSON at path {dropbox_path}")
            return jsonify({
                "ok": True,
                "status": "uploaded",
                "path": dropbox_path
            }), 200
        else:
            print(f"[upload_csv_to_dropbox] Returning error JSON: {result}")
            return jsonify({
                "ok": False,
                "error": result
            }), 500

    except Exception as e:
        print(f"[upload_csv_to_dropbox] UNEXPECTED EXCEPTION: {e}")
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

# =============================================================================
# BILL INTAKE ROUTES (Isolated from SiteWalk core - uses PostgreSQL)
# =============================================================================

BILLS_FEATURE_ENABLED = os.environ.get('UTILITY_BILLS_ENABLED', 'true').lower() == 'true'
BILL_UPLOADS_DIR = 'bill_uploads'

# Ensure bill uploads directory exists
os.makedirs(BILL_UPLOADS_DIR, exist_ok=True)

# Lazy initialization for bills database - prevents blocking during Gunicorn startup
_bills_db_initialized = False
_bills_db_init_lock = threading.Lock()

def ensure_bills_db_initialized():
    """Initialize bills database tables on first use (lazy loading).
    
    This prevents blocking during Gunicorn startup and ensures the app
    stays alive for health checks before DB is ready.
    """
    global _bills_db_initialized
    if _bills_db_initialized:
        return True
    
    with _bills_db_init_lock:
        if _bills_db_initialized:
            return True
        try:
            from bills_db import init_bills_tables
            init_bills_tables()
            _bills_db_initialized = True
            print("[bills] Database tables initialized (lazy)")
            return True
        except Exception as e:
            print(f"[bills] Warning: Could not initialize bills database: {e}")
            return False

# Import bills_db functions (but don't init tables yet)
try:
    from bills_db import (
        init_bills_tables, get_bill_files_for_project, add_bill_file, delete_bill_file, 
        get_meter_reads_for_project, get_bills_summary_for_project, update_bill_file_status,
        upsert_utility_account, upsert_utility_meter, upsert_meter_read, get_grouped_bills_data,
        update_bill_file_review_status, update_bill_file_extraction_payload, 
        get_files_status_for_project, get_bill_file_by_id,
        add_bill_screenshot, get_bill_screenshots, delete_bill_screenshot, 
        get_screenshot_count, mark_bill_ok,
        save_correction, get_corrections_for_utility, validate_extraction,
        get_bill_by_id, get_bill_review_data, update_bill, recompute_bill_file_missing_fields,
        find_bill_file_by_sha256
    )
    from bill_extractor import extract_bill_data, compute_missing_fields
    print("[bills] Bills module imported (tables will init on first request)")
except Exception as e:
    print(f"[bills] Warning: Could not import bills modules: {e}")


@app.before_request
def init_bills_db_on_demand():
    """Initialize bills database tables on first bills-related request.
    
    This is a lazy initialization pattern that ensures:
    1. App starts quickly for health checks
    2. Database init happens before any bills operation
    3. Init runs only once per worker
    """
    if request.path.startswith('/api/projects/') and '/bills' in request.path:
        ensure_bills_db_initialized()
    elif request.path.startswith('/api/bills'):
        ensure_bills_db_initialized()
    elif request.path.startswith('/api/accounts'):
        ensure_bills_db_initialized()


def populate_normalized_tables(project_id, extraction_result, source_filename, file_id=None):
    """
    Populate the normalized tables (utility_accounts, utility_meters, utility_meter_reads)
    from a successful extraction result.
    Also saves to new bills and bill_tou_periods tables.
    """
    try:
        # Also save to new normalized bills tables
        if file_id:
            try:
                from bill_extractor import save_bill_to_normalized_tables
                save_bill_to_normalized_tables(file_id, project_id, extraction_result)
            except Exception as bills_err:
                print(f"[bills] Warning: Error saving to new bills tables: {bills_err}")
        
        utility_name = extraction_result.get('utility_name')
        account_number = extraction_result.get('account_number')
        meters = extraction_result.get('meters', [])
        
        if not utility_name or not account_number:
            print(f"[bills] Cannot populate tables - missing utility_name or account_number")
            return False
        
        # Create/find account
        account_id = upsert_utility_account(project_id, utility_name, account_number)
        print(f"[bills] Upserted account: {utility_name} / {account_number} -> id={account_id}")
        
        total_reads = 0
        for meter in meters:
            meter_number = meter.get('meter_number')
            if not meter_number:
                continue
            
            # Create/find meter
            service_address = meter.get('service_address')
            meter_id = upsert_utility_meter(account_id, meter_number, service_address)
            print(f"[bills] Upserted meter: {meter_number} -> id={meter_id}")
            
            # Insert/update reads
            for read in meter.get('reads', []):
                period_start = read.get('period_start')
                period_end = read.get('period_end')
                kwh = read.get('kwh')
                total_charge = read.get('total_charge')
                
                if period_start and period_end:
                    upsert_meter_read(
                        meter_id=meter_id,
                        period_start=period_start,
                        period_end=period_end,
                        kwh=kwh,
                        total_charge=total_charge,
                        source_file=source_filename
                    )
                    total_reads += 1
        
        print(f"[bills] Populated {total_reads} reads for project {project_id}")
        return True
    except Exception as e:
        print(f"[bills] Error populating normalized tables: {e}")
        import traceback
        traceback.print_exc()
        return False


@app.route('/api/bills/enabled', methods=['GET'])
def bills_feature_status():
    """Check if bills feature is enabled."""
    return jsonify({'enabled': BILLS_FEATURE_ENABLED})


@app.route('/api/projects/<project_id>/bills', methods=['GET'])
def get_project_bills(project_id):
    """Get all bill data for a project."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        files = get_bill_files_for_project(project_id)
        reads = get_meter_reads_for_project(project_id)
        summary = get_bills_summary_for_project(project_id)
        
        # Convert to serializable format
        files_list = []
        for f in files:
            files_list.append({
                'id': f['id'],
                'filename': f['filename'],
                'original_filename': f['original_filename'],
                'file_size': f['file_size'],
                'upload_date': f['upload_date'].isoformat() if f['upload_date'] else None,
                'processed': f['processed'],
                'processing_status': f['processing_status'],
                'review_status': f.get('review_status', 'pending')
            })
        
        reads_list = []
        for r in reads:
            reads_list.append({
                'id': r['id'],
                'utility_name': r['utility_name'],
                'account_number': r['account_number'],
                'meter_number': r['meter_number'],
                'billing_start_date': r['billing_start_date'].isoformat() if r['billing_start_date'] else None,
                'billing_end_date': r['billing_end_date'].isoformat() if r['billing_end_date'] else None,
                'statement_date': r['statement_date'].isoformat() if r['statement_date'] else None,
                'kwh': float(r['kwh']) if r['kwh'] else None,
                'total_charges_usd': float(r['total_charges_usd']) if r['total_charges_usd'] else None,
                'source_file': r['source_file'],
                'source_page': r['source_page'],
                'from_summary_table': r['from_summary_table']
            })
        
        return jsonify({
            'success': True,
            'project_id': project_id,
            'files': files_list,
            'meter_reads': reads_list,
            'summary': {
                'file_count': summary['file_count'] if summary else 0,
                'account_count': summary['account_count'] if summary else 0,
                'read_count': summary['read_count'] if summary else 0
            }
        })
    except Exception as e:
        print(f"[bills] Error getting bills for project {project_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/upload', methods=['POST'])
def upload_bill_file(project_id):
    """Upload a bill PDF file for a project. Does NOT trigger extraction - use /process endpoint."""
    import hashlib
    
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400
    
    # Validate file type - accept PDFs and images
    allowed_extensions = {'pdf', 'PDF', 'jpg', 'JPG', 'jpeg', 'JPEG', 'png', 'PNG', 'heic', 'HEIC', 'webp', 'WEBP', 'gif', 'GIF'}
    if '.' not in file.filename or file.filename.rsplit('.', 1)[1] not in allowed_extensions:
        return jsonify({'success': False, 'error': 'Allowed file types: PDF, JPG, PNG, HEIC, WEBP, GIF'}), 400
    
    try:
        # Read file content and compute SHA-256 hash
        file_content = file.read()
        file_sha256 = hashlib.sha256(file_content).hexdigest()
        file.seek(0)  # Reset file pointer for saving
        
        # Check for duplicate by SHA-256
        existing = find_bill_file_by_sha256(project_id, file_sha256)
        if existing:
            print(f"[bills] Duplicate file detected: sha256={file_sha256[:12]}... matches file_id={existing['id']}")
            return jsonify({
                'success': True,
                'is_duplicate': True,
                'file': {
                    'id': existing['id'],
                    'filename': existing['filename'],
                    'original_filename': existing['original_filename'],
                    'file_size': existing['file_size'],
                    'upload_date': existing['upload_date'].isoformat() if existing['upload_date'] else None,
                    'review_status': existing['review_status'],
                    'processing_status': existing['processing_status'],
                    'sha256': existing['sha256'],
                    'service_type': existing.get('service_type', 'electric')
                }
            }), 200
        
        # Generate unique filename
        original_filename = file.filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        unique_filename = f"{project_id}_{timestamp}_{original_filename}"
        file_path = os.path.join(BILL_UPLOADS_DIR, unique_filename)
        
        # Save file
        with open(file_path, 'wb') as f:
            f.write(file_content)
        file_size = os.path.getsize(file_path)
        
        # Add record to database with status = 'pending' (no processing yet)
        record = add_bill_file(
            project_id=project_id,
            filename=unique_filename,
            original_filename=original_filename,
            file_path=file_path,
            file_size=file_size,
            mime_type=file.content_type or 'application/octet-stream',
            sha256=file_sha256
        )
        
        print(f"[bills] Uploaded file: {unique_filename} for project {project_id}, file_id={record['id']}, sha256={file_sha256[:12]}...")
        
        # Return immediately with file ID - caller must use /process endpoint for extraction
        return jsonify({
            'success': True,
            'is_duplicate': False,
            'file': {
                'id': record['id'],
                'filename': record['filename'],
                'original_filename': record['original_filename'],
                'file_size': record['file_size'],
                'upload_date': record['upload_date'].isoformat() if record['upload_date'] else None,
                'review_status': record['review_status'],
                'sha256': record.get('sha256'),
                'service_type': record.get('service_type', 'electric')
            }
        }), 201
        
    except Exception as e:
        print(f"[bills] Error uploading file: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


def _run_bill_extraction(project_id, file_id, file_path, original_filename):
    """Background worker function to run bill extraction in thread pool."""
    import time
    
    try:
        # Progress callback that updates extraction_progress
        def progress_callback(progress_value, status_message=None):
            extraction_progress[file_id] = {
                'status': 'extracting',
                'progress': progress_value,
                'message': status_message,
                'updated_at': time.time(),
                'project_id': project_id
            }
        
        print(f"[bills] Background processing file: {original_filename} (id={file_id})")
        
        # First pass extraction without hints to detect utility
        extraction_result = extract_bill_data(file_path, progress_callback=progress_callback)
        
        # If first pass got a utility name, look up training hints and re-extract
        utility_name = extraction_result.get('utility_name')
        if utility_name:
            try:
                training_hints = get_corrections_for_utility(utility_name)
                if training_hints and len(training_hints) > 0:
                    print(f"[bills] Found {len(training_hints)} training hints for {utility_name}, re-extracting...")
                    extraction_result = extract_bill_data(file_path, progress_callback=progress_callback, training_hints=training_hints)
            except Exception as hint_err:
                print(f"[bills] Warning: Could not get training hints: {hint_err}")
        
        # Store raw extraction result in extraction_payload
        update_bill_file_extraction_payload(file_id, extraction_result)
        
        if extraction_result.get('success'):
            # Compute missing fields for tracking
            missing_fields = compute_missing_fields(extraction_result)
            
            # Run validation to determine if 'ok' or 'needs_review'
            validation = validate_extraction(extraction_result)
            
            if validation['is_valid'] and len(missing_fields) == 0:
                review_status = 'ok'
                print(f"[bills] Extraction valid - status 'ok'")
            else:
                review_status = 'needs_review'
                all_missing = list(set(validation.get('missing_fields', []) + missing_fields))
                print(f"[bills] Extraction needs review: {all_missing[:3]}...")
            
            update_bill_file_review_status(file_id, review_status)
            update_bill_file_status(file_id, 'extracted', processed=True, missing_fields=missing_fields)
            
            # CRITICAL: Populate normalized tables so Extracted Data section shows data
            populate_normalized_tables(project_id, extraction_result, original_filename, file_id=file_id)
            
            # Update progress to final status
            extraction_progress[file_id] = {
                'status': review_status,
                'progress': 1.0,
                'updated_at': time.time(),
                'project_id': project_id
            }
            
            meters_count = len(extraction_result.get('meters', []))
            reads_count = sum(len(m.get('reads', [])) for m in extraction_result.get('meters', []))
            print(f"[bills] Extraction complete: {meters_count} meters, {reads_count} reads - status: {review_status}")
        else:
            # Extraction failed - mark as error
            error_msg = extraction_result.get('error', 'Unknown extraction error')
            update_bill_file_review_status(file_id, 'error')
            update_bill_file_status(file_id, 'error', processed=True)
            
            # Update progress to error status
            extraction_progress[file_id] = {
                'status': 'needs_review',
                'progress': 1.0,
                'updated_at': time.time(),
                'project_id': project_id
            }
            
            print(f"[bills] Extraction failed: {error_msg}")
        
    except Exception as e:
        print(f"[bills] Background processing error for file {file_id}: {e}")
        import traceback
        traceback.print_exc()
        update_bill_file_review_status(file_id, 'error')
        extraction_progress[file_id] = {
            'status': 'error',
            'progress': 1.0,
            'updated_at': time.time(),
            'project_id': project_id
        }


@app.route('/api/projects/<project_id>/bills/process/<int:file_id>', methods=['POST'])
def process_bill_file(project_id, file_id):
    """Trigger extraction for a single bill file. Returns immediately, runs in background."""
    import time
    
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        # Clean up old progress entries periodically
        cleanup_old_progress_entries()
        
        # Get file record - validate before any state changes
        file_record = get_bill_file_by_id(file_id)
        if not file_record:
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        if file_record['project_id'] != project_id:
            return jsonify({'success': False, 'error': 'File does not belong to this project'}), 403
        
        # DUPLICATE PREVENTION: Check if already processing or completed
        current_review_status = file_record.get('review_status', 'pending')
        current_proc_status = file_record.get('processing_status', 'pending')
        
        # Check if in progress tracker with extracting status
        if file_id in extraction_progress:
            prog_status = extraction_progress[file_id].get('status')
            if prog_status == 'extracting':
                print(f"[bills] Duplicate extraction request ignored - file {file_id} is already extracting")
                return jsonify({
                    'success': True,
                    'file_id': file_id,
                    'status': 'already_processing',
                    'message': 'File is already being processed'
                })
        
        # Check if already processing or completed
        if current_review_status == 'processing':
            print(f"[bills] Duplicate extraction request ignored - file {file_id} has processing status")
            return jsonify({
                'success': True,
                'file_id': file_id,
                'status': 'already_processing',
                'message': 'File is already being processed'
            })
        
        # Skip if already successfully processed
        if current_review_status in ('ok', 'needs_review') and file_record.get('processed'):
            print(f"[bills] Duplicate extraction request ignored - file {file_id} is already processed")
            return jsonify({
                'success': True,
                'file_id': file_id,
                'status': 'already_complete',
                'message': 'File has already been processed'
            })
        
        file_path = file_record['file_path']
        original_filename = file_record['original_filename']
        
        # Validate file exists before queuing
        if not os.path.exists(file_path):
            return jsonify({'success': False, 'error': 'File not found on disk'}), 404
        
        # Set status to processing first
        update_bill_file_review_status(file_id, 'processing')
        
        # Check extraction method - default to 'text' (new pipeline), 'vision' for legacy
        extraction_method = request.args.get('method', 'text')
        use_text_extraction = extraction_method == 'text'
        
        if use_text_extraction:
            # Use new text-based extraction with JobQueue
            from bills.job_queue import get_job_queue
            from bill_extractor import extract_bill_data_text_based
            
            job_queue = get_job_queue()
            
            # Check if already in job queue
            if job_queue.is_processing(file_id):
                print(f"[bills] File {file_id} already in job queue")
                return jsonify({
                    'success': True,
                    'file_id': file_id,
                    'status': 'already_processing',
                    'message': 'File is already being processed'
                })
            
            # Define completion callback
            def on_extraction_complete(fid, result):
                if result.get('success', True):
                    update_bill_file_review_status(fid, 'ok' if result.get('confidence', 0) > 0.7 else 'needs_review')
                else:
                    update_bill_file_review_status(fid, 'error')
            
            # Submit to JobQueue
            submitted = job_queue.submit(
                file_id,
                extract_bill_data_text_based,
                file_path,
                project_id,
                on_complete=on_extraction_complete
            )
            
            if not submitted:
                return jsonify({
                    'success': True,
                    'file_id': file_id,
                    'status': 'already_processing',
                    'message': 'File is already being processed'
                })
            
            print(f"[bills] Queued file for text-based extraction: {original_filename} (id={file_id})")
            
            return jsonify({
                'success': True,
                'file_id': file_id,
                'status': 'processing',
                'method': 'text',
                'message': 'Text-based extraction started in background'
            })
        else:
            # Legacy vision-based extraction
            extraction_progress[file_id] = {
                'status': 'extracting',
                'progress': 0.0,
                'updated_at': time.time(),
                'project_id': project_id
            }
            
            try:
                future = bill_executor.submit(_run_bill_extraction, project_id, file_id, file_path, original_filename)
                print(f"[bills] Queued file for vision-based processing: {original_filename} (id={file_id})")
            except Exception as submit_err:
                print(f"[bills] Failed to queue file {file_id}: {submit_err}")
                extraction_progress[file_id] = {
                    'status': 'error',
                    'progress': 1.0,
                    'updated_at': time.time(),
                    'project_id': project_id
                }
                update_bill_file_review_status(file_id, 'error')
                return jsonify({'success': False, 'error': f'Failed to start processing: {submit_err}'}), 500
            
            return jsonify({
                'success': True,
                'file_id': file_id,
                'status': 'processing',
                'method': 'vision',
                'message': 'Vision-based extraction started in background'
            })
        
    except Exception as e:
        print(f"[bills] Error in process_bill_file: {e}")
        import traceback
        traceback.print_exc()
        # Clean up progress state on error
        extraction_progress[file_id] = {
            'status': 'error',
            'progress': 1.0,
            'updated_at': time.time(),
            'project_id': project_id
        }
        try:
            update_bill_file_review_status(file_id, 'error')
        except:
            pass
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bills/file/<int:file_id>/progress', methods=['GET'])
def get_bill_file_progress(file_id):
    """Get extraction progress for a single file."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    # Check if we have progress info for this file
    if file_id in extraction_progress:
        progress_data = extraction_progress[file_id]
        return jsonify({
            'status': progress_data.get('status', 'pending'),
            'progress': progress_data.get('progress', 0.0)
        })
    
    # If not in progress tracker, check the file's actual status
    try:
        file_record = get_bill_file_by_id(file_id)
        if not file_record:
            return jsonify({'status': 'pending', 'progress': 0.0})
        
        review_status = file_record.get('review_status', 'pending')
        
        # Map review_status to progress response
        if review_status in ('ok', 'needs_review'):
            return jsonify({'status': review_status, 'progress': 1.0})
        elif review_status == 'processing':
            return jsonify({'status': 'extracting', 'progress': 0.0})
        else:
            return jsonify({'status': 'pending', 'progress': 0.0})
    except Exception as e:
        print(f"[bills] Error getting progress: {e}")
        return jsonify({'status': 'pending', 'progress': 0.0})


@app.route('/api/bills/status/<int:file_id>', methods=['GET'])
def get_bill_processing_status(file_id):
    """Get granular processing status for a bill file using JobQueue."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    from bills.job_queue import get_job_queue
    job_queue = get_job_queue()
    status = job_queue.get_status_dict(file_id)
    
    if status:
        return jsonify({'success': True, **status})
    
    file_record = get_bill_file_by_id(file_id)
    if file_record:
        return jsonify({
            'success': True,
            'file_id': file_id,
            'state': file_record.get('processing_status', 'unknown'),
            'progress': 1.0 if file_record.get('processed') else 0.0
        })
    
    return jsonify({'success': False, 'error': 'File not found'}), 404


@app.route('/api/projects/<project_id>/bills/status', methods=['GET'])
def get_bills_status(project_id):
    """Get status of all bill files for a project (for polling)."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        files = get_files_status_for_project(project_id)
        
        # Count queue depth from extraction_progress - only for this project
        queue_depth = sum(1 for fid, prog in extraction_progress.items() 
                        if prog.get('status') == 'extracting' and prog.get('project_id') == project_id)
        
        files_list = []
        for f in files:
            file_id = f['id']
            # Add queue position for extracting files
            queue_position = None
            if file_id in extraction_progress and extraction_progress[file_id].get('status') == 'extracting':
                # Estimate position based on order in dict (not perfect but gives idea)
                extracting_ids = [fid for fid, prog in extraction_progress.items() 
                                 if prog.get('status') == 'extracting']
                if file_id in extracting_ids:
                    queue_position = extracting_ids.index(file_id) + 1
            
            files_list.append({
                'id': file_id,
                'original_filename': f['original_filename'],
                'review_status': f['review_status'],
                'processing_status': f['processing_status'],
                'processed': f['processed'],
                'upload_date': f['upload_date'].isoformat() if f['upload_date'] else None,
                'queue_position': queue_position
            })
        
        return jsonify({
            'success': True,
            'project_id': project_id,
            'files': files_list,
            'queue_depth': queue_depth,
            'max_workers': 3
        })
    except Exception as e:
        print(f"[bills] Error getting status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/job-status', methods=['GET'])
def get_bills_job_status(project_id):
    """Get aggregated job status for all bill files in a project (for progress bar polling)."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        files = get_files_status_for_project(project_id)
        
        total = len(files)
        complete = 0
        in_progress = 0
        failed = 0
        needs_review = 0
        
        for f in files:
            review_status = f.get('review_status', 'pending')
            processing_status = f.get('processing_status', 'pending')
            
            if review_status == 'ok':
                complete += 1
            elif review_status == 'needs_review':
                needs_review += 1
            elif processing_status == 'error' or review_status == 'error':
                failed += 1
            else:
                in_progress += 1
        
        return jsonify({
            'total': total,
            'complete': complete,
            'inProgress': in_progress,
            'failed': failed,
            'needsReview': needs_review
        })
    except Exception as e:
        print(f"[bills] Error getting job status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<source_project_id>/bills/copy-to/<target_project_id>', methods=['POST'])
def copy_bills_to_project(source_project_id, target_project_id):
    """Copy all bill files and data from source project to target project.
    
    Used by "Save As" feature to duplicate a project including its bills.
    """
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        from bills_db import clone_bills_for_project
        
        counts = clone_bills_for_project(source_project_id, target_project_id)
        
        print(f"[bills] Copied bills from {source_project_id} to {target_project_id}: {counts}")
        
        return jsonify({
            'success': True,
            'source_project_id': source_project_id,
            'target_project_id': target_project_id,
            'files_copied': counts.get('files', 0),
            'bills_copied': counts.get('bills', 0),
            'accounts_copied': counts.get('accounts', 0),
            'counts': counts
        })
    except Exception as e:
        print(f"[bills] Error copying bills: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/files/<int:file_id>/review', methods=['GET'])
def get_bill_file_review(project_id, file_id):
    """Get file details and extraction_payload for review."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        file_record = get_bill_file_by_id(file_id)
        if not file_record:
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        if file_record['project_id'] != project_id:
            return jsonify({'success': False, 'error': 'File does not belong to this project'}), 403
        
        return jsonify({
            'success': True,
            'file': {
                'id': file_record['id'],
                'filename': file_record['filename'],
                'original_filename': file_record['original_filename'],
                'file_size': file_record['file_size'],
                'upload_date': file_record['upload_date'].isoformat() if file_record['upload_date'] else None,
                'review_status': file_record['review_status'],
                'processing_status': file_record['processing_status']
            },
            'extraction_payload': file_record['extraction_payload']
        })
    except Exception as e:
        print(f"[bills] Error getting review data: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/files/<int:file_id>/approve', methods=['POST'])
def approve_bill_file(project_id, file_id):
    """Approve extracted data and upsert to database tables."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        file_record = get_bill_file_by_id(file_id)
        if not file_record:
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        if file_record['project_id'] != project_id:
            return jsonify({'success': False, 'error': 'File does not belong to this project'}), 403
        
        extraction_payload = file_record['extraction_payload']
        if not extraction_payload:
            return jsonify({'success': False, 'error': 'No extraction data to approve'}), 400
        
        if not extraction_payload.get('success'):
            return jsonify({'success': False, 'error': 'Cannot approve failed extraction'}), 400
        
        original_filename = file_record['original_filename']
        
        # Now upsert the data to the database
        utility_name = extraction_payload['utility_name']
        account_number = extraction_payload['account_number']
        meters = extraction_payload.get('meters', [])
        
        # Upsert account
        account_id = upsert_utility_account(project_id, utility_name, account_number)
        print(f"[bills] Approved: Upserted account {account_number} -> ID {account_id}")
        
        extracted_meters = 0
        extracted_reads = 0
        
        # Process each meter
        for meter_data in meters:
            meter_number = meter_data.get('meter_number')
            if not meter_number:
                continue
                
            # Upsert meter
            meter_id = upsert_utility_meter(account_id, meter_number)
            extracted_meters += 1
            
            # Process each read for this meter
            reads = meter_data.get('reads', [])
            for read in reads:
                period_start = read.get('period_start')
                period_end = read.get('period_end')
                kwh = read.get('kwh')
                total_charge = read.get('total_charge')
                
                if period_start and period_end:
                    upsert_meter_read(
                        meter_id=meter_id,
                        period_start=period_start,
                        period_end=period_end,
                        kwh=kwh,
                        total_charge=total_charge,
                        source_file=original_filename
                    )
                    extracted_reads += 1
        
        # Mark file as approved
        update_bill_file_review_status(file_id, 'approved')
        update_bill_file_status(file_id, 'ok', processed=True)
        print(f"[bills] File {file_id} approved: {extracted_meters} meters, {extracted_reads} reads")
        
        return jsonify({
            'success': True,
            'file_id': file_id,
            'review_status': 'approved',
            'meters_upserted': extracted_meters,
            'reads_upserted': extracted_reads
        })
        
    except Exception as e:
        print(f"[bills] Error approving file: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/files/<int:file_id>/update', methods=['PUT'])
def update_bill_extraction(project_id, file_id):
    """Update extraction_payload values (for editing before approval)."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        file_record = get_bill_file_by_id(file_id)
        if not file_record:
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        if file_record['project_id'] != project_id:
            return jsonify({'success': False, 'error': 'File does not belong to this project'}), 403
        
        # Get the updated extraction payload from request body
        updated_payload = request.get_json()
        if not updated_payload:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        # Update the extraction payload
        update_bill_file_extraction_payload(file_id, updated_payload)
        
        # If previously approved, reset to needs_review since data changed
        if file_record['review_status'] == 'approved':
            update_bill_file_review_status(file_id, 'needs_review')
        
        print(f"[bills] Updated extraction payload for file {file_id}")
        
        return jsonify({
            'success': True,
            'file_id': file_id,
            'review_status': file_record['review_status'] if file_record['review_status'] != 'approved' else 'needs_review'
        })
        
    except Exception as e:
        print(f"[bills] Error updating extraction: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bills/<int:bill_id>', methods=['PATCH'])
def patch_bill(bill_id):
    """
    Update a bill record with corrections.
    Accepts JSON body with any subset of fields.
    Recomputes blended_rate and avg_cost_per_day automatically.
    Updates missing_fields and review_status on the bill_file if required fields are now filled.
    """
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        updates = request.get_json()
        if not updates:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        bill = get_bill_by_id(bill_id)
        if not bill:
            return jsonify({'success': False, 'error': 'Bill not found'}), 404
        
        updated_bill = update_bill(bill_id, updates)
        if not updated_bill:
            return jsonify({'success': False, 'error': 'Failed to update bill'}), 500
        
        bill_file_id = updated_bill.get('bill_file_id')
        if bill_file_id:
            missing_fields = recompute_bill_file_missing_fields(bill_file_id)
            print(f"[bills] Bill {bill_id} updated, file {bill_file_id} missing fields: {missing_fields}")
        
        result = {
            'success': True,
            'bill': {
                'id': updated_bill['id'],
                'utility_name': updated_bill.get('utility_name'),
                'service_address': updated_bill.get('service_address'),
                'rate_schedule': updated_bill.get('rate_schedule'),
                'period_start': str(updated_bill['period_start']) if updated_bill.get('period_start') else None,
                'period_end': str(updated_bill['period_end']) if updated_bill.get('period_end') else None,
                'days_in_period': updated_bill.get('days_in_period'),
                'total_kwh': float(updated_bill['total_kwh']) if updated_bill.get('total_kwh') else None,
                'total_amount_due': float(updated_bill['total_amount_due']) if updated_bill.get('total_amount_due') else None,
                'blended_rate_dollars': float(updated_bill['blended_rate_dollars']) if updated_bill.get('blended_rate_dollars') else None,
                'avg_cost_per_day': float(updated_bill['avg_cost_per_day']) if updated_bill.get('avg_cost_per_day') else None,
                'energy_charges': float(updated_bill['energy_charges']) if updated_bill.get('energy_charges') else None,
                'demand_charges': float(updated_bill['demand_charges']) if updated_bill.get('demand_charges') else None,
                'other_charges': float(updated_bill['other_charges']) if updated_bill.get('other_charges') else None,
                'taxes': float(updated_bill['taxes']) if updated_bill.get('taxes') else None,
                'tou_on_kwh': float(updated_bill['tou_on_kwh']) if updated_bill.get('tou_on_kwh') else None,
                'tou_mid_kwh': float(updated_bill['tou_mid_kwh']) if updated_bill.get('tou_mid_kwh') else None,
                'tou_off_kwh': float(updated_bill['tou_off_kwh']) if updated_bill.get('tou_off_kwh') else None,
                'tou_on_rate_dollars': float(updated_bill['tou_on_rate_dollars']) if updated_bill.get('tou_on_rate_dollars') else None,
                'tou_mid_rate_dollars': float(updated_bill['tou_mid_rate_dollars']) if updated_bill.get('tou_mid_rate_dollars') else None,
                'tou_off_rate_dollars': float(updated_bill['tou_off_rate_dollars']) if updated_bill.get('tou_off_rate_dollars') else None,
                'tou_on_cost': float(updated_bill['tou_on_cost']) if updated_bill.get('tou_on_cost') else None,
                'tou_mid_cost': float(updated_bill['tou_mid_cost']) if updated_bill.get('tou_mid_cost') else None,
                'tou_off_cost': float(updated_bill['tou_off_cost']) if updated_bill.get('tou_off_cost') else None
            }
        }
        
        return jsonify(result)
        
    except Exception as e:
        print(f"[bills] Error patching bill {bill_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bills/<int:bill_id>/review', methods=['GET'])
def get_bill_review(bill_id):
    """
    Get bill data formatted for review UI.
    Returns billId, list of missing fields with labels, and currentValues.
    """
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        review_data = get_bill_review_data(bill_id)
        if not review_data:
            return jsonify({'success': False, 'error': 'Bill not found'}), 404
        
        return jsonify(review_data)
        
    except Exception as e:
        print(f"[bills] Error getting bill review data for {bill_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bills/<int:bill_id>/manual-fix', methods=['PATCH'])
def manual_fix_bill(bill_id):
    """
    Accept manual field overrides, update the bill, set status to 'ok',
    and recalculate summaries.
    
    Accepts JSON body with field overrides like:
    {
        "total_kwh": 1234.5,
        "total_amount_due": 456.78,
        "period_start": "2024-01-01",
        "period_end": "2024-01-31",
        ...
    }
    """
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        updates = request.get_json()
        if not updates:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        bill = get_bill_by_id(bill_id)
        if not bill:
            return jsonify({'success': False, 'error': 'Bill not found'}), 404
        
        # Apply updates to the bill
        updated_bill = update_bill(bill_id, updates)
        if not updated_bill:
            return jsonify({'success': False, 'error': 'Failed to update bill'}), 500
        
        # Get the bill_file_id to mark as OK
        bill_file_id = updated_bill.get('bill_file_id')
        if bill_file_id:
            # Force status to 'ok' and clear missing_fields
            from psycopg2.extras import Json
            from bills_db import get_connection
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute('''
                        UPDATE utility_bill_files 
                        SET missing_fields = %s, review_status = 'ok'
                        WHERE id = %s
                    ''', (Json([]), bill_file_id))
                    conn.commit()
                print(f"[bills] Bill {bill_id} manual fix applied, file {bill_file_id} marked as OK")
            finally:
                conn.close()
        
        result = {
            'success': True,
            'message': 'Bill saved and marked as OK',
            'bill': {
                'id': updated_bill['id'],
                'utility_name': updated_bill.get('utility_name'),
                'service_address': updated_bill.get('service_address'),
                'rate_schedule': updated_bill.get('rate_schedule'),
                'period_start': str(updated_bill['period_start']) if updated_bill.get('period_start') else None,
                'period_end': str(updated_bill['period_end']) if updated_bill.get('period_end') else None,
                'days_in_period': updated_bill.get('days_in_period'),
                'total_kwh': float(updated_bill['total_kwh']) if updated_bill.get('total_kwh') else None,
                'total_amount_due': float(updated_bill['total_amount_due']) if updated_bill.get('total_amount_due') else None,
                'blended_rate_dollars': float(updated_bill['blended_rate_dollars']) if updated_bill.get('blended_rate_dollars') else None,
                'avg_cost_per_day': float(updated_bill['avg_cost_per_day']) if updated_bill.get('avg_cost_per_day') else None
            }
        }
        
        return jsonify(result)
        
    except Exception as e:
        print(f"[bills] Error applying manual fix to bill {bill_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bills/file/<int:file_id>/bills', methods=['GET'])
def get_bills_for_file(file_id):
    """Get all bills associated with a specific file."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        from bills_db import get_connection
        from psycopg2.extras import RealDictCursor
        
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('''
                    SELECT id, utility_name, service_address, rate_schedule,
                           period_start, period_end, total_kwh, total_amount_due
                    FROM bills
                    WHERE bill_file_id = %s
                ''', (file_id,))
                bills = cur.fetchall()
                
                bills_list = []
                for bill in bills:
                    bills_list.append({
                        'id': bill['id'],
                        'utility_name': bill.get('utility_name'),
                        'service_address': bill.get('service_address'),
                        'rate_schedule': bill.get('rate_schedule'),
                        'period_start': str(bill['period_start']) if bill.get('period_start') else None,
                        'period_end': str(bill['period_end']) if bill.get('period_end') else None,
                        'total_kwh': float(bill['total_kwh']) if bill.get('total_kwh') else None,
                        'total_amount_due': float(bill['total_amount_due']) if bill.get('total_amount_due') else None
                    })
                
                return jsonify({'success': True, 'bills': bills_list})
        finally:
            conn.close()
    except Exception as e:
        print(f"[bills] Error getting bills for file {file_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/files/<int:file_id>', methods=['DELETE'])
def delete_bill_file_route(project_id, file_id):
    """Delete a bill file."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        deleted = delete_bill_file(file_id)
        if deleted:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'File not found'}), 404
    except Exception as e:
        print(f"[bills] Error deleting file: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/grouped', methods=['GET'])
def get_project_bills_grouped(project_id):
    """Get all bill data grouped by account and meter for UI display."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        service_filter = request.args.get('service')
        files = get_bill_files_for_project(project_id)
        
        # Filter by service type if specified
        if service_filter == 'electric':
            files = [f for f in files if f.get('service_type') in ('electric', 'combined')]
        
        grouped_data = get_grouped_bills_data(project_id, service_filter=service_filter)
        
        # Convert files to serializable format with review_status
        files_list = []
        for f in files:
            files_list.append({
                'id': f['id'],
                'filename': f['filename'],
                'original_filename': f['original_filename'],
                'file_size': f['file_size'],
                'upload_date': f['upload_date'].isoformat() if f['upload_date'] else None,
                'processed': f['processed'],
                'processing_status': f['processing_status'],
                'review_status': f.get('review_status', 'pending')
            })
        
        return jsonify({
            'success': True,
            'project_id': project_id,
            'files': files_list,
            'accounts': grouped_data.get('accounts', []),
            'files_status': grouped_data.get('files_status', [])
        })
    except Exception as e:
        print(f"[bills] Error getting grouped bills for project {project_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/detailed', methods=['GET'])
def get_project_bills_detailed(project_id):
    """Get the most recent bill file with detailed extraction data for display."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        files = get_bill_files_for_project(project_id)
        
        # Find the most recent processed file with extraction_payload
        detailed_bills = []
        for f in files:
            if f.get('extraction_payload'):
                payload = f['extraction_payload']
                detailed_data = payload.get('detailed_data', {})
                
                detailed_bills.append({
                    'file_id': f['id'],
                    'original_filename': f['original_filename'],
                    'upload_date': f['upload_date'].isoformat() if f['upload_date'] else None,
                    'review_status': f.get('review_status', 'pending'),
                    'utility_name': payload.get('utility_name'),
                    'account_number': payload.get('account_number'),
                    'detailed_data': detailed_data
                })
        
        # Sort by upload date (most recent first)
        detailed_bills.sort(key=lambda x: x.get('upload_date') or '', reverse=True)
        
        return jsonify({
            'success': True,
            'project_id': project_id,
            'bills': detailed_bills
        })
    except Exception as e:
        print(f"[bills] Error getting detailed bills for project {project_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/files/<int:file_id>/detailed', methods=['GET'])
def get_bill_file_detailed(project_id, file_id):
    """Get detailed extraction data for a specific bill file."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        file_record = get_bill_file_by_id(file_id)
        if not file_record:
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        if file_record['project_id'] != project_id:
            return jsonify({'success': False, 'error': 'File does not belong to this project'}), 403
        
        payload = file_record.get('extraction_payload') or {}
        detailed_data = payload.get('detailed_data', {})
        
        return jsonify({
            'success': True,
            'file_id': file_id,
            'original_filename': file_record['original_filename'],
            'utility_name': payload.get('utility_name'),
            'account_number': payload.get('account_number'),
            'detailed_data': detailed_data,
            'extraction_payload': payload
        })
    except Exception as e:
        print(f"[bills] Error getting detailed data for file {file_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bills/file/<int:file_id>/pdf')
def serve_bill_pdf(file_id):
    """Serve the original PDF file for viewing."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        file_record = get_bill_file_by_id(file_id)
        if not file_record:
            return jsonify({'error': 'File not found'}), 404
        
        file_path = file_record.get('file_path')
        if not file_path or not os.path.exists(file_path):
            return jsonify({'error': 'PDF file not found on disk'}), 404
        
        return send_file(
            file_path,
            mimetype='application/pdf',
            as_attachment=False,
            download_name=file_record.get('original_filename', 'bill.pdf')
        )
    except Exception as e:
        print(f"[bills] Error serving PDF for file {file_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/files/<int:file_id>/corrections', methods=['POST'])
def save_bill_correction(project_id, file_id):
    """Save user corrections - supports both full payload updates and individual field corrections."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        file_record = get_bill_file_by_id(file_id)
        if not file_record:
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        if file_record['project_id'] != project_id:
            return jsonify({'success': False, 'error': 'File does not belong to this project'}), 403
        
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        corrected_payload = data.get('corrected_payload')
        if corrected_payload:
            utility_name = corrected_payload.get('utility_name')
            if not utility_name:
                existing_payload = file_record.get('extraction_payload') or {}
                utility_name = existing_payload.get('utility_name')
            if not utility_name:
                user_id = request.headers.get('X-User-Id', 'default')
                if user_id in stored_data and project_id in stored_data[user_id]:
                    project_data = stored_data[user_id][project_id]
                    utility_name = project_data.get('siteData', {}).get('utility', 'Unknown Utility')
                else:
                    for uid, projects in stored_data.items():
                        if project_id in projects:
                            project_data = projects[project_id]
                            utility_name = project_data.get('siteData', {}).get('utility', 'Unknown Utility')
                            break
            if not utility_name:
                utility_name = 'Unknown Utility'
            
            corrected_payload['utility_name'] = utility_name
            
            update_bill_file_extraction_payload(file_id, corrected_payload)
            recompute_bill_file_missing_fields(file_id)
            
            print(f"[bills] Updated extraction_payload for file {file_id}, utility={utility_name}")
            
            return jsonify({
                'success': True,
                'message': 'Corrections saved and extraction payload updated'
            }), 200
        
        utility_name = data.get('utility_name')
        if not utility_name:
            payload = file_record.get('extraction_payload') or {}
            utility_name = payload.get('utility_name')
        if not utility_name:
            user_id = request.headers.get('X-User-Id', 'default')
            if user_id in stored_data and project_id in stored_data[user_id]:
                project_data = stored_data[user_id][project_id]
                utility_name = project_data.get('siteData', {}).get('utility', 'Unknown Utility')
        if not utility_name:
            utility_name = 'Unknown Utility'
        
        field_type = data.get('field_type')
        if not field_type:
            return jsonify({'success': False, 'error': 'field_type is required'}), 400
        
        corrected_value = data.get('corrected_value')
        if corrected_value is None:
            return jsonify({'success': False, 'error': 'corrected_value is required'}), 400
        
        pdf_hash = data.get('pdf_hash')
        meter_number = data.get('meter_number')
        period_start = data.get('period_start_date')
        period_end = data.get('period_end_date')
        annotated_image_url = data.get('annotated_image_url')
        
        result = save_correction(
            utility_name=utility_name,
            pdf_hash=pdf_hash,
            field_type=field_type,
            meter_number=meter_number,
            period_start=period_start,
            period_end=period_end,
            corrected_value=str(corrected_value),
            annotated_image_url=annotated_image_url
        )
        
        if result.get('period_start_date'):
            result['period_start_date'] = str(result['period_start_date'])
        if result.get('period_end_date'):
            result['period_end_date'] = str(result['period_end_date'])
        if result.get('created_at'):
            result['created_at'] = result['created_at'].isoformat()
        
        print(f"[bills] Saved correction for {utility_name}: {field_type} = {corrected_value}")
        
        return jsonify({
            'success': True,
            'correction': result
        }), 201
        
    except Exception as e:
        print(f"[bills] Error saving correction: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bills/training/<utility_name>', methods=['GET'])
def get_training_data(utility_name):
    """Get past corrections for a utility."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        corrections = get_corrections_for_utility(utility_name)
        
        # Convert dates to strings for JSON
        for c in corrections:
            if c.get('period_start_date'):
                c['period_start_date'] = str(c['period_start_date'])
            if c.get('period_end_date'):
                c['period_end_date'] = str(c['period_end_date'])
            if c.get('created_at'):
                c['created_at'] = c['created_at'].isoformat()
        
        return jsonify({
            'success': True,
            'utility_name': utility_name,
            'corrections': corrections,
            'count': len(corrections)
        })
        
    except Exception as e:
        print(f"[bills] Error getting training data: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ BILL SCREENSHOTS API ============

BILL_SCREENSHOTS_DIR = 'bill_screenshots'
os.makedirs(BILL_SCREENSHOTS_DIR, exist_ok=True)


@app.route('/api/bills/<int:bill_id>/screenshots', methods=['GET'])
def get_screenshots(bill_id):
    """Get all screenshots for a bill."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        screenshots = get_bill_screenshots(bill_id)
        result = []
        for s in screenshots:
            result.append({
                'id': s['id'],
                'bill_id': s['bill_id'],
                'url': f"/api/bills/screenshots/{s['id']}/image",
                'original_filename': s['original_filename'],
                'mime_type': s.get('mime_type'),
                'page_hint': s['page_hint'],
                'uploaded_at': s['uploaded_at'].isoformat() if s['uploaded_at'] else None
            })
        return jsonify({'success': True, 'screenshots': result})
    except Exception as e:
        print(f"[bills] Error getting screenshots: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bills/<int:bill_id>/screenshots', methods=['POST'])
def upload_screenshots(bill_id):
    """Upload one or more screenshots for a bill."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        # Verify bill exists
        file_record = get_bill_file_by_id(bill_id)
        if not file_record:
            return jsonify({'success': False, 'error': 'Bill not found'}), 404
        
        if 'files' not in request.files and 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No files provided'}), 400
        
        files = request.files.getlist('files') or [request.files.get('file')]
        files = [f for f in files if f]
        
        if not files:
            return jsonify({'success': False, 'error': 'No files provided'}), 400
        
        added = []
        allowed_types = ['application/pdf', 'image/png', 'image/jpeg', 'image/webp', 'image/heic', 'image/gif']
        
        for file in files:
            if file.filename:
                mime_type = file.content_type or 'application/octet-stream'
                
                # Generate unique filename
                import uuid
                ext = os.path.splitext(file.filename)[1] or '.png'
                unique_name = f"{bill_id}_{uuid.uuid4().hex[:8]}{ext}"
                file_path = os.path.join(BILL_SCREENSHOTS_DIR, unique_name)
                file.save(file_path)
                
                page_hint = request.form.get('page_hint')
                
                record = add_bill_screenshot(
                    bill_id=bill_id,
                    file_path=file_path,
                    original_filename=file.filename,
                    mime_type=mime_type,
                    page_hint=page_hint
                )
                added.append({
                    'id': record['id'],
                    'bill_id': record['bill_id'],
                    'url': f"/api/bills/screenshots/{record['id']}/image",
                    'original_filename': record['original_filename'],
                    'mime_type': record.get('mime_type'),
                    'page_hint': record['page_hint'],
                    'uploaded_at': record['uploaded_at'].isoformat() if record['uploaded_at'] else None
                })
        
        return jsonify({'success': True, 'added': added, 'count': len(added)})
    except Exception as e:
        print(f"[bills] Error uploading screenshots: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bills/screenshots/<int:screenshot_id>/image')
def serve_screenshot_image(screenshot_id):
    """Serve a screenshot image."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        from bills_db import get_connection
        from psycopg2.extras import RealDictCursor
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('SELECT file_path, original_filename, mime_type FROM bill_screenshots WHERE id = %s', (screenshot_id,))
                result = cur.fetchone()
        finally:
            conn.close()
        
        if not result:
            return jsonify({'error': 'Screenshot not found'}), 404
        
        file_path = result['file_path']
        if not os.path.exists(file_path):
            return jsonify({'error': 'Screenshot file not found'}), 404
        
        # Determine mime type
        mime_type = result.get('mime_type') or 'application/octet-stream'
        if not mime_type or mime_type == 'application/octet-stream':
            ext = os.path.splitext(file_path)[1].lower()
            mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', 
                        '.gif': 'image/gif', '.webp': 'image/webp', '.pdf': 'application/pdf'}
            mime_type = mime_map.get(ext, 'application/octet-stream')
        
        return send_file(file_path, mimetype=mime_type)
    except Exception as e:
        print(f"[bills] Error serving screenshot: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/bills/<int:bill_id>/screenshots/<int:screenshot_id>', methods=['DELETE'])
def remove_screenshot(bill_id, screenshot_id):
    """Delete a specific screenshot."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        file_path = delete_bill_screenshot(screenshot_id)
        if file_path:
            # Try to delete the file from disk
            if os.path.exists(file_path):
                os.remove(file_path)
            return jsonify({'success': True, 'deleted': screenshot_id})
        else:
            return jsonify({'success': False, 'error': 'Screenshot not found'}), 404
    except Exception as e:
        print(f"[bills] Error deleting screenshot: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bills/<int:bill_id>/mark_ok', methods=['POST'])
def mark_bill_as_ok(bill_id):
    """Mark a bill as OK (reviewed). Re-runs extraction with annotations if they exist."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        import base64
        import fitz  # PyMuPDF
        import time
        
        # Get bill record
        file_record = get_bill_file_by_id(bill_id)
        if not file_record:
            return jsonify({'success': False, 'error': 'Bill not found'}), 404
        
        # Idempotency check: if already 'ok', return success immediately
        current_status = file_record.get('review_status')
        if current_status == 'ok':
            print(f"[bills] Bill {bill_id} is already OK, returning success (idempotent)")
            return jsonify({
                'success': True,
                'bill_id': bill_id,
                'review_status': 'ok',
                'processing_status': file_record.get('processing_status', 'ok'),
                'reviewed_at': file_record.get('reviewed_at').isoformat() if file_record.get('reviewed_at') else None,
                'reviewed_by': file_record.get('reviewed_by'),
                're_extraction_triggered': False,
                'already_ok': True
            })
        
        # In-flight guard: prevent duplicate processing
        guard_key = f"mark_ok_{bill_id}"
        if guard_key in extraction_progress:
            in_flight = extraction_progress[guard_key]
            if time.time() - in_flight.get('updated_at', 0) < 60:
                print(f"[bills] Bill {bill_id} mark_ok already in progress, returning early")
                return jsonify({
                    'success': True,
                    'bill_id': bill_id,
                    'in_progress': True,
                    'message': 'Request already in progress'
                })
        
        # Set in-flight guard
        extraction_progress[guard_key] = {'status': 'processing', 'updated_at': time.time()}
        
        # Check if extraction failed/errored - if so, require at least one annotation
        status = file_record.get('processing_status') or file_record.get('review_status')
        if status in ['error', 'needs_review']:
            screenshot_count = get_screenshot_count(bill_id)
            if screenshot_count == 0:
                del extraction_progress[guard_key]
                return jsonify({
                    'success': False, 
                    'error': 'Please upload at least one annotated file before marking this bill as OK.'
                }), 400
        
        data = request.get_json() or {}
        reviewed_by = data.get('reviewed_by', 'User')
        note = data.get('note')
        
        # Get annotation files and convert to base64 images for re-extraction
        screenshots = get_bill_screenshots(bill_id)
        annotated_images = []
        re_extraction_triggered = False
        
        if screenshots and len(screenshots) > 0:
            print(f"[bills] Found {len(screenshots)} annotation(s) for bill {bill_id}, triggering re-extraction")
            
            for ss in screenshots:
                file_path = ss.get('file_path')
                mime_type = ss.get('mime_type', '')
                
                if not file_path or not os.path.exists(file_path):
                    continue
                
                try:
                    if mime_type == 'application/pdf' or file_path.lower().endswith('.pdf'):
                        # Convert PDF pages to images
                        doc = fitz.open(file_path)
                        for page_num in range(min(len(doc), 5)):  # Limit to 5 pages per PDF
                            page = doc[page_num]
                            mat = fitz.Matrix(150/72, 150/72)
                            pix = page.get_pixmap(matrix=mat)
                            img_bytes = pix.tobytes("png")
                            b64_img = base64.b64encode(img_bytes).decode('utf-8')
                            annotated_images.append(b64_img)
                        doc.close()
                    else:
                        # Direct image file
                        with open(file_path, 'rb') as f:
                            img_bytes = f.read()
                            b64_img = base64.b64encode(img_bytes).decode('utf-8')
                            annotated_images.append(b64_img)
                except Exception as e:
                    print(f"[bills] Error processing annotation file {file_path}: {e}")
            
            if annotated_images:
                # Run re-extraction with annotations
                try:
                    from bill_extractor import extract_bill_data
                    
                    original_file = file_record.get('file_path')
                    if original_file and os.path.exists(original_file):
                        print(f"[bills] Re-extracting with {len(annotated_images)} annotation image(s)")
                        
                        extraction_result = extract_bill_data(
                            original_file,
                            annotated_images=annotated_images
                        )
                        
                        if extraction_result.get('success'):
                            re_extraction_triggered = True
                            # CRITICAL: Populate normalized tables FIRST so bills table has data
                            # before marking status as 'ok'
                            bills_saved = populate_normalized_tables(
                                file_record['project_id'],
                                extraction_result,
                                file_record.get('original_filename', 'unknown'),
                                file_id=bill_id
                            )
                            print(f"[bills] Re-extraction bills saved: {bills_saved}")
                            
                            # Update extraction payload in database and set status AFTER bills are saved
                            update_bill_file_review_status(
                                bill_id,
                                'ok',
                                extraction_payload=extraction_result
                            )
                            print(f"[bills] Re-extraction successful for bill {bill_id}")
                        else:
                            print(f"[bills] Re-extraction failed: {extraction_result.get('error')}")
                except Exception as e:
                    print(f"[bills] Re-extraction error: {e}")
                    import traceback
                    traceback.print_exc()
        
        result = mark_bill_ok(bill_id, reviewed_by=reviewed_by, note=note)
        
        # Clean up in-flight guard
        if guard_key in extraction_progress:
            del extraction_progress[guard_key]
        
        if result:
            return jsonify({
                'success': True,
                'bill_id': bill_id,
                'review_status': result['review_status'],
                'processing_status': result['processing_status'],
                'reviewed_at': result['reviewed_at'].isoformat() if result['reviewed_at'] else None,
                'reviewed_by': result['reviewed_by'],
                're_extraction_triggered': re_extraction_triggered
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to update bill'}), 500
    except Exception as e:
        # Clean up in-flight guard on error
        guard_key = f"mark_ok_{bill_id}"
        if guard_key in extraction_progress:
            del extraction_progress[guard_key]
        
        print(f"[bills] Error marking bill as OK: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ NEW BILLS API ENDPOINTS ============

@app.route('/api/accounts/<int:account_id>/summary', methods=['GET'])
def get_account_summary_endpoint(account_id):
    """Get annual summary for an account: combined totals + per-meter breakdown."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        from bills_db import get_account_summary
        months = request.args.get('months', 12, type=int)
        result = get_account_summary(account_id, months)
        return jsonify({'success': True, **result})
    except Exception as e:
        print(f"[bills] Error getting account summary: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/meters/<int:meter_id>/bills', methods=['GET'])
def get_meter_bills_endpoint(meter_id):
    """Get list of bills for a meter with summary data."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        from bills_db import get_meter_bills
        months = request.args.get('months', 12, type=int)
        result = get_meter_bills(meter_id, months)
        return jsonify({'success': True, **result})
    except Exception as e:
        print(f"[bills] Error getting meter bills: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bills/<int:bill_id>/detail', methods=['GET'])
def get_bill_detail_endpoint(bill_id):
    """Get full detail for a single bill including TOU fields and source file metadata."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        from bills_db import get_bill_detail
        result = get_bill_detail(bill_id)
        if result:
            return jsonify({'success': True, **result})
        else:
            return jsonify({'success': False, 'error': 'Bill not found'}), 404
    except Exception as e:
        print(f"[bills] Error getting bill detail: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/accounts/<int:account_id>/meters/<int:meter_id>/months', methods=['GET'])
def get_meter_months_endpoint(account_id, meter_id):
    """Get month-by-month breakdown for a specific meter under an account."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        from bills_db import get_meter_months
        months = request.args.get('months', 12, type=int)
        result = get_meter_months(account_id, meter_id, months)
        return jsonify({'success': True, **result})
    except Exception as e:
        print(f"[bills] Error getting meter months: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/summary', methods=['GET'])
def get_project_bills_summary(project_id):
    """Get bills summary for a project including annual summaries per account."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        from bills_db import get_utility_accounts_for_project, get_account_summary, get_connection
        months = request.args.get('months', 12, type=int)
        service_filter = request.args.get('service')
        
        # Pass service_filter to get only accounts with matching service type
        accounts = get_utility_accounts_for_project(project_id, service_filter=service_filter)
        summaries = []
        
        for acc in accounts:
            # Pass service_filter to filter bills by service type in aggregation
            summary = get_account_summary(acc['id'], months, service_filter=service_filter)
            summary['utilityName'] = acc['utility_name']
            summary['accountNumber'] = acc['account_number']
            summaries.append(summary)
        
        # Build service filter condition for SQL
        if service_filter == 'electric':
            service_condition = "AND service_type IN ('electric', 'combined')"
        else:
            service_condition = ""
        
        file_counts = {'uploaded': 0, 'ok': 0, 'needsReview': 0, 'processing': 0, 'error': 0}
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute(f'''
                    SELECT 
                        COUNT(*) AS total,
                        COUNT(*) FILTER (WHERE review_status = 'ok') AS ok_count,
                        COUNT(*) FILTER (WHERE review_status = 'needs_review') AS needs_review_count,
                        COUNT(*) FILTER (WHERE processing_status = 'extracting' OR processing_status = 'pending') AS processing_count,
                        COUNT(*) FILTER (WHERE processing_status = 'error') AS error_count
                    FROM utility_bill_files
                    WHERE project_id = %s {service_condition}
                ''', (project_id,))
                row = cur.fetchone()
                if row:
                    file_counts = {
                        'uploaded': row[0] or 0,
                        'ok': row[1] or 0,
                        'needsReview': row[2] or 0,
                        'processing': row[3] or 0,
                        'error': row[4] or 0
                    }
            conn.close()
            
            if service_filter:
                print(f"[bills] Service filter: {service_filter}, Files returned: {file_counts['uploaded']}")
        except Exception as fc_err:
            print(f"[bills] Error getting file counts: {fc_err}")
        
        return jsonify({
            'success': True,
            'projectId': project_id,
            'months': months,
            'accounts': summaries,
            'fileCounts': file_counts
        })
    except Exception as e:
        print(f"[bills] Error getting project bills summary: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/bills/export-csv', methods=['GET'])
def export_bills_csv_endpoint(project_id):
    """Export all bills for a project as CSV for jMaster import."""
    if not BILLS_FEATURE_ENABLED:
        return jsonify({'error': 'Bills feature is disabled'}), 403
    
    try:
        from bills_db import export_bills_csv
        csv_content = export_bills_csv(project_id)
        
        if csv_content is None:
            return jsonify({'success': False, 'error': 'No bills found for this project', 'csv': None})
        
        return jsonify({
            'success': True,
            'csv': csv_content
        })
    except Exception as e:
        print(f"[bills] Error exporting bills CSV: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# CONFIG ENDPOINT (for mobile-safe feature flags)
# =============================================================================

@app.route('/api/config', methods=['GET'])
def get_config():
    """Return application configuration including feature flags.
    This endpoint is used by the frontend to check feature availability
    without any device/UA detection - purely server-driven.
    """
    return jsonify({
        'billsEnabled': BILLS_FEATURE_ENABLED
    })


# =============================================================================
# PRINT PDF ENDPOINT
# =============================================================================

@app.route('/api/projects/<project_id>/print.pdf', methods=['GET'])
def get_project_print_pdf(project_id):
    """Generate PDF from print view HTML using weasyprint."""
    try:
        from weasyprint import HTML, CSS
        import io
        
        # Get project data
        user_id = 'default'
        if user_id not in stored_data or project_id not in stored_data[user_id]:
            return jsonify({'error': 'Project not found'}), 404
        
        project = stored_data[user_id][project_id]
        sd = project.get('siteData', {})
        entries = project.get('entries', [])
        
        customer = sd.get('customer', 'Project')
        visit_date = sd.get('visitDate', sd.get('date', ''))
        
        # Build print HTML (same structure as frontend)
        evaps = [e for e in entries if e.get('section') == 'evap']
        conds = [e for e in entries if e.get('section') == 'cond']
        
        def spec_pair(label, value):
            return f'<span class="spec-label">{label}:</span> <span class="spec-value">{value}</span>'
        
        def build_mfr_html(entry):
            if not entry.get('room-mfg'):
                return ''
            return f'<div class="print-room-mfr"><span class="mfr-label">Mfr:</span> <span class="mfr-value">{entry.get("room-mfg")}</span></div>'
        
        def build_info_line(entry, is_evap):
            parts = []
            if is_evap:
                if entry.get('room-setPoint'):
                    parts.append(f'<span class="mfr-label">Set Point:</span> <span class="mfr-value">{entry.get("room-setPoint")}°F</span>')
                if entry.get('room-currentTemp'):
                    parts.append(f'<span class="mfr-label">Current Temp:</span> <span class="mfr-value">{entry.get("room-currentTemp")}°F</span>')
            if entry.get('room-runTime'):
                parts.append(f'<span class="mfr-label">Run Time:</span> <span class="mfr-value">{entry.get("room-runTime")}%</span>')
            if not is_evap and entry.get('room-split') == True:
                parts.append('Split')
            return f'<div class="print-room-info">{" | ".join(parts)}</div>' if parts else ''
        
        def build_spec_columns(entry, is_evap):
            left_lines = []
            right_lines = []
            
            # Left column
            line1 = []
            if entry.get('room-count'):
                line1.append(spec_pair('Units', entry.get('room-count')))
            if entry.get('room-fanMotorsPerUnit'):
                line1.append(spec_pair('Motors Per Unit', entry.get('room-fanMotorsPerUnit')))
            if line1:
                left_lines.append(' | '.join(line1))
            
            line2 = []
            if entry.get('room-voltage'):
                line2.append(spec_pair('Voltage', entry.get('room-voltage')))
            if entry.get('room-phase'):
                line2.append(spec_pair('Phase', entry.get('room-phase')))
            if entry.get('room-amps'):
                line2.append(spec_pair('FLA', entry.get('room-amps')))
            if entry.get('room-hp'):
                line2.append(spec_pair('HP', entry.get('room-hp')))
            if entry.get('room-rpm'):
                line2.append(spec_pair('RPM', entry.get('room-rpm')))
            if line2:
                left_lines.append(' | '.join(line2))
            
            # Right column
            r_line1 = []
            if entry.get('room-frame'):
                r_line1.append(spec_pair('Frame', entry.get('room-frame')))
            if entry.get('room-motorMounting'):
                mount_val = entry.get('room-motorMounting', '').capitalize()
                r_line1.append(spec_pair('Mount', mount_val))
            if entry.get('room-shaftSize'):
                r_line1.append(spec_pair('Shaft', entry.get('room-shaftSize')))
            if entry.get('room-rotation'):
                r_line1.append(spec_pair('Rotation', entry.get('room-rotation')))
            if r_line1:
                right_lines.append(' | '.join(r_line1))
            
            r_line2 = []
            if entry.get('room-shaftAdapterQty') and int(entry.get('room-shaftAdapterQty', 0)) > 0 and entry.get('room-shaftAdapterType'):
                r_line2.append(f'<span class="spec-label">Adapters:</span> <span class="spec-value">({entry.get("room-shaftAdapterQty")}) {entry.get("room-shaftAdapterType")}</span>')
            if entry.get('room-bladesNeeded') and int(entry.get('room-bladesNeeded', 0)) > 0 and entry.get('room-bladeSpec'):
                r_line2.append(f'<span class="spec-label">FanBlade(s):</span> <span class="spec-value">({entry.get("room-bladesNeeded")}) {entry.get("room-bladeSpec")}</span>')
            elif entry.get('room-bladeSpec'):
                r_line2.append(f'<span class="spec-label">FanBlade(s):</span> <span class="spec-value">{entry.get("room-bladeSpec")}</span>')
            if r_line2:
                right_lines.append(' | '.join(r_line2))
            
            if not left_lines and not right_lines:
                return ''
            
            left_html = ''.join([f'<div class="spec-line">{l}</div>' for l in left_lines])
            right_html = ''.join([f'<div class="spec-line">{l}</div>' for l in right_lines])
            
            if not right_lines:
                return f'<div class="print-room-specs"><div class="spec-column spec-left">{left_html}</div></div>'
            
            return f'<div class="print-room-specs"><div class="spec-column spec-left">{left_html}</div><div class="spec-column spec-right">{right_html}</div></div>'
        
        # Build HTML
        html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Print View - {customer}</title>
    <style>
        @page {{ size: letter; margin: 0.5in; }}
        body {{ font-family: Arial, sans-serif; margin: 16px; color: #333; }}
        .print-project {{ max-width: 1100px; margin: 0 auto; padding: 10px 16px; }}
        h1 {{ color: #1e5a99; margin-bottom: 0.5rem; }}
        h2 {{ color: #2d7bb8; border-bottom: 2px solid #2d7bb8; padding-bottom: 0.25rem; margin-top: 1.5rem; }}
        .site-info {{ background: #f5f5f5; padding: 0.75rem 1rem; border-radius: 4px; margin-bottom: 1.5rem; }}
        .site-info p {{ margin: 0.2rem 0; font-size: 0.95rem; }}
        .print-room {{ border: 1px solid #bbb; border-radius: 4px; padding: 10px 14px; margin-bottom: 12px; page-break-inside: avoid; }}
        .room-title {{ font-weight: 700; font-size: 1.15rem; color: #1e5a99; margin-bottom: 4px; }}
        .print-room-mfr {{ font-size: 0.9rem; margin-bottom: 6px; }}
        .mfr-label {{ font-weight: 400; color: #777; }}
        .mfr-value {{ font-weight: 400; color: #000; }}
        .spec-label {{ font-weight: 700; color: #000; }}
        .spec-value {{ font-weight: 400; color: #000; }}
        .print-room-info {{ font-size: 0.85rem; color: #555; margin-bottom: 6px; }}
        .print-room-specs {{ display: flex; justify-content: space-between; gap: 40px; margin-bottom: 6px; }}
        .spec-column {{ flex: 1; text-align: left; }}
        .spec-line {{ margin-bottom: 3px; font-size: 0.95rem; color: #333; white-space: normal; line-height: 1.4; }}
        .print-notes-separator {{ border: 0; border-top: 1px solid #dddddd; margin: 6px 0 4px 0; }}
        .print-room-notes {{ font-size: 0.9rem; }}
        .print-room-notes .notes-label {{ font-weight: 600; }}
        .print-room-notes .notes-text {{ font-weight: normal; white-space: pre-wrap; color: #555; }}
    </style>
</head>
<body>
<div class="print-project">
    <h1>{customer}</h1>
    <div class="site-info">
        <p><strong>Address:</strong> {sd.get('street', '')}, {sd.get('city', '')}, {sd.get('state', '')} {sd.get('zip', '')}</p>
        <p><strong>Contact:</strong> {sd.get('contact', '')} {('(' + sd.get('phone') + ')') if sd.get('phone') else ''}</p>
        <p><strong>Utility:</strong> {sd.get('utility', '')}</p>
        <p><strong>Date of Site Visit:</strong> {visit_date}</p>
    </div>'''
        
        # Evaporators
        if evaps:
            html += '<h2>Evaporators</h2>'
            for i, e in enumerate(evaps):
                notes = e.get('room-notes', '') or '—'
                room_name = e.get('room-name', f'Evaporator {i + 1}')
                html += f'''
    <div class="print-room">
        <div class="room-title">{room_name}</div>
        {build_mfr_html(e)}
        {build_info_line(e, True)}
        {build_spec_columns(e, True)}
        <hr class="print-notes-separator">
        <div class="print-room-notes"><span class="notes-label">Notes:</span> <span class="notes-text">{notes}</span></div>
    </div>'''
        
        # Condensers
        if conds:
            html += '<h2>Condensers</h2>'
            for i, c in enumerate(conds):
                notes = c.get('room-notes', '') or '—'
                room_name = c.get('room-name', f'Condenser {i + 1}')
                html += f'''
    <div class="print-room">
        <div class="room-title">{room_name}</div>
        {build_mfr_html(c)}
        {build_info_line(c, False)}
        {build_spec_columns(c, False)}
        <hr class="print-notes-separator">
        <div class="print-room-notes"><span class="notes-label">Notes:</span> <span class="notes-text">{notes}</span></div>
    </div>'''
        
        html += '</div></body></html>'
        
        # Generate PDF
        pdf_buffer = io.BytesIO()
        HTML(string=html).write_pdf(pdf_buffer)
        pdf_buffer.seek(0)
        
        # Build filename
        safe_customer = ''.join(c for c in customer if c.isalnum() or c in ' -_').strip()
        safe_date = visit_date.replace('/', '-').replace(' ', '_') if visit_date else datetime.now().strftime('%Y-%m-%d')
        filename = f"Print View - {safe_customer} - {safe_date}.pdf"
        
        return send_file(
            pdf_buffer,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        print(f"[print-pdf] Error generating PDF: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
