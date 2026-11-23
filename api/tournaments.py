"""
MatchBot — Ligas y torneos de pádel y fútbol.

Formatos:
  grupos  — Fase de grupos (round-robin) + eliminación directa.
            Grupos aleatorios de ~4; avanzan los 2 primeros de cada grupo
            al cuadro (16avos/octavos/cuartos según cuántos califiquen).
  liga    — Todos contra todos (una vuelta) con tabla de posiciones.

Los nombres se pegan desde Excel (uno por renglón; para pádel puede ser
la pareja completa en un renglón, ej. "Juan Pérez / Luis García").
Todo se guarda en la tabla tournaments (estructura en JSONB).
"""
import json
import logging
import random
from datetime import datetime

from db.database import execute

logger = logging.getLogger("matchbot.tournaments")


def ensure_table():
    execute("""
        CREATE TABLE IF NOT EXISTS tournaments (
            id SERIAL PRIMARY KEY,
            club_id INT DEFAULT 1,
            name TEXT NOT NULL,
            sport TEXT DEFAULT 'padel',
            fmt TEXT DEFAULT 'grupos',
            data JSONB NOT NULL DEFAULT '{}',
            status TEXT DEFAULT 'grupos',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)


def _round_robin(teams: list) -> list:
    """Método del círculo: jornadas de todos contra todos.
    Devuelve [[(a,b), ...] por jornada]. Índices sobre la lista teams."""
    n = len(teams)
    idx = list(range(n))
    if n % 2:
        idx.append(None)  # descanso
        n += 1
    rounds = []
    for _ in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = idx[i], idx[n - 1 - i]
            if a is not None and b is not None:
                pairs.append([a, b])
        rounds.append(pairs)
        idx = [idx[0]] + [idx[-1]] + idx[1:-1]
    return rounds


def create(name: str, sport: str, fmt: str, players: list[str]) -> int:
    """Crea el torneo: baraja jugadores y arma grupos (o liga)."""
    players = [p.strip() for p in players if p.strip()]
    random.shuffle(players)

    data = {"players": players}
    if fmt == "liga" or len(players) <= 5:
        fmt = "liga"
        fixtures = []
        for jornada in _round_robin(players):
            for a, b in jornada:
                fixtures.append({"a": a, "b": b, "sa": None, "sb": None})
        data["groups"] = [{"name": "Liga", "members": list(range(len(players))),
                           "fixtures": fixtures}]
    else:
        # Grupos de 4 (y de 3/5 para cuadrar)
        n = len(players)
        n_groups = max(1, round(n / 4))
        groups = [[] for _ in range(n_groups)]
        for i, p in enumerate(players):
            groups[i % n_groups].append(i)
        gdata = []
        for gi, members in enumerate(groups):
            fixtures = []
            local = {m: m for m in members}
            for jornada in _round_robin(members):
                for a, b in jornada:
                    fixtures.append({"a": a, "b": b, "sa": None, "sb": None})
            gdata.append({"name": f"Grupo {chr(65 + gi)}",
                          "members": members, "fixtures": fixtures})
        data["groups"] = gdata
    data["bracket"] = None

    row = execute("""
        INSERT INTO tournaments (name, sport, fmt, data, status)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
    """, [name[:120], sport, fmt, json.dumps(data), "grupos"], fetch_one=True)
    return int(row["id"])


def get(tid: int) -> dict | None:
    row = execute("SELECT * FROM tournaments WHERE id=%s", [tid], fetch_one=True)
    if not row:
        return None
    t = dict(row)
    if isinstance(t.get("data"), str):
        t["data"] = json.loads(t["data"])
    return t


def list_all() -> list[dict]:
    rows = execute("SELECT id, name, sport, fmt, status, created_at "
                   "FROM tournaments ORDER BY created_at DESC LIMIT 50",
                   fetch_all=True) or []
    return [dict(r) for r in rows]


def _save(tid: int, data: dict, status: str = None):
    if status:
        execute("UPDATE tournaments SET data=%s, status=%s WHERE id=%s",
                [json.dumps(data), status, tid])
    else:
        execute("UPDATE tournaments SET data=%s WHERE id=%s",
                [json.dumps(data), tid])


def set_score(tid: int, g: int, f: int, sa: int, sb: int):
    t = get(tid)
    if not t:
        return
    data = t["data"]
    try:
        fx = data["groups"][g]["fixtures"][f]
        fx["sa"], fx["sb"] = sa, sb
        _save(tid, data)
    except (IndexError, KeyError) as e:
        logger.error(f"set_score failed: {e}")


def standings(data: dict, g: int) -> list[dict]:
    """Tabla del grupo: PJ, G, P, puntos a favor/contra."""
    group = data["groups"][g]
    stats = {m: {"i": m, "pj": 0, "g": 0, "p": 0, "pf": 0, "pc": 0}
             for m in group["members"]}
    for fx in group["fixtures"]:
        if fx["sa"] is None or fx["sb"] is None:
            continue
        a, b, sa, sb = fx["a"], fx["b"], int(fx["sa"]), int(fx["sb"])
        stats[a]["pj"] += 1; stats[b]["pj"] += 1
        stats[a]["pf"] += sa; stats[a]["pc"] += sb
        stats[b]["pf"] += sb; stats[b]["pc"] += sa
        if sa > sb:
            stats[a]["g"] += 1; stats[b]["p"] += 1
        elif sb > sa:
            stats[b]["g"] += 1; stats[a]["p"] += 1
    return sorted(stats.values(),
                  key=lambda s: (-s["g"], -(s["pf"] - s["pc"]), -s["pf"]))


def gen_bracket(tid: int, per_group: int = 2):
    """Genera la eliminatoria: avanzan los primeros N de cada grupo.
    Cruces estilo mundial (A1-B2, B1-A2...) y byes si no es potencia de 2."""
    t = get(tid)
    if not t:
        return
    data = t["data"]
    groups = data["groups"]
    firsts, seconds = [], []
    for g in range(len(groups)):
        tabla = standings(data, g)
        if len(tabla) >= 1:
            firsts.append(tabla[0]["i"])
        if per_group >= 2 and len(tabla) >= 2:
            seconds.append(tabla[1]["i"])

    # Cruces: 1º del grupo i vs 2º del grupo i+1 (circular)
    seeds = []
    ng = len(firsts)
    if seconds:
        for i in range(ng):
            seeds.append(firsts[i])
            seeds.append(seconds[(i + 1) % len(seconds)])
    else:
        seeds = firsts

    # Completar a potencia de 2 con byes (None)
    size = 1
    while size < len(seeds):
        size *= 2
    while len(seeds) < size:
        seeds.append(None)

    matches = []
    for i in range(0, len(seeds), 2):
        a, b = seeds[i], seeds[i + 1]
        winner = a if b is None else (b if a is None else None)
        matches.append({"a": a, "b": b, "winner": winner})
    bracket = [matches]
    # Pre-crear rondas vacías hasta la final
    n = len(matches)
    while n > 1:
        n //= 2
        bracket.append([{"a": None, "b": None, "winner": None} for _ in range(n)])
    _fill_from_winners(bracket)
    data["bracket"] = bracket
    _save(tid, data, status="eliminatoria")


def _fill_from_winners(bracket: list):
    """Propaga ganadores a la siguiente ronda."""
    for r in range(len(bracket) - 1):
        for i, m in enumerate(bracket[r]):
            nxt = bracket[r + 1][i // 2]
            slot = "a" if i % 2 == 0 else "b"
            nxt[slot] = m["winner"]


def set_winner(tid: int, rnd: int, match: int, winner_idx: int):
    t = get(tid)
    if not t or not t["data"].get("bracket"):
        return
    data = t["data"]
    try:
        m = data["bracket"][rnd][match]
        if winner_idx in (m.get("a"), m.get("b")):
            m["winner"] = winner_idx
            _fill_from_winners(data["bracket"])
            final = data["bracket"][-1][0]
            _save(tid, data,
                  status="terminado" if final.get("winner") is not None else None)
    except (IndexError, KeyError) as e:
        logger.error(f"set_winner failed: {e}")


ROUND_NAMES = {16: "16avos de final", 8: "Octavos de final",
               4: "Cuartos de final", 2: "Semifinales", 1: "Final"}


def round_label(n_matches: int) -> str:
    return ROUND_NAMES.get(n_matches, f"Ronda de {n_matches * 2}")


# ─────────────────────────────────────────────────────
# HTML (página de detalle /panel/torneo/{id})
# ─────────────────────────────────────────────────────

def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_detail(t: dict) -> str:
    data = t["data"]
    players = data["players"]
    tid = t["id"]

    def pname(i):
        if i is None:
            return "—"
        try:
            return _esc(players[i])
        except (IndexError, TypeError):
            return "?"

    # Grupos con tabla y partidos
    ghtml = ""
    for g, group in enumerate(data["groups"]):
        tabla = standings(data, g)
        filas = "".join(
            f'<tr><td>{pos+1}</td><td><b>{pname(s["i"])}</b></td>'
            f'<td>{s["pj"]}</td><td>{s["g"]}</td><td>{s["p"]}</td>'
            f'<td>{s["pf"]-s["pc"]:+d}</td></tr>'
            for pos, s in enumerate(tabla))
        partidos = ""
        for f, fx in enumerate(group["fixtures"]):
            sa = "" if fx["sa"] is None else fx["sa"]
            sb = "" if fx["sb"] is None else fx["sb"]
            partidos += (
                f'<form class="fx" method="post" action="/api/panel/torneos/{tid}/score">'
                f'<input type="hidden" name="g" value="{g}">'
                f'<input type="hidden" name="f" value="{f}">'
                f'<span class="fxn">{pname(fx["a"])}</span>'
                f'<input name="sa" type="number" min="0" value="{sa}">'
                f'<b>vs</b>'
                f'<input name="sb" type="number" min="0" value="{sb}">'
                f'<span class="fxn">{pname(fx["b"])}</span>'
                f'<button type="submit">✓</button></form>'
            )
        ghtml += (
            f'<div class="gcard"><h3>{_esc(group["name"])}</h3>'
            f'<table class="tbl"><tr><th>#</th><th>Jugador/Pareja</th>'
            f'<th>PJ</th><th>G</th><th>P</th><th>Dif</th></tr>{filas}</table>'
            f'<h4>Partidos</h4>{partidos}</div>'
        )

    # Llaves
    bhtml = ""
    bracket = data.get("bracket")
    if bracket:
        cols = ""
        for r, rnd in enumerate(bracket):
            mz = ""
            for i, m in enumerate(rnd):
                def btn(side):
                    idx = m.get(side)
                    if idx is None:
                        return '<div class="bslot empty">—</div>'
                    win = ' win' if m.get("winner") == idx else ''
                    return (f'<form method="post" action="/api/panel/torneos/{tid}/winner" '
                            f'style="margin:0">'
                            f'<input type="hidden" name="r" value="{r}">'
                            f'<input type="hidden" name="m" value="{i}">'
                            f'<input type="hidden" name="w" value="{idx}">'
                            f'<button class="bslot{win}" type="submit">{pname(idx)}</button>'
                            f'</form>')
                mz += f'<div class="bmatch">{btn("a")}{btn("b")}</div>'
            cols += (f'<div class="bround"><div class="brname">'
                     f'{round_label(len(rnd))}</div>{mz}</div>')
        campeon = ""
        final = bracket[-1][0]
        if final.get("winner") is not None:
            campeon = (f'<div class="champ">🏆 Campeón: '
                       f'<b>{pname(final["winner"])}</b></div>')
        bhtml = (f'<h2>🏆 Eliminatoria</h2>{campeon}'
                 f'<p class="hint">Pica el nombre del ganador de cada partido '
                 f'para avanzarlo.</p>'
                 f'<div class="bwrap">{cols}</div>')
    elif t["fmt"] == "grupos":
        bhtml = (
            f'<form method="post" action="/api/panel/torneos/{tid}/bracket" '
            f'onsubmit="return confirm(\'¿Generar las llaves con la tabla actual? '
            f'Avanzan los 2 primeros de cada grupo.\')">'
            f'<button class="bigbtn" type="submit">⚔️ Generar eliminatoria '
            f'(avanzan 2 por grupo)</button></form>'
        )

    sport_emoji = "🎾" if t["sport"] == "padel" else "⚽"
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(t["name"])} — MatchBot</title>
<style>
body {{ font-family:-apple-system,'Segoe UI',Roboto,sans-serif; background:#f6f7f9;
       color:#0f172a; margin:0; padding:1rem; }}
.top {{ display:flex; align-items:center; gap:1rem; flex-wrap:wrap; margin-bottom:1rem; }}
h1 {{ font-size:1.2rem; margin:0; }}
a.back {{ color:#059669; text-decoration:none; font-size:0.85rem; }}
.badge {{ background:#eef2ff; color:#3730a3; border-radius:999px; padding:3px 10px;
         font-size:0.75rem; }}
.gwrap {{ display:flex; flex-wrap:wrap; gap:1rem; }}
.gcard {{ background:white; border:1px solid #e5e7eb; border-radius:12px;
         padding:1rem; min-width:300px; flex:1; max-width:430px; }}
.gcard h3 {{ margin:0 0 0.5rem; }} .gcard h4 {{ margin:0.8rem 0 0.4rem; font-size:0.8rem; }}
.tbl {{ border-collapse:collapse; width:100%; font-size:0.82rem; }}
.tbl th, .tbl td {{ border-bottom:1px solid #f1f5f9; padding:5px 7px; text-align:left; }}
.tbl th {{ color:#6b7280; font-weight:600; font-size:0.72rem; }}
.fx {{ display:flex; align-items:center; gap:6px; margin:4px 0; font-size:0.8rem; }}
.fx input {{ width:46px; border:1px solid #d1d5db; border-radius:6px; padding:4px; }}
.fx .fxn {{ flex:1; min-width:90px; }} .fx .fxn:last-of-type {{ text-align:right; }}
.fx button {{ border:1px solid #10b981; color:#059669; background:white;
             border-radius:6px; cursor:pointer; padding:3px 8px; }}
h2 {{ font-size:1rem; margin:1.4rem 0 0.6rem; }}
.bwrap {{ display:flex; gap:1.2rem; overflow-x:auto; padding-bottom:1rem; }}
.bround {{ min-width:190px; display:flex; flex-direction:column; justify-content:space-around; }}
.brname {{ font-size:0.75rem; color:#6b7280; margin-bottom:0.5rem; text-align:center; }}
.bmatch {{ background:white; border:1px solid #e5e7eb; border-radius:10px;
          margin:0.45rem 0; overflow:hidden; }}
.bslot {{ display:block; width:100%; text-align:left; padding:7px 10px; border:none;
         background:white; font-size:0.82rem; cursor:pointer; }}
.bslot:first-child {{ border-bottom:1px solid #f1f5f9; }}
.bslot.win {{ background:#d1fae5; font-weight:700; }}
.bslot.empty {{ color:#9ca3af; cursor:default; }}
.bigbtn {{ background:#111827; color:white; border:none; border-radius:10px;
          padding:12px 18px; font-size:0.9rem; cursor:pointer; margin-top:0.6rem; }}
.champ {{ background:#fef9c3; border:1px solid #fde047; border-radius:10px;
         padding:10px 14px; margin:0.5rem 0; }}
.hint {{ color:#6b7280; font-size:0.75rem; }}
@media print {{ .fx button, .bigbtn, a.back {{ display:none; }} }}
</style></head><body>
<div class="top">
  <a class="back" href="/panel">← Panel</a>
  <h1>{sport_emoji} {_esc(t["name"])}</h1>
  <span class="badge">{_esc(t["fmt"])}</span>
  <span class="badge">{len(players)} participantes</span>
  <span class="badge">{_esc(t["status"])}</span>
</div>
<div class="gwrap">{ghtml}</div>
{bhtml}
</body></html>"""
