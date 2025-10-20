# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`click-odoo-contrib` is a collection of Odoo database maintenance and management CLI tools built on top of `click-odoo`. The project provides both command-line scripts and composable Python functions for common Odoo operations.

## Development Commands

### Running Tests

Tests use `tox` to run against multiple Python and Odoo versions:

```bash
# Run all tests
tox

# Run tests for specific Odoo version and Python version
tox -e py36-13.0

# Run tests matching a keyword for Odoo 12 with Python 3.6
tox -e py36-12.0 -- -k keyword

# Run specific test file
tox -e py36-14.0 -- tests/test_initdb.py
```

Tests are written using pytest with coverage reporting. The test suite automatically installs the appropriate Odoo version in the tox environment.

### Code Quality

The project uses pre-commit hooks for code quality:

```bash
# Install pre-commit hooks
pre-commit install

# Run all pre-commit checks manually
pre-commit run --all-files

# Or via tox
tox -e pre_commit
```

Code formatting standards:
- **black** for code formatting
- **isort** for import sorting
- **flake8** for linting (with flake8-bugbear)
- **pyupgrade** for Python syntax upgrades

### Package Validation

```bash
# Check that package metadata is valid for PyPI
tox -e check_readme
```

## Architecture

### Core Modules

**Database Management Scripts** (in `click_odoo_contrib/`):
- `initdb.py` - Create databases with template caching system
- `updatedb.py` - Smart update using content hashing
- `copydb.py` - Copy databases with filestore
- `dropdb.py` - Drop databases with filestore
- `backupdb.py` - Backup to zip or folder format
- `restoredb.py` - Restore from backup
- `uninstall.py` - Uninstall modules
- `makepot.py` - Export translations

**Shared Utilities**:
- `_dbutils.py` - Database utilities (advisory locks, connection management, config parameter reset)
- `_addon_hash.py` - Compute SHA1 checksums of addon directories
- `manifest.py` - Parse Odoo manifests, expand dependencies, find addons
- `gitutils.py` - Git operations for translation commits
- `core_addons.py` - List of Odoo CE/EE core addons by version

### Key Architectural Concepts

#### 1. Database Template Caching (`initdb.py`)

The `DbCache` class implements an intelligent caching system for test database initialization:

- Templates named as `{prefix}-{YYYYmmddHHMM}-{hashsum}` where the timestamp represents last usage (MRU)
- Hash computed from addon content + dependencies + auto_install modules + demo flag
- Advisory locks prevent race conditions during cache operations
- Automatic cache trimming by age (30 days default) and size (5 templates default)
- When cache is enabled, attachments are forced to database storage (not filestore) for consistency

**Important**: When creating databases from cache, the template is "touched" (renamed with current timestamp) to implement MRU eviction.

#### 2. Content-Based Update Detection (`update.py`)

The update script avoids unnecessary Odoo updates by:

- Computing SHA1 checksums of installed addon directories (via `_addon_hash.py`)
- Storing checksums in `ir_config_parameter` with key `module_auto_update.installed_checksums`
- Comparing current checksums with stored values to detect changes
- Supporting exclude patterns (default: `*.pyc,*.pyo,i18n/*.pot,i18n_extra/*.pot,static/*`)
- Filtering by active language `.po` files to avoid false positives

The checksum excludes irrelevant files and only includes translations for active languages.

**Auto-Install Module Checking**: After the update completes, the script checks for modules that have `auto_install=True`, are installable, have all dependencies installed, but are not themselves installed. This helps detect inconsistent states where auto-install modules should have been installed but weren't. The check respects the `--ignore-addons` and `--ignore-core-addons` flags.

#### 3. Parallel Update Watcher (`update.py`)

The `DbLockWatcher` thread monitors database locks during updates:

- Allows updating while another Odoo instance serves the same database
- Queries `pg_stat_activity` and `pg_locks` to detect blocking queries
- Terminates update session via `pg_terminate_backend` if locks exceed threshold
- Uses separate psycopg2 connection with autocommit to avoid blocking itself
- Designed for zero-downtime deployments with old/new codebase swap

#### 4. Advisory Locks (`_dbutils.py`)

PostgreSQL advisory locks prevent concurrent operations:

- Lock ID generated from SHA1 hash of operation name
- Used in `initdb.py` for cache operations
- Used in `update.py` with pattern `click-odoo-update/{database}`

#### 5. Multi-Version Odoo Compatibility

All scripts support Odoo 8.0 through master branch:

- Version detection via `odoo.tools.parse_version(odoo.release.version)`
- Conditional imports for relocated modules (e.g., `IrAttachment` location varies by version)
- Registry API differences (RegistryManager vs Registry)
- Different database connection info methods

### Manifest Parsing

The `manifest.py` module handles Odoo addon manifests:

- Supports both `__manifest__.py` (v10+) and `__openerp__.py` (older versions)
- `expand_dependencies()` recursively resolves deps + auto_install modules
- Used by `initdb.py` to compute accurate cache keys

### Entry Points

All console scripts are defined in `setup.py`:
- `click-odoo-initdb` → `click_odoo_contrib.initdb:main`
- `click-odoo-update` → `click_odoo_contrib.update:main`
- etc.

## Testing Architecture

Tests are organized in `tests/`:
- Each script has a corresponding `test_*.py` file
- `conftest.py` provides shared fixtures
- `tests/data/` contains test addon structures
- `tests/scripts/install_odoo.py` handles Odoo installation for test environments

Test databases use prefix `click-odoo-contrib-test*` and are cleaned up in fixtures.

## Important Patterns

### Environment Management

Scripts use `@click_odoo.env_options()` decorator for common Odoo CLI options:
- Handles config file, database, addons path, log level
- Some scripts use custom environment managers (e.g., `OdooEnvironmentWithUpdate`)

### Database Operations

Always use context managers for database connections:
- Advisory locks: `with advisory_lock(cr, name):`
- Postgres connection: `with pg_connect() as cr:`
- Odoo environment: `with OdooEnvironment(dbname) as env:`

### Checksum Computation

When computing addon hashes:
1. Walk directory in sorted order for deterministic results
2. Hash both filepath and file content
3. Exclude patterns to avoid false positives (.pyc, .pot, etc.)
4. Filter translations by active languages

## Dependencies

Core dependencies (from `setup.py`):
- `click-odoo>=1.3.0` - Foundation framework
- `importlib_resources` (Python < 3.9)

Test dependencies (from `tox.ini`):
- `pytest`, `pytest-cov` for testing
- `psycopg2` for database access

The project explicitly does NOT vendor Odoo - it expects Odoo to be installed in the environment.

## Version Support

Tested against:
- Python: 2.7, 3.5, 3.6
- Odoo: 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, master

Check `tox.ini` for the compatibility matrix.
