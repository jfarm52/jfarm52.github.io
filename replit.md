# Refrigeration Data Collection

## Overview
This full-stack application streamlines data collection for refrigeration units and site information, primarily for Carlisle Energy Solutions, Inc. It provides a mobile-friendly web interface for field technicians to efficiently collect data, which then aids in proposal generation and project management. The system also includes a Python API for automation and a specialized module for utility bill data extraction using AI. The business vision is to enhance field data accuracy, accelerate proposal generation, and improve project management efficiency.

## User Preferences
- Primary use case: Excel integration for proposal workflow
- Also supports Python automation/scripting

## System Architecture
The application is a single-page Flask application with a pure HTML/CSS/JavaScript frontend designed for broad mobile compatibility. The backend is a Flask server leveraging Flask-CORS.

### UI/UX Decisions
The design is optimized for mobile devices with professional typography and intuitive interactions. It features multi-view navigation (Home, Projects, Customer Info, Current Project), a dashboard with CTAs, and comprehensive search/sort capabilities for projects. Equipment cards are responsive with detailed tabs. Date formats are standardized, notes auto-save, and project creation integrates with customer info. All native browser dialogs are replaced with custom in-app modals for consistent UX. iOS-specific optimizations include `autocomplete="new-password"` for forms, `100dvh` for viewport heights, and safe area support.

### Technical Implementations
The frontend uses a `ProjectService` for operations, while the backend persists data in `projects_data.json` and uses an in-memory `ProjectStore`. Key features include project CRUD, duplication, autosave (on explicit user actions), CSV import/export, unique UUIDs for projects, drag-and-drop reordering, automated calculations, geolocation/address autocomplete, and Dropbox integration for exports. Photo management includes a dedicated data model, room key assignment, auto-assign wizard, and duplicate protection. The application includes a "Recently Deleted" archive for projects with a 30-day retention period, accessible via dedicated API endpoints and UI.

### System Design Choices
The application is a Single-Page Application (SPA) designed for stateless deployment with Gunicorn. It uses `requirements.txt` for dependency management. A `ProjectState` object centralizes state management with UUIDs and dirty-state tracking. `localStorage` is used for client-side crash recovery and complete JSON snapshots for draft restoration. Photo uploads to Dropbox are individual for reliability. A `SaveController` module manages all save operations to prevent race conditions and duplicate project creation, ensuring single-threaded saves and immediate ID syncing. Project loading errors are handled by clearing state and notifying the user. Navigation always lands on the Home screen, with deep linking as the only exception for auto-navigation to a specific project. A `NavigationController.refreshCurrentView()` allows for route-scoped data refreshes without resetting the entire view state. Backend responses are size-limited, and project listings are paginated with auto-pagination on the frontend. `localStorage` photo data is optimized by stripping base64 data for synced photos.

### Utility Bill Intake System
This isolated feature extracts utility account and meter data from PDF bills using AI/OCR, stored in PostgreSQL. Database tables are initialized lazily.
- **Upload + Extraction Flow**: Users upload PDFs, processed by an AI-powered `bill_extractor.py`, with progress updates, data validation, and user review/correction. Corrections are saved for AI training.
- **Data Model**: Stores detailed utility account, meter, and billing period information, including Time-of-Use (TOU) data.
- **UI**: Embedded directly in the Current Project view, displaying progress bars, clickable file rows for review, PDF viewing, and an annotation system. Status labels provide visual feedback. Missing fields are highlighted in the review modal, and bill values are consistently formatted.

### Replit Crash Prevention (December 2025)
To prevent Replit chat service crashes from >4MB payloads:
- **Response Size Blocking**: `@app.after_request` middleware BLOCKS JSON responses >1MB (dev) or >2MB (prod) with 413 error. Every JSON response logs `[API] METHOD /path bytes=N`.
- **Python Print Wrapper**: At the top of `app.py`, `builtins.print` is wrapped. Objects >50KB become `[OMITTED] type size=N`.
- **JavaScript Console Wrapper**: First `<script>` in `index.html` wraps `console.log/warn/error/debug`. Arguments >50KB become `[OMITTED] argN size=N type=summary` in Replit environments.
- **API Pagination**: `/api/projects` uses `limit`/`offset` params (max 100 per page). Frontend auto-fetches all pages.
- **localStorage Optimization**: Photo snapshots strip base64 from synced photos (with `dropboxPath`), keeping full data for unsynced photos.

## External Dependencies
-   **Flask**: Python web framework.
-   **Flask-CORS**: For Cross-Origin Resource Sharing.
-   **Gunicorn**: WSGI HTTP server.
-   **Browser LocalStorage**: Client-side data persistence.
-   **Google Places API**: For location-based autocomplete and geocoding.
-   **Flatpickr**: JavaScript date picker library.
-   **Dropbox API**: For automated CSV and file management.
-   **xAI Grok 4 API**: Vision-based utility bill extraction.
-   **PyMuPDF**: PDF to image conversion.
-   **PostgreSQL**: Database for utility bill data.