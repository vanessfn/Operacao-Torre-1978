import argparse
import csv
from collections import defaultdict, deque, Counter
from datetime import datetime, time, timedelta
from pathlib import Path
import logging
import sys
import re
import statistics

# --- Paths (assume cwd ~ ~/aero70 or call with absolute paths) ---
BASE = Path.home() / "aero70"  # change if needed
DADOS = BASE / "dados"
LOGS = BASE / "logs"
REL = BASE / "relatorios"

PLANOS = DADOS / "planos_voo.csv"
PISTAS = DADOS / "pistas.txt"
FROTA = DADOS / "frota.csv"
PILOTOS = DADOS / "pilotos.csv"
METAR = DADOS / "metar.txt"
NOTAM = DADOS / "notam.txt"

FILA_DECOL = DADOS / "fila_decolagem.txt"
FILA_POUSO = DADOS / "fila_pouso.txt"
LOG_FILE = LOGS / "torre.log"

# Ensure directories exist (won't create files)
for p in (DADOS, LOGS, REL):
    p.mkdir(parents=True, exist_ok=True)

# --- Logging setup ---
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
fmt = logging.Formatter("%(message)s")
console.setFormatter(fmt)
logging.getLogger().addHandler(console)


# --- Helpers: parse times, files, rules ---


def parse_hhmm(s):
    """Parse HH:MM into datetime.time. Raise ValueError on bad format."""
    return datetime.strptime(s.strip(), "%H:%M").time()


def now_time():
    """Current time as time() object"""
    return datetime.now().time()


def read_planos():
    """Return list of dicts for each valid line in planos_voo.csv.
    Columns: voo,origem,destino,etd,eta,aeronave,tipo,prioridade,pista_pref
    """
    if not PLANOS.exists():
        raise FileNotFoundError(f"{PLANOS} não encontrado")
    planos = []
    seen = set()
    with PLANOS.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = ["voo", "origem", "destino", "etd", "eta", "aeronave", "tipo", "prioridade", "pista_pref"]
        for h in required:
            if h not in reader.fieldnames:
                raise ValueError(f"Arquivo {PLANOS} faltando coluna '{h}'")
        for row in reader:
            voo = row["voo"].strip()
            # simple duplicate detection: same voo + same ETD
            key = (voo, row["etd"].strip())
            if key in seen:
                logging.warning(f"Duplicidade detectada em planos_voo: {voo} {row['etd']}. Linha ignorada.")
                continue
            seen.add(key)
            try:
                row["etd_t"] = parse_hhmm(row["etd"])
                row["eta_t"] = parse_hhmm(row["eta"])
                row["prioridade"] = int(row["prioridade"])
            except Exception as e:
                logging.warning(f"Erro parse plano {voo}: {e}. Linha ignorada.")
                continue
            planos.append(row)
    return planos


def read_pistas():
    """Return dict pista -> status ('ABERTA'|'FECHADA')"""
    if not PISTAS.exists():
        raise FileNotFoundError(f"{PISTAS} não encontrado")
    pistas = {}
    with PISTAS.open(encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) >= 2:
                pistas[parts[0]] = parts[1]
    return pistas


def read_frota():
    """Return dict aeronave -> comprimento_min_pista (int) and obs"""
    if not FROTA.exists():
        raise FileNotFoundError(f"{FROTA} não encontrado")
    d = {}
    with FROTA.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                d[r["aeronave"].strip()] = {"comprimento_min_pista": int(r["comprimento_min_pista"]), "obs": r.get("obs", "").strip()}
            except Exception:
                logging.warning(f"Erro no registro de frota: {r}")
    return d


def read_pilotos():
    """Return dict matricula -> data (nome,licenca,habilitacao,validade(datetime.date))"""
    if not PILOTOS.exists():
        raise FileNotFoundError(f"{PILOTOS} não encontrado")
    d = {}
    with PILOTOS.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            mat = r["matricula"].strip()
            nome = r["nome"].strip()
            lic = r["licenca"].strip()
            hab = r["habilitacao"].strip()
            try:
                val = datetime.strptime(r["validade"].strip(), "%Y-%m-%d").date()
            except Exception:
                # try alternative common format (YYYY or other) - if fail, set very old date
                try:
                    val = datetime.strptime(r["validade"].strip(), "%Y-%m-%d").date()
                except Exception:
                    val = datetime(1900, 1, 1).date()
            d[mat] = {"nome": nome, "licenca": lic, "habilitacao": hab, "validade": val}
    return d


def read_metar():
    """Parse metar.txt to list of (time, dict) entries. We only need VIS value in KM and time."""
    if not METAR.exists():
        raise FileNotFoundError(f"{METAR} não encontrado")
    entries = []
    with METAR.open(encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            # example: "13:00 VENTO 090/12KT VIS 7KM CHUVA LEVE"
            m = re.match(r"(\d{2}:\d{2}) .*VIS (\d+)KM", ln)
            try:
                t = parse_hhmm(ln.split()[0])
            except Exception:
                continue
            vis = None
            if m:
                vis = int(m.group(2))
            entries.append({"time": t, "raw": ln, "vis_km": vis})
    return entries


def active_metar_for_now(current_time=None):
    """Return metar applicable to current_time (closest time <= now). If none, return None."""
    current_time = current_time or now_time()
    entries = read_metar()
    if not entries:
        return None
    # find latest entry with time <= current_time
    candidates = [e for e in entries if e["time"] <= current_time]
    if not candidates:
        # if none earlier, take earliest (wrap)
        return entries[0]
    # pick max by time
    best = max(candidates, key=lambda e: e["time"])
    return best


def read_notams():
    """Return list of notam dicts. For PISTA closures will have keys: type='PISTA', pista, start, end, text.
    Time strings HH:MM-HH:MM.
    """
    if not NOTAM.exists():
        return []
    out = []
    with NOTAM.open(encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            # Try to parse "PISTA 01/19 FECHADA 14:00-16:00 MANUTENCAO"
            m = re.match(r"PISTA\s+(\d{2}/\d{2})\s+FECHADA\s+(\d{2}:\d{2})-(\d{2}:\d{2})(?:\s+(.*))?", ln)
            if m:
                pista = m.group(1)
                start = parse_hhmm(m.group(2))
                end = parse_hhmm(m.group(3))
                text = (m.group(4) or "").strip()
                out.append({"type": "PISTA", "pista": pista, "start": start, "end": end, "text": text, "raw": ln})
                continue
            # Other generic notam with time window pattern maybe "RADIO ... 15:00-15:30"
            m2 = re.search(r"(\d{2}:\d{2})-(\d{2}:\d{2})", ln)
            if m2:
                start = parse_hhmm(m2.group(1))
                end = parse_hhmm(m2.group(2))
                out.append({"type": "GEN", "start": start, "end": end, "text": ln, "raw": ln})
            else:
                out.append({"type": "GEN", "text": ln, "raw": ln})
    return out


def notam_blocks_pista(pista, current_time=None):
    """Return True if any NOTAM blocks the pista at current_time."""
    current_time = current_time or now_time()
    notams = read_notams()
    for n in notams:
        if n["type"] == "PISTA" and n["pista"] == pista:
            s = n.get("start")
            e = n.get("end")
            if s and e:
                if s <= current_time <= e:
                    return True, n
    return False, None


# --- Queue helpers ---

def load_queue(path):
    """Load queue file as list of dicts. Format per line: voo;hora;prioridade;pista_atribuida?;tipo"""
    out = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            parts = ln.split(";")
            # safe parse
            rec = {"voo": parts[0], "hora": parts[1] if len(parts) > 1 else "", "prioridade": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0, "pista": parts[3] if len(parts) > 3 else "", "tipo": parts[4] if len(parts) > 4 else ""}
            out.append(rec)
    return out


def save_queue(path, items):
    """Items is list of dicts with keys voo,hora,prioridade,pista,tipo"""
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(f"{it.get('voo','')};{it.get('hora','')};{it.get('prioridade',0)};{it.get('pista','')};{it.get('tipo','')}\n")


# --- Business rules utilities ---


def can_operate_due_to_vis(current_metar):
    """If VIS < 6KM, allow only 1 operation at a time. We'll return max_concurrent allowed (1 or large)."""
    if not current_metar or current_metar.get("vis_km") is None:
        return 999
    if current_metar["vis_km"] < 6:
        return 1
    return 999


def piloto_valido_for_aeronave(pilotos_dict, matricula, aeronave):
    """Check whether there is a pilot with matricula and habilitacao matches aeronave model (simple substring match),
    and license not expired (validade >= today)."""
    p = pilotos_dict.get(matricula)
    if not p:
        return False, "Piloto não encontrado"
    if p["validade"] < datetime.now().date():
        return False, "Licença do piloto vencida"
    # We check habilitacao equals aeronave model or is substring
    # Example: habilitacao 'B727' must match aeronave 'B727'
    if p["habilitacao"].upper() not in aeronave.upper():
        # permit if habilitacao appears in aeronave string or vice-versa
        return False, f"Habilitação '{p['habilitacao']}' incompatível com aeronave '{aeronave}'"
    return True, ""


def aeronave_compatível(frota_dict, aeronave, pista):
    """Check if given aircraft can use given pista by length requirement.
    Pista code like '10/28' - we don't parse length from pista file; instead caller must supply pista length.
    But spec: pistas.txt doesn't include length; so we will only check using frota.min_length vs a mapping of pista->length if available.
    We'll assume pistas file doesn't have length, so we simply accept (but will warn) unless pista has 'FECHADA' or NOTAM blocks it.
    """
    # Because pista lengths not provided, we cannot check numeric length. Return True and note.
    return True, ""


# --- Commands Implementation ---


def cmd_importar_dados(args):
    """Validate all files, precompute queues (empty), log missing, detect duplicate flights."""
    missing = []
    for p in (PLANOS, PISTAS, FROTA, PILOTOS, METAR, NOTAM):
        if not p.exists():
            missing.append(str(p))
    if missing:
        msg = f"Arquivos ausentes: {', '.join(missing)}"
        logging.error(msg)
        print(msg)
        return 1

    # Try to read all (and validate)
    try:
        planos = read_planos()
        pistas = read_pistas()
        frota = read_frota()
        pilotos = read_pilotos()
        metars = read_metar()
        notams = read_notams()
    except Exception as e:
        logging.error(f"Erro ao ler dados: {e}")
        print("Erro ao ler dados. Veja logs.")
        return 1

    # Pre-calc: clear filas (but keep file existence)
    FILA_DECOL.write_text("", encoding="utf-8")
    FILA_POUSO.write_text("", encoding="utf-8")

    # Basic checks: duplicidades já tratadas em read_planos
    logging.info("importar-dados: importação concluída, filas inicializadas (vazias).")
    print("Importação concluída. Filas inicializadas (vazias).")
    return 0


def format_table(rows, headers):
    """Simple pretty table printer."""
    col_widths = [max(len(str(r.get(h, ""))) for r in rows + [dict(zip(headers, headers))]) for h in headers]
    sep = " | "
    header_line = sep.join(h.ljust(w) for h, w in zip(headers, col_widths))
    lines = [header_line, "-".join("-" * (w + 2) for w in col_widths)]
    for r in rows:
        lines.append(sep.join(str(r.get(h, "")).ljust(w) for h, w in zip(headers, col_widths)))
    return "\n".join(lines)


def cmd_listar(args):
    """List flights from planos_voo.csv ordered by --por"""
    try:
        planos = read_planos()
    except Exception as e:
        logging.error(f"listar: erro lendo planos: {e}")
        print("Erro lendo planos_voo.csv. Veja logs.")
        return 1
    key = args.por
    if key == "voo":
        planos.sort(key=lambda r: r["voo"])
    elif key == "etd":
        planos.sort(key=lambda r: r["etd_t"])
    elif key == "tipo":
        # Emergencia first
        tipo_rank = {"EMERGENCIA": 0, "COMERCIAL": 1, "CARGA": 2}
        planos.sort(key=lambda r: (tipo_rank.get(r["tipo"], 9), -r["prioridade"], r["etd_t"]))
    elif key == "prioridade":
        # show emergencias on top and then by priority desc (3->0) and etd
        planos.sort(key=lambda r: ((r["tipo"] != "EMERGENCIA"), -r["prioridade"], r["etd_t"]))
    else:
        planos.sort(key=lambda r: r["voo"])

    # Print simplified table
    rows = []
    for p in planos:
        rows.append({"voo": p["voo"], "orig": p["origem"], "dest": p["destino"], "etd": p["etd"], "eta": p["eta"], "aeronave": p["aeronave"], "tipo": p["tipo"], "prior": p["prioridade"]})
    print(format_table(rows, ["voo", "orig", "dest", "etd", "eta", "aeronave", "tipo", "prior"]))
    logging.info(f"listar --por={key}: exibidos {len(rows)} voos.")
    return 0


def find_plan_by_voo(planos, voo):
    for p in planos:
        if p["voo"] == voo:
            return p
    return None


def cmd_enfileirar(args):
    """Add a flight to appropriate queue (decolagem/pouso) if rules permit."""
    modo = args.op  # 'decolagem' or 'pouso'
    voo = args.voo
    # Read data
    try:
        planos = read_planos()
        frota = read_frota()
        pilotos = read_pilotos()
    except Exception as e:
        logging.error(f"enfileirar: erro lendo dados: {e}")
        print("Erro lendo dados. Veja logs.")
        return 1

    plan = find_plan_by_voo(planos, voo)
    if not plan:
        msg = f"Voo {voo} não encontrado em planos_voo.csv"
        logging.warning(msg)
        print(msg)
        return 1

    # Duplicidade: if exists in any queue with same voo, reject
    qd = load_queue(FILA_DECOL)
    qp = load_queue(FILA_POUSO)
    for q in (qd + qp):
        if q["voo"] == voo:
            msg = f"Voo {voo} já enfileirado em outra fila. Ação recusada."
            logging.warning(msg)
            print(msg)
            return 1

    # For simplicity: pick a pilot by scanning pilotos and finding one with habilitacao matching aircraft
    # In real exercise pilot matricula would come from planos or user input; we'll assume first matching pilot is assigned.
    assigned_pilot = None
    for mat, info in pilotos.items():
        if info["habilitacao"].upper() in plan["aeronave"].upper():
            assigned_pilot = mat
            break
    if not assigned_pilot:
        msg = f"Nenhum piloto habilitado encontrado para aeronave {plan['aeronave']}. Enfileirar negado."
        logging.warning(msg)
        print(msg)
        return 1

    # Validate pilot license not expired
    valid, motivo = piloto_valido_for_aeronave(pilotos, assigned_pilot, plan["aeronave"])
    if not valid:
        msg = f"Enfileirar {voo} negado: {motivo}"
        logging.warning(msg)
        print(msg)
        return 1

    # Validate aeronave in frota
    if plan["aeronave"] not in frota:
        logging.warning(f"Aeronave {plan['aeronave']} não encontrada em frota.csv; aceitando com aviso.")
        # proceed but warn

    # Add to appropriate file
    rec = {"voo": voo, "hora": plan["etd"], "prioridade": plan["prioridade"], "pista": plan.get("pista_pref", ""), "tipo": modo}
    if modo == "decolagem":
        arr = load_queue(FILA_DECOL)
        arr.append(rec)
        # sort by priority desc, then hour
        arr.sort(key=lambda r: (-int(r.get("prioridade", 0)), r.get("hora", "")))
        save_queue(FILA_DECOL, arr)
    else:
        arr = load_queue(FILA_POUSO)
        arr.append(rec)
        arr.sort(key=lambda r: (-int(r.get("prioridade", 0)), r.get("hora", "")))
        save_queue(FILA_POUSO, arr)

    logging.info(f"enfileirar {modo} {voo}: OK (pilot {assigned_pilot}).")
    print(f"Voo {voo} enfileirado para {modo}.")
    return 0


def cmd_autorizar(args):
    """Authorize the first eligible in the chosen queue for the selected pista."""
    modo = args.op  # decolagem or pouso
    pista = args.pista
    current = now_time()
    # Read dynamic files
    try:
        pistas = read_pistas()
        notams = read_notams()
        met = active_metar_for_now(current)
    except Exception as e:
        logging.error(f"autorizar: erro lendo dados: {e}")
        print("Erro lendo arquivos de apoio. Veja logs.")
        return 1

    # check pista exists and is ABERTA
    status = pistas.get(pista)
    if not status:
        msg = f"Pista {pista} não encontrada em pistas.txt"
        logging.warning(msg)
        print(msg)
        return 1
    if status != "ABERTA":
        msg = f"Pista {pista} não está ABERTA (status: {status}). Autorizar negado."
        logging.info(msg)
        print(msg)
        return 1

    # check NOTAM
    blocked, nm = notam_blocks_pista(pista, current)
    if blocked:
        msg = f"Pista {pista} bloqueada por NOTAM ativo: {nm['raw']}. Autorizar negado."
        logging.info(msg)
        print(msg)
        return 1

    # check vis
    max_conc = can_operate_due_to_vis(met)
    # Count current operations authorized now: for simplicity, read relatorio file of today's authorized? We'll check logs
    # We'll assume if met restricts to 1 and there is any AUTHORIZATION in logs in the last 10 minutes, block.
    if max_conc == 1:
        # look into log file for AUTHORIZADO entries in last 10 minutes
        ten_min_ago = datetime.now() - timedelta(minutes=10)
        recent_auth = False
        if LOG_FILE.exists():
            with LOG_FILE.open(encoding="utf-8") as f:
                for ln in f:
                    if "AUTORIZADO" in ln:
                        try:
                            # parse timestamp at start of line format "YYYY-MM-DD HH:MM:SS"
                            ts = ln.split(" ", 2)[:2]
                            ts_s = " ".join(ts)
                            tdt = datetime.strptime(ts_s, "%Y-%m-%d %H:%M:%S")
                            if tdt >= ten_min_ago:
                                recent_auth = True
                                break
                        except Exception:
                            continue
        if recent_auth:
            msg = "Visibilidade baixa (VIS < 6KM): já existe operação autorizada recentemente. Nova autorização negada."
            logging.info(msg)
            print(msg)
            return 1

    # choose queue
    qpath = FILA_DECOL if modo == "decolagem" else FILA_POUSO
    q = load_queue(qpath)
    if not q:
        msg = f"Fila de {modo} vazia."
        logging.info(msg)
        print(msg)
        return 0

    # find first eligible entry in q (respecting priority, tipo emergency rule)
    # Emergency flights (tipo 'EMERGENCIA') have top precedence regardless of queue type.
    # We'll read planos to check tipo.
    planos = read_planos()
    eligible_idx = None
    for idx, rec in enumerate(q):
        plan = find_plan_by_voo(planos, rec["voo"])
        if not plan:
            # cannot authorize unknown plan
            continue
        # if emergency -> immediate eligible
        if plan["tipo"] == "EMERGENCIA":
            eligible_idx = idx
            break
        # else check pista compatibility (we simplified earlier)
        # check NOTAM per pista already done
        # check pilot valid? We didn't store pilot matricula in queue; skip deep check
        eligible_idx = idx
        break

    if eligible_idx is None:
        msg = "Nenhum voo elegível para autorização."
        logging.info(msg)
        print(msg)
        return 0

    rec = q.pop(eligible_idx)
    # Authorize: write to log as AUTORIZADO with details and return
    logging.info(f"AUTORIZADO {modo.upper()} voo={rec['voo']} pista={pista} motivo=OK")
    print(f"AUTORIZADO {modo} {rec['voo']} na pista {pista}.")
    # save updated queue
    save_queue(qpath, q)
    return 0


def cmd_status(args):
    """Print pistas status, tamanho das filas, próximos 3 voos e ocorrências ativas"""
    out_lines = []
    try:
        pistas = read_pistas()
        qd = load_queue(FILA_DECOL)
        qp = load_queue(FILA_POUSO)
        met = active_metar_for_now()
        notams = read_notams()
    except Exception as e:
        logging.error(f"status: erro lendo dados: {e}")
        print("Erro lendo arquivos de apoio. Veja logs.")
        return 1

    out_lines.append("Pistas:")
    for p, st in pistas.items():
        blocked, nm = notam_blocks_pista(p, now_time())
        if blocked:
            out_lines.append(f"  {p}: {st} (BLOQUEADA POR NOTAM: {nm['raw']})")
        else:
            out_lines.append(f"  {p}: {st}")

    out_lines.append("")
    out_lines.append(f"Tamanho fila decolagem: {len(qd)}")
    out_lines.append(f"Tamanho fila pouso: {len(qp)}")
    out_lines.append("")
    out_lines.append("Próximos 3 decolagens:")
    for i, r in enumerate(qd[:3], 1):
        out_lines.append(f"  {i}. {r['voo']} hora={r['hora']} prioridade={r['prioridade']} pista_pref={r['pista']}")
    out_lines.append("")
    out_lines.append("Próximos 3 pousos:")
    for i, r in enumerate(qp[:3], 1):
        out_lines.append(f"  {i}. {r['voo']} hora={r['hora']} prioridade={r['prioridade']} pista_pref={r['pista']}")

    out_lines.append("")
    out_lines.append("METAR atual:")
    if met:
        out_lines.append(f"  {met['raw']}")
    else:
        out_lines.append("  Nenhum METAR disponível")

    out_lines.append("")
    out_lines.append("NOTAMs ativos (janela de validade):")
    nowt = now_time()
    any_active = False
    for n in notams:
        s = n.get("start")
        e = n.get("end")
        if s and e and s <= nowt <= e:
            any_active = True
            out_lines.append(f"  ATIVO: {n['raw']}")
    if not any_active:
        out_lines.append("  Nenhum NOTAM ativo no horário atual.")

    print("\n".join(out_lines))
    logging.info("status exibido.")
    return 0


def cmd_relatorio(args):
    """Generate a simple report of the day based on logs: counts authorized, denied, reasons, avg wait (approx)."""
    date_tag = datetime.now().strftime("%Y%m%d")
    out_file = REL / f"operacao_{date_tag}.txt"
    stats = {"autorizadas": 0, "negadas": 0}
    motivos = Counter()
    wait_times = []
    # We'll parse log to collect AUTHORIZADO and NEGADO lines
    if LOG_FILE.exists():
        with LOG_FILE.open(encoding="utf-8") as f:
            for ln in f:
                if "AUTORIZADO" in ln:
                    stats["autorizadas"] += 1
                if "negado" in ln.lower() or "NEGADO" in ln:
                    stats["negadas"] += 1
                    # capture reason after "negado:" or "NEGADO"
                    m = re.search(r"[nN]egad[oa]\s*[:\-]\s*(.*)", ln)
                    if m:
                        motivos[m.group(1).strip()] += 1
    # Average wait cannot be computed precisely without timestamps per flight; we'll place placeholder
    with out_file.open("w", encoding="utf-8") as f:
        f.write(f"Relatorio de Operação - {datetime.now().isoformat()}\n")
        f.write(f"Autorizadas: {stats['autorizadas']}\n")
        f.write(f"Negadas: {stats['negadas']}\n")
        f.write("\nMotivos mais comuns (top 10):\n")
        for motivo, cnt in motivos.most_common(10):
            f.write(f"  {motivo}: {cnt}\n")
        f.write("\nObservações:\n")
        f.write("  - Média de espera não calculada (dados insuficientes). Implementar registro de timestamps por voo para medir.\n")
    logging.info(f"relatorio gerado: {out_file}")
    print(f"Relatório gerado: {out_file}")
    return 0


# --- CLI wiring ---


def main():
    parser = argparse.ArgumentParser(description="CLI Torre - operações de voo (atividade SO)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser("importar-dados", help="Ler e validar arquivos iniciais")
    p_import.set_defaults(func=cmd_importar_dados)

    p_list = sub.add_parser("listar", help="Listar voos (planos_voo.csv)")
    p_list.add_argument("--por", choices=["voo", "etd", "tipo", "prioridade"], default="voo")
    p_list.set_defaults(func=cmd_listar)

    p_enq = sub.add_parser("enfileirar", help="Enfileirar voo em decolagem ou pouso")
    p_enq.add_argument("op", choices=["decolagem", "pouso"])
    p_enq.add_argument("--voo", required=True)
    p_enq.set_defaults(func=cmd_enfileirar)

    p_aut = sub.add_parser("autorizar", help="Autorizar primeira operação elegível na fila")
    p_aut.add_argument("op", choices=["decolagem", "pouso"])
    p_aut.add_argument("--pista", required=True)
    p_aut.set_defaults(func=cmd_autorizar)

    p_sta = sub.add_parser("status", help="Exibir status das pistas, filas e ocorrências")
    p_sta.set_defaults(func=cmd_status)

    p_rel = sub.add_parser("relatorio", help="Gerar relatório do turno")
    p_rel.set_defaults(func=cmd_relatorio)

    args = parser.parse_args()
    try:
        rc = args.func(args)
        sys.exit(rc if isinstance(rc, int) else 0)
    except Exception as e:
        logging.exception(f"Erro ao executar comando {args.cmd if hasattr(args,'cmd') else '??'}: {e}")
        print("Erro interno. Veja logs para detalhes.")
        sys.exit(1)


if __name__ == "__main__":
    main()
