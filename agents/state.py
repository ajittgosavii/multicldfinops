"""The graph's shared state.

One TypedDict travels the whole supervisor loop. It is deliberately small:
the conversation itself (`messages`) carries the substance, and the extra keys
exist only to steer routing and to make the loop terminate.

`messages` uses the `add_messages` reducer so every node -- supervisor and each
react specialist -- appends rather than overwrites. Without the reducer a
specialist's answer would clobber the supervisor's context and the next routing
decision would be blind.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class FinOpsState(TypedDict):
    """State shared by the supervisor and the four specialists.

    messages    the running transcript; `add_messages` appends per node.
    question    the user's original question, kept verbatim for the specialists
                so a long tool-chatter transcript never buries the ask.
    findings    structured breadcrumbs each specialist can leave for the next
                (tool name + headline figure); the UI can render an audit trail.
    next_agent  the supervisor's most recent routing decision.
    iterations  hard-stop guard; the supervisor increments it and forces FINISH
                once it crosses the ceiling, so a mis-routing loop cannot run the
                bill up or blow the recursion limit.
    persona     who is asking (Leadership / FinOps Practitioner / Engineering /
                Finance); every prompt is tuned to it.
    """

    messages: Annotated[list, add_messages]
    question: str
    findings: list[dict]
    next_agent: str
    iterations: int
    persona: str
