#!/usr/bin/env python3
"""
Refrigeration Project Manager
Tool to view, search, and clean up saved projects
"""
import json
from datetime import datetime
from collections import defaultdict

DATA_FILE = 'projects_data.json'

def load_data():
    with open(DATA_FILE, 'r') as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def list_all_projects():
    """List all projects with their details"""
    data = load_data()
    
    all_projects = []
    for user_id, projects in data.items():
        for proj_id, proj_data in projects.items():
            site_data = proj_data.get('siteData', {})
            customer = site_data.get('customer', 'No Customer')
            saved_at = proj_data.get('_metadata', {}).get('saved_at', 'Unknown')
            units = proj_data.get('totalUnits', 0)
            city = site_data.get('city', '')
            
            all_projects.append({
                'customer': customer,
                'saved_at': saved_at,
                'units': units,
                'city': city,
                'id': proj_id,
                'user_id': user_id
            })
    
    # Sort by saved_at descending
    all_projects.sort(key=lambda x: x['saved_at'], reverse=True)
    
    print(f"\n{'='*80}")
    print(f"TOTAL PROJECTS: {len(all_projects)}")
    print(f"{'='*80}\n")
    
    for i, proj in enumerate(all_projects, 1):
        print(f"{i}. {proj['customer']}")
        print(f"   Saved: {proj['saved_at']}")
        print(f"   Units: {proj['units']}, City: {proj['city']}")
        print(f"   ID: {proj['id'][:20]}...")
        print()

def search_projects(search_term):
    """Search for projects by customer name"""
    data = load_data()
    
    matches = []
    search_lower = search_term.lower()
    
    for user_id, projects in data.items():
        for proj_id, proj_data in projects.items():
            site_data = proj_data.get('siteData', {})
            customer = site_data.get('customer', '')
            
            if search_lower in customer.lower():
                saved_at = proj_data.get('_metadata', {}).get('saved_at', 'Unknown')
                units = proj_data.get('totalUnits', 0)
                city = site_data.get('city', '')
                
                matches.append({
                    'customer': customer,
                    'saved_at': saved_at,
                    'units': units,
                    'city': city,
                    'id': proj_id
                })
    
    matches.sort(key=lambda x: x['saved_at'], reverse=True)
    
    print(f"\n{'='*80}")
    print(f"SEARCH RESULTS for '{search_term}': {len(matches)} matches")
    print(f"{'='*80}\n")
    
    for i, proj in enumerate(matches, 1):
        print(f"{i}. {proj['customer']}")
        print(f"   Saved: {proj['saved_at']}")
        print(f"   Units: {proj['units']}, City: {proj['city']}")
        print(f"   Full ID: {proj['id']}")
        print()

def find_duplicates():
    """Find duplicate projects (same customer, same unit count)"""
    data = load_data()
    
    # Group by customer + unit count
    groups = defaultdict(list)
    
    for user_id, projects in data.items():
        for proj_id, proj_data in projects.items():
            site_data = proj_data.get('siteData', {})
            customer = site_data.get('customer', '').strip()
            units = proj_data.get('totalUnits', 0)
            saved_at = proj_data.get('_metadata', {}).get('saved_at', '')
            
            key = f"{customer}|{units}"
            groups[key].append({
                'id': proj_id,
                'saved_at': saved_at,
                'customer': customer,
                'units': units
            })
    
    # Find groups with duplicates
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    
    print(f"\n{'='*80}")
    print(f"DUPLICATE ANALYSIS")
    print(f"{'='*80}\n")
    
    total_duplicates = sum(len(v) - 1 for v in duplicates.values())
    print(f"Found {len(duplicates)} customer/project groups with duplicates")
    print(f"Total duplicate projects: {total_duplicates}\n")
    
    for key, projects in sorted(duplicates.items(), key=lambda x: len(x[1]), reverse=True):
        customer, units = key.split('|')
        print(f"\n{customer} ({units} units) - {len(projects)} copies:")
        for proj in sorted(projects, key=lambda x: x['saved_at'], reverse=True):
            print(f"  - {proj['saved_at']} (ID: {proj['id'][:20]}...)")

def recent_projects(days=2):
    """Show projects from the last N days"""
    data = load_data()
    
    now = datetime.now()
    recent = []
    
    for user_id, projects in data.items():
        for proj_id, proj_data in projects.items():
            saved_at_str = proj_data.get('_metadata', {}).get('saved_at', '')
            if saved_at_str:
                try:
                    saved_at = datetime.fromisoformat(saved_at_str.replace('Z', '+00:00'))
                    age_days = (now - saved_at).days
                    
                    if age_days <= days:
                        site_data = proj_data.get('siteData', {})
                        customer = site_data.get('customer', 'No Customer')
                        units = proj_data.get('totalUnits', 0)
                        
                        recent.append({
                            'customer': customer,
                            'saved_at': saved_at_str,
                            'units': units,
                            'id': proj_id,
                            'age_days': age_days
                        })
                except:
                    pass
    
    recent.sort(key=lambda x: x['saved_at'], reverse=True)
    
    print(f"\n{'='*80}")
    print(f"PROJECTS FROM LAST {days} DAYS: {len(recent)}")
    print(f"{'='*80}\n")
    
    for proj in recent:
        print(f"• {proj['customer']} ({proj['units']} units)")
        print(f"  Saved: {proj['saved_at']}")
        print(f"  ID: {proj['id']}")
        print()

if __name__ == '__main__':
    import sys
    
    if len(sys.argv) == 1:
        print("\nRefrigeration Project Manager")
        print("\nUsage:")
        print("  python project_manager.py list          - List all projects")
        print("  python project_manager.py recent        - Show projects from last 2 days")
        print("  python project_manager.py search <term> - Search for projects")
        print("  python project_manager.py duplicates    - Find duplicate projects")
        print()
    elif sys.argv[1] == 'list':
        list_all_projects()
    elif sys.argv[1] == 'recent':
        recent_projects()
    elif sys.argv[1] == 'search' and len(sys.argv) > 2:
        search_term = ' '.join(sys.argv[2:])
        search_projects(search_term)
    elif sys.argv[1] == 'duplicates':
        find_duplicates()
    else:
        print("Unknown command. Use: list, recent, search, or duplicates")
