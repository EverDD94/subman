"""Lo principal del panel de control de Jellyfin, para manejarlo desde Videotecario.

Bibliotecas (escanear), tareas programadas, usuarios (crear, borrar, contraseña, qué bibliotecas ve),
quién está viendo ahora, dispositivos y la actividad reciente. Todo con la clave de API de Ajustes.
"""
from __future__ import annotations

import requests

from .jellyfin import _headers


class Jellyfin:
    def __init__(self, cfg: dict):
        jf = cfg.get("jellyfin", {})
        if not (jf.get("url") and jf.get("api_key")):
            raise RuntimeError("Pon la dirección y la clave de Jellyfin en Ajustes")
        self.base = jf["url"].rstrip("/")
        self.h = _headers(jf["api_key"])

    def _do(self, method: str, path: str, **kw):
        r = requests.request(method, self.base + path, headers=self.h, timeout=kw.pop("timeout", 30), **kw)
        if r.status_code >= 400:
            msg = (r.text or "").strip()[:200] or r.reason
            raise RuntimeError(f"Jellyfin dice: {msg}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def get(self, path: str, **params):
        return self._do("GET", path, params=params or None)

    def post(self, path: str, body=None, **params):
        return self._do("POST", path, json=body, params=params or None)

    # ---- resumen ----
    def overview(self) -> dict:
        info = self.get("/System/Info") or {}
        return {"name": info.get("ServerName", ""), "version": info.get("Version", ""),
                "restart": bool(info.get("HasPendingRestart")), "update": bool(info.get("HasUpdateAvailable")),
                "url": self.base}

    def restart(self) -> dict:
        self.post("/System/Restart")
        return {"ok": True}

    # ---- bibliotecas ----
    def libraries(self) -> list[dict]:
        out = []
        for f in self.get("/Library/VirtualFolders") or []:
            out.append({"id": f.get("ItemId"), "name": f.get("Name", ""), "type": f.get("CollectionType") or "mixta",
                        "paths": f.get("Locations") or [], "status": f.get("RefreshStatus") or "",
                        "progress": f.get("RefreshProgress")})
        counts = self.get("/Items/Counts") or {}
        return out, {"movies": counts.get("MovieCount", 0), "series": counts.get("SeriesCount", 0),
                     "episodes": counts.get("EpisodeCount", 0)}

    def scan(self, item_id: str | None = None, full: bool = False) -> dict:
        if not item_id:
            self.post("/Library/Refresh")
        else:
            mode = "FullRefresh" if full else "Default"
            self.post(f"/Items/{item_id}/Refresh", Recursive="true", MetadataRefreshMode=mode, ImageRefreshMode=mode,
                      ReplaceAllMetadata="true" if full else "false", ReplaceAllImages="false")
        return {"ok": True}

    # ---- tareas programadas ----
    def tasks(self) -> list[dict]:
        out = []
        for t in self.get("/ScheduledTasks", isHidden="false") or []:
            last = t.get("LastExecutionResult") or {}
            out.append({"id": t.get("Id"), "name": t.get("Name", ""), "category": t.get("Category", ""),
                        "about": t.get("Description", ""), "state": t.get("State", ""),
                        "progress": t.get("CurrentProgressPercentage"), "last_status": last.get("Status", ""),
                        "last_end": last.get("EndTimeUtc"), "last_error": last.get("ErrorMessage") or ""})
        return out

    def task_run(self, task_id: str, stop: bool = False) -> dict:
        self._do("DELETE" if stop else "POST", f"/ScheduledTasks/Running/{task_id}")
        return {"ok": True}

    # ---- usuarios ----
    def users(self) -> list[dict]:
        out = []
        for u in self.get("/Users") or []:
            p = u.get("Policy") or {}
            out.append({"id": u.get("Id"), "name": u.get("Name", ""), "admin": bool(p.get("IsAdministrator")),
                        "disabled": bool(p.get("IsDisabled")), "all": bool(p.get("EnableAllFolders")),
                        "folders": p.get("EnabledFolders") or [], "has_password": bool(u.get("HasPassword")),
                        "last": u.get("LastActivityDate"), "image": bool(u.get("PrimaryImageTag"))})
        return out

    def user_new(self, name: str, password: str, folders: list[str] | None, admin: bool = False) -> dict:
        name = (name or "").strip()
        if not name:
            raise RuntimeError("Falta el nombre")
        u = self.post("/Users/New", {"Name": name, "Password": password or ""})
        self.user_policy(u["Id"], folders=folders, admin=admin)
        return {"ok": True, "id": u["Id"]}

    def user_policy(self, user_id: str, folders: list[str] | None = None, admin: bool | None = None,
                    disabled: bool | None = None) -> dict:
        """Cambia sólo lo que se pide; el resto de la política se manda tal cual estaba (Jellyfin la quiere entera)."""
        p = dict((self.get(f"/Users/{user_id}") or {}).get("Policy") or {})
        if folders is not None:  # None = no tocar; [] o lista = esas; ["*"] = todas
            p["EnableAllFolders"] = folders == ["*"]
            p["EnabledFolders"] = [] if folders == ["*"] else folders
        if admin is not None:
            p["IsAdministrator"] = admin
        if disabled is not None:
            p["IsDisabled"] = disabled
        self.post(f"/Users/{user_id}/Policy", p)
        return {"ok": True}

    def user_password(self, user_id: str, password: str) -> dict:
        self.post(f"/Users/{user_id}/Password", {"NewPw": password or "", "ResetPassword": not password})
        return {"ok": True}

    def user_delete(self, user_id: str) -> dict:
        users = self.users()
        me = next((u for u in users if u["id"] == user_id), None)
        if me and me["admin"] and sum(u["admin"] for u in users) <= 1:
            raise RuntimeError("Es el único administrador: no se puede borrar")
        self._do("DELETE", f"/Users/{user_id}")
        return {"ok": True}

    # ---- ahora mismo, dispositivos y actividad ----
    def sessions(self) -> list[dict]:
        out = []
        for s in self.get("/Sessions", activeWithinSeconds="960") or []:
            it, ps = s.get("NowPlayingItem") or {}, s.get("PlayState") or {}
            title = it.get("Name", "")
            if it.get("SeriesName"):
                title = f"{it['SeriesName']} · T{it.get('ParentIndexNumber', '?')}E{it.get('IndexNumber', '?')} · {title}"
            run, pos = it.get("RunTimeTicks") or 0, ps.get("PositionTicks") or 0
            out.append({"id": s.get("Id"), "user": s.get("UserName", ""), "client": s.get("Client", ""),
                        "device": s.get("DeviceName", ""), "last": s.get("LastActivityDate"),
                        "ip": s.get("RemoteEndPoint", ""), "playing": title, "item": it.get("Id"),
                        "paused": bool(ps.get("IsPaused")), "progress": (pos / run) if run else None,
                        "method": ps.get("PlayMethod", "")})
        out.sort(key=lambda s: (not s["playing"], s["user"]))
        return out

    def session_stop(self, session_id: str) -> dict:
        self.post(f"/Sessions/{session_id}/Playing/Stop")
        return {"ok": True}

    def session_message(self, session_id: str, text: str) -> dict:
        self.post(f"/Sessions/{session_id}/Message", {"Header": "Everflix", "Text": text, "TimeoutMs": 10000})
        return {"ok": True}

    def devices(self) -> list[dict]:
        d = self.get("/Devices") or {}
        return [{"id": x.get("Id"), "name": x.get("Name", ""), "app": f"{x.get('AppName', '')} {x.get('AppVersion', '')}".strip(),
                 "user": x.get("LastUserName", ""), "last": x.get("DateLastActivity")} for x in d.get("Items") or []]

    def device_delete(self, device_id: str) -> dict:
        self._do("DELETE", "/Devices", params={"id": device_id})
        return {"ok": True}

    def activity(self, limit: int = 40) -> list[dict]:
        d = self.get("/System/ActivityLog/Entries", limit=str(limit)) or {}
        return [{"name": x.get("Name", ""), "date": x.get("Date"), "level": x.get("Severity", ""),
                 "more": x.get("ShortOverview") or ""} for x in d.get("Items") or []]


def state(cfg: dict) -> dict:
    """Todo de una vez para la pestaña (cada parte por separado: si una falla, las demás salen)."""
    jf = Jellyfin(cfg)
    out: dict = {"ok": True, "errors": {}}
    for key, fn in (("server", jf.overview), ("users", jf.users), ("tasks", jf.tasks),
                    ("sessions", jf.sessions), ("devices", jf.devices), ("activity", jf.activity)):
        try:
            out[key] = fn()
        except Exception as e:
            out[key], out["errors"][key] = None, str(e)
    try:
        out["libraries"], out["counts"] = jf.libraries()
    except Exception as e:
        out["libraries"], out["counts"], out["errors"]["libraries"] = None, None, str(e)
    return out
