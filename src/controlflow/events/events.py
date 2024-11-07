from typing import TYPE_CHECKING, Literal, Optional, Union

from pydantic import ConfigDict, field_validator, model_validator

from controlflow.agents.agent import Agent
from controlflow.events.base import Event, UnpersistedEvent
from controlflow.llm.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from controlflow.tools.tools import InvalidToolCall, Tool, ToolCall, ToolResult
from controlflow.utilities.logging import get_logger

if TYPE_CHECKING:
    from controlflow.events.message_compiler import CompileContext
logger = get_logger(__name__)

ORCHESTRATOR_PREFIX = "The following message is from the orchestrator."


class OrchestratorMessage(Event):
    """
    Messages from the orchestrator to agents.
    """

    event: Literal["orchestrator-message"] = "orchestrator-message"
    content: Union[str, list[Union[str, dict]]]
    prefix: Optional[str] = ORCHESTRATOR_PREFIX
    name: Optional[str] = None

    def to_messages(self, context: "CompileContext") -> list[BaseMessage]:
        messages = []
        # if self.prefix:
        #     messages.append(SystemMessage(content=self.prefix))
        messages.append(
            HumanMessage(content=f"({self.prefix})\n\n{self.content}", name=self.name)
        )
        return messages


class UserMessage(Event):
    event: Literal["user-message"] = "user-message"
    content: Union[str, list[Union[str, dict]]]

    def to_messages(self, context: "CompileContext") -> list[BaseMessage]:
        return [HumanMessage(content=self.content)]


class AgentMessage(Event):
    event: Literal["agent-message"] = "agent-message"
    agent: Agent
    message: dict

    @field_validator("message", mode="before")
    def _message(cls, v):
        if isinstance(v, BaseMessage):
            v = v.model_dump()
        v["type"] = "ai"
        return v

    @model_validator(mode="after")
    def _finalize(self):
        self.message["name"] = self.agent.name
        return self

    @property
    def ai_message(self) -> AIMessage:
        return AIMessage(**self.message)

    def to_tool_calls(self, tools: list[Tool]) -> list["AgentToolCall"]:
        calls = []
        for tool_call in (
            self.message["tool_calls"] + self.message["invalid_tool_calls"]
        ):
            tool = next((t for t in tools if t.name == tool_call.get("name")), None)
            if tool:
                calls.append(
                    AgentToolCall(
                        agent=self.agent,
                        tool_call=tool_call,
                        tool=tool,
                        args=tool_call["args"],
                    )
                )
        return calls

    def to_content(self) -> "AgentContent":
        return AgentContent(agent=self.agent, content=self.message["content"])

    def all_related_events(self, tools: list[Tool]) -> list[Event]:
        return [self, self.to_content()] + self.to_tool_calls(tools)

    def to_messages(self, context: "CompileContext") -> list[BaseMessage]:
        if self.agent.name == context.agent.name:
            return [self.ai_message]
        elif self.message["content"]:
            return OrchestratorMessage(
                prefix=f'The following message was posted by Agent "{self.agent.name}" with ID {self.agent.id}',
                content=self.message["content"],
                name=self.agent.name,
            ).to_messages(context)
        else:
            return []


class AgentMessageDelta(UnpersistedEvent):
    event: Literal["agent-message-delta"] = "agent-message-delta"

    agent: Agent
    delta: dict
    snapshot: dict

    @field_validator("delta", "snapshot", mode="before")
    def _message(cls, v):
        if isinstance(v, BaseMessage):
            v = v.model_dump()
        v["type"] = "AIMessageChunk"
        return v

    @model_validator(mode="after")
    def _finalize(self):
        self.delta["name"] = self.agent.name
        self.snapshot["name"] = self.agent.name
        return self

    @property
    def delta_message(self) -> AIMessageChunk:
        return AIMessageChunk(**self.delta)

    @property
    def snapshot_message(self) -> AIMessage:
        return AIMessage(**self.snapshot | {"type": "ai"})

    def to_tool_call_deltas(self, tools: list[Tool]) -> list["AgentToolCallDelta"]:
        deltas = []
        for call_delta in self.delta["tool_call_chunks"]:
            # try to retrieve the matching snapshot based on index
            call_snapshot = next(
                (
                    c
                    for i, c in enumerate(self.snapshot["tool_calls"])
                    if i == call_delta.get("index")
                ),
                None,
            )

            tool = next((t for t in tools if t.name == call_snapshot.get("name")), None)
            if call_snapshot:
                deltas.append(
                    AgentToolCallDelta(
                        agent=self.agent,
                        delta=call_delta,
                        snapshot=call_snapshot,
                        tool=tool,
                        args=call_snapshot["args"],
                    )
                )
        return deltas

    def to_content_delta(self) -> "AgentContentDelta":
        return AgentContentDelta(
            agent=self.agent,
            delta=self.delta["content"],
            snapshot=self.snapshot["content"],
        )

    def all_related_events(self, tools: list[Tool]) -> list[Event]:
        return [self, self.to_content_delta()] + self.to_tool_call_deltas(tools)


class AgentContent(UnpersistedEvent):
    event: Literal["agent-content"] = "agent-content"
    agent: Agent
    content: Union[str, list[Union[str, dict]]]


class AgentContentDelta(UnpersistedEvent):
    event: Literal["agent-content-delta"] = "agent-content-delta"
    agent: Agent
    delta: str
    snapshot: str


class AgentToolCallDelta(UnpersistedEvent):
    event: Literal["agent-tool-call-delta"] = "agent-tool-call-delta"
    agent: Agent
    delta: dict
    snapshot: dict
    tool: Tool
    args: dict


class EndTurn(Event):
    event: Literal["end-turn"] = "end-turn"
    agent: Agent
    next_agent_name: Optional[str] = None


class AgentToolCall(Event):
    event: Literal["tool-call"] = "tool-call"
    agent: Agent
    tool_call: Union[ToolCall, InvalidToolCall]
    tool: Tool
    args: dict


class ToolResult(Event):
    event: Literal["tool-result"] = "tool-result"
    agent: Agent
    tool_call: Union[ToolCall, InvalidToolCall]
    tool_result: ToolResult

    def to_messages(self, context: "CompileContext") -> list[BaseMessage]:
        if self.agent.name == context.agent.name:
            return [
                ToolMessage(
                    content=self.tool_result.str_result,
                    tool_call_id=self.tool_call["id"],
                    name=self.agent.name,
                )
            ]
        else:
            return OrchestratorMessage(
                prefix=f'Agent "{self.agent.name}" with ID {self.agent.id} made a tool '
                f'call: {self.tool_call}. The tool{" failed and" if self.tool_result.is_error else " "} '
                f'produced this result:',
                content=self.tool_result.str_result,
                name=self.agent.name,
            ).to_messages(context)
