"""Tools do agente elevec_sdr (Renata)."""

from .agenda import (
    TOOLS_AGENDA,
    calendar_agendar,
    calendar_delete,
    calendar_get_event,
    calendar_get_many,
    calendar_update,
)
from .crm import TOOLS_CRM, update_crm
from .handover import TOOLS_HANDOVER, human_handover

# A lista que o agente recebe. Ordem = a do SOP: agenda, funil, saída.
TOOLS_ELEVEC = [*TOOLS_AGENDA, *TOOLS_CRM, *TOOLS_HANDOVER]

__all__ = [
    "TOOLS_AGENDA",
    "TOOLS_CRM",
    "TOOLS_ELEVEC",
    "TOOLS_HANDOVER",
    "calendar_agendar",
    "calendar_delete",
    "calendar_get_event",
    "calendar_get_many",
    "calendar_update",
    "human_handover",
    "update_crm",
]
