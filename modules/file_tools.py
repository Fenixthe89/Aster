"""Primo tool filesystem di Aster: list_directory (dominio "filesystem")."""

from dataclasses import dataclass
from pathlib import Path

from modules.filesystem_policy import risolvi_path_autorizzato
from modules.tool_registry import RegistroStrumenti, ToolSpec

MAX_DIRECTORY_ENTRIES = 100

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


def _risultato_deny(status: str) -> dict:
    return {
        "ok": False,
        "operation": "list_directory",
        "status": status,
    }


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
        return _risultato_deny("access_denied")

    # Rivalidazione immediatamente prima dell'I/O: usiamo esclusivamente
    # il resolved_path di QUESTA seconda decisione.
    decisione_finale = risolvi_path_autorizzato(path_richiesto, allowed_roots)
    if not decisione_finale.allowed:
        return _risultato_deny("access_denied")

    target = decisione_finale.resolved_path

    try:
        if not target.exists():
            return _risultato_deny("not_found")

        if not target.is_dir():
            return _risultato_deny("not_a_directory")

        entries_totali = [
            (voce.name, _classifica_entry(voce)) for voce in target.iterdir()
        ]
    except OSError:
        # Il messaggio nativo dell'eccezione può contenere il path
        # assoluto: non lo esponiamo mai, nemmeno nell'errore.
        return {
            "ok": False,
            "operation": "list_directory",
            "status": "tool_error",
            "error": "Errore durante l'accesso alla directory.",
        }

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


def fallback_deterministico_file(risultato_tool: dict) -> str:
    """
    Fallback deterministico per il dominio "filesystem", solo per
    list_directory in questo step. Non fa json.dumps: costruisce una
    risposta leggibile solo dai campi noti, mai un path assoluto (il
    risultato non ne contiene comunque alcuno).
    """

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


def registra_tool_filesystem(registro: RegistroStrumenti) -> None:
    """Registra list_directory nel RegistroStrumenti esistente (dominio "filesystem")."""

    registro.registra(
        ToolSpec(
            nome="list_directory",
            schema=TOOLS_FILE[0],
            handler=list_directory,
            livello="READ_ONLY",
            dominio="filesystem",
        )
    )
