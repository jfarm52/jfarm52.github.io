#!/usr/bin/env python3
"""
Fix project counts in the database - sum the room-count fields instead of counting cards
"""
import json

DATA_FILE = 'projects_data.json'

def fix_counts():
    """Recalculate evap/cond counts by summing the room-count fields from entries"""
    with open(DATA_FILE, 'r') as f:
        data = json.load(f)
    
    total_projects = 0
    fixed_projects = 0
    
    print("\n" + "="*80)
    print("FIXING PROJECT COUNTS")
    print("="*80 + "\n")
    
    for user_id, projects in data.items():
        for proj_id, proj_data in projects.items():
            total_projects += 1
            
            entries = proj_data.get('entries', [])
            if not entries:
                # No entries, counts should be 0
                continue
            
            # Calculate correct counts by summing the room-count field
            evap_count = sum(
                int(entry.get('room-count', 0)) 
                for entry in entries 
                if entry.get('section') == 'evap'
            )
            
            cond_count = sum(
                int(entry.get('room-count', 0)) 
                for entry in entries 
                if entry.get('section') == 'cond'
            )
            
            # Get old counts
            old_evap = proj_data.get('evapCount', 0)
            old_cond = proj_data.get('condCount', 0)
            
            # Check if they need updating
            if old_evap != evap_count or old_cond != cond_count:
                customer = proj_data.get('siteData', {}).get('customer', 'Unknown')
                print(f"{customer}")
                print(f"  Old: {old_evap} evap, {old_cond} cond")
                print(f"  New: {evap_count} evap, {cond_count} cond")
                
                # Update counts
                proj_data['evapCount'] = evap_count
                proj_data['condCount'] = cond_count
                proj_data['totalUnits'] = evap_count + cond_count
                
                # Also update evaporators and condensers arrays
                proj_data['evaporators'] = [e for e in entries if e.get('section') == 'evap']
                proj_data['condensers'] = [e for e in entries if e.get('section') == 'cond']
                
                # Update metadata if it exists
                if '_metadata' in proj_data:
                    proj_data['_metadata']['evap_count'] = evap_count
                    proj_data['_metadata']['cond_count'] = cond_count
                
                fixed_projects += 1
                print()
    
    # Save updated data
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    
    print("="*80)
    print(f"Total projects:  {total_projects}")
    print(f"Fixed:           {fixed_projects}")
    print(f"Unchanged:       {total_projects - fixed_projects}")
    print("="*80)
    print("\n✓ Project counts have been recalculated and saved")

if __name__ == '__main__':
    fix_counts()
