"""Seguimiento: series y sagas que se bajan solas cuando sale algo nuevo.

Series: capítulos nuevos de la temporada en emisión y temporadas nuevas (desde la temporada en que
empezaste a seguirla). Películas: nuevas partes de su saga ya estrenadas. Se revisa una vez al día
y lo que falta se pone en la cola de Descargas (que sólo baja los capítulos que no tienes).
"""
from __future__ import annotations

import json
import logging
import time

from . import downloads
from .library import episode_numbers

log = logging.getLogger("subman.follow")

SCHEMA = """
CREATE TABLE IF NOT EXISTS autofollow (
    kind TEXT NOT NULL, tmdb INTEGER NOT NULL, title TEXT, year INTEGER,
    from_season INTEGER, info TEXT, created REAL, checked REAL, note TEXT,
    PRIMARY KEY (kind, tmdb)
);
"""
BUSY = ("cola", "pidiendo", "descargando", "manual")


class Follow:
    def __init__(self, dl: downloads.Downloads):
        self.dl = dl
        self.db = dl.db
        self.db.executescript(SCHEMA)

    def rows(self) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM autofollow ORDER BY created DESC"):
            row = dict(r)
            row.update(json.loads(row.pop("info") or "{}"))
            out.append(row)
        return out

    def keys(self) -> list[str]:
        return [f"{r[0]}{r[1]}" for r in self.db.execute("SELECT kind, tmdb FROM autofollow")]

    def has(self, kind: str, tmdb: int) -> bool:
        return self.db.execute("SELECT 1 FROM autofollow WHERE kind=? AND tmdb=?", (kind, int(tmdb))).fetchone() is not None

    def set(self, item: dict, on: bool = True, from_season: int | None = None) -> None:
        kind, tid = item["kind"], int(item["tmdb"])
        if not on:
            self.db.execute("DELETE FROM autofollow WHERE kind=? AND tmdb=?", (kind, tid))
        else:
            info = json.dumps({k: item.get(k) for k in ("poster",)}, ensure_ascii=False)
            self.db.execute("INSERT OR IGNORE INTO autofollow (kind, tmdb, title, year, from_season, info, created) "
                            "VALUES (?,?,?,?,?,?,?)", (kind, tid, item.get("title") or "", item.get("year"),
                                                       from_season, info, time.time()))
            if from_season:
                self.db.execute("UPDATE autofollow SET from_season=MIN(COALESCE(from_season, ?), ?) WHERE kind=? AND tmdb=?",
                                (from_season, from_season, kind, tid))
        self.db.commit()

    # --- la revisión diaria ---------------------------------------------------------------
    def _busy(self, kind: str, tid: int, season: int | None = None) -> bool:
        q = f"SELECT 1 FROM downloads WHERE kind=? AND tmdb=? AND state IN ({','.join('?' * len(BUSY))})"
        args = [kind, tid, *BUSY]
        if season is not None:
            q += " AND season=?"
            args.append(season)
        return self.db.execute(q, args).fetchone() is not None

    def owned_eps(self, row: dict, db) -> dict[int, set[int]]:
        out: dict[int, set[int]] = {}
        folder = self.dl.show_folder({"kind": "tv", "tmdb": row["tmdb"], "title": row["title"], "year": row["year"],
                                      "season": None}, db)
        for f in folder.rglob("*") if folder.is_dir() else []:
            if f.suffix.lower() in downloads.VIDEO_EXT:
                ep = episode_numbers(f)
                if ep:
                    out.setdefault(ep[0], set()).add(ep[1])
        return out

    def check_tv(self, row: dict, db) -> list[int]:
        """Temporadas que hay que (volver a) pedir: con capítulos ya emitidos que no tienes."""
        tid = row["tmdb"]
        d = db._get(f"/tv/{tid}")
        last = d.get("last_episode_to_air") or {}
        ls, le = last.get("season_number") or 0, last.get("episode_number") or 0
        counts = {s["season_number"]: s.get("episode_count") or 0 for s in d.get("seasons") or []}
        have = self.owned_eps(row, db)
        start = row.get("from_season") or 1
        out = []
        for n in sorted(counts):
            if n < max(1, start) or n > ls:
                continue
            aired = le if n == ls else counts[n]
            if aired and len(have.get(n, set())) < aired and not self._busy("tv", tid, n):
                out.append(n)
        return out

    def check_movie(self, row: dict, db) -> list[dict]:
        """Partes de su saga ya estrenadas que no tienes ni están en Descargas."""
        m = db.movie(row["tmdb"])
        col = m.get("collection")
        if not col:
            return []
        parts = db._get(f"/collection/{col['id']}").get("parts", [])
        today = time.strftime("%Y-%m-%d")
        out = []
        for p in parts:
            rd = p.get("release_date") or ""
            if not rd or rd > today or p["id"] == row["tmdb"]:
                continue
            if self.dl.has("movie", p["id"]):
                continue
            if self.db.execute("SELECT 1 FROM discover WHERE kind='movie' AND tmdb=? AND have=1", (p["id"],)).fetchone():
                continue
            title = p.get("title") or p.get("original_title") or ""
            if self.dl.copies(p["id"], [title, p.get("original_title")], int(rd[:4])):
                continue
            out.append({"tmdb": p["id"], "title": title, "year": int(rd[:4])})
        return out

    def check(self, db) -> list[str]:
        """Revisa todo lo que sigues y pide lo nuevo. Devuelve qué se pidió (para avisarte)."""
        done = []
        for row in self.rows():
            try:
                if row["kind"] == "tv":
                    for n in self.check_tv(row, db):
                        self.dl.add("tv", row["tmdb"], row["title"], row["year"], "seguimiento", season=n)
                        done.append(f"{row['title']} · temporada {n}")
                else:
                    for p in self.check_movie(row, db):
                        self.dl.add("movie", p["tmdb"], p["title"], p["year"], "seguimiento")
                        done.append(f"{p['title']} ({p['year']})")
                note = ""
            except Exception as e:
                log.info("seguimiento: %s: %s", row["title"], e)
                note = str(e)[:200]
            self.db.execute("UPDATE autofollow SET checked=?, note=? WHERE kind=? AND tmdb=?",
                            (time.time(), note, row["kind"], row["tmdb"]))
            self.db.commit()
            time.sleep(1)
        return done
