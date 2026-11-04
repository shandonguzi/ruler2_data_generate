"""Observation hooks for the pinned DeepEval 4.3.2 async synthesis pipeline."""

from collections import defaultdict, deque
from contextvars import ContextVar

from .progress import context_scope, current_context, current_progress

_state = ContextVar("synthesis_state", default=None)
_item = ContextVar("synthesis_item", default=None)
_evolving = ContextVar("synthesis_evolving", default=False)


class SynthesisProgressMixin:
    async def a_generate_goldens_from_contexts(self, *args, **kwargs):
        # Replace the nested Rich bars with explicit stage messages during a run.
        if current_progress.get() and kwargs.get("_progress") is None:
            from rich.progress import Progress

            progress = Progress(disable=True)
            kwargs["_progress"] = progress
            contexts = kwargs.get("contexts", args[0] if args else [])
            kwargs["_pbar_id"] = progress.add_task("synthesis", total=len(contexts))
        return await super().a_generate_goldens_from_contexts(*args, **kwargs)

    async def _a_generate_from_context(self, *args, **kwargs):
        if not current_progress.get():
            return await super()._a_generate_from_context(*args, **kwargs)
        index = kwargs["context_index"]
        sources = kwargs.get("source_files") or []
        context_id = current_context.get() or f"deepeval_context_{index}"
        if index < len(sources) and isinstance(sources[index], str):
            context_id = sources[index]
        token = _state.set(
            {
                "indices": defaultdict(deque),
                "answers": 0,
                "input_total": 0,
                "max_goldens": kwargs["max_goldens_per_context"],
            }
        )
        try:
            with context_scope(context_id):
                return await super()._a_generate_from_context(*args, **kwargs)
        finally:
            _state.reset(token)

    async def _a_generate_inputs(self, prompt):
        result = await super()._a_generate_inputs(prompt)
        reporter = current_progress.get()
        if reporter and _state.get() is not None:
            state = _state.get()
            used = min(len(result), state["max_goldens"])
            state["input_total"] = used
            for index, item in enumerate(result[:used], 1):
                state["indices"][item.input].append(index)
            reporter.record(
                "input_generation",
                "result",
                f"初始问题生成完成：返回 {len(result)} 条，本次使用 {used} 条；最终合格数量待审核",
                counts={"inputs_returned": len(result), "inputs_used": used},
                output=[item.model_dump() for item in result],
            )
        return result

    def _prepare_query(self, input, qa_type):
        state = _state.get()
        if current_progress.get() and state is not None:
            # DeepEval creates one task per generated question; ContextVar keeps
            # the identity with that task even when evolution/styling are off.
            indices = state["indices"][input]
            _item.set(
                {
                    "item_index": indices.popleft() if indices else None,
                    "item_total": state["input_total"],
                    "question": input,
                    "qa_type": qa_type,
                }
            )

    async def _a_evolve_input(self, input, num_evolutions, evolutions, **kwargs):
        token = _evolving.set(True)
        try:
            result = await super()._a_evolve_input(input, num_evolutions, evolutions, **kwargs)
            if _item.get():
                _item.set({**_item.get(), "question": result[0]})
            return result
        finally:
            _evolving.reset(token)

    async def _a_generate_schema(self, prompt, schema, model):
        reporter = current_progress.get()
        if not reporter:
            return await super()._a_generate_schema(prompt, schema, model)
        stages = {
            "SyntheticDataList": ("input_generation", "批量生成初始问题"),
            "SyntheticData": ("question_styling", "调整问题表述"),
        }
        stage, title = stages.get(schema.__name__, ("structured_generation", schema.__name__))
        item = _item.get() or {}
        if item:
            title += f" {item['item_index']}/{item['item_total']}"
        with reporter.operation(stage, title, model=model.name, **item) as details:
            result = await super()._a_generate_schema(prompt, schema, model)
            details["output"] = result.model_dump()
            if schema.__name__ == "SyntheticData" and item:
                _item.set({**item, "question": result.input, "qa_type": result.qa_type})
            return result

    async def _a_generate(self, prompt):
        reporter = current_progress.get()
        if not reporter:
            return await super()._a_generate(prompt)
        evolving = _evolving.get()
        stage, title = (
            ("question_evolution", "演化问题") if evolving else ("answer_generation", "生成答案")
        )
        item = _item.get() or {}
        if item:
            title += f" {item['item_index']}/{item['item_total']}"
        with reporter.operation(stage, title, model=self.model.name, **item) as details:
            result = await super()._a_generate(prompt)
            details["output"] = result
            if not evolving and _state.get() is not None:
                state = _state.get()
                state["answers"] += 1
                details["counts"] = {"answers_generated": state["answers"]}
            return result
