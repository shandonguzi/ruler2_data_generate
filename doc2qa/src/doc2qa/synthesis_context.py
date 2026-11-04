"""Keep generation evidence scoped to each sync call or async Context task.

DeepEval 4.3.2 does not pass evidence to its styling template or task rules to
its answer template. Preserve those inputs without changing package files or
sharing mutable evidence across concurrently generated Contexts.
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class SynthesisInputs:
    context: str | list[str] | None
    task: str | None


_inputs = ContextVar("doc2qa_synthesis_inputs", default=None)


def generation_inputs(context=None, task=None):
    scoped = _inputs.get()
    if context is None and scoped is not None:
        context = scoped.context
    if task is None and scoped is not None:
        task = scoped.task
    if context is None:
        raise ValueError("A reference Context is required for styling and answer generation")
    return context, task


class ContextAwareGenerationMixin:
    def generate_goldens_from_contexts(self, *args, **kwargs):
        token = _inputs.set(SynthesisInputs(None, self.styling_config.task))
        try:
            return super().generate_goldens_from_contexts(*args, **kwargs)
        finally:
            _inputs.reset(token)

    def _set_generation_context(self, context):
        # The synchronous generation loop supplies each Context directly,
        # independently of any filtering or rewriting step.
        if _inputs.get() is not None:
            _inputs.set(SynthesisInputs(context, self.styling_config.task))

    async def _a_generate_from_context(self, semaphore, context, *args, **kwargs):
        # Child question tasks inherit this ContextVar; sibling Context tasks
        # retain their own evidence even when their query strings are identical.
        chunk_sources = kwargs.get("context_chunk_source_files")
        index = kwargs.get("context_index", 0)
        formatted = self._format_context_with_sources(
            context=context,
            chunk_source_files=(
                chunk_sources[index]
                if chunk_sources is not None and index < len(chunk_sources)
                else None
            ),
        )
        token = _inputs.set(SynthesisInputs(formatted, self.styling_config.task))
        try:
            return await super()._a_generate_from_context(semaphore, context, *args, **kwargs)
        finally:
            _inputs.reset(token)
