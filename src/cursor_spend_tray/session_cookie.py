"""Early Cursor session detection via dedicated-profile cookie stores.

Reads cookie *names* only (no decrypt). Presence of WorkosCursorSessionToken
means the profile has signed in at least once; scrape still verifies liveness.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .browser import BrowserFamily, BrowserInfo, profile_dir_for

log = logging.getLogger(__name__)

# Primary web session cookie (Firefox + Chromium families use the same name).
CURSOR_SESSION_COOKIE = "WorkosCursorSessionToken"


def profile_has_cursor_session_cookie(
    info: BrowserInfo,
    *,
    app_name: str = "cursor-spend-tray",
) -> bool:
    """True when the automation profile has a WorkosCursorSessionToken row."""
    profile = profile_dir_for(info, app_name)
    if info.family is BrowserFamily.FIREFOX:
        return _firefox_has_cookie(profile, CURSOR_SESSION_COOKIE)
    return _chromium_has_cookie(profile, CURSOR_SESSION_COOKIE)


def _firefox_has_cookie(profile: Path, name: str) -> bool:
    db = profile / "cookies.sqlite"
    if not db.is_file():
        log.info("No Firefox cookies.sqlite at %s", db)
        return False
    try:
        copy = _copy_sqlite(db)
        with sqlite3.connect(copy) as con:
            row = con.execute(
                "SELECT 1 FROM moz_cookies WHERE name = ? LIMIT 1",
                (name,),
            ).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        log.warning("Firefox cookie check failed for %s: %s", db, exc)
        return False


def _chromium_has_cookie(profile: Path, name: str) -> bool:
    for db in (profile / "Default" / "Cookies", profile / "Cookies"):
        if not db.is_file():
            continue
        try:
            copy = _copy_sqlite(db)
            with sqlite3.connect(copy) as con:
                row = con.execute(
                    "SELECT 1 FROM cookies WHERE name = ? LIMIT 1",
                    (name,),
                ).fetchone()
            return row is not None
        except sqlite3.Error as exc:
            log.warning("Chromium cookie check failed for %s: %s", db, exc)
            return False
    log.info("No Chromium Cookies DB under %s", profile)
    return False


def _copy_sqlite(src: Path) -> Path:
    """Copy DB (+ WAL/SHM) so we can read while the browser holds a lock."""
    td = Path(tempfile.mkdtemp(prefix="cst-cookies-"))
    dst = td / src.name
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.is_file():
            try:
                shutil.copy2(side, Path(str(dst) + suffix))
            except OSError:
                pass
    return dst
