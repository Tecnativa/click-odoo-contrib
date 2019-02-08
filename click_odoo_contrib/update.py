#!/usr/bin/env python
# Copyright 2018 ACSONE SA/NV (<http://acsone.eu>)
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

import json
import logging
import multiprocessing
import os
import signal
from contextlib import closing, contextmanager
from datetime import timedelta

import click
import click_odoo
import psycopg2
from click_odoo import OdooEnvironment, odoo

from ._addon_hash import addon_hash

_logger = logging.getLogger(__name__)


PARAM_INSTALLED_CHECKSUMS = "module_auto_update.installed_checksums"
PARAM_EXCLUDE_PATTERNS = "module_auto_update.exclude_patterns"
DEFAULT_EXCLUDE_PATTERNS = "*.pyc,*.pyo,i18n/*.pot,i18n_extra/*.pot,static/*"


class LongDBLockError(click.ClickException):
    pass


def _db_lock_watcher(database, update_process, beat, max_seconds):
    """Watch DB while another process is updating Odoo.

    This method will wait until :param:`update_process` finishes, and as long
    as it is running, it will query :param:`database` to see if there are
    DB locks each :param:`beat` seconds. If a lock longer than
    :param:`max_seconds` is detected, then :param:`update_process` will be
    terminated and an exception will be raised.

    :param str database:
        Name of the database that is being updated in :param:`update_process`.

    :param multiprocessing.Process update_process:
        Parallel process that should be currently updating Odoo.

    :param float beat:
        How long to wait between watchdog loops.

    :param float max_seconds:
        Max length of DB lock allowed.
    """
    max_td = timedelta(seconds=max_seconds)
    # SQL explained in https://wiki.postgresql.org/wiki/Lock_Monitoring
    locks_query = """
        SELECT
            pg_stat_activity.datname,
            pg_class.relname,
            pg_locks.transactionid,
            pg_locks.mode,
            pg_locks.granted,
            pg_stat_activity.usename,
            pg_stat_activity.query,
            pg_stat_activity.query_start,
            AGE(NOW(), pg_stat_activity.query_start) AS age,
            pg_stat_activity.pid
        FROM
            pg_stat_activity
            JOIN pg_locks ON pg_locks.pid = pg_stat_activity.pid
            JOIN pg_class ON pg_class.oid = pg_locks.relation
        WHERE
            NOT pg_locks.granted
            AND pg_stat_activity.datname = %s
        ORDER BY pg_stat_activity.query_start
    """
    # See https://stackoverflow.com/a/35319598/1468388
    stop_query = "SELECT pg_cancel_backend(%s)"
    if odoo.release.version_info < (9, 0):
        params = {"dsn": odoo.sql_db.dsn(database)[1]}
    else:
        params = odoo.sql_db.connection_info_for(database)[1]
    # Need a separate raw psycopg2 cursor without transactioning to avoid
    # weird concurrency errors; this cursor will only trigger SELECTs, and
    # it needs to access current Postgres server status, monitoring other
    # transactions' status, so running inside a normal, transactioned,
    # Odoo cursor would block such monitoring and, after all, offer no
    # additional protection
    with closing(psycopg2.connect(**params)) as watcher_conn:
        watcher_conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        while True:
            # Wait some time before checking locks
            update_process.join(beat)
            if not update_process.is_alive():
                # Update finished!
                break
            # Ensure no long blocking queries happen
            _logger.debug("DB lock watcher triggered")
            with closing(
                watcher_conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            ) as watcher_cr:
                watcher_cr.execute(locks_query, (database,))
                locks = watcher_cr.fetchall()
                if locks:
                    _logger.warning("Found locked queries:\n%s", locks)
                bad = False
                for row in locks:
                    if row["age"] > max_td:
                        bad = True
                        _logger.warning(
                            "Long locked query detected; aborting update cursor..."
                        )
                        # The normal thing to do here would be to terminate
                        # `update_process` and let it reap its open cursors.
                        # However, Odoo doesn't behave like that. If you
                        # terminate or kill an Odoo process that has a locked
                        # cursor, it will instead leave the cursor open,
                        # effectively not solving the locked cursor issue.
                        # To workaround that problem, what we are doing here
                        # instead is stopping the cursor itself, so that
                        # Odoo dies. The end result is the same
                        # (`update_process` terminated), but doing it
                        # bottom-up we make sure the cursor is closed and
                        # parallel Odoo instance users can continue to
                        # work fine. Notice that, to do this operation,
                        # **the Postgres user for the watcher must be the
                        # same as for `update_process`, or one with
                        # higher privileges**
                        watcher_cr.execute(stop_query, (row["pid"],))
                if bad:
                    raise LongDBLockError("Update aborted due to long DB lock")


def _get_param(cr, key, default=None):
    cr.execute("SELECT value FROM ir_config_parameter WHERE key=%s", (key,))
    r = cr.fetchone()
    if r:
        return r[0]
    else:
        return default


def _set_param(cr, key, value):
    cr.execute(
        "INSERT INTO ir_config_parameter "
        "(key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value=%s",
        (key, value, value),
    )


def _load_installed_checksums(cr):
    value = _get_param(cr, PARAM_INSTALLED_CHECKSUMS)
    if value:
        return json.loads(value)
    else:
        return {}


def _save_installed_checksums(cr):
    checksums = {}
    cr.execute("SELECT name FROM ir_module_module WHERE state='installed'")
    for (module_name,) in cr.fetchall():
        checksums[module_name] = _get_checksum_dir(cr, module_name)
    _set_param(cr, PARAM_INSTALLED_CHECKSUMS, json.dumps(checksums))
    _logger.info("Database updated, new checksums stored")


def _get_checksum_dir(cr, module_name):
    exclude_patterns = _get_param(cr, PARAM_EXCLUDE_PATTERNS, DEFAULT_EXCLUDE_PATTERNS)
    exclude_patterns = [p.strip() for p in exclude_patterns.split(",")]
    cr.execute("SELECT code FROM res_lang WHERE active")
    keep_langs = [r[0] for r in cr.fetchall()]

    module_path = odoo.modules.module.get_module_path(module_name)
    if module_path and os.path.isdir(module_path):
        checksum_dir = addon_hash(module_path, exclude_patterns, keep_langs)
    else:
        checksum_dir = False

    return checksum_dir


@contextmanager
def OdooEnvironmentWithUpdate(database, ctx, **kwargs):
    conn = odoo.sql_db.db_connect(database)
    to_update = odoo.tools.config["update"]
    if ctx.params["update_all"]:
        to_update["base"] = 1
    else:
        with conn.cursor() as cr:
            checksums = _load_installed_checksums(cr)
            cr.execute("SELECT name FROM ir_module_module WHERE state = 'installed'")
            for (module_name,) in cr.fetchall():
                if _get_checksum_dir(cr, module_name) != checksums.get(module_name):
                    to_update[module_name] = 1
        if to_update:
            _logger.info(
                "Updating addons for their hash changed: %s.",
                ",".join(to_update.keys()),
            )
    odoo.tools.config["overwrite_existing_translations"] = ctx.params["i18n_overwrite"]
    if odoo.release.version_info[0] < 10:
        Registry = odoo.modules.registry.RegistryManager
    else:
        Registry = odoo.modules.registry.Registry
    try:
        # Update Odoo spawning a different process
        update_process = multiprocessing.Process(
            target=Registry.new, kwargs={"db_name": database, "update_module": True}
        )
        update_process.start()
        # Watch for database locks while Odoo updates
        _db_lock_watcher(
            database,
            update_process,
            ctx.params["watcher_beat"],
            ctx.params["watcher_max_seconds"],
        )
    except LongDBLockError:
        # Nothing to do; `update_process` should be finishing now
        _logger.debug("Aborted by DB lock watcher", exc_info=True)
    finally:
        # Too slow to finish? Terminate it
        update_process.join(ctx.params["watcher_beat"] * 3)
        if update_process.is_alive():
            _logger.info("Terminating odoo update process")
            update_process.terminate()
        # Still too slow? Kill it
        update_process.join(ctx.params["watcher_beat"] * 3)
        if update_process.is_alive():
            _logger.info("Killing odoo update process")
            os.kill(update_process.pid, signal.SIGKILL)
        # No matter what happens, join it to never leave a zombie
        update_process.join()
        # Raise an exception if update failed somehow
        if update_process.exitcode:
            error = click.ClickException("Update failed")
            error.exit_code = update_process.exitcode
            raise error
    # If we get here, the database has been updated
    with conn.cursor() as cr:
        _save_installed_checksums(cr)
    with OdooEnvironment(database) as env:
        yield env


@click.command()
@click_odoo.env_options(
    with_rollback=False,
    database_must_exist=False,
    with_addons_path=True,
    environment_manager=OdooEnvironmentWithUpdate,
)
@click.option("--i18n-overwrite", is_flag=True, help="Overwrite existing translations")
@click.option("--update-all", is_flag=True, help="Force a complete upgrade (-u base)")
@click.option(
    "--if-exists", is_flag=True, help="Don't report error if database doesn't exist"
)
@click.option(
    "--watcher-beat",
    default=10.0,
    type=float,
    help="Time to wait between DB lock watcher loops",
)
@click.option(
    "--watcher-max-seconds",
    default=30.0,
    type=float,
    help="Max DB lock seconds allowed before aborting the update process",
)
def main(env, i18n_overwrite, update_all, if_exists, watcher_beat, watcher_max_seconds):
    """ Update an Odoo database (odoo -u), automatically detecting
    addons to update based on a hash of their file content, compared
    to the hashes stored in the database.

    It allows updating in parallel while another Odoo instance is still
    running against the same database, by using a watcher that aborts the
    update in case a DB lock happens.
    """
    if not env:
        msg = "Database does not exist"
        if if_exists:
            click.echo(click.style(msg, fg="yellow"))
            return
        else:
            raise click.ClickException(msg)
    # TODO: warn/err if modules to upgrade


if __name__ == "__main__":  # pragma: no cover
    main()
