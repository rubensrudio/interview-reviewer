"""Scripted stand-in for the LLM client (CT-19). Tests never call a real LLM.

``FakeLLM`` returns the responses scripted for each task name, in order. A response may be:

- an instance of the requested ``output_model`` (returned as is);
- a ``dict`` (validated into ``output_model``);
- a ``str`` (parsed as JSON into ``output_model``);
- an exception instance or class (raised, e.g. ``LLMUnavailable``).

A dict or string that does not match ``output_model`` raises ``LLMInvalidOutput``, like the
real client. Every call is recorded in ``calls`` so tests can inspect the prompts.
"""

from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from app.llm.client import LLMInvalidOutput


@dataclass(frozen=True)
class FakeLLMCall:
    task: str
    system: str
    user: str
    output_model: type[BaseModel]


class FakeLLM:
    """In-memory ``LLMClient`` with responses scripted per task name."""

    def __init__(self, responses: dict[str, list[object]]) -> None:
        self._responses = {task: list(items) for task, items in responses.items()}
        self.calls: list[FakeLLMCall] = []

    def calls_for(self, task: str) -> list[FakeLLMCall]:
        return [call for call in self.calls if call.task == task]

    def remaining(self, task: str) -> int:
        return len(self._responses.get(task, []))

    def complete_structured[T: BaseModel](
        self, task: str, system: str, user: str, output_model: type[T]
    ) -> T:
        self.calls.append(FakeLLMCall(task, system, user, output_model))
        queue = self._responses.get(task)
        if not queue:
            raise AssertionError(f"FakeLLM has no scripted response left for task {task!r}")
        response = queue.pop(0)

        if isinstance(response, BaseException):
            raise response
        if isinstance(response, type) and issubclass(response, BaseException):
            raise response()
        if isinstance(response, output_model):
            return response
        try:
            if isinstance(response, str):
                return output_model.model_validate_json(response)
            if isinstance(response, dict):
                return output_model.model_validate(response)
        except ValidationError as error:
            raise LLMInvalidOutput("fake response does not match the output model") from error
        raise AssertionError(
            f"FakeLLM response for task {task!r} has unsupported type {type(response).__name__}"
        )
