"""
Bill Intake Database Module
Isolated database layer for utility bill management.
Uses PostgreSQL (separate from projects_data.json used by SiteWalk core).
"""
import os
import json
import re
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from datetime import datetime

DATABASE_URL = os.environ.get('DATABASE_URL')


def normalize_account_number(raw):
    """Strip spaces, punctuation, return digits only."""
    if not raw:
        return raw
    return re.sub(r'[^0-9]', '', str(raw))


def normalize_meter_number(raw):
    """Strip spaces, punctuation, return digits only."""
    if not raw:
        get_bill_files_for_project
        return raw
    return re.sub(r'[^0-9]', '', str(raw))

def get_connection():
    """Get a database connection."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not configured")
    return psycopg2.connect(DATABASE_URL)

def init_bills_tables():
    """Create all bill intake tables if they don't exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS utility_accounts (
                    id SERIAL PRIMARY KEY,
                    project_id VARCHAR(255) NOT NULL,
                    utility_name VARCHAR(255) NOT NULL,
                    account_number VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_utility_accounts_project 
                ON utility_accounts(project_id);
            ''')
            
            cur.execute('''
                CREATE TABLE IF NOT EXISTS utility_meters (
                    id SERIAL PRIMARY KEY,
                    utility_account_id INTEGER REFERENCES utility_accounts(id) ON DELETE CASCADE,
                    meter_number VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_utility_meters_account 
                ON utility_meters(utility_account_id);
            ''')
            
            cur.execute('''
                CREATE TABLE IF NOT EXISTS utility_meter_reads (
                    id SERIAL PRIMARY KEY,
                    utility_meter_id INTEGER REFERENCES utility_meters(id) ON DELETE CASCADE,
                    billing_start_date DATE,
                    billing_end_date DATE,
                    statement_date DATE,
                    kwh NUMERIC(12,2),
                    total_charges_usd NUMERIC(12,2),
                    source_file VARCHAR(512),
                    source_page INTEGER,
                    from_summary_table BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_utility_meter_reads_meter 
                ON utility_meter_reads(utility_meter_id);
            ''')
            
            cur.execute('''
                CREATE TABLE IF NOT EXISTS utility_bill_files (
                    id SERIAL PRIMARY KEY,
                    project_id VARCHAR(255) NOT NULL,
                    filename VARCHAR(512) NOT NULL,
                    original_filename VARCHAR(512) NOT NULL,
                    file_path VARCHAR(1024) NOT NULL,
                    file_size INTEGER,
                    mime_type VARCHAR(255),
                    upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed BOOLEAN DEFAULT FALSE,
                    processing_status VARCHAR(50) DEFAULT 'pending',
                    review_status VARCHAR(50) DEFAULT 'pending',
                    extraction_payload JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_utility_bill_files_project 
                ON utility_bill_files(project_id);
            ''')
            
            cur.execute('''
                CREATE TABLE IF NOT EXISTS bill_training_data (
                    id SERIAL PRIMARY KEY,
                    utility_name VARCHAR(200),
                    pdf_hash VARCHAR(64),
                    field_type VARCHAR(100),
                    meter_number VARCHAR(100),
                    period_start_date DATE,
                    period_end_date DATE,
                    corrected_value TEXT,
                    annotated_image_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_bill_training_utility 
                ON bill_training_data(utility_name);
            ''')
            
            cur.execute('''
                CREATE TABLE IF NOT EXISTS bill_screenshots (
                    id SERIAL PRIMARY KEY,
                    bill_id INTEGER REFERENCES utility_bill_files(id) ON DELETE CASCADE,
                    file_path VARCHAR(1024) NOT NULL,
                    original_filename VARCHAR(512),
                    mime_type VARCHAR(100),
                    page_hint VARCHAR(100),
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_bill_screenshots_bill 
                ON bill_screenshots(bill_id);
            ''')
            
            cur.execute('''
                CREATE TABLE IF NOT EXISTS bills (
                    id SERIAL PRIMARY KEY,
                    bill_file_id INTEGER REFERENCES utility_bill_files(id) ON DELETE SET NULL,
                    account_id INTEGER REFERENCES utility_accounts(id) ON DELETE CASCADE,
                    meter_id INTEGER REFERENCES utility_meters(id) ON DELETE CASCADE,
                    utility_name VARCHAR(255),
                    service_address TEXT,
                    rate_schedule VARCHAR(100),
                    period_start DATE,
                    period_end DATE,
                    days_in_period INTEGER,
                    total_kwh NUMERIC(12,2),
                    total_amount_due NUMERIC(12,2),
                    energy_charges NUMERIC(12,2),
                    demand_charges NUMERIC(12,2),
                    other_charges NUMERIC(12,2),
                    taxes NUMERIC(12,2),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_bills_account 
                ON bills(account_id);
                
                CREATE INDEX IF NOT EXISTS idx_bills_meter 
                ON bills(meter_id);
                
                CREATE INDEX IF NOT EXISTS idx_bills_period 
                ON bills(period_start, period_end);
            ''')
            
            cur.execute('''
                CREATE TABLE IF NOT EXISTS bill_tou_periods (
                    id SERIAL PRIMARY KEY,
                    bill_id INTEGER REFERENCES bills(id) ON DELETE CASCADE,
                    period VARCHAR(50),
                    kwh NUMERIC(12,2),
                    rate_dollars_per_kwh NUMERIC(10,6),
                    est_cost_dollars NUMERIC(12,2),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_bill_tou_periods_bill 
                ON bill_tou_periods(bill_id);
            ''')
            
            conn.commit()
            
            # Run migrations to add new columns if they don't exist
            _migrate_add_review_columns(conn)
            _migrate_add_bills_tables(conn)
            _migrate_add_tou_columns(conn)
            _migrate_add_normalization_columns(conn)
            _migrate_add_sha256_column(conn)
            _migrate_add_service_type_column(conn)
            
            print("[bills_db] Tables initialized successfully")
            return True
    except Exception as e:
        print(f"[bills_db] Error initializing tables: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def _migrate_add_review_columns(conn):
    """Add review_status, extraction_payload, reviewed_at, reviewed_by columns if they don't exist."""
    try:
        with conn.cursor() as cur:
            # Check if review_status column exists
            cur.execute('''
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'utility_bill_files' AND column_name = 'review_status'
            ''')
            if not cur.fetchone():
                print("[bills_db] Adding review_status column...")
                cur.execute('''
                    ALTER TABLE utility_bill_files 
                    ADD COLUMN review_status VARCHAR(50) DEFAULT 'pending'
                ''')
            
            # Check if extraction_payload column exists
            cur.execute('''
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'utility_bill_files' AND column_name = 'extraction_payload'
            ''')
            if not cur.fetchone():
                print("[bills_db] Adding extraction_payload column...")
                cur.execute('''
                    ALTER TABLE utility_bill_files 
                    ADD COLUMN extraction_payload JSONB
                ''')
            
            # Check if reviewed_at column exists
            cur.execute('''
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'utility_bill_files' AND column_name = 'reviewed_at'
            ''')
            if not cur.fetchone():
                print("[bills_db] Adding reviewed_at column...")
                cur.execute('''
                    ALTER TABLE utility_bill_files 
                    ADD COLUMN reviewed_at TIMESTAMP
                ''')
            
            # Check if reviewed_by column exists
            cur.execute('''
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'utility_bill_files' AND column_name = 'reviewed_by'
            ''')
            if not cur.fetchone():
                print("[bills_db] Adding reviewed_by column...")
                cur.execute('''
                    ALTER TABLE utility_bill_files 
                    ADD COLUMN reviewed_by VARCHAR(255)
                ''')
            
            # Check if mime_type column exists in bill_screenshots
            cur.execute('''
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'bill_screenshots' AND column_name = 'mime_type'
            ''')
            if not cur.fetchone():
                print("[bills_db] Adding mime_type column to bill_screenshots...")
                cur.execute('''
                    ALTER TABLE bill_screenshots 
                    ADD COLUMN mime_type VARCHAR(100)
                ''')
            
            conn.commit()
            print("[bills_db] Migration complete")
    except Exception as e:
        print(f"[bills_db] Migration error (non-fatal): {e}")
        conn.rollback()


def _migrate_add_bills_tables(conn):
    """Add service_address column to utility_meters and ensure bills/bill_tou_periods tables exist."""
    try:
        with conn.cursor() as cur:
            # Check if service_address column exists in utility_meters
            cur.execute('''
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'utility_meters' AND column_name = 'service_address'
            ''')
            if not cur.fetchone():
                print("[bills_db] Adding service_address column to utility_meters...")
                cur.execute('''
                    ALTER TABLE utility_meters 
                    ADD COLUMN service_address TEXT
                ''')
            
            conn.commit()
            print("[bills_db] Bills tables migration complete")
    except Exception as e:
        print(f"[bills_db] Bills tables migration error (non-fatal): {e}")
        conn.rollback()


def _migrate_add_tou_columns(conn):
    """Add TOU columns to bills table and missing_fields to utility_bill_files."""
    try:
        with conn.cursor() as cur:
            tou_columns = [
                ('tou_on_kwh', 'NUMERIC(12,2)'),
                ('tou_mid_kwh', 'NUMERIC(12,2)'),
                ('tou_off_kwh', 'NUMERIC(12,2)'),
                ('tou_super_off_kwh', 'NUMERIC(12,2)'),
                ('tou_on_rate_dollars', 'NUMERIC(10,6)'),
                ('tou_mid_rate_dollars', 'NUMERIC(10,6)'),
                ('tou_off_rate_dollars', 'NUMERIC(10,6)'),
                ('tou_super_off_rate_dollars', 'NUMERIC(10,6)'),
                ('tou_on_cost', 'NUMERIC(12,2)'),
                ('tou_mid_cost', 'NUMERIC(12,2)'),
                ('tou_off_cost', 'NUMERIC(12,2)'),
                ('tou_super_off_cost', 'NUMERIC(12,2)'),
                ('blended_rate_dollars', 'NUMERIC(10,6)'),
                ('avg_cost_per_day', 'NUMERIC(12,2)'),
            ]
            
            for col_name, col_type in tou_columns:
                cur.execute('''
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name = 'bills' AND column_name = %s
                ''', (col_name,))
                if not cur.fetchone():
                    print(f"[bills_db] Adding {col_name} column to bills...")
                    cur.execute(f'''
                        ALTER TABLE bills 
                        ADD COLUMN {col_name} {col_type}
                    ''')
            
            cur.execute('''
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'utility_bill_files' AND column_name = 'missing_fields'
            ''')
            if not cur.fetchone():
                print("[bills_db] Adding missing_fields column to utility_bill_files...")
                cur.execute('''
                    ALTER TABLE utility_bill_files 
                    ADD COLUMN missing_fields JSONB DEFAULT '[]'::jsonb
                ''')
            
            conn.commit()
            print("[bills_db] TOU columns migration complete")
    except Exception as e:
        print(f"[bills_db] TOU columns migration error (non-fatal): {e}")
        conn.rollback()


def _migrate_add_normalization_columns(conn):
    """Add normalization columns to utility_bill_files for text-based processing."""
    try:
        with conn.cursor() as cur:
            normalization_columns = [
                ('normalized_text', 'TEXT'),
                ('normalized_hash', 'VARCHAR(64)'),
                ('processing_metrics', 'JSONB'),
            ]
            
            for col_name, col_type in normalization_columns:
                cur.execute('''
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name = 'utility_bill_files' AND column_name = %s
                ''', (col_name,))
                if not cur.fetchone():
                    print(f"[bills_db] Adding {col_name} column to utility_bill_files...")
                    cur.execute(f'''
                        ALTER TABLE utility_bill_files 
                        ADD COLUMN {col_name} {col_type}
                    ''')
            
            cur.execute('''
                CREATE INDEX IF NOT EXISTS idx_utility_bill_files_hash 
                ON utility_bill_files(normalized_hash)
                WHERE normalized_hash IS NOT NULL
            ''')
            
            conn.commit()
            print("[bills_db] Normalization columns migration complete")
    except Exception as e:
        print(f"[bills_db] Normalization columns migration error (non-fatal): {e}")
        conn.rollback()


def _migrate_add_sha256_column(conn):
    """Add sha256 column to utility_bill_files for upload deduplication."""
    try:
        with conn.cursor() as cur:
            cur.execute('''
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'utility_bill_files' AND column_name = 'sha256'
            ''')
            if not cur.fetchone():
                print("[bills_db] Adding sha256 column to utility_bill_files...")
                cur.execute('''
                    ALTER TABLE utility_bill_files 
                    ADD COLUMN sha256 VARCHAR(64)
                ''')
            
            cur.execute('''
                SELECT constraint_name FROM information_schema.table_constraints 
                WHERE table_name = 'utility_bill_files' 
                AND constraint_name = 'uq_project_sha256'
            ''')
            if not cur.fetchone():
                print("[bills_db] Adding unique constraint on (project_id, sha256)...")
                cur.execute('''
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_project_sha256 
                    ON utility_bill_files(project_id, sha256)
                    WHERE sha256 IS NOT NULL
                ''')
            
            conn.commit()
            print("[bills_db] SHA256 column migration complete")
    except Exception as e:
        print(f"[bills_db] SHA256 column migration error (non-fatal): {e}")
        conn.rollback()


def _migrate_add_service_type_column(conn):
    """Add service_type column to utility_bill_files and bills tables."""
    try:
        with conn.cursor() as cur:
            cur.execute('''
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'utility_bill_files' AND column_name = 'service_type'
            ''')
            if not cur.fetchone():
                print("[bills_db] Adding service_type column to utility_bill_files...")
                cur.execute('''
                    ALTER TABLE utility_bill_files 
                    ADD COLUMN service_type VARCHAR(50) DEFAULT 'electric'
                ''')
            
            cur.execute('''
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'bills' AND column_name = 'service_type'
            ''')
            if not cur.fetchone():
                print("[bills_db] Adding service_type column to bills...")
                cur.execute('''
                    ALTER TABLE bills 
                    ADD COLUMN service_type VARCHAR(50) DEFAULT 'electric'
                ''')
            
            conn.commit()
            print("[bills_db] Service type column migration complete")
    except Exception as e:
        print(f"[bills_db] Service type column migration error (non-fatal): {e}")
        conn.rollback()


def find_bill_file_by_sha256(project_id, sha256):
    """Find an existing bill file by project_id and SHA256 hash."""
    if not sha256:
        return None
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT id, project_id, filename, original_filename, file_path,
                       file_size, mime_type, upload_date, processed, processing_status,
                       review_status, extraction_payload, sha256, service_type
                FROM utility_bill_files
                WHERE project_id = %s AND sha256 = %s
            ''', (project_id, sha256))
            return cur.fetchone()
    finally:
        conn.close()


def get_cached_result_by_hash(normalized_hash):
    """
    Look up cached extraction result by normalized text hash.
    
    Args:
        normalized_hash: SHA256 hash of normalized_text + version
        
    Returns:
        Dict with extraction_payload if found, None otherwise
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT id, extraction_payload, processing_metrics, normalized_text
                FROM utility_bill_files
                WHERE normalized_hash = %s
                  AND extraction_payload IS NOT NULL
                  AND processing_status = 'complete'
                ORDER BY upload_date DESC
                LIMIT 1
            ''', (normalized_hash,))
            result = cur.fetchone()
            if result:
                return {
                    'file_id': result['id'],
                    'parse_result': result['extraction_payload'],
                    'metrics': result['processing_metrics'],
                }
            return None
    finally:
        conn.close()


def save_cache_entry(file_id, normalized_hash, normalized_text, parse_result, metrics):
    """
    Save extraction result to enable future cache hits.
    
    Args:
        file_id: Database ID of the bill file
        normalized_hash: SHA256 hash of normalized_text + version
        normalized_text: The normalized text content
        parse_result: Extracted bill data (dict)
        metrics: Processing metrics (timing, tokens, etc)
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('''
                UPDATE utility_bill_files
                SET normalized_hash = %s,
                    normalized_text = %s,
                    extraction_payload = %s,
                    processing_metrics = %s,
                    processing_status = 'complete',
                    processed = TRUE
                WHERE id = %s
            ''', (
                normalized_hash,
                normalized_text[:50000] if normalized_text else None,
                Json(parse_result),
                Json(metrics),
                file_id
            ))
            conn.commit()
            print(f"[bills_db] Saved cache entry for file {file_id}, hash {normalized_hash[:12]}...")
    except Exception as e:
        conn.rollback()
        print(f"[bills_db] Error saving cache entry: {e}")
        raise
    finally:
        conn.close()


def invalidate_cache_for_file(file_id):
    """
    Invalidate cache entry for a file (clear hash so it won't match).
    
    Args:
        file_id: Database ID of the bill file
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('''
                UPDATE utility_bill_files
                SET normalized_hash = NULL,
                    processing_status = 'pending'
                WHERE id = %s
            ''', (file_id,))
            conn.commit()
    finally:
        conn.close()


def update_file_processing_status(file_id, status, metrics=None):
    """
    Update processing status for a bill file.
    
    Args:
        file_id: Database ID of the bill file
        status: New status string
        metrics: Optional processing metrics to update
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if metrics:
                cur.execute('''
                    UPDATE utility_bill_files
                    SET processing_status = %s,
                        processing_metrics = %s
                    WHERE id = %s
                ''', (status, Json(metrics), file_id))
            else:
                cur.execute('''
                    UPDATE utility_bill_files
                    SET processing_status = %s
                    WHERE id = %s
                ''', (status, file_id))
            conn.commit()
    finally:
        conn.close()


def get_bill_files_for_project(project_id):
    """Get all uploaded bill files for a project."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT id, project_id, filename, original_filename, file_path,
                       file_size, mime_type, upload_date, processed, processing_status,
                       review_status, extraction_payload
                FROM utility_bill_files
                WHERE project_id = %s
                ORDER BY upload_date DESC
            ''', (project_id,))
            return cur.fetchall()
    finally:
        conn.close()


def get_bill_file_by_id(file_id):
    """Get a single bill file by ID."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT id, project_id, filename, original_filename, file_path,
                       file_size, mime_type, upload_date, processed, processing_status,
                       review_status, extraction_payload
                FROM utility_bill_files
                WHERE id = %s
            ''', (file_id,))
            return cur.fetchone()
    finally:
        conn.close()


def add_bill_file(project_id, filename, original_filename, file_path, file_size, mime_type, sha256=None, service_type='electric'):
    """Add a bill file record to the database with status='pending'."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                INSERT INTO utility_bill_files 
                (project_id, filename, original_filename, file_path, file_size, mime_type,
                 review_status, processing_status, sha256, service_type)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', 'pending', %s, %s)
                RETURNING id, project_id, filename, original_filename, file_path,
                          file_size, mime_type, upload_date, processed, processing_status,
                          review_status, extraction_payload, sha256, service_type
            ''', (project_id, filename, original_filename, file_path, file_size, mime_type, sha256, service_type))
            result = cur.fetchone()
            conn.commit()
            return dict(result)
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def delete_bill_file(file_id):
    """Delete a bill file record."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM utility_bill_files WHERE id = %s', (file_id,))
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()


def get_utility_accounts_for_project(project_id, service_filter=None):
    """Get all utility accounts for a project.
    
    Args:
        project_id: The project ID
        service_filter: Optional filter ('electric' filters to accounts with electric/combined bills)
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if service_filter == 'electric':
                # Only return accounts that have at least one bill with electric/combined service type
                cur.execute('''
                    SELECT DISTINCT a.id, a.project_id, a.utility_name, a.account_number, a.created_at
                    FROM utility_accounts a
                    JOIN bills b ON b.account_id = a.id
                    JOIN utility_bill_files ubf ON b.bill_file_id = ubf.id
                    WHERE a.project_id = %s
                      AND ubf.service_type IN ('electric', 'combined')
                    ORDER BY a.utility_name
                ''', (project_id,))
            else:
                cur.execute('''
                    SELECT id, project_id, utility_name, account_number, created_at
                    FROM utility_accounts
                    WHERE project_id = %s
                    ORDER BY utility_name
                ''', (project_id,))
            return cur.fetchall()
    finally:
        conn.close()


def get_meter_reads_for_project(project_id):
    """Get all meter reads for a project (via accounts and meters)."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT 
                    r.id, r.billing_start_date, r.billing_end_date, r.statement_date,
                    r.kwh, r.total_charges_usd, r.source_file, r.source_page,
                    r.from_summary_table,
                    m.meter_number,
                    a.utility_name, a.account_number
                FROM utility_meter_reads r
                JOIN utility_meters m ON r.utility_meter_id = m.id
                JOIN utility_accounts a ON m.utility_account_id = a.id
                WHERE a.project_id = %s
                ORDER BY r.billing_end_date DESC
            ''', (project_id,))
            return cur.fetchall()
    finally:
        conn.close()


def get_bills_summary_for_project(project_id):
    """Get a summary of bills data for a project."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT 
                    COUNT(DISTINCT f.id) as file_count,
                    (SELECT COUNT(*) FROM utility_accounts WHERE project_id = %s) as account_count,
                    (SELECT COUNT(*) FROM utility_meter_reads r 
                     JOIN utility_meters m ON r.utility_meter_id = m.id
                     JOIN utility_accounts a ON m.utility_account_id = a.id
                     WHERE a.project_id = %s) as read_count
                FROM utility_bill_files f
                WHERE f.project_id = %s
            ''', (project_id, project_id, project_id))
            return cur.fetchone()
    finally:
        conn.close()


def update_bill_file_status(file_id, status, processed=True, missing_fields=None):
    """
    Update the processing status of a bill file.
    
    Args:
        file_id: The bill file ID
        status: Processing status string
        processed: Boolean for processed flag
        missing_fields: Optional list of missing field names. If provided and not empty,
                       sets review_status to 'needs_review'.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if missing_fields is not None:
                # If missing_fields provided, also update review_status
                review_status = 'needs_review' if len(missing_fields) > 0 else 'ok'
                cur.execute('''
                    UPDATE utility_bill_files 
                    SET processing_status = %s, processed = %s, 
                        missing_fields = %s, review_status = %s
                    WHERE id = %s
                ''', (status, processed, Json(missing_fields), review_status, file_id))
            else:
                cur.execute('''
                    UPDATE utility_bill_files 
                    SET processing_status = %s, processed = %s
                    WHERE id = %s
                ''', (status, processed, file_id))
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()


def update_bill_file_review_status(file_id, review_status, extraction_payload=None):
    """Update the review status and extraction payload of a bill file."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if extraction_payload is not None:
                cur.execute('''
                    UPDATE utility_bill_files 
                    SET review_status = %s, extraction_payload = %s
                    WHERE id = %s
                ''', (review_status, Json(extraction_payload), file_id))
            else:
                cur.execute('''
                    UPDATE utility_bill_files 
                    SET review_status = %s
                    WHERE id = %s
                ''', (review_status, file_id))
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()


def update_bill_file_extraction_payload(file_id, extraction_payload):
    """Update only the extraction payload of a bill file."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('''
                UPDATE utility_bill_files 
                SET extraction_payload = %s
                WHERE id = %s
            ''', (Json(extraction_payload), file_id))
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()


def get_files_status_for_project(project_id):
    """Get status summary for all files in a project (for polling)."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT id, original_filename, review_status, processing_status, 
                       processed, upload_date
                FROM utility_bill_files
                WHERE project_id = %s
                ORDER BY upload_date DESC
            ''', (project_id,))
            return cur.fetchall()
    finally:
        conn.close()


def upsert_utility_account(project_id, utility_name, account_number):
    """Find or create a utility account. Returns account ID."""
    from bill_extractor import normalize_utility_name
    utility_name = normalize_utility_name(utility_name)
    account_number = normalize_account_number(account_number)
    
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT id FROM utility_accounts 
                WHERE project_id = %s AND utility_name = %s AND account_number = %s
            ''', (project_id, utility_name, account_number))
            row = cur.fetchone()
            if row:
                return row['id']
            
            # Create new
            cur.execute('''
                INSERT INTO utility_accounts (project_id, utility_name, account_number)
                VALUES (%s, %s, %s)
                RETURNING id
            ''', (project_id, utility_name, account_number))
            result = cur.fetchone()
            conn.commit()
            return result['id']
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def upsert_utility_meter(account_id, meter_number, service_address=None):
    """Find or create a utility meter. Returns meter ID."""
    meter_number = normalize_meter_number(meter_number)
    
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT id FROM utility_meters 
                WHERE utility_account_id = %s AND meter_number = %s
            ''', (account_id, meter_number))
            row = cur.fetchone()
            if row:
                return row['id']
            
            # Create new
            cur.execute('''
                INSERT INTO utility_meters (utility_account_id, meter_number)
                VALUES (%s, %s)
                RETURNING id
            ''', (account_id, meter_number))
            result = cur.fetchone()
            conn.commit()
            return result['id']
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def upsert_meter_read(meter_id, period_start, period_end, kwh, total_charge, source_file=None):
    """
    Upsert a meter reading. Key is (meter_id, period_start, period_end).
    If exists, updates with new values. Otherwise creates new.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Try to find existing by unique key
            cur.execute('''
                SELECT id FROM utility_meter_reads 
                WHERE utility_meter_id = %s 
                AND billing_start_date = %s 
                AND billing_end_date = %s
            ''', (meter_id, period_start, period_end))
            row = cur.fetchone()
            
            if row:
                # Update existing
                cur.execute('''
                    UPDATE utility_meter_reads 
                    SET kwh = %s, total_charges_usd = %s, source_file = %s, 
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id
                ''', (kwh, total_charge, source_file, row['id']))
                result = cur.fetchone()
                conn.commit()
                return result['id']
            else:
                # Insert new
                cur.execute('''
                    INSERT INTO utility_meter_reads 
                    (utility_meter_id, billing_start_date, billing_end_date, kwh, total_charges_usd, source_file)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (meter_id, period_start, period_end, kwh, total_charge, source_file))
                result = cur.fetchone()
                conn.commit()
                return result['id']
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_grouped_bills_data(project_id, service_filter=None):
    """
    Get all bills data for a project, grouped by account and meter.
    Returns structure suitable for UI display.
    Also includes file-level review_status info.
    
    Args:
        project_id: The project ID
        service_filter: Optional filter ('electric' filters to electric/combined/None service types)
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Build service filter for accounts/meters queries
            if service_filter == 'electric':
                # Only get accounts that have at least one bill with matching service type
                cur.execute('''
                    SELECT DISTINCT a.id, a.utility_name, a.account_number 
                    FROM utility_accounts a
                    JOIN bills b ON b.account_id = a.id
                    JOIN utility_bill_files ubf ON b.bill_file_id = ubf.id
                    WHERE a.project_id = %s 
                      AND ubf.service_type IN ('electric', 'combined')
                    ORDER BY a.utility_name, a.account_number
                ''', (project_id,))
            else:
                cur.execute('''
                    SELECT id, utility_name, account_number 
                    FROM utility_accounts 
                    WHERE project_id = %s 
                    ORDER BY utility_name, account_number
                ''', (project_id,))
            accounts = cur.fetchall()
            
            result = []
            for acc in accounts:
                account_data = {
                    'id': acc['id'],
                    'utility_name': acc['utility_name'],
                    'account_number': acc['account_number'],
                    'meters': []
                }
                
                # Get meters for this account (filtered by service type if needed)
                if service_filter == 'electric':
                    cur.execute('''
                        SELECT DISTINCT m.id, m.meter_number 
                        FROM utility_meters m
                        JOIN bills b ON b.meter_id = m.id
                        JOIN utility_bill_files ubf ON b.bill_file_id = ubf.id
                        WHERE m.utility_account_id = %s 
                          AND ubf.service_type IN ('electric', 'combined')
                        ORDER BY m.meter_number
                    ''', (acc['id'],))
                else:
                    cur.execute('''
                        SELECT id, meter_number 
                        FROM utility_meters 
                        WHERE utility_account_id = %s 
                        ORDER BY meter_number
                    ''', (acc['id'],))
                meters = cur.fetchall()
                
                for meter in meters:
                    meter_data = {
                        'id': meter['id'],
                        'meter_number': meter['meter_number'],
                        'bills': []
                    }
                    
                    # Get reads for this meter (from bills table for proper service_type filtering)
                    if service_filter == 'electric':
                        # Query from bills table which has bill_file_id for service_type join
                        cur.execute('''
                            SELECT DISTINCT b.id, b.period_start, b.period_end,
                                   b.total_kwh, b.total_amount_due,
                                   ubf.original_filename AS source_file
                            FROM bills b
                            JOIN utility_bill_files ubf ON b.bill_file_id = ubf.id
                            WHERE b.meter_id = %s
                              AND ubf.service_type IN ('electric', 'combined')
                            ORDER BY b.period_end DESC
                        ''', (meter['id'],))
                    else:
                        cur.execute('''
                            SELECT id, billing_start_date, billing_end_date, 
                                   kwh, total_charges_usd, source_file
                            FROM utility_meter_reads 
                            WHERE utility_meter_id = %s 
                            ORDER BY billing_end_date DESC
                        ''', (meter['id'],))
                    reads = cur.fetchall()
                    
                    for read in reads:
                        meter_data['bills'].append({
                            'id': read['id'],
                            'period_start': str(read['period_start']) if read['period_start'] else None,
                            'period_end': str(read['period_end']) if read['period_end'] else None,
                            'total_kwh': float(read['total_kwh']) if read['total_kwh'] else None,
                            'total_amount_due': float(read['total_amount_due']) if read['total_amount_due'] else None,
                            'source_file': read.get('source_file')
                        })
                    
                    account_data['meters'].append(meter_data)
                
                result.append(account_data)
            
            # Build service filter condition for files query
            if service_filter == 'electric':
                service_condition = "AND service_type IN ('electric', 'combined')"
            else:
                service_condition = ""
            
            # Get file review status summary
            cur.execute(f'''
                SELECT id, original_filename, review_status, processing_status
                FROM utility_bill_files 
                WHERE project_id = %s {service_condition}
                ORDER BY upload_date DESC
            ''', (project_id,))
            files = cur.fetchall()
            
            files_status = []
            for f in files:
                files_status.append({
                    'id': f['id'],
                    'original_filename': f['original_filename'],
                    'review_status': f['review_status'],
                    'processing_status': f['processing_status']
                })
            
            return {
                'accounts': result,
                'files_status': files_status
            }
    finally:
        conn.close()


def save_correction(utility_name, pdf_hash, field_type, meter_number, period_start, period_end, corrected_value, annotated_image_url=None):
    """Save a user correction to training data table."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                INSERT INTO bill_training_data 
                (utility_name, pdf_hash, field_type, meter_number, period_start_date, period_end_date, corrected_value, annotated_image_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, utility_name, pdf_hash, field_type, meter_number, period_start_date, period_end_date, corrected_value, annotated_image_url, created_at
            ''', (utility_name, pdf_hash, field_type, meter_number, period_start, period_end, corrected_value, annotated_image_url))
            result = cur.fetchone()
            conn.commit()
            return dict(result)
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_corrections_for_utility(utility_name):
    """Get all past corrections for a utility."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT id, utility_name, pdf_hash, field_type, meter_number, 
                       period_start_date, period_end_date, corrected_value, 
                       annotated_image_url, created_at
                FROM bill_training_data
                WHERE utility_name = %s
                ORDER BY created_at DESC
            ''', (utility_name,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.close()


def validate_extraction(extraction_payload):
    """
    Validate that all required fields are present in extraction payload.
    
    Required fields:
    - Bill-level: utility_name, account_number
    - Per meter: meter_number, service_address
    - Per read: period_start, period_end, kwh, total_charge
    
    Returns:
        {
            'is_valid': bool,
            'missing_fields': list of strings describing what's missing
        }
    """
    missing_fields = []
    
    if not extraction_payload:
        return {'is_valid': False, 'missing_fields': ['No extraction data']}
    
    utility_name = extraction_payload.get('utility_name')
    account_number = extraction_payload.get('account_number')
    meters = extraction_payload.get('meters', [])
    
    if not utility_name:
        missing_fields.append('missing utility_name')
    if not account_number:
        missing_fields.append('missing account_number')
    
    if not meters:
        missing_fields.append('no meters found')
    else:
        for i, meter in enumerate(meters):
            meter_number = meter.get('meter_number')
            service_address = meter.get('service_address')
            reads = meter.get('reads', [])
            
            meter_id = meter_number or f"meter_{i+1}"
            
            if not meter_number:
                missing_fields.append(f'missing meter_number for meter {i+1}')
            if not service_address:
                missing_fields.append(f'missing service_address for meter {meter_id}')
            
            if not reads:
                missing_fields.append(f'no reads found for meter {meter_id}')
            else:
                for j, read in enumerate(reads):
                    period_start = read.get('period_start')
                    period_end = read.get('period_end')
                    kwh = read.get('kwh')
                    total_charge = read.get('total_charge')
                    
                    period_desc = f"{period_start or '?'} to {period_end or '?'}"
                    
                    if not period_start:
                        missing_fields.append(f'missing period_start for meter {meter_id} read {j+1}')
                    if not period_end:
                        missing_fields.append(f'missing period_end for meter {meter_id} read {j+1}')
                    if kwh is None:
                        missing_fields.append(f'missing kWh for period {period_desc} on meter {meter_id}')
                    if total_charge is None:
                        missing_fields.append(f'missing total_charge for period {period_desc} on meter {meter_id}')
    
    return {
        'is_valid': len(missing_fields) == 0,
        'missing_fields': missing_fields
    }


# ============ BILL SCREENSHOTS ============

def add_bill_screenshot(bill_id, file_path, original_filename=None, mime_type=None, page_hint=None):
    """Add a screenshot/annotation file for a bill."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                INSERT INTO bill_screenshots (bill_id, file_path, original_filename, mime_type, page_hint)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, bill_id, file_path, original_filename, mime_type, page_hint, uploaded_at
            ''', (bill_id, file_path, original_filename, mime_type, page_hint))
            result = cur.fetchone()
            conn.commit()
            return dict(result)
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_bill_screenshots(bill_id):
    """Get all screenshots/annotation files for a bill."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT id, bill_id, file_path, original_filename, mime_type, page_hint, uploaded_at
                FROM bill_screenshots
                WHERE bill_id = %s
                ORDER BY uploaded_at ASC
            ''', (bill_id,))
            return cur.fetchall()
    finally:
        conn.close()


def delete_bill_screenshot(screenshot_id):
    """Delete a screenshot by ID. Returns the file_path for cleanup."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # First get the file path
            cur.execute('SELECT file_path FROM bill_screenshots WHERE id = %s', (screenshot_id,))
            result = cur.fetchone()
            if not result:
                return None
            file_path = result['file_path']
            
            # Then delete
            cur.execute('DELETE FROM bill_screenshots WHERE id = %s', (screenshot_id,))
            conn.commit()
            return file_path
    finally:
        conn.close()


def get_screenshot_count(bill_id):
    """Get the number of screenshots for a bill."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM bill_screenshots WHERE bill_id = %s', (bill_id,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def mark_bill_ok(bill_id, reviewed_by=None, note=None):
    """Mark a bill as OK (reviewed). Returns updated record."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                UPDATE utility_bill_files
                SET review_status = 'ok',
                    processing_status = 'ok',
                    reviewed_at = CURRENT_TIMESTAMP,
                    reviewed_by = %s
                WHERE id = %s
                RETURNING id, project_id, filename, original_filename, review_status, 
                          processing_status, reviewed_at, reviewed_by
            ''', (reviewed_by, bill_id))
            result = cur.fetchone()
            conn.commit()
            return dict(result) if result else None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


# ============ NEW BILLS API FUNCTIONS ============

def delete_bills_for_file(bill_file_id):
    """Delete all bills and their TOU periods for a given bill file ID.
    Used to ensure idempotent re-extraction."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # First delete TOU periods for bills of this file
            cur.execute('''
                DELETE FROM bill_tou_periods
                WHERE bill_id IN (SELECT id FROM bills WHERE bill_file_id = %s)
            ''', (bill_file_id,))
            tou_deleted = cur.rowcount
            
            # Then delete the bills themselves
            cur.execute('DELETE FROM bills WHERE bill_file_id = %s', (bill_file_id,))
            bills_deleted = cur.rowcount
            
            conn.commit()
            if bills_deleted > 0:
                print(f"[bills_db] Deleted {bills_deleted} bill(s) and {tou_deleted} TOU period(s) for file {bill_file_id}")
            return bills_deleted
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def delete_account_if_empty(account_id):
    """
    Delete an account if it has no bills.
    Returns True if account was deleted, False otherwise.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Check if account has any bills
            cur.execute('SELECT COUNT(*) FROM bills WHERE account_id = %s', (account_id,))
            bill_count = cur.fetchone()[0]

            if bill_count == 0:
                # Delete the account (meters will cascade delete)
                cur.execute('DELETE FROM utility_accounts WHERE id = %s', (account_id,))
                conn.commit()
                print(f"[bills_db] Deleted empty account {account_id} with no bills")
                return True
            return False
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def delete_all_empty_accounts(project_id):
    """
    Delete all accounts in a project that have no bills.
    Returns count of accounts deleted.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Find all accounts with no bills for this project
            cur.execute('''
                SELECT ua.id, ua.account_number
                FROM utility_accounts ua
                LEFT JOIN bills b ON ua.id = b.account_id
                WHERE ua.project_id = %s
                GROUP BY ua.id, ua.account_number
                HAVING COUNT(b.id) = 0
            ''', (project_id,))
            empty_accounts = cur.fetchall()

            deleted_count = 0
            for account_row in empty_accounts:
                account_id = account_row[0]
                account_number = account_row[1]
                # Delete the account (meters will cascade delete)
                cur.execute('DELETE FROM utility_accounts WHERE id = %s', (account_id,))
                deleted_count += 1
                print(f"[bills_db] Deleted empty account {account_id} (account_number={account_number}) with no bills")

            conn.commit()
            if deleted_count > 0:
                print(f"[bills_db] Total empty accounts deleted: {deleted_count}")
            return deleted_count
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def insert_bill(bill_file_id, account_id, meter_id, utility_name, service_address, 
                rate_schedule, period_start, period_end, total_kwh, total_amount_due,
                energy_charges=None, demand_charges=None, other_charges=None, taxes=None,
                tou_on_kwh=None, tou_mid_kwh=None, tou_off_kwh=None, tou_super_off_kwh=None,
                tou_on_rate_dollars=None, tou_mid_rate_dollars=None, tou_off_rate_dollars=None, tou_super_off_rate_dollars=None,
                tou_on_cost=None, tou_mid_cost=None, tou_off_cost=None, tou_super_off_cost=None,
                due_date=None, service_type='electric'):
    """Insert a normalized bill record with TOU data. Returns bill ID."""
    conn = get_connection()
    try:
        # Calculate days in period
        days_in_period = None
        if period_start and period_end:
            from datetime import datetime
            if isinstance(period_start, str):
                ps = datetime.strptime(period_start, '%Y-%m-%d').date()
            else:
                ps = period_start
            if isinstance(period_end, str):
                pe = datetime.strptime(period_end, '%Y-%m-%d').date()
            else:
                pe = period_end
            days_in_period = (pe - ps).days + 1
        
        # Compute blended rate: total_amount_due / total_kwh
        blended_rate_dollars = None
        if total_kwh is not None and total_kwh > 0 and total_amount_due is not None:
            blended_rate_dollars = float(total_amount_due) / float(total_kwh)
        
        # Compute avg cost per day: total_amount_due / days_in_period
        avg_cost_per_day = None
        if days_in_period is not None and days_in_period > 0 and total_amount_due is not None:
            avg_cost_per_day = float(total_amount_due) / float(days_in_period)
        
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                INSERT INTO bills 
                (bill_file_id, account_id, meter_id, utility_name, service_address,
                 rate_schedule, period_start, period_end, days_in_period, total_kwh,
                 total_amount_due, energy_charges, demand_charges, other_charges, taxes,
                 tou_on_kwh, tou_mid_kwh, tou_off_kwh, tou_super_off_kwh,
                 tou_on_rate_dollars, tou_mid_rate_dollars, tou_off_rate_dollars, tou_super_off_rate_dollars,
                 tou_on_cost, tou_mid_cost, tou_off_cost, tou_super_off_cost,
                 blended_rate_dollars, avg_cost_per_day, due_date, service_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (bill_file_id, account_id, meter_id, utility_name, service_address,
                  rate_schedule, period_start, period_end, days_in_period, total_kwh,
                  total_amount_due, energy_charges, demand_charges, other_charges, taxes,
                  tou_on_kwh, tou_mid_kwh, tou_off_kwh, tou_super_off_kwh,
                  tou_on_rate_dollars, tou_mid_rate_dollars, tou_off_rate_dollars, tou_super_off_rate_dollars,
                  tou_on_cost, tou_mid_cost, tou_off_cost, tou_super_off_cost,
                  blended_rate_dollars, avg_cost_per_day, due_date, service_type))
            result = cur.fetchone()
            conn.commit()
            return result['id']
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def insert_bill_tou_period(bill_id, period, kwh, rate_dollars_per_kwh, est_cost_dollars=None):
    """Insert a TOU period for a bill. Returns period ID."""
    conn = get_connection()
    try:
        # If est_cost not provided, calculate it
        if est_cost_dollars is None and rate_dollars_per_kwh is not None and kwh is not None:
            est_cost_dollars = round(float(kwh) * float(rate_dollars_per_kwh), 2)
        
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                INSERT INTO bill_tou_periods (bill_id, period, kwh, rate_dollars_per_kwh, est_cost_dollars)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            ''', (bill_id, period, kwh, rate_dollars_per_kwh, est_cost_dollars))
            result = cur.fetchone()
            conn.commit()
            return result['id']
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_account_summary(account_id, months=12, service_filter=None):
    """
    Get summary for an account: combined totals + per-meter breakdown.
    Returns blended rate in dollars/kWh, avg cost per day, and TOU breakdown totals.
    Deduplicates bills by (meter_id, period_start, period_end, total_kwh, total_amount_due).
    
    Args:
        account_id: The account ID
        months: Number of months to include (default 12)
        service_filter: Optional filter ('electric' filters to electric/combined bills only)
    """
    conn = get_connection()
    try:
        # Build service filter condition
        if service_filter == 'electric':
            service_join = "JOIN utility_bill_files ubf ON b.bill_file_id = ubf.id"
            service_condition = "AND ubf.service_type IN ('electric', 'combined')"
        else:
            service_join = ""
            service_condition = ""
        
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f'''
                WITH dedupe AS (
                    SELECT DISTINCT ON (b.meter_id, b.period_start, b.period_end, b.total_kwh, b.total_amount_due)
                        b.*
                    FROM bills b
                    {service_join}
                    WHERE b.account_id = %s
                    AND b.period_end >= (CURRENT_DATE - INTERVAL '%s months')
                    {service_condition}
                    ORDER BY b.meter_id, b.period_start, b.period_end, b.total_kwh, b.total_amount_due, b.id
                )
                SELECT 
                    SUM(total_kwh) AS total_kwh,
                    SUM(total_amount_due) AS total_cost,
                    SUM(days_in_period) AS total_days,
                    COUNT(*) AS bill_count,
                    SUM(tou_on_kwh) AS tou_on_kwh,
                    SUM(tou_mid_kwh) AS tou_mid_kwh,
                    SUM(tou_off_kwh) AS tou_off_kwh,
                    SUM(tou_super_off_kwh) AS tou_super_off_kwh,
                    SUM(tou_on_cost) AS tou_on_cost,
                    SUM(tou_mid_cost) AS tou_mid_cost,
                    SUM(tou_off_cost) AS tou_off_cost,
                    SUM(tou_super_off_cost) AS tou_super_off_cost
                FROM dedupe
            ''', (account_id, months))
            combined = cur.fetchone()
            
            combined_data = {
                'sumKwh': float(combined['total_kwh']) if combined['total_kwh'] else 0,
                'sumCost': float(combined['total_cost']) if combined['total_cost'] else 0,
                'totalKwh': float(combined['total_kwh']) if combined['total_kwh'] else 0,
                'totalCost': float(combined['total_cost']) if combined['total_cost'] else 0,
                'blendedRateDollars': 0,
                'avgCostPerDay': 0,
                'avgCostPerDayDollars': 0,
                'billCount': combined['bill_count'] or 0,
                'tou': {
                    'onPeakKwh': float(combined['tou_on_kwh']) if combined['tou_on_kwh'] else None,
                    'midPeakKwh': float(combined['tou_mid_kwh']) if combined['tou_mid_kwh'] else None,
                    'offPeakKwh': float(combined['tou_off_kwh']) if combined['tou_off_kwh'] else None,
                    'superOffPeakKwh': float(combined['tou_super_off_kwh']) if combined['tou_super_off_kwh'] else None,
                    'onPeakCost': float(combined['tou_on_cost']) if combined['tou_on_cost'] else None,
                    'midPeakCost': float(combined['tou_mid_cost']) if combined['tou_mid_cost'] else None,
                    'offPeakCost': float(combined['tou_off_cost']) if combined['tou_off_cost'] else None,
                    'superOffPeakCost': float(combined['tou_super_off_cost']) if combined['tou_super_off_cost'] else None
                }
            }
            if combined_data['sumKwh'] > 0:
                combined_data['blendedRateDollars'] = combined_data['sumCost'] / combined_data['sumKwh']
            if combined['total_days'] and combined['total_days'] > 0:
                combined_data['avgCostPerDay'] = combined_data['sumCost'] / float(combined['total_days'])
                combined_data['avgCostPerDayDollars'] = combined_data['avgCostPerDay']
            
            cur.execute(f'''
                WITH dedupe AS (
                    SELECT DISTINCT ON (b.meter_id, b.period_start, b.period_end, b.total_kwh, b.total_amount_due)
                        b.*
                    FROM bills b
                    {service_join}
                    WHERE b.account_id = %s
                    AND b.period_end >= (CURRENT_DATE - INTERVAL '%s months')
                    {service_condition}
                    ORDER BY b.meter_id, b.period_start, b.period_end, b.total_kwh, b.total_amount_due, b.id
                )
                SELECT 
                    d.meter_id,
                    m.meter_number,
                    SUM(d.total_kwh) AS total_kwh,
                    SUM(d.total_amount_due) AS total_cost,
                    SUM(d.days_in_period) AS total_days,
                    COUNT(*) AS bill_count,
                    SUM(d.tou_on_kwh) AS tou_on_kwh,
                    SUM(d.tou_mid_kwh) AS tou_mid_kwh,
                    SUM(d.tou_off_kwh) AS tou_off_kwh,
                    SUM(d.tou_super_off_kwh) AS tou_super_off_kwh,
                    SUM(d.tou_on_cost) AS tou_on_cost,
                    SUM(d.tou_mid_cost) AS tou_mid_cost,
                    SUM(d.tou_off_cost) AS tou_off_cost,
                    SUM(d.tou_super_off_cost) AS tou_super_off_cost
                FROM dedupe d
                JOIN utility_meters m ON d.meter_id = m.id
                GROUP BY d.meter_id, m.meter_number
                ORDER BY m.meter_number
            ''', (account_id, months))
            meters_raw = cur.fetchall()
            
            meters = []
            for m in meters_raw:
                meter_data = {
                    'meterId': m['meter_id'],
                    'meterNumber': m['meter_number'],
                    'sumKwh': float(m['total_kwh']) if m['total_kwh'] else 0,
                    'sumCost': float(m['total_cost']) if m['total_cost'] else 0,
                    'totalKwh': float(m['total_kwh']) if m['total_kwh'] else 0,
                    'totalCost': float(m['total_cost']) if m['total_cost'] else 0,
                    'blendedRateDollars': 0,
                    'avgCostPerDay': 0,
                    'avgCostPerDayDollars': 0,
                    'billCount': m['bill_count'] or 0,
                    'tou': {
                        'onPeakKwh': float(m['tou_on_kwh']) if m['tou_on_kwh'] else None,
                        'midPeakKwh': float(m['tou_mid_kwh']) if m['tou_mid_kwh'] else None,
                        'offPeakKwh': float(m['tou_off_kwh']) if m['tou_off_kwh'] else None,
                        'superOffPeakKwh': float(m['tou_super_off_kwh']) if m['tou_super_off_kwh'] else None,
                        'onPeakCost': float(m['tou_on_cost']) if m['tou_on_cost'] else None,
                        'midPeakCost': float(m['tou_mid_cost']) if m['tou_mid_cost'] else None,
                        'offPeakCost': float(m['tou_off_cost']) if m['tou_off_cost'] else None,
                        'superOffPeakCost': float(m['tou_super_off_cost']) if m['tou_super_off_cost'] else None
                    }
                }
                if meter_data['sumKwh'] > 0:
                    meter_data['blendedRateDollars'] = meter_data['sumCost'] / meter_data['sumKwh']
                if m['total_days'] and m['total_days'] > 0:
                    meter_data['avgCostPerDay'] = meter_data['sumCost'] / float(m['total_days'])
                    meter_data['avgCostPerDayDollars'] = meter_data['avgCostPerDay']
                meters.append(meter_data)
            
            # Get bills for each meter
            for meter in meters:
                meter_id = meter['meterId']
                cur.execute(f'''
                    SELECT
                        b.id, b.period_start, b.period_end, b.days_in_period,
                        b.total_kwh, b.total_amount_due, b.blended_rate_dollars,
                        b.service_address, b.rate_schedule, b.due_date,
                        b.energy_charges, b.demand_charges, b.other_charges, b.taxes,
                        b.tou_on_kwh, b.tou_mid_kwh, b.tou_off_kwh, b.tou_super_off_kwh,
                        b.tou_on_rate_dollars, b.tou_mid_rate_dollars, b.tou_off_rate_dollars, b.tou_super_off_rate_dollars,
                        b.tou_on_cost, b.tou_mid_cost, b.tou_off_cost, b.tou_super_off_cost
                    FROM bills b
                    {service_join}
                    WHERE b.meter_id = %s
                    AND b.period_end >= (CURRENT_DATE - INTERVAL '%s months')
                    {service_condition}
                    ORDER BY b.period_end DESC
                ''', (meter_id, months))
                bills_raw = cur.fetchall()
                
                bills = []
                for b in bills_raw:
                    total_kwh = float(b['total_kwh']) if b['total_kwh'] else 0
                    total_cost = float(b['total_amount_due']) if b['total_amount_due'] else 0
                    days = b['days_in_period'] or 1
                    
                    # Format period label
                    period_label = ''
                    if b['period_end']:
                        from datetime import datetime
                        pe = b['period_end']
                        if isinstance(pe, str):
                            pe = datetime.strptime(pe, '%Y-%m-%d').date()
                        period_label = pe.strftime('%b %Y')
                    
                    blended_rate = float(b['blended_rate_dollars']) if b['blended_rate_dollars'] else (total_cost / total_kwh if total_kwh > 0 else 0)
                    
                    bills.append({
                        'billId': b['id'],
                        'periodLabel': period_label,
                        'periodStart': str(b['period_start']) if b['period_start'] else None,
                        'periodEnd': str(b['period_end']) if b['period_end'] else None,
                        'daysInPeriod': days,
                        'totalKwh': total_kwh,
                        'totalAmountDue': total_cost,
                        'blendedRateDollars': blended_rate,
                        'serviceAddress': b['service_address'],
                        'rateSchedule': b['rate_schedule'],
                        'dueDate': str(b['due_date']) if b['due_date'] else None
                    })
                
                meter['bills'] = bills
            
            return {
                'accountId': account_id,
                'months': months,
                'combined': combined_data,
                'meters': meters
            }
    finally:
        conn.close()


def get_meter_bills(meter_id, months=12):
    """
    Get list of bills for a meter with summary data.
    Returns bills ordered by period_end DESC, including TOU breakdown.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT 
                    id, bill_file_id, account_id, meter_id, utility_name,
                    service_address, rate_schedule, period_start, period_end,
                    days_in_period, total_kwh, total_amount_due,
                    energy_charges, demand_charges, other_charges, taxes,
                    tou_on_kwh, tou_mid_kwh, tou_off_kwh, tou_super_off_kwh,
                    tou_on_rate_dollars, tou_mid_rate_dollars, tou_off_rate_dollars, tou_super_off_rate_dollars,
                    tou_on_cost, tou_mid_cost, tou_off_cost, tou_super_off_cost,
                    blended_rate_dollars, avg_cost_per_day
                FROM bills
                WHERE meter_id = %s
                AND period_end >= (CURRENT_DATE - INTERVAL '%s months')
                ORDER BY period_end DESC
            ''', (meter_id, months))
            bills_raw = cur.fetchall()
            
            bills = []
            for b in bills_raw:
                total_kwh = float(b['total_kwh']) if b['total_kwh'] else 0
                total_cost = float(b['total_amount_due']) if b['total_amount_due'] else 0
                days = b['days_in_period'] or 1
                
                # Format period label
                period_label = ''
                if b['period_end']:
                    from datetime import datetime
                    pe = b['period_end']
                    if isinstance(pe, str):
                        pe = datetime.strptime(pe, '%Y-%m-%d').date()
                    period_label = pe.strftime('%b %Y')
                
                # Use stored blended_rate if available, else compute
                blended_rate = float(b['blended_rate_dollars']) if b['blended_rate_dollars'] else (total_cost / total_kwh if total_kwh > 0 else 0)
                avg_cost_day = float(b['avg_cost_per_day']) if b['avg_cost_per_day'] else (round(total_cost / days, 2) if days > 0 else 0)
                
                bill_data = {
                    'billId': b['id'],
                    'periodLabel': period_label,
                    'periodStart': str(b['period_start']) if b['period_start'] else None,
                    'periodEnd': str(b['period_end']) if b['period_end'] else None,
                    'daysInPeriod': days,
                    'totalKwh': total_kwh,
                    'totalAmountDue': total_cost,
                    'avgKwhPerDay': round(total_kwh / days, 1) if days > 0 else 0,
                    'blendedRateDollars': blended_rate,
                    'avgCostPerDay': avg_cost_day,
                    'avgCostPerDayDollars': avg_cost_day,
                    'tou': {
                        'onPeakKwh': float(b['tou_on_kwh']) if b['tou_on_kwh'] else None,
                        'midPeakKwh': float(b['tou_mid_kwh']) if b['tou_mid_kwh'] else None,
                        'offPeakKwh': float(b['tou_off_kwh']) if b['tou_off_kwh'] else None,
                        'superOffPeakKwh': float(b['tou_super_off_kwh']) if b['tou_super_off_kwh'] else None,
                        'onPeakRateDollars': float(b['tou_on_rate_dollars']) if b['tou_on_rate_dollars'] else None,
                        'midPeakRateDollars': float(b['tou_mid_rate_dollars']) if b['tou_mid_rate_dollars'] else None,
                        'offPeakRateDollars': float(b['tou_off_rate_dollars']) if b['tou_off_rate_dollars'] else None,
                        'superOffPeakRateDollars': float(b['tou_super_off_rate_dollars']) if b['tou_super_off_rate_dollars'] else None,
                        'onPeakCost': float(b['tou_on_cost']) if b['tou_on_cost'] else None,
                        'midPeakCost': float(b['tou_mid_cost']) if b['tou_mid_cost'] else None,
                        'offPeakCost': float(b['tou_off_cost']) if b['tou_off_cost'] else None,
                        'superOffPeakCost': float(b['tou_super_off_cost']) if b['tou_super_off_cost'] else None
                    }
                }
                bills.append(bill_data)
            
            return {
                'meterId': meter_id,
                'months': months,
                'bills': bills
            }
    finally:
        conn.close()


def get_bill_detail(bill_id):
    """
    Get full detail for a single bill including TOU fields and source file metadata.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get bill with file metadata and TOU columns
            cur.execute('''
                SELECT 
                    b.id, b.bill_file_id, b.account_id, b.meter_id, b.utility_name,
                    b.service_address, b.rate_schedule, b.period_start, b.period_end,
                    b.days_in_period, b.total_kwh, b.total_amount_due, b.due_date,
                    b.energy_charges, b.demand_charges, b.other_charges, b.taxes,
                    b.tou_on_kwh, b.tou_mid_kwh, b.tou_off_kwh, b.tou_super_off_kwh,
                    b.tou_on_rate_dollars, b.tou_mid_rate_dollars, b.tou_off_rate_dollars, b.tou_super_off_rate_dollars,
                    b.tou_on_cost, b.tou_mid_cost, b.tou_off_cost, b.tou_super_off_cost,
                    b.blended_rate_dollars, b.avg_cost_per_day,
                    a.account_number,
                    m.meter_number,
                    f.original_filename, f.upload_date, f.file_path, f.extraction_payload
                FROM bills b
                JOIN utility_accounts a ON b.account_id = a.id
                JOIN utility_meters m ON b.meter_id = m.id
                LEFT JOIN utility_bill_files f ON b.bill_file_id = f.id
                WHERE b.id = %s
            ''', (bill_id,))
            b = cur.fetchone()
            
            if not b:
                return None
            
            total_kwh = float(b['total_kwh']) if b['total_kwh'] else 0
            total_cost = float(b['total_amount_due']) if b['total_amount_due'] else 0
            days = b['days_in_period'] or 1
            
            # Use stored blended_rate if available, else compute
            blended_rate = float(b['blended_rate_dollars']) if b['blended_rate_dollars'] else (total_cost / total_kwh if total_kwh > 0 else 0)
            avg_cost_day = float(b['avg_cost_per_day']) if b['avg_cost_per_day'] else (round(total_cost / days, 2) if days > 0 else 0)
            
            payload = b.get('extraction_payload')
            if payload and isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except:
                    payload = {}
            if not payload:
                payload = {}
            detailed_data = payload.get('detailed_data', {}) if payload else {}
            
            due_date = b['due_date']
            if not due_date:
                due_date = detailed_data.get('due_date') or payload.get('due_date')
            
            service_address = b['service_address']
            if not service_address:
                service_address = detailed_data.get('service_address') or payload.get('service_address')
                meters = payload.get('meters', [])
                if not service_address and meters:
                    service_address = meters[0].get('service_address')
            
            rate_schedule = b['rate_schedule']
            if not rate_schedule:
                rate_schedule = detailed_data.get('rate_schedule') or payload.get('rate_schedule')
            
            cur.execute('''
                SELECT period, kwh, rate_dollars_per_kwh, est_cost_dollars
                FROM bill_tou_periods
                WHERE bill_id = %s
                ORDER BY 
                    CASE period 
                        WHEN 'On-Peak' THEN 1 
                        WHEN 'Mid-Peak' THEN 2 
                        WHEN 'Off-Peak' THEN 3 
                        ELSE 4 
                    END
            ''', (bill_id,))
            tou_raw = cur.fetchall()
            
            tou_periods = []
            if tou_raw:
                for t in tou_raw:
                    tou_periods.append({
                        'period': t['period'],
                        'kwh': float(t['kwh']) if t['kwh'] else 0,
                        'rateDollarsPerKwh': float(t['rate_dollars_per_kwh']) if t['rate_dollars_per_kwh'] else 0,
                        'estCostDollars': float(t['est_cost_dollars']) if t['est_cost_dollars'] else 0
                    })
            else:
                if b['tou_on_kwh'] is not None:
                    tou_periods.append({
                        'period': 'On-Peak',
                        'kwh': float(b['tou_on_kwh']),
                        'rateDollarsPerKwh': float(b['tou_on_rate_dollars']) if b['tou_on_rate_dollars'] else 0,
                        'estCostDollars': float(b['tou_on_cost']) if b['tou_on_cost'] else 0
                    })
                if b['tou_mid_kwh'] is not None:
                    tou_periods.append({
                        'period': 'Mid-Peak',
                        'kwh': float(b['tou_mid_kwh']),
                        'rateDollarsPerKwh': float(b['tou_mid_rate_dollars']) if b['tou_mid_rate_dollars'] else 0,
                        'estCostDollars': float(b['tou_mid_cost']) if b['tou_mid_cost'] else 0
                    })
                if b['tou_off_kwh'] is not None:
                    tou_periods.append({
                        'period': 'Off-Peak',
                        'kwh': float(b['tou_off_kwh']),
                        'rateDollarsPerKwh': float(b['tou_off_rate_dollars']) if b['tou_off_rate_dollars'] else 0,
                        'estCostDollars': float(b['tou_off_cost']) if b['tou_off_cost'] else 0
                    })
                if b['tou_super_off_kwh'] is not None:
                    tou_periods.append({
                        'period': 'Super Off-Peak',
                        'kwh': float(b['tou_super_off_kwh']),
                        'rateDollarsPerKwh': float(b['tou_super_off_rate_dollars']) if b['tou_super_off_rate_dollars'] else 0,
                        'estCostDollars': float(b['tou_super_off_cost']) if b['tou_super_off_cost'] else 0
                    })
            
            return {
                'billId': b['id'],
                'billFileId': b['bill_file_id'],
                'accountId': b['account_id'],
                'accountNumber': b['account_number'],
                'meterId': b['meter_id'],
                'meterNumber': b['meter_number'],
                'utilityName': b['utility_name'],
                'serviceAddress': service_address,
                'rateSchedule': rate_schedule,
                'periodStart': str(b['period_start']) if b['period_start'] else None,
                'periodEnd': str(b['period_end']) if b['period_end'] else None,
                'dueDate': str(due_date) if due_date else None,
                'daysInPeriod': days,
                'totalKwh': total_kwh,
                'totalAmountDue': total_cost,
                'avgKwhPerDay': round(total_kwh / days, 1) if days > 0 else 0,
                'blendedRateDollars': blended_rate,
                'avgCostPerDay': avg_cost_day,
                'avgCostPerDayDollars': avg_cost_day,
                'charges': {
                    'energyCharges': float(b['energy_charges']) if b['energy_charges'] else 0,
                    'demandCharges': float(b['demand_charges']) if b['demand_charges'] else 0,
                    'otherCharges': float(b['other_charges']) if b['other_charges'] else 0,
                    'taxes': float(b['taxes']) if b['taxes'] else 0
                },
                'tou': {
                    'onPeakKwh': float(b['tou_on_kwh']) if b['tou_on_kwh'] else None,
                    'midPeakKwh': float(b['tou_mid_kwh']) if b['tou_mid_kwh'] else None,
                    'offPeakKwh': float(b['tou_off_kwh']) if b['tou_off_kwh'] else None,
                    'superOffPeakKwh': float(b['tou_super_off_kwh']) if b['tou_super_off_kwh'] else None,
                    'onPeakRateDollars': float(b['tou_on_rate_dollars']) if b['tou_on_rate_dollars'] else None,
                    'midPeakRateDollars': float(b['tou_mid_rate_dollars']) if b['tou_mid_rate_dollars'] else None,
                    'offPeakRateDollars': float(b['tou_off_rate_dollars']) if b['tou_off_rate_dollars'] else None,
                    'superOffPeakRateDollars': float(b['tou_super_off_rate_dollars']) if b['tou_super_off_rate_dollars'] else None,
                    'onPeakCost': float(b['tou_on_cost']) if b['tou_on_cost'] else None,
                    'midPeakCost': float(b['tou_mid_cost']) if b['tou_mid_cost'] else None,
                    'offPeakCost': float(b['tou_off_cost']) if b['tou_off_cost'] else None,
                    'superOffPeakCost': float(b['tou_super_off_cost']) if b['tou_super_off_cost'] else None
                },
                'touPeriods': tou_periods,
                'sourceFile': {
                    'originalFilename': b['original_filename'],
                    'uploadDate': b['upload_date'].isoformat() if b['upload_date'] else None
                } if b['original_filename'] else None
            }
    finally:
        conn.close()


def get_meter_months(account_id, meter_id, months=12):
    """
    Get month-by-month breakdown for a specific meter under an account.
    Returns each billing period with total_kwh, total_cost, blended_rate, avg_cost_per_day, and TOU breakdown.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT 
                    id, period_start, period_end, days_in_period,
                    total_kwh, total_amount_due,
                    tou_on_kwh, tou_mid_kwh, tou_off_kwh, tou_super_off_kwh,
                    tou_on_rate_dollars, tou_mid_rate_dollars, tou_off_rate_dollars, tou_super_off_rate_dollars,
                    tou_on_cost, tou_mid_cost, tou_off_cost, tou_super_off_cost,
                    blended_rate_dollars, avg_cost_per_day
                FROM bills
                WHERE account_id = %s AND meter_id = %s
                AND period_end >= (CURRENT_DATE - INTERVAL '%s months')
                ORDER BY period_end DESC
            ''', (account_id, meter_id, months))
            bills_raw = cur.fetchall()
            
            monthly_data = []
            for b in bills_raw:
                total_kwh = float(b['total_kwh']) if b['total_kwh'] else 0
                total_cost = float(b['total_amount_due']) if b['total_amount_due'] else 0
                days = b['days_in_period'] or 1
                
                # Format period label
                period_label = ''
                if b['period_end']:
                    pe = b['period_end']
                    if isinstance(pe, str):
                        pe = datetime.strptime(pe, '%Y-%m-%d').date()
                    period_label = pe.strftime('%b %Y')
                
                # Use stored blended_rate if available, else compute
                blended_rate = float(b['blended_rate_dollars']) if b['blended_rate_dollars'] else (total_cost / total_kwh if total_kwh > 0 else 0)
                avg_cost_day = float(b['avg_cost_per_day']) if b['avg_cost_per_day'] else (round(total_cost / days, 2) if days > 0 else 0)
                
                month_data = {
                    'billId': b['id'],
                    'period': period_label,
                    'periodStart': str(b['period_start']) if b['period_start'] else None,
                    'periodEnd': str(b['period_end']) if b['period_end'] else None,
                    'daysInPeriod': days,
                    'totalKwh': total_kwh,
                    'totalCost': total_cost,
                    'blendedRate': blended_rate,
                    'blendedRateDollars': blended_rate,
                    'avgCostPerDay': avg_cost_day,
                    'avgCostPerDayDollars': avg_cost_day,
                    'tou': {
                        'onPeakKwh': float(b['tou_on_kwh']) if b['tou_on_kwh'] else None,
                        'midPeakKwh': float(b['tou_mid_kwh']) if b['tou_mid_kwh'] else None,
                        'offPeakKwh': float(b['tou_off_kwh']) if b['tou_off_kwh'] else None,
                        'superOffPeakKwh': float(b['tou_super_off_kwh']) if b['tou_super_off_kwh'] else None,
                        'onPeakRateDollars': float(b['tou_on_rate_dollars']) if b['tou_on_rate_dollars'] else None,
                        'midPeakRateDollars': float(b['tou_mid_rate_dollars']) if b['tou_mid_rate_dollars'] else None,
                        'offPeakRateDollars': float(b['tou_off_rate_dollars']) if b['tou_off_rate_dollars'] else None,
                        'superOffPeakRateDollars': float(b['tou_super_off_rate_dollars']) if b['tou_super_off_rate_dollars'] else None,
                        'onPeakCost': float(b['tou_on_cost']) if b['tou_on_cost'] else None,
                        'midPeakCost': float(b['tou_mid_cost']) if b['tou_mid_cost'] else None,
                        'offPeakCost': float(b['tou_off_cost']) if b['tou_off_cost'] else None,
                        'superOffPeakCost': float(b['tou_super_off_cost']) if b['tou_super_off_cost'] else None
                    }
                }
                monthly_data.append(month_data)
            
            return {
                'accountId': account_id,
                'meterId': meter_id,
                'months': months,
                'data': monthly_data
            }
    finally:
        conn.close()


def get_bill_by_id(bill_id):
    """
    Get a single bill record by ID with all fields.
    Returns dict with bill data or None if not found.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''
                SELECT 
                    b.id, b.bill_file_id, b.account_id, b.meter_id,
                    b.utility_name, b.service_address, b.rate_schedule,
                    b.period_start, b.period_end, b.days_in_period,
                    b.total_kwh, b.total_amount_due,
                    b.energy_charges, b.demand_charges, b.other_charges, b.taxes,
                    b.tou_on_kwh, b.tou_mid_kwh, b.tou_off_kwh, b.tou_super_off_kwh,
                    b.tou_on_rate_dollars, b.tou_mid_rate_dollars, b.tou_off_rate_dollars, b.tou_super_off_rate_dollars,
                    b.tou_on_cost, b.tou_mid_cost, b.tou_off_cost, b.tou_super_off_cost,
                    b.blended_rate_dollars, b.avg_cost_per_day,
                    b.created_at,
                    f.original_filename, f.upload_date, f.missing_fields
                FROM bills b
                LEFT JOIN utility_bill_files f ON b.bill_file_id = f.id
                WHERE b.id = %s
            ''', (bill_id,))
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)
    finally:
        conn.close()


def get_bill_review_data(bill_id):
    """
    Get bill data formatted for review UI.
    Returns billId, list of missing fields with labels, and currentValues.
    """
    bill = get_bill_by_id(bill_id)
    if not bill:
        return None
    
    # Field label mapping
    field_labels = {
        'utility_name': 'Utility Name',
        'account_number': 'Account Number',
        'total_kwh': 'Total kWh',
        'total_amount_due': 'Total Amount Due',
        'rate_schedule': 'Rate Schedule',
        'service_address': 'Service Address',
        'period_start': 'Period Start',
        'period_end': 'Period End',
        'days_in_period': 'Days in Period',
        'energy_charges': 'Energy Charges',
        'demand_charges': 'Demand Charges',
        'other_charges': 'Other Charges',
        'taxes': 'Taxes',
        'tou_on_kwh': 'TOU On-Peak kWh',
        'tou_mid_kwh': 'TOU Mid-Peak kWh',
        'tou_off_kwh': 'TOU Off-Peak kWh',
        'tou_on_rate_dollars': 'TOU On-Peak Rate',
        'tou_mid_rate_dollars': 'TOU Mid-Peak Rate',
        'tou_off_rate_dollars': 'TOU Off-Peak Rate'
    }
    
    # Get missing_fields from the bill_file
    missing_fields = bill.get('missing_fields') or []
    if isinstance(missing_fields, str):
        import json
        try:
            missing_fields = json.loads(missing_fields)
        except:
            missing_fields = []
    
    missing_list = []
    for field in missing_fields:
        label = field_labels.get(field, field.replace('_', ' ').title())
        missing_list.append({'field': field, 'label': label})
    
    # Build currentValues
    current_values = {
        'total_kwh': float(bill['total_kwh']) if bill['total_kwh'] else None,
        'total_amount_due': float(bill['total_amount_due']) if bill['total_amount_due'] else None,
        'rate_schedule': bill['rate_schedule'],
        'service_address': bill['service_address'],
        'utility_name': bill['utility_name'],
        'period_start': str(bill['period_start']) if bill['period_start'] else None,
        'period_end': str(bill['period_end']) if bill['period_end'] else None,
        'days_in_period': bill['days_in_period'],
        'energy_charges': float(bill['energy_charges']) if bill['energy_charges'] else None,
        'demand_charges': float(bill['demand_charges']) if bill['demand_charges'] else None,
        'other_charges': float(bill['other_charges']) if bill['other_charges'] else None,
        'taxes': float(bill['taxes']) if bill['taxes'] else None,
        'tou_on_kwh': float(bill['tou_on_kwh']) if bill['tou_on_kwh'] else None,
        'tou_mid_kwh': float(bill['tou_mid_kwh']) if bill['tou_mid_kwh'] else None,
        'tou_off_kwh': float(bill['tou_off_kwh']) if bill['tou_off_kwh'] else None,
        'tou_on_rate_dollars': float(bill['tou_on_rate_dollars']) if bill['tou_on_rate_dollars'] else None,
        'tou_mid_rate_dollars': float(bill['tou_mid_rate_dollars']) if bill['tou_mid_rate_dollars'] else None,
        'tou_off_rate_dollars': float(bill['tou_off_rate_dollars']) if bill['tou_off_rate_dollars'] else None,
        'tou_on_cost': float(bill['tou_on_cost']) if bill['tou_on_cost'] else None,
        'tou_mid_cost': float(bill['tou_mid_cost']) if bill['tou_mid_cost'] else None,
        'tou_off_cost': float(bill['tou_off_cost']) if bill['tou_off_cost'] else None,
        'blended_rate_dollars': float(bill['blended_rate_dollars']) if bill['blended_rate_dollars'] else None,
        'avg_cost_per_day': float(bill['avg_cost_per_day']) if bill['avg_cost_per_day'] else None,
        'bill_file_id': bill['bill_file_id'],
        'account_id': bill['account_id'],
        'meter_id': bill['meter_id']
    }
    
    return {
        'billId': bill_id,
        'missing': missing_list,
        'currentValues': current_values
    }


def update_bill(bill_id, updates):
    """
    Update a bill record with the provided fields.
    Automatically recomputes blended_rate_dollars and avg_cost_per_day.
    
    Args:
        bill_id: The bill ID to update
        updates: Dict with field names and new values
    
    Returns:
        Updated bill record or None if not found
    """
    # Get current bill to have all values for recalculation
    current_bill = get_bill_by_id(bill_id)
    if not current_bill:
        return None
    
    # Allowed update fields
    allowed_fields = {
        'total_kwh', 'total_amount_due', 'rate_schedule', 'service_address',
        'utility_name', 'period_start', 'period_end', 'days_in_period',
        'energy_charges', 'demand_charges', 'other_charges', 'taxes',
        'tou_on_kwh', 'tou_mid_kwh', 'tou_off_kwh',
        'tou_on_rate_dollars', 'tou_mid_rate_dollars', 'tou_off_rate_dollars',
        'tou_on_cost', 'tou_mid_cost', 'tou_off_cost'
    }
    
    # Filter to only allowed fields
    filtered_updates = {k: v for k, v in updates.items() if k in allowed_fields}
    
    if not filtered_updates:
        return current_bill
    
    # Merge updates with current values for recalculation
    merged = dict(current_bill)
    for k, v in filtered_updates.items():
        merged[k] = v
    
    # Recompute blended_rate_dollars and avg_cost_per_day
    total_kwh = merged.get('total_kwh')
    total_amount_due = merged.get('total_amount_due')
    days_in_period = merged.get('days_in_period')
    
    blended_rate = None
    if total_kwh and total_amount_due and float(total_kwh) > 0:
        blended_rate = float(total_amount_due) / float(total_kwh)
    
    avg_cost_per_day = None
    if days_in_period and total_amount_due and int(days_in_period) > 0:
        avg_cost_per_day = float(total_amount_due) / float(days_in_period)
    
    # Add computed fields to updates
    filtered_updates['blended_rate_dollars'] = blended_rate
    filtered_updates['avg_cost_per_day'] = avg_cost_per_day
    
    # Compute TOU costs if rate and kwh provided
    if 'tou_on_kwh' in filtered_updates or 'tou_on_rate_dollars' in filtered_updates:
        on_kwh = filtered_updates.get('tou_on_kwh', merged.get('tou_on_kwh'))
        on_rate = filtered_updates.get('tou_on_rate_dollars', merged.get('tou_on_rate_dollars'))
        if on_kwh is not None and on_rate is not None:
            filtered_updates['tou_on_cost'] = round(float(on_kwh) * float(on_rate), 2)
    
    if 'tou_mid_kwh' in filtered_updates or 'tou_mid_rate_dollars' in filtered_updates:
        mid_kwh = filtered_updates.get('tou_mid_kwh', merged.get('tou_mid_kwh'))
        mid_rate = filtered_updates.get('tou_mid_rate_dollars', merged.get('tou_mid_rate_dollars'))
        if mid_kwh is not None and mid_rate is not None:
            filtered_updates['tou_mid_cost'] = round(float(mid_kwh) * float(mid_rate), 2)
    
    if 'tou_off_kwh' in filtered_updates or 'tou_off_rate_dollars' in filtered_updates:
        off_kwh = filtered_updates.get('tou_off_kwh', merged.get('tou_off_kwh'))
        off_rate = filtered_updates.get('tou_off_rate_dollars', merged.get('tou_off_rate_dollars'))
        if off_kwh is not None and off_rate is not None:
            filtered_updates['tou_off_cost'] = round(float(off_kwh) * float(off_rate), 2)
    
    # Build SQL update
    conn = get_connection()
    try:
        set_clauses = []
        values = []
        for field, value in filtered_updates.items():
            set_clauses.append(f"{field} = %s")
            values.append(value)
        
        values.append(bill_id)
        
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f'''
                UPDATE bills 
                SET {', '.join(set_clauses)}
                WHERE id = %s
                RETURNING id
            ''', values)
            result = cur.fetchone()
            conn.commit()
            
            if result:
                return get_bill_by_id(bill_id)
            return None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def recompute_bill_file_missing_fields(bill_file_id):
    """
    Recompute missing fields for a bill file based on current bill data.
    Updates the utility_bill_files record with new missing_fields and review_status.
    
    Returns:
        List of missing field names
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get all bills for this file
            cur.execute('''
                SELECT 
                    b.utility_name, b.service_address, b.rate_schedule,
                    b.period_start, b.period_end,
                    b.total_kwh, b.total_amount_due,
                    m.meter_number
                FROM bills b
                LEFT JOIN utility_meters m ON b.meter_id = m.id
                WHERE b.bill_file_id = %s
            ''', (bill_file_id,))
            bills = cur.fetchall()
            
            if not bills:
                return ["no_bills_for_file"]
            
            missing = []
            
            # Check first bill for common fields
            first_bill = bills[0]
            if not first_bill.get('utility_name'):
                missing.append('utility_name')
            if not first_bill.get('rate_schedule'):
                missing.append('rate_schedule')
            
            # Check each bill
            for i, bill in enumerate(bills):
                if bill.get('total_kwh') is None:
                    missing.append(f'total_kwh')
                if bill.get('total_amount_due') is None:
                    missing.append(f'total_amount_due')
                if not bill.get('period_start'):
                    missing.append(f'period_start')
                if not bill.get('period_end'):
                    missing.append(f'period_end')
                if not bill.get('meter_number'):
                    missing.append(f'meter_number')
                if not bill.get('service_address'):
                    missing.append(f'service_address')
            
            # Deduplicate
            missing = list(set(missing))
            
            # Update the bill file
            review_status = 'needs_review' if missing else 'ok'
            cur.execute('''
                UPDATE utility_bill_files 
                SET missing_fields = %s, review_status = %s
                WHERE id = %s
            ''', (Json(missing), review_status, bill_file_id))
            conn.commit()
            
            return missing
    finally:
        conn.close()


def clone_bills_for_project(old_project_id, new_project_id):
    """
    Clone all utility bill data from one project to another.
    
    This includes:
    - utility_bill_files entries (referencing the same file paths)
    - utility_accounts entries
    - utility_meters entries (linked to new accounts)
    - bills entries (linked to new accounts/meters/files)
    - bill_tou_periods entries (linked to new bills)
    - bill_screenshots entries (linked to new bill files)
    
    Args:
        old_project_id: The source project ID to clone from
        new_project_id: The target project ID to clone to
        
    Returns:
        dict with counts: {files, accounts, meters, bills, tou_periods, screenshots}
    """
    conn = get_connection()
    try:
        counts = {'files': 0, 'accounts': 0, 'meters': 0, 'bills': 0, 'tou_periods': 0, 'screenshots': 0}
        
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Mapping from old IDs to new IDs
            file_id_map = {}
            account_id_map = {}
            meter_id_map = {}
            bill_id_map = {}
            
            # 1. Clone utility_bill_files
            cur.execute('''
                SELECT id, filename, original_filename, file_path, file_size, mime_type,
                       processed, processing_status, review_status, extraction_payload, missing_fields
                FROM utility_bill_files
                WHERE project_id = %s
            ''', (old_project_id,))
            old_files = cur.fetchall()
            
            for f in old_files:
                cur.execute('''
                    INSERT INTO utility_bill_files 
                    (project_id, filename, original_filename, file_path, file_size, mime_type,
                     processed, processing_status, review_status, extraction_payload, missing_fields)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (new_project_id, f['filename'], f['original_filename'], f['file_path'],
                      f['file_size'], f['mime_type'], f['processed'], f['processing_status'],
                      f['review_status'], Json(f['extraction_payload']) if f['extraction_payload'] else None,
                      Json(f['missing_fields']) if f.get('missing_fields') else None))
                new_file = cur.fetchone()
                file_id_map[f['id']] = new_file['id']
                counts['files'] += 1
            
            # 2. Clone utility_accounts
            cur.execute('''
                SELECT id, utility_name, account_number
                FROM utility_accounts
                WHERE project_id = %s
            ''', (old_project_id,))
            old_accounts = cur.fetchall()
            
            for a in old_accounts:
                cur.execute('''
                    INSERT INTO utility_accounts (project_id, utility_name, account_number)
                    VALUES (%s, %s, %s)
                    RETURNING id
                ''', (new_project_id, a['utility_name'], a['account_number']))
                new_account = cur.fetchone()
                account_id_map[a['id']] = new_account['id']
                counts['accounts'] += 1
            
            # 3. Clone utility_meters (linked to new accounts)
            cur.execute('''
                SELECT id, utility_account_id, meter_number, service_address
                FROM utility_meters
                WHERE utility_account_id IN (SELECT id FROM utility_accounts WHERE project_id = %s)
            ''', (old_project_id,))
            old_meters = cur.fetchall()
            
            for m in old_meters:
                new_account_id = account_id_map.get(m['utility_account_id'])
                if new_account_id:
                    cur.execute('''
                        INSERT INTO utility_meters (utility_account_id, meter_number, service_address)
                        VALUES (%s, %s, %s)
                        RETURNING id
                    ''', (new_account_id, m['meter_number'], m['service_address']))
                    new_meter = cur.fetchone()
                    meter_id_map[m['id']] = new_meter['id']
                    counts['meters'] += 1
            
            # 4. Clone bills (linked to new accounts, meters, and files)
            cur.execute('''
                SELECT b.id, b.bill_file_id, b.account_id, b.meter_id, b.utility_name,
                       b.service_address, b.rate_schedule, b.period_start, b.period_end,
                       b.days_in_period, b.total_kwh, b.total_amount_due,
                       b.energy_charges, b.demand_charges, b.other_charges, b.taxes,
                       b.tou_on_kwh, b.tou_mid_kwh, b.tou_off_kwh,
                       b.tou_on_rate_dollars, b.tou_mid_rate_dollars, b.tou_off_rate_dollars,
                       b.tou_on_cost, b.tou_mid_cost, b.tou_off_cost,
                       b.blended_rate_dollars, b.avg_cost_per_day
                FROM bills b
                WHERE b.account_id IN (SELECT id FROM utility_accounts WHERE project_id = %s)
            ''', (old_project_id,))
            old_bills = cur.fetchall()
            
            for b in old_bills:
                new_account_id = account_id_map.get(b['account_id'])
                new_meter_id = meter_id_map.get(b['meter_id'])
                new_file_id = file_id_map.get(b['bill_file_id'])
                
                if new_account_id and new_meter_id:
                    cur.execute('''
                        INSERT INTO bills 
                        (bill_file_id, account_id, meter_id, utility_name, service_address,
                         rate_schedule, period_start, period_end, days_in_period, total_kwh,
                         total_amount_due, energy_charges, demand_charges, other_charges, taxes,
                         tou_on_kwh, tou_mid_kwh, tou_off_kwh,
                         tou_on_rate_dollars, tou_mid_rate_dollars, tou_off_rate_dollars,
                         tou_on_cost, tou_mid_cost, tou_off_cost,
                         blended_rate_dollars, avg_cost_per_day)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    ''', (new_file_id, new_account_id, new_meter_id, b['utility_name'],
                          b['service_address'], b['rate_schedule'], b['period_start'],
                          b['period_end'], b['days_in_period'], b['total_kwh'],
                          b['total_amount_due'], b['energy_charges'], b['demand_charges'],
                          b['other_charges'], b['taxes'],
                          b['tou_on_kwh'], b['tou_mid_kwh'], b['tou_off_kwh'],
                          b['tou_on_rate_dollars'], b['tou_mid_rate_dollars'], b['tou_off_rate_dollars'],
                          b['tou_on_cost'], b['tou_mid_cost'], b['tou_off_cost'],
                          b['blended_rate_dollars'], b['avg_cost_per_day']))
                    new_bill = cur.fetchone()
                    bill_id_map[b['id']] = new_bill['id']
                    counts['bills'] += 1
            
            # 5. Clone bill_tou_periods (linked to new bills)
            for old_bill_id, new_bill_id in bill_id_map.items():
                cur.execute('''
                    SELECT period, kwh, rate_dollars_per_kwh, est_cost_dollars
                    FROM bill_tou_periods
                    WHERE bill_id = %s
                ''', (old_bill_id,))
                old_tou_periods = cur.fetchall()
                
                for tp in old_tou_periods:
                    cur.execute('''
                        INSERT INTO bill_tou_periods (bill_id, period, kwh, rate_dollars_per_kwh, est_cost_dollars)
                        VALUES (%s, %s, %s, %s, %s)
                    ''', (new_bill_id, tp['period'], tp['kwh'], tp['rate_dollars_per_kwh'], tp['est_cost_dollars']))
                    counts['tou_periods'] += 1
            
            # 6. Clone bill_screenshots (linked to new bill files)
            for old_file_id, new_file_id in file_id_map.items():
                cur.execute('''
                    SELECT file_path, original_filename, mime_type, page_hint
                    FROM bill_screenshots
                    WHERE bill_id = %s
                ''', (old_file_id,))
                old_screenshots = cur.fetchall()
                
                for ss in old_screenshots:
                    cur.execute('''
                        INSERT INTO bill_screenshots (bill_id, file_path, original_filename, mime_type, page_hint)
                        VALUES (%s, %s, %s, %s, %s)
                    ''', (new_file_id, ss['file_path'], ss['original_filename'], ss['mime_type'], ss['page_hint']))
                    counts['screenshots'] += 1
            
            conn.commit()
            print(f"[bills_db] Cloned bills for project {old_project_id} -> {new_project_id}: {counts}")
            return counts
            
    except Exception as e:
        conn.rollback()
        print(f"[bills_db] Error cloning bills: {e}")
        raise e
    finally:
        conn.close()


def export_bills_csv(project_id):
    """
    Export all bills for a project as CSV data.
    Returns CSV string with headers and all bill data.
    Format designed for Excel/jMaster import.
    """
    import csv
    import io
    
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get all bills for this project with account and meter info
            cur.execute('''
                SELECT 
                    a.utility_name,
                    a.account_number,
                    m.meter_number,
                    b.service_address,
                    b.rate_schedule,
                    b.period_start,
                    b.period_end,
                    b.due_date,
                    b.days_in_period,
                    b.total_kwh,
                    b.total_amount_due,
                    b.blended_rate_dollars,
                    b.avg_cost_per_day,
                    b.energy_charges,
                    b.demand_charges,
                    b.other_charges,
                    b.taxes,
                    b.tou_on_kwh,
                    b.tou_on_rate_dollars,
                    b.tou_on_cost,
                    b.tou_mid_kwh,
                    b.tou_mid_rate_dollars,
                    b.tou_mid_cost,
                    b.tou_off_kwh,
                    b.tou_off_rate_dollars,
                    b.tou_off_cost,
                    b.tou_super_off_kwh,
                    b.tou_super_off_rate_dollars,
                    b.tou_super_off_cost,
                    f.original_filename AS source_file
                FROM bills b
                JOIN utility_accounts a ON b.account_id = a.id
                JOIN utility_meters m ON b.meter_id = m.id
                LEFT JOIN utility_bill_files f ON b.bill_file_id = f.id
                WHERE a.project_id = %s
                ORDER BY a.account_number, m.meter_number, b.period_end DESC
            ''', (project_id,))
            bills = cur.fetchall()
            
            if not bills:
                return None
            
            # Create CSV in memory
            output = io.StringIO()
            
            # Define column headers (Excel-friendly names)
            headers = [
                'Utility',
                'Account Number',
                'Meter Number',
                'Service Address',
                'Rate Schedule',
                'Period Start',
                'Period End',
                'Due Date',
                'Days',
                'Total kWh',
                'Total Amount ($)',
                'Blended Rate ($/kWh)',
                'Avg Cost/Day ($)',
                'Energy Charges ($)',
                'Demand Charges ($)',
                'Other Charges ($)',
                'Taxes ($)',
                'On-Peak kWh',
                'On-Peak Rate ($/kWh)',
                'On-Peak Cost ($)',
                'Mid-Peak kWh',
                'Mid-Peak Rate ($/kWh)',
                'Mid-Peak Cost ($)',
                'Off-Peak kWh',
                'Off-Peak Rate ($/kWh)',
                'Off-Peak Cost ($)',
                'Super Off-Peak kWh',
                'Super Off-Peak Rate ($/kWh)',
                'Super Off-Peak Cost ($)',
                'Source File'
            ]
            
            writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(headers)
            
            for b in bills:
                row = [
                    b['utility_name'] or '',
                    b['account_number'] or '',
                    b['meter_number'] or '',
                    b['service_address'] or '',
                    b['rate_schedule'] or '',
                    str(b['period_start']) if b['period_start'] else '',
                    str(b['period_end']) if b['period_end'] else '',
                    str(b['due_date']) if b['due_date'] else '',
                    b['days_in_period'] or '',
                    float(b['total_kwh']) if b['total_kwh'] else '',
                    float(b['total_amount_due']) if b['total_amount_due'] else '',
                    round(float(b['blended_rate_dollars']), 4) if b['blended_rate_dollars'] else '',
                    round(float(b['avg_cost_per_day']), 2) if b['avg_cost_per_day'] else '',
                    float(b['energy_charges']) if b['energy_charges'] else '',
                    float(b['demand_charges']) if b['demand_charges'] else '',
                    float(b['other_charges']) if b['other_charges'] else '',
                    float(b['taxes']) if b['taxes'] else '',
                    float(b['tou_on_kwh']) if b['tou_on_kwh'] else '',
                    round(float(b['tou_on_rate_dollars']), 4) if b['tou_on_rate_dollars'] else '',
                    float(b['tou_on_cost']) if b['tou_on_cost'] else '',
                    float(b['tou_mid_kwh']) if b['tou_mid_kwh'] else '',
                    round(float(b['tou_mid_rate_dollars']), 4) if b['tou_mid_rate_dollars'] else '',
                    float(b['tou_mid_cost']) if b['tou_mid_cost'] else '',
                    float(b['tou_off_kwh']) if b['tou_off_kwh'] else '',
                    round(float(b['tou_off_rate_dollars']), 4) if b['tou_off_rate_dollars'] else '',
                    float(b['tou_off_cost']) if b['tou_off_cost'] else '',
                    float(b['tou_super_off_kwh']) if b['tou_super_off_kwh'] else '',
                    round(float(b['tou_super_off_rate_dollars']), 4) if b['tou_super_off_rate_dollars'] else '',
                    float(b['tou_super_off_cost']) if b['tou_super_off_cost'] else '',
                    b['source_file'] or ''
                ]
                writer.writerow(row)
            
            csv_content = output.getvalue()
            output.close()
            return csv_content
            
    except Exception as e:
        print(f"[bills_db] Error exporting bills CSV: {e}")
        return None
    finally:
        conn.close()
