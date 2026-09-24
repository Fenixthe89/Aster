"""Tool filesystem di Aster: list_directory e read_file (dominio "filesystem")."""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from modules.filesystem_policy import risolvi_path_autorizzato
from modules.tool_registry import RegistroStrumenti, ToolSpec

MAX_DIRECTORY_ENTRIES = 100
MAX_READ_BYTES = 64 * 1024

TOOLS_FILE = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "Elenca nomi e tipo (file, directory o other) di un solo "
                "livello di una directory dentro una posizione autorizzata. "
                "Nessuna ricorsione, nessuna ricerca, nessun contenuto di "
                "file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Percorso della directory da elencare, cosi' "
                            "come espresso dall'utente."
                        ),
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Legge il contenuto testuale di un singolo file dentro una "
                "posizione autorizzata, fino a 64 KiB. Rifiuta file binari, "
                "troppo grandi o riconosciuti come potenzialmente sensibili "
                "(per nome o per contenuto)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Percorso del file da leggere, cosi' come "
                            "espresso dall'utente."
                        ),
                    }
                },
                "required": ["path"],
            },
        },
    },
]


@dataclass
class ContestoFilesystem:
    """Stato minimo richiesto dagli handler filesystem: solo le root autorizzate."""

    allowed_roots: list


def _prepara_allowed_roots(allowed_roots_raw, base_dir: Path) -> list:
    """
    Trasforma le stringhe grezze di config in path da passare a
    filesystem_policy.risolvi_path_autorizzato: un path assoluto resta
    invariato, un path relativo viene risolto rispetto a base_dir
    (l'installazione di Aster), mai rispetto a os.getcwd().

    Non duplica exists()/is_dir()/containment/dedup: sono responsabilità
    esclusiva di filesystem_policy.py, che resta invariato e riceve qui
    solo candidati grezzi da validare a sua volta.
    """

    roots_preparate = []

    for voce in allowed_roots_raw:
        try:
            candidata = Path(voce)
        except (TypeError, ValueError):
            continue

        if not candidata.is_absolute():
            candidata = base_dir / candidata

        roots_preparate.append(candidata)

    return roots_preparate


def prepara_contesto_filesystem(config: dict, base_dir: Path) -> ContestoFilesystem:
    """
    Costruisce il ContestoFilesystem da passare al dispatch per i tool
    filesystem, leggendo config["tools"]["filesystem"]["allowed_roots"].
    Assente o vuota -> nessuna root preparata -> default deny (la
    validazione stessa vive comunque in filesystem_policy.py).
    """

    tools_config = config.get("tools", {})
    filesystem_config = tools_config.get("filesystem", {}) if isinstance(tools_config, dict) else {}
    allowed_roots_raw = filesystem_config.get("allowed_roots", []) if isinstance(filesystem_config, dict) else []

    return ContestoFilesystem(
        allowed_roots=_prepara_allowed_roots(allowed_roots_raw or [], base_dir)
    )


def _risultato_deny(operation: str, status: str) -> dict:
    return {
        "ok": False,
        "operation": operation,
        "status": status,
    }


def _risultato_tool_error(operation: str) -> dict:
    # Nessun messaggio nativo di eccezione OS: può contenere il path
    # assoluto e non va mai esposto, nemmeno nell'errore.
    return {
        "ok": False,
        "operation": operation,
        "status": "tool_error",
        "error": "Errore durante l'accesso al filesystem.",
    }


# =====================================================================
# list_directory
# =====================================================================


def _classifica_entry(voce: Path) -> str:
    """
    Classifica una entry senza usare un eventuale symlink/junction come
    sonda del target: is_symlink() e' un controllo sul link stesso, non
    sulla destinazione a cui punta. Qualunque link (interno o esterno)
    e' sempre classificato "other", mai risolto qui.
    """

    try:
        if voce.is_symlink():
            return "other"
        if voce.is_dir():
            return "directory"
        if voce.is_file():
            return "file"
        return "other"
    except OSError:
        return "other"


def list_directory(argomenti: dict, contesto: ContestoFilesystem) -> dict:
    """
    Handler del tool list_directory.

    Qwen propone path; Python decide. Due validazioni indipendenti
    tramite filesystem_policy.risolvi_path_autorizzato: la prima come
    controllo immediato, la seconda immediatamente prima dell'I/O reale
    (mitigazione TOCTOU pratica) - l'I/O usa sempre e solo il
    resolved_path della SECONDA decisione, mai quello della prima. Se la
    decisione finale nega, nessuna operazione di I/O viene eseguita.

    Nessun path (richiesto, risolto o di root) compare mai nel
    risultato: il modello conosce già il path che ha proposto.
    """

    path_richiesto = argomenti.get("path") if isinstance(argomenti, dict) else None
    allowed_roots = contesto.allowed_roots if contesto is not None else []

    decisione = risolvi_path_autorizzato(path_richiesto, allowed_roots)
    if not decisione.allowed:
        return _risultato_deny("list_directory", "access_denied")

    # Rivalidazione immediatamente prima dell'I/O: usiamo esclusivamente
    # il resolved_path di QUESTA seconda decisione.
    decisione_finale = risolvi_path_autorizzato(path_richiesto, allowed_roots)
    if not decisione_finale.allowed:
        return _risultato_deny("list_directory", "access_denied")

    target = decisione_finale.resolved_path

    try:
        if not target.exists():
            return _risultato_deny("list_directory", "not_found")

        if not target.is_dir():
            return _risultato_deny("list_directory", "not_a_directory")

        entries_totali = [
            (voce.name, _classifica_entry(voce)) for voce in target.iterdir()
        ]
    except OSError:
        return _risultato_tool_error("list_directory")

    entries_totali.sort(key=lambda coppia: coppia[0].casefold())

    truncated = len(entries_totali) > MAX_DIRECTORY_ENTRIES
    entries_finali = entries_totali[:MAX_DIRECTORY_ENTRIES]

    return {
        "ok": True,
        "operation": "list_directory",
        "status": "success",
        "data": {
            "entries": [
                {"name": nome, "type": tipo} for nome, tipo in entries_finali
            ],
            "truncated": truncated,
        },
    }


# =====================================================================
# read_file: filename sensibile
# =====================================================================

_NOMI_SENSIBILI_ESATTI = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "credentials.json",
    ".npmrc",
    ".netrc",
}

_PREFISSI_SENSIBILI = (".env.", "secrets.")

_ESTENSIONI_SENSIBILI = (".pem", ".key", ".pfx", ".p12")


def _nome_file_sensibile(nome: str) -> bool:
    """
    Controllo high-precision, case-insensitive, sul solo nome del file
    (mai sul percorso completo). id_rsa.pub NON viene bloccato: non è
    un nome esatto né ha un'estensione sensibile.
    """

    nome_normalizzato = nome.lower()

    if nome_normalizzato in _NOMI_SENSIBILI_ESATTI:
        return True

    if any(nome_normalizzato.startswith(prefisso) for prefisso in _PREFISSI_SENSIBILI):
        return True

    if any(nome_normalizzato.endswith(estensione) for estensione in _ESTENSIONI_SENSIBILI):
        return True

    return False


# =====================================================================
# read_file: content guard dedicato (NON riusa memory_tools.py)
# =====================================================================

_PEM_PRIVATE_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
)

_TOKEN_PREFISSI_PATTERN = re.compile(
    r"\bsk-[A-Za-z0-9]{16,}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{16,}\b"
    r"|\bAIza[0-9A-Za-z_\-]{16,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"
    r"|\bAKIA[0-9A-Z]{12,}\b"
    r"|\bya29\.[0-9A-Za-z_\-]{16,}\b"
)

# Boundary che riconosce anche identificatori composti (CLIENT_SECRET,
# ACCESS_TOKEN) senza richiedere un underscore come confine di parola,
# ma blocca comunque parole più lunghe (SECRETARY, PASSWORDLESS).
_KEYWORD_SEGRETO = (
    r"(?<![A-Za-z])(?:password|passwd|pwd|secret|token|api[_\s-]?key)(?![A-Za-z])"
)
_ASSEGNAZIONE = r"[:=]"
_VALORE_QUOTATO = r"""['"]([^'"\n]{4,128})['"]"""

_SEGRETO_KEYWORD_PATTERN = re.compile(
    rf"{_KEYWORD_SEGRETO}[^\n]{{0,20}}?{_ASSEGNAZIONE}\s*{_VALORE_QUOTATO}",
    re.IGNORECASE,
)

_VALORE_SEMBRA_COSTANTE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_VALORE_SEMBRA_TEMPLATE = re.compile(r"^\$\{.*\}$|^%.*%$|^\{\{.*\}\}$")

_PLACEHOLDER_LETTERALI = {
    "changeme", "change_me", "change-me",
    "xxx", "todo", "fixme", "placeholder", "example",
    "yourpassword", "your_password", "your-password",
}


def _sembra_placeholder(valore: str) -> bool:
    """
    Riconosce placeholder/nomi-costante evidenti, senza un parser
    Python/JS: valori tra <...>, sintassi di template (${...}, %...%,
    {{...}}), costanti urlate stile SCREAMING_SNAKE_CASE, e una piccola
    lista fissa di parole placeholder comuni.
    """

    valore_pulito = valore.strip()

    if valore_pulito.startswith("<") and valore_pulito.endswith(">"):
        return True

    if _VALORE_SEMBRA_TEMPLATE.match(valore_pulito):
        return True

    if _VALORE_SEMBRA_COSTANTE.fullmatch(valore_pulito):
        return True

    if valore_pulito.lower().replace(" ", "_") in _PLACEHOLDER_LETTERALI:
        return True

    return False


def _contenuto_sensibile(testo: str) -> bool:
    """
    Guard dedicato ai file, indipendente dal Secret Guard della memoria
    (contratto diverso: qui serve un valore esplicitamente quotato, non
    testo naturale con copula opzionale). Alta precisione: privilegia i
    falsi negativi sui falsi positivi su codice sorgente normale.
    """

    if _PEM_PRIVATE_PATTERN.search(testo):
        return True

    if _TOKEN_PREFISSI_PATTERN.search(testo):
        return True

    corrispondenza = _SEGRETO_KEYWORD_PATTERN.search(testo)
    if corrispondenza and not _sembra_placeholder(corrispondenza.group(1)):
        return True

    return False


# =====================================================================
# read_file: binary/text detection ed encoding
# =====================================================================

_CARATTERI_CONTROLLO_AMMESSI = {"\n", "\r", "\t"}
_SOGLIA_CARATTERI_CONTROLLO = 0.01


def _sembra_testo(testo: str) -> bool:
    """
    Text-likeness stdlib-only: rapporto di caratteri di categoria
    Unicode "Cc" (controllo) non ammessi (tutti tranne \\n \\r \\t).
    Una stringa vuota è considerata testo valido (nessuna divisione
    per zero).
    """

    if not testo:
        return True

    non_ammessi = sum(
        1
        for carattere in testo
        if unicodedata.category(carattere) == "Cc"
        and carattere not in _CARATTERI_CONTROLLO_AMMESSI
    )

    return (non_ammessi / len(testo)) <= _SOGLIA_CARATTERI_CONTROLLO


def _decodifica_testo(dati: bytes):
    """
    Pipeline di rilevamento binario/testo, stdlib-only, mai
    errors="replace". Un NUL byte è sempre binario immediato. Sia il
    tentativo utf-8-sig sia il fallback cp1252 sono soggetti allo
    stesso controllo di text-likeness (non solo il fallback).

    Restituisce (testo, etichetta_encoding) oppure None se il
    contenuto va considerato binario.
    """

    if b"\x00" in dati:
        return None

    try:
        testo = dati.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        pass
    else:
        if _sembra_testo(testo):
            return testo, "utf-8"
        return None

    try:
        testo = dati.decode("cp1252", errors="strict")
    except UnicodeDecodeError:
        return None

    if _sembra_testo(testo):
        return testo, "cp1252"

    return None


def read_file(argomenti: dict, contesto: ContestoFilesystem) -> dict:
    """
    Handler del tool read_file.

    Ordine di sicurezza fisso: doppia validazione (I/O solo sul
    resolved_path della SECONDA) -> exists -> is_file -> nome sensibile
    su resolved_path.name -> apertura binaria con lettura limitata a
    MAX_READ_BYTES+1 -> controllo dimensione -> rilevamento
    binario/testo e decodifica -> content guard -> SOLO ORA il
    risultato di successo con content. Nessuno status diverso da
    "success" porta mai un campo content, né altri dettagli del file.
    """

    path_richiesto = argomenti.get("path") if isinstance(argomenti, dict) else None
    allowed_roots = contesto.allowed_roots if contesto is not None else []

    decisione = risolvi_path_autorizzato(path_richiesto, allowed_roots)
    if not decisione.allowed:
        return _risultato_deny("read_file", "access_denied")

    decisione_finale = risolvi_path_autorizzato(path_richiesto, allowed_roots)
    if not decisione_finale.allowed:
        return _risultato_deny("read_file", "access_denied")

    target = decisione_finale.resolved_path

    try:
        if not target.exists():
            return _risultato_deny("read_file", "not_found")

        if not target.is_file():
            return _risultato_deny("read_file", "not_a_file")

        if _nome_file_sensibile(target.name):
            return _risultato_deny("read_file", "sensitive_file")

        with target.open("rb") as file:
            dati = file.read(MAX_READ_BYTES + 1)
    except OSError:
        return _risultato_tool_error("read_file")

    if len(dati) > MAX_READ_BYTES:
        return _risultato_deny("read_file", "too_large")

    esito = _decodifica_testo(dati)
    if esito is None:
        return _risultato_deny("read_file", "binary_file")

    testo, encoding_label = esito

    if _contenuto_sensibile(testo):
        return _risultato_deny("read_file", "sensitive_file")

    return {
        "ok": True,
        "operation": "read_file",
        "status": "success",
        "data": {
            "content": testo,
            "encoding": encoding_label,
        },
    }


# =====================================================================
# Fallback deterministico del dominio filesystem
# =====================================================================


def _fallback_list_directory(risultato_tool: dict) -> str:
    status = risultato_tool.get("status")

    if status == "success":
        data = risultato_tool.get("data", {})
        entries = data.get("entries", [])

        if not entries:
            return "La directory è vuota."

        righe = ["Contenuto della directory:", ""]

        for entry in entries:
            righe.append(f"- {entry.get('name')} ({entry.get('type')})")

        if data.get("truncated"):
            righe.append("")
            righe.append(
                f"(elenco troncato ai primi {MAX_DIRECTORY_ENTRIES} elementi)"
            )

        return "\n".join(righe)

    if status == "access_denied":
        return "Non ho accesso a quella posizione."

    if status == "not_found":
        return "Non ho trovato quella directory."

    if status == "not_a_directory":
        return "Quel percorso non è una directory."

    return risultato_tool.get("error") or "Non sono riuscito a leggere quella directory."


def _fallback_read_file(risultato_tool: dict) -> str:
    status = risultato_tool.get("status")

    if status == "success":
        content = risultato_tool.get("data", {}).get("content", "")
        return f"Contenuto del file:\n\n{content}"

    if status == "access_denied":
        return "Non ho accesso a quel file."

    if status == "not_found":
        return "Non ho trovato quel file."

    if status == "not_a_file":
        return "Quel percorso non è un file."

    if status == "too_large":
        return "Il file supera il limite di lettura consentito."

    if status == "binary_file":
        return "Quel file non è leggibile come testo."

    if status == "sensitive_file":
        return "Il file è stato bloccato perché potrebbe contenere dati sensibili."

    return risultato_tool.get("error") or "Non sono riuscito a leggere quel file."


def fallback_deterministico_file(risultato_tool: dict) -> str:
    """
    Router del fallback deterministico per il dominio "filesystem".

    Sceglie il rendering in base a risultato_tool["operation"]: mai un
    dump generico, ogni tool ha il proprio fallback scritto a mano sui
    propri campi noti. Nessun risultato di errore include mai
    path/filename/frammenti di contenuto.
    """

    if risultato_tool.get("operation") == "read_file":
        return _fallback_read_file(risultato_tool)

    return _fallback_list_directory(risultato_tool)


def registra_tool_filesystem(registro: RegistroStrumenti) -> None:
    """Registra i tool del dominio "filesystem" nel RegistroStrumenti esistente."""

    registro.registra(
        ToolSpec(
            nome="list_directory",
            schema=TOOLS_FILE[0],
            handler=list_directory,
            livello="READ_ONLY",
            dominio="filesystem",
        )
    )

    registro.registra(
        ToolSpec(
            nome="read_file",
            schema=TOOLS_FILE[1],
            handler=read_file,
            livello="READ_ONLY",
            dominio="filesystem",
        )
    )
