"""Registro generico dei tool: nome -> ToolSpec, schema aggregati, dispatch validato."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from modules.memory import StatoMemoria
from modules.memory_session import MemorySessionState
from modules.memory_tools import TOOLS_MEMORIA, esegui_tool_memoria

# Livelli di governance ammessi. FORBIDDEN è solo una policy: un tool
# classificato FORBIDDEN non ottiene mai un handler eseguibile (vedi
# RegistroStrumenti.dispatch). Non esiste ancora un'infrastruttura
# generica di pending per CONFIRM_REQUIRED: per i tool memoria il
# pending resta gestito interamente da esegui_tool_memoria e da
# modules/memory_session.py, invariati.
LIVELLI_AMMESSI = frozenset({
    "READ_ONLY",
    "CONFIRM_REQUIRED",
    "FORBIDDEN",
})


@dataclass(frozen=True)
class ToolSpec:
    """Un singolo tool registrato: schema per il modello, handler, livello e dominio."""

    nome: str
    schema: dict
    handler: Callable[[dict, object], dict]
    livello: str
    dominio: str

    def __post_init__(self):
        if not isinstance(self.nome, str) or not self.nome.strip():
            raise ValueError("nome deve essere una stringa non vuota.")

        if not isinstance(self.schema, dict):
            raise TypeError("schema deve essere un dizionario.")

        nome_schema = self.schema.get("function", {}).get("name")
        if nome_schema != self.nome:
            raise ValueError(
                f"Il nome dello schema ('{nome_schema}') non corrisponde "
                f"al nome del tool ('{self.nome}')."
            )

        if not callable(self.handler):
            raise TypeError("handler deve essere una funzione chiamabile.")

        if self.livello not in LIVELLI_AMMESSI:
            raise ValueError(f"Livello non ammesso: {self.livello}.")

        if not isinstance(self.dominio, str) or not self.dominio.strip():
            raise ValueError("dominio deve essere una stringa non vuota.")


def _errore_tool_non_registrato(nome_tool: str) -> dict:
    """Errore strutturato quando il modello richiede un tool sconosciuto al registry."""

    return {
        "ok": False,
        "domain": None,
        "operation": nome_tool,
        "status": "tool_error",
        "error": f"Tool non registrato: {nome_tool}.",
    }


def _errore_tool_forbidden(tool_spec: ToolSpec) -> dict:
    """Errore strutturato per un tool classificato FORBIDDEN: mai eseguito."""

    return {
        "ok": False,
        "domain": tool_spec.dominio,
        "operation": tool_spec.nome,
        "status": "tool_error",
        "error": (
            "Tool non eseguibile per policy di governance (FORBIDDEN): "
            f"{tool_spec.nome}."
        ),
    }


class RegistroStrumenti:
    """Registro minimale: registrazione, lookup per nome, schema aggregati, dispatch."""

    def __init__(self):
        self._strumenti: dict[str, ToolSpec] = {}

    def registra(self, tool_spec: ToolSpec) -> None:
        """Registra un tool. Rifiuta la sovrascrittura silenziosa di un nome esistente."""

        if not isinstance(tool_spec, ToolSpec):
            raise TypeError("tool_spec deve essere un'istanza di ToolSpec.")

        if tool_spec.nome in self._strumenti:
            raise ValueError(
                f"Tool già registrato con questo nome: {tool_spec.nome}."
            )

        self._strumenti[tool_spec.nome] = tool_spec

    def trova(self, nome_tool: str) -> ToolSpec | None:
        """Restituisce il ToolSpec registrato per nome, o None se non registrato."""

        return self._strumenti.get(nome_tool)

    def elenco_schema(self) -> list[dict]:
        """Schema di tutti i tool registrati, nell'ordine di registrazione."""

        return [tool_spec.schema for tool_spec in self._strumenti.values()]

    def dispatch(self, nome_tool: str, argomenti: dict, contesto) -> dict:
        """
        Esegue il tool richiesto tramite il suo handler registrato.

        Un nome non registrato non chiama mai alcun handler: restituisce
        un errore strutturato. Un tool FORBIDDEN non viene mai eseguito,
        indipendentemente dagli argomenti ricevuti.
        """

        tool_spec = self.trova(nome_tool)

        if tool_spec is None:
            return _errore_tool_non_registrato(nome_tool)

        if tool_spec.livello == "FORBIDDEN":
            return _errore_tool_forbidden(tool_spec)

        return tool_spec.handler(argomenti, contesto)


@dataclass
class ContestoMemoria:
    """Stato minimo richiesto oggi dagli handler dei tool memoria."""

    stato_memoria: StatoMemoria
    stato_sessione: MemorySessionState
    percorso_memoria: Path
    limite_ricerca: int


# Classificazione di governance dei 7 tool memoria esistenti.
# In 0.6.1 è un'etichetta puramente informativa: il registry non la usa
# ancora per applicare policy di conferma o blocco. Le regole reali di
# pending/conferma restano interamente dentro esegui_tool_memoria.
_LIVELLO_TOOL_MEMORIA = {
    "cerca_memoria": "READ_ONLY",
    "crea_memoria": "CONFIRM_REQUIRED",
    "modifica_memoria": "CONFIRM_REQUIRED",
    "elimina_memoria": "CONFIRM_REQUIRED",
    "elimina_memoria_per_query": "CONFIRM_REQUIRED",
    "ripristina_memoria": "CONFIRM_REQUIRED",
    "gestisci_pending_memoria": "CONFIRM_REQUIRED",
}


def _crea_handler_memoria(nome_tool: str) -> Callable[[dict, ContestoMemoria], dict]:
    """Adapter minimo: inoltra la chiamata a esegui_tool_memoria, invariato."""

    def handler(argomenti: dict, contesto: ContestoMemoria) -> dict:
        return esegui_tool_memoria(
            nome_tool=nome_tool,
            argomenti=argomenti,
            stato_memoria=contesto.stato_memoria,
            stato_sessione=contesto.stato_sessione,
            percorso_memoria=contesto.percorso_memoria,
            limite_ricerca=contesto.limite_ricerca,
        )

    return handler


def crea_registro_memoria() -> RegistroStrumenti:
    """
    Costruisce un RegistroStrumenti con i 7 tool memoria esistenti,
    registrati come adapter su esegui_tool_memoria (memory_tools.py
    non viene modificato).
    """

    registro = RegistroStrumenti()

    for schema in TOOLS_MEMORIA:
        nome = schema["function"]["name"]

        registro.registra(
            ToolSpec(
                nome=nome,
                schema=schema,
                handler=_crea_handler_memoria(nome),
                livello=_LIVELLO_TOOL_MEMORIA.get(nome, "CONFIRM_REQUIRED"),
                dominio="memory",
            )
        )

    return registro
