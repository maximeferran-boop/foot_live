"""
foot_live — plugin FiestaBoard qui affiche le score en direct sur le board.

Une tâche de fond (thread) tourne dans FiestaBoard :
- calendrier : TheSportsDB gratuit (quand l'équipe joue) ;
- direct     : API-Football /fixtures?live=all (plan gratuit), seulement pendant la fenêtre du match ;
- affichage  : POST /api/v1/boards/{board}/message (interruption temporaire, retour au planning).
Écrit sur le board uniquement au coup d'envoi, à chaque but / but annulé, et à la fin.
Diagnostic : GET /api/plugins/foot_live/data
"""
import json
import logging
import os
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from src.plugins.base import PluginBase, PluginResult

log = logging.getLogger("foot_live")

FB_URL = os.environ.get("FOOT_LIVE_FB_URL", "http://127.0.0.1:3000/api")
STATE_FILE = os.environ.get("FOOT_LIVE_STATE", "/app/data/foot_live_state.json")
DEFAULT_TEAMS = '[{"label":"CAEN","af":88,"tsdb":134787},{"label":"FRANCE","af":2,"tsdb":133913}]'
BEFORE = timedelta(minutes=2)
AFTER = timedelta(minutes=170)
GIVE_UP = timedelta(minutes=30)
SCHEDULE_REFRESH = 6 * 3600
DAILY_CAP = 95

_lock = threading.Lock()
_worker = None          # un seul thread pour tout le processus
STATUS: Dict[str, Any] = {"status": "DEMARRAGE", "next_match": "", "quota_used": "0",
                          "last_event": "", "log": []}


def note(msg: str) -> None:
    log.info("foot_live: %s", msg)
    stamp = datetime.now().strftime("%d/%m %H:%M")
    STATUS["log"] = (STATUS["log"] + [f"{stamp} {msg}"])[-30:]


def clean(text, max_len=15):
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return text.upper().strip()[:max_len]


# Codes caractères Vestaboard (0 = vide). Couleurs : 63 rouge … 67 bleu.
VB = {" ": 0}
VB.update({chr(ord("A") + i): i + 1 for i in range(26)})
VB.update({str(d): 26 + d for d in range(1, 10)})
VB["0"] = 36
VB.update({"!": 37, "@": 38, "#": 39, "$": 40, "(": 41, ")": 42, "-": 44, "+": 46, "&": 47,
           "=": 48, ";": 49, ":": 50, "'": 52, '"': 53, "%": 54, ",": 55, ".": 56, "/": 59,
           "?": 60})
TILE = {"R": 63, "O": 64, "Y": 65, "G": 66, "B": 67, "V": 68, "W": 69}


def encode(line, cols):
    """Texte -> codes. Une pastille de couleur s'écrit {B} (bleu), {R} (rouge)…"""
    out, i = [], 0
    while i < len(line):
        if line[i] == "{" and i + 2 < len(line) and line[i + 2] == "}" and line[i + 1] in TILE:
            out.append(TILE[line[i + 1]])
            i += 3
            continue
        out.append(VB.get(clean(line[i], 1) or " ", 0))
        i += 1
    return (out + [0] * cols)[:cols]


def now_utc():
    return datetime.now(timezone.utc)


def http_json(url, headers=None, method="GET", body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": "foot_live", **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw else {}


class Worker:
    def __init__(self, plugin):
        self.plugin = plugin
        self.st = self.load_state()
        self.schedule: List[dict] = []
        self.last_sched = 0.0
        self.trackers: Dict[str, "Tracker"] = {}

    # ---------- config (relue à chaque tour : les changements dans l'interface s'appliquent)
    @property
    def cfg(self):
        return getattr(self.plugin, "config", None) or {}

    def teams(self):
        try:
            return json.loads(self.cfg.get("teams") or DEFAULT_TEAMS)
        except Exception:
            note("JSON des équipes invalide")
            return []

    @property
    def cols(self):
        return int(self.cfg.get("board_cols") or 15)

    # ---------- état persistant
    def load_state(self):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {"codes": {}, "quota": {"day": "", "used": 0}, "done": []}

    def save_state(self):
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.st, f)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log.warning("foot_live: état non sauvegardé : %s", e)

    # ---------- API-Football
    def af_get(self, path, params):
        day = now_utc().strftime("%Y-%m-%d")
        q = self.st["quota"]
        if q.get("day") != day:
            q["day"], q["used"] = day, 0
        if q["used"] >= DAILY_CAP:
            note("quota du jour atteint")
            return None
        q["used"] += 1
        STATUS["quota_used"] = str(q["used"])
        self.save_state()
        url = f"https://v3.football.api-sports.io/{path}?{urllib.parse.urlencode(params)}"
        try:
            res = http_json(url, {"x-apisports-key": self.cfg.get("api_key", "")})
        except Exception as e:
            note(f"API-Football injoignable : {e}")
            return None
        if res.get("errors"):
            note(f"API-Football erreur : {res['errors']}")
            return None
        return res.get("response", [])

    def team_code(self, team):
        tid = str(team["id"])
        if tid not in self.st["codes"]:
            resp = self.af_get("teams", {"id": tid}) or []
            self.st["codes"][tid] = (resp[0]["team"].get("code") if resp else None) or ""
            self.save_state()
        return self.st["codes"][tid]

    def display_name(self, team, room, me):
        if team["id"] == me["af"]:
            return clean(me["label"], self.cols)
        name = clean(team.get("name"), 50)
        if len(name) <= room:
            return name
        code = clean(self.team_code(team))
        if code and len(code) <= room:
            return code
        return clean(name.replace("-", " ").split()[0], room)

    # ---------- FiestaBoard
    def board_send(self, lines, minutes):
        """Essaie la grille avec pastilles ; si refusée, repli en texte simple. Renvoie True si affiché."""
        board = self.cfg.get("board_id") or "primary"
        url = f"{FB_URL}/v1/boards/{board}/message"
        cols = self.cols
        rows = 3 if cols <= 15 else 6
        plain = [clean(l.replace("{B}", " ").replace("{R}", " "), cols) for l in lines[:rows]]
        attempts = []
        if not self.st.get("grid_refused"):
            grid = [encode(l, cols) for l in lines[:rows]]
            grid += [[0] * cols for _ in range(rows - len(grid))]
            attempts.append(("grille", {"characters": grid}))
        attempts.append(("texte", {"lines": plain}))
        for mode, content in attempts:
            body = {**content, "duration_minutes": minutes, "revert_mode": "schedule"}
            try:
                http_json(url, None, "POST", body)
                note(f"board ({mode}) <- " + " / ".join(plain))
                STATUS["last_event"] = clean(" ".join(plain[:2]))
                return True
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "ignore")[:200]
                except Exception:
                    detail = ""
                note(f"FiestaBoard a refusé ({mode}, {e.code}) {detail}")
                if mode == "grille" and e.code == 400:
                    self.st["grid_refused"] = True      # on ne réessaie plus la grille
                    self.save_state()
            except Exception as e:
                note(f"FiestaBoard injoignable : {e}")
                return False
        return False

    # ---------- calendrier
    def fetch_schedule(self):
        out = []
        for t in self.teams():
            url = f"https://www.thesportsdb.com/api/v1/json/123/eventsnext.php?id={t['tsdb']}"
            try:
                events = http_json(url).get("events") or []
            except Exception as e:
                note(f"TheSportsDB injoignable ({t['label']}) : {e}")
                continue
            for ev in events:
                ts = ev.get("strTimestamp") or (
                    f"{ev['dateEvent']}T{ev['strTime']}" if ev.get("dateEvent") and ev.get("strTime") else None)
                if not ts:
                    continue
                try:
                    ko = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ko.tzinfo is None:
                    ko = ko.replace(tzinfo=timezone.utc)
                out.append({"team": t, "kickoff": ko, "event_id": str(ev.get("idEvent"))})
        out.sort(key=lambda m: m["kickoff"])
        if out:
            m = out[0]
            local = m["kickoff"].astimezone()
            STATUS["next_match"] = clean(f"{m['team']['label']} {local.strftime('%d/%m %Hh%M')}")
            note("calendrier : " + ", ".join(
                f"{x['team']['label']} {x['kickoff'].astimezone().strftime('%d/%m %Hh%M')}" for x in out))
        else:
            STATUS["next_match"] = "AUCUN"
            note("calendrier : aucun match à venir")
        return out

    # ---------- boucle
    def run(self):
        note("tâche de fond démarrée")
        while True:
            try:
                self.tick()
            except Exception as e:
                note(f"erreur : {e}")
                log.exception("foot_live")
            time.sleep(int(self.cfg.get("poll_seconds") or 120) if self.trackers else 60)

    def tick(self):
        if not self.cfg.get("api_key"):
            STATUS["status"] = "CLE MANQUANTE"
            return
        if time.time() - self.last_sched > SCHEDULE_REFRESH:
            self.schedule, self.last_sched = self.fetch_schedule(), time.time()
        t = now_utc()
        for m in self.schedule:
            if m["kickoff"] - BEFORE <= t <= m["kickoff"] + AFTER and m["event_id"] not in self.st["done"]:
                self.trackers.setdefault(m["event_id"], Tracker(self, m))
        for key in list(self.trackers):
            tr = self.trackers[key]
            m = tr.match
            gone = t > m["kickoff"] + AFTER
            never = tr.goals is None and t > m["kickoff"] + GIVE_UP
            if tr.finished or gone or never:
                if never and not tr.finished:
                    note(f"{m['team']['label']} introuvable en direct (reporté ou non couvert)")
                    self.mark_done(key)
                elif gone and not tr.finished:
                    tr.finish()
                del self.trackers[key]
        if self.trackers:
            STATUS["status"] = "MATCH EN COURS"
            live = self.af_get("fixtures", {"live": "all"})
            if live is not None:
                for tr in list(self.trackers.values()):
                    tr.update(live)
        else:
            STATUS["status"] = "EN VEILLE"

    def mark_done(self, key):
        self.st["done"] = (self.st.get("done", []) + [key])[-50:]
        self.save_state()


class Tracker:
    def __init__(self, w, match):
        self.w, self.match, self.me = w, match, match["team"]
        self.fixture_id = None
        self.goals = None
        self.home = self.away = None
        self.missing = 0
        self.finished = False
        self.pending = None        # dernier affichage raté, renvoyé au tour suivant

    def tile(self, team):
        return "{B}" if team["id"] == self.me["af"] else "{R}"

    def team_row(self, team, goals):
        """Pastille + nom à gauche, score calé à droite : {R}LAVAL        0"""
        g = str(goals)
        room = self.w.cols - 1 - 1 - len(g)          # pastille + espace mini + score
        name = self.w.display_name(team, room, self.me)
        return self.tile(team) + name + " " * (self.w.cols - 1 - len(name) - len(g)) + g

    def rows(self, gh, ga, third):
        return [self.team_row(self.home, gh), self.team_row(self.away, ga), third]

    @staticmethod
    def minute(status):
        el, ex = status.get("elapsed"), status.get("extra")
        return "" if el is None else (f"{el}+{ex}'" if ex else f"{el}'")

    def send(self, lines, minutes):
        self.pending = None if self.w.board_send(lines, minutes) else (lines, minutes)

    def update(self, live):
        if self.pending:
            self.send(*self.pending)
        af = self.me["af"]
        fx = next((f for f in live if af in (f["teams"]["home"]["id"], f["teams"]["away"]["id"])), None)
        if fx is None:
            if self.goals is not None:
                self.missing += 1
                if self.missing >= 2:
                    self.finish()
            return
        self.missing = 0
        self.fixture_id = fx["fixture"]["id"]
        self.home, self.away = fx["teams"]["home"], fx["teams"]["away"]
        gh, ga = fx["goals"]["home"] or 0, fx["goals"]["away"] or 0
        status = fx["fixture"]["status"]
        if self.goals is None:
            self.goals = (gh, ga)
            l3 = "COUP D'ENVOI" if (status.get("elapsed") or 0) <= 5 else "EN COURS"
            self.send(self.rows(gh, ga, l3), 180)
            return
        if (gh, ga) == self.goals:
            return
        oh, oa = self.goals
        self.goals = (gh, ga)
        if gh + ga < oh + oa:
            l3 = "BUT ANNULE"
        else:
            scorer = self.home if gh > oh else self.away
            l3 = f"{self.tile(scorer)}BUT ! {self.minute(status)}"
        self.send(self.rows(gh, ga, l3), 180)

    def finish(self):
        if self.finished or self.goals is None:
            return
        self.finished = True
        gh, ga = self.goals
        if self.fixture_id:
            resp = self.w.af_get("fixtures", {"id": self.fixture_id})
            if resp and (resp[0].get("goals") or {}).get("home") is not None:
                gh, ga = resp[0]["goals"]["home"], resp[0]["goals"]["away"]
        me_home = self.home["id"] == self.me["af"]
        mine, theirs = (gh, ga) if me_home else (ga, gh)
        verdict = "VICTOIRE" if mine > theirs else "NUL" if mine == theirs else "DEFAITE"
        self.w.board_send(self.rows(gh, ga, f"FIN - {verdict}"),
                          int(self.w.cfg.get("final_minutes") or 30))
        self.w.mark_done(self.match["event_id"])


class FootLive(PluginBase):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ensure_worker()

    @property
    def plugin_id(self) -> str:
        return "foot_live"

    def _ensure_worker(self):
        global _worker
        with _lock:
            if _worker is None:
                _worker = Worker(self)
                threading.Thread(target=_worker.run, name="foot_live", daemon=True).start()
            else:
                _worker.plugin = self      # toujours lire la config de l'instance la plus récente

    def fetch_data(self) -> PluginResult:
        try:
            self._ensure_worker()
            data = {k: STATUS[k] for k in ("status", "next_match", "quota_used", "last_event")}
            data["journal"] = " | ".join(STATUS["log"][-12:])
            return PluginResult(available=True, data=data,
                                formatted_lines=[data["status"], data["next_match"], data["last_event"]])
        except Exception as e:
            return PluginResult(available=False, error=str(e))

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors = []
        if not config.get("api_key"):
            errors.append("Clé API-Football requise")
        try:
            teams = json.loads(config.get("teams") or DEFAULT_TEAMS)
            for t in teams:
                if not all(k in t for k in ("label", "af", "tsdb")):
                    errors.append("Chaque équipe doit avoir label, af et tsdb")
                    break
        except Exception:
            errors.append("Le champ Équipes n'est pas un JSON valide")
        return errors
