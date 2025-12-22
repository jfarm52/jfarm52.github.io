#!/usr/bin/env python3
"""
Clean up duplicate projects - keeps only the MOST RECENT version of each customer/unit combination
"""
import json
from datetime import datetime
from collections import defaultdict

DATA_FILE = 'projects_data.json'
BACKUP_FILE = 'projects_data_backup.json'

def backup_data():
    """Create backup before cleanup"""
    with open(DATA_FILE, 'r') as f:
        data = json.load(f)
    with open(BACKUP_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"✓ Backup created: {BACKUP_FILE}")

def cleanup_duplicates(dry_run=True):
    """Remove duplicate projects, keeping only the most recent"""
    with open(DATA_FILE, 'r') as f:
        data = json.load(f)
    
    # Track statistics
    total_before = sum(len(projects) for projects in data.values())
    removed_count = 0
    
    # Process each user's projects
    for user_id, projects in data.items():
        # Group by customer + unit count
        groups = defaultdict(list)
        
        for proj_id, proj_data in projects.items():
            site_data = proj_data.get('siteData', {})
            customer = site_data.get('customer', '').strip()
            units = proj_data.get('totalUnits', 0)
            saved_at = proj_data.get('_metadata', {}).get('saved_at', '')
            
            # Skip completely empty projects (no customer, no units)
            if not customer and units == 0:
                # Group all empty projects together
                key = "___EMPTY___"
            else:
                key = f"{customer}|{units}"
            
            groups[key].append({
                'id': proj_id,
                'saved_at': saved_at,
                'customer': customer,
                'units': units,
                'data': proj_data
            })
        
        # For each group, keep only the most recent
        projects_to_keep = {}
        for key, group_projects in groups.items():
            if len(group_projects) == 1:
                # Only one - keep it
                proj = group_projects[0]
                projects_to_keep[proj['id']] = proj['data']
            else:
                # Multiple - sort by saved_at and keep most recent
                sorted_projects = sorted(group_projects, key=lambda x: x['saved_at'], reverse=True)
                most_recent = sorted_projects[0]
                
                # For empty projects, keep only the most recent one
                if key == "___EMPTY___":
                    projects_to_keep[most_recent['id']] = most_recent['data']
                    print(f"\n  Removing {len(sorted_projects) - 1} empty duplicates, keeping most recent")
                else:
                    # Keep the most recent version
                    projects_to_keep[most_recent['id']] = most_recent['data']
                    print(f"\n  {most_recent['customer']} ({most_recent['units']} units):")
                    print(f"    Keeping:  {most_recent['saved_at']}")
                    print(f"    Removing: {len(sorted_projects) - 1} older versions")
                
                removed_count += len(sorted_projects) - 1
        
        # Update the user's projects
        data[user_id] = projects_to_keep
    
    total_after = sum(len(projects) for projects in data.values())
    
    print(f"\n{'='*80}")
    print(f"CLEANUP SUMMARY")
    print(f"{'='*80}")
    print(f"Projects before:  {total_before}")
    print(f"Projects after:   {total_after}")
    print(f"Removed:          {removed_count}")
    print(f"{'='*80}\n")
    
    if not dry_run:
        # Save cleaned data
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        print("✓ Cleanup completed - duplicates removed")
        print(f"✓ Original data backed up to: {BACKUP_FILE}")
    else:
        print("DRY RUN - No changes made")
        print("Run with --execute to actually remove duplicates")
    
    return total_before, total_after, removed_count

if __name__ == '__main__':
    import sys
    
    print("\n" + "="*80)
    print("REFRIGERATION PROJECT DUPLICATE CLEANUP")
    print("="*80)
    
    if '--execute' in sys.argv:
        print("\n⚠️  EXECUTING CLEANUP - This will remove duplicates!\n")
        backup_data()
        cleanup_duplicates(dry_run=False)
    else:
        print("\nDRY RUN MODE - Showing what would be removed")
        print("(Run with --execute to actually clean up)\n")
        cleanup_duplicates(dry_run=True)
