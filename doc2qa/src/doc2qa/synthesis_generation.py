"""Generate queries, optionally evolve/style them, then generate answers.

Skip DeepEval 4.3.2's internal input scoring and low-score rewriting.

The two orchestration methods are adapted from
``deepeval/synthesizer/synthesizer.py`` (Apache-2.0), Copyright 2024 Confident AI Inc.
See licenses/deepeval-LICENSE.md. Keep the pinned model dispatch, concurrency,
metadata, progress and cost behavior, use local templates, and enable styling
only when num_evolutions > 0. Legacy styling kwargs are intentionally omitted.
"""

import asyncio
from contextlib import nullcontext
from typing import List, Optional, Union

from rich import print
from rich.progress import Progress

from deepeval.dataset.golden import Golden
from deepeval.progress_context import synthesizer_progress_context
from deepeval.utils import add_pbar, get_or_create_event_loop, remove_pbars, update_pbar

from .synthesis_templates import SynthesizerTemplate
from .synthesis_schema import SyntheticData, SyntheticDataList


class EvolutionStylingMixin:
    def _validate_generated_type(self, item):
        if item.qa_type not in self.allowed_question_types:
            raise ValueError(f"Generated qa_type is not configured: {item.qa_type}")
        return item

    def _generate_inputs(self, prompt):
        result = self._generate_schema(prompt, SyntheticDataList, self.model)
        return [self._validate_generated_type(item) for item in result.data]

    async def _a_generate_inputs(self, prompt):
        result = await self._a_generate_schema(prompt, SyntheticDataList, self.model)
        return [self._validate_generated_type(item) for item in result.data]

    def _prepare_query(self, input, qa_type):
        """Optional progress hook, independent of whether evolution is enabled."""

    def generate_goldens_from_contexts(
        self,
        contexts: List[List[str]],
        include_expected_output: bool = True,
        max_goldens_per_context: int = 2,
        source_files: Optional[List[Union[str, List[str]]]] = None,
        context_chunk_source_files: Optional[List[List[str]]] = None,
        target_files_per_context: Optional[int] = None,
        _context_scores: Optional[List[float]] = None,
        _progress: Optional[Progress] = None,
        _pbar_id: Optional[int] = None,
        _send_data: bool = True,
        _reset_cost: bool = True,
    ) -> List[Golden]:
        if _reset_cost:
            self.synthetic_goldens = []
            self.synthesis_cost = 0 if self.using_native_model else None
        goldens: List[Golden] = []

        if self.async_mode:
            loop = get_or_create_event_loop()
            goldens.extend(
                loop.run_until_complete(
                    self.a_generate_goldens_from_contexts(
                        contexts=contexts,
                        include_expected_output=include_expected_output,
                        max_goldens_per_context=max_goldens_per_context,
                        source_files=source_files,
                        context_chunk_source_files=context_chunk_source_files,
                        target_files_per_context=target_files_per_context,
                    )
                )
            )
        else:
            with (
                synthesizer_progress_context(
                    method="default",
                    num_evolutions=self.evolution_config.num_evolutions,
                    evolutions=self.evolution_config.evolutions,
                    evaluation_model=self.model.get_model_name(),
                    embedder=None,
                    max_generations=len(contexts) * max_goldens_per_context,
                    async_mode=False,
                    progress=_progress,
                    pbar_id=_pbar_id,
                    pbar_total=len(contexts),
                ) as (progress, pbar_id),
                progress if _progress is None else nullcontext(),
            ):
                for context_index, context in enumerate(contexts):
                    formatted_context = self._format_context_with_sources(
                        context=context,
                        chunk_source_files=(
                            context_chunk_source_files[context_index]
                            if context_chunk_source_files is not None
                            and context_index < len(context_chunk_source_files)
                            else None
                        ),
                    )
                    # Calculate pbar lengths
                    should_style = self.evolution_config.num_evolutions > 0
                    pbar_len_style = 1 if should_style else 0
                    pbar_len_expected_output = 1 if include_expected_output else 0
                    pbar_len_evolve = (
                        self.evolution_config.num_evolutions
                        + pbar_len_style
                        + pbar_len_expected_output
                    )

                    # Add pbars
                    pbar_generate_goldens_id = add_pbar(
                        progress,
                        f"\t⚡ Generating goldens from context #{context_index}",
                        total=1 + max_goldens_per_context,
                    )
                    pbar_generate_inputs_id = add_pbar(
                        progress,
                        f"\t\t💡 Generating {max_goldens_per_context} input(s)",
                        total=1,
                    )
                    pbar_evolve_input_ids = []
                    for i in range(max_goldens_per_context):
                        pbar_evolve_input_ids.append(
                            add_pbar(
                                progress,
                                f"\t\t🧬 Evolving input #{i}",
                                total=pbar_len_evolve,
                            )
                        )

                    # Generate inputs
                    prompt = SynthesizerTemplate.generate_synthetic_inputs(
                        context=formatted_context,
                        max_goldens_per_context=max_goldens_per_context,
                        task=self.styling_config.task,
                        available_source_files=(
                            source_files[context_index]
                            if source_files is not None
                            and context_index < len(source_files)
                            and isinstance(source_files[context_index], list)
                            else None
                        ),
                        target_files_per_context=target_files_per_context,
                    )
                    synthetic_inputs = self._generate_inputs(prompt)
                    update_pbar(progress, pbar_generate_inputs_id, remove=False)

                    synthetic_inputs = synthetic_inputs[:max_goldens_per_context]
                    self._set_generation_context(formatted_context)
                    update_pbar(progress, pbar_generate_goldens_id, remove=False)

                    for input_index, data in enumerate(synthetic_inputs):
                        context_source_files: List[str] = []
                        if source_files is not None and context_index < len(source_files):
                            context_source = source_files[context_index]
                            if isinstance(context_source, list):
                                context_source_files = context_source
                            elif context_source:
                                context_source_files = [context_source]

                        self._prepare_query(data.input, data.qa_type)
                        evolved_input, evolutions_used, qa_type = data.input, [], data.qa_type
                        if should_style:
                            evolved_input, evolutions_used = self._evolve_input(
                                input=data.input,
                                context=formatted_context,
                                num_evolutions=self.evolution_config.num_evolutions,
                                evolutions=self.evolution_config.evolutions,
                                progress=progress,
                                pbar_evolve_input_id=pbar_evolve_input_ids[input_index],
                                remove_pbar=False,
                            )
                            prompt = SynthesizerTemplate.rewrite_evolved_input(
                                evolved_input=evolved_input,
                                task=self.styling_config.task,
                            )
                            update_pbar(
                                progress,
                                pbar_evolve_input_ids[input_index],
                                remove=False,
                            )
                            res: SyntheticData = self._generate_schema(
                                prompt,
                                SyntheticData,
                                self.model,
                            )
                            qa_type = self._validate_generated_type(res).qa_type
                            evolved_input = res.input

                        # Synthesize Golden
                        golden = Golden(
                            input=evolved_input,
                            context=context,
                            source_file=(context_source_files[0] if context_source_files else None),
                            additional_metadata={
                                "qa_type": qa_type,
                                "initial_qa_type": data.qa_type,
                                "evolutions": evolutions_used,
                                "context_source_files": context_source_files,
                                "used_source_files": (data.used_source_files or []),
                                "context_quality": (
                                    _context_scores[context_index]
                                    if _context_scores is not None
                                    else None
                                ),
                            },
                        )

                        # Generated expected output
                        if include_expected_output:
                            prompt = SynthesizerTemplate.generate_synthetic_expected_output(
                                input=golden.input,
                                context="\n".join(golden.context),
                                expected_output_format=self.styling_config.expected_output_format,
                            )
                            res = self._generate(prompt)
                            golden.expected_output = res
                            update_pbar(
                                progress,
                                pbar_evolve_input_ids[input_index],
                                remove=False,
                            )

                        goldens.append(golden)
                        update_pbar(progress, pbar_generate_goldens_id, remove=False)

                    # Add remaining progress if not enough goldens generated
                    update_pbar(progress, pbar_id, remove=False)
                    remove_pbars(
                        progress,
                        pbar_evolve_input_ids + [pbar_generate_inputs_id, pbar_generate_goldens_id],
                    )

                # Remove pbar if not from docs
                remove_pbars(progress, [pbar_id]) if _progress is None else None

        if _send_data:
            pass
        if _reset_cost and self.cost_tracking and self.using_native_model:
            print(f"💰 API cost: {self.synthesis_cost:.6f}")
        self.synthetic_goldens.extend(goldens)
        return goldens

    async def _a_generate_from_context(
        self,
        semaphore: asyncio.Semaphore,
        context: List[str],
        goldens: List[Golden],
        include_expected_output: bool,
        max_goldens_per_context: int,
        source_files: Optional[List[Union[str, List[str]]]],
        context_chunk_source_files: Optional[List[List[str]]],
        target_files_per_context: Optional[int],
        context_index: int,
        progress: Optional[Progress] = None,
        pbar_id: Optional[int] = None,
        context_scores: Optional[List[float]] = None,
    ):
        # Calculate pbar lengths
        should_style = self.evolution_config.num_evolutions > 0
        pbar_len_style = 1 if should_style else 0
        pbar_len_expected_output = 1 if include_expected_output else 0
        pbar_len_evolve = (
            self.evolution_config.num_evolutions + pbar_len_style + pbar_len_expected_output
        )

        # Add pbars
        pbar_generate_goldens_id = add_pbar(
            progress,
            f"\t⚡ Generating goldens from context #{context_index}",
            total=1 + max_goldens_per_context,
        )
        pbar_generate_inputs_id = add_pbar(
            progress,
            f"\t\t💡 Generating {max_goldens_per_context} input(s)",
            total=1,
        )
        pbar_evolve_input_ids = []
        for i in range(max_goldens_per_context):
            pbar_evolve_input_ids.append(
                add_pbar(
                    progress,
                    f"\t\t🧬 Evolving input #{i}",
                    total=pbar_len_evolve,
                )
            )

        # Generate inputs
        formatted_context = self._format_context_with_sources(
            context=context,
            chunk_source_files=(
                context_chunk_source_files[context_index]
                if context_chunk_source_files is not None
                and context_index < len(context_chunk_source_files)
                else None
            ),
        )
        prompt = SynthesizerTemplate.generate_synthetic_inputs(
            context=formatted_context,
            max_goldens_per_context=max_goldens_per_context,
            task=self.styling_config.task,
            available_source_files=(
                source_files[context_index]
                if source_files is not None
                and context_index < len(source_files)
                and isinstance(source_files[context_index], list)
                else None
            ),
            target_files_per_context=target_files_per_context,
        )
        synthetic_inputs: List[SyntheticData] = await self._a_generate_inputs(prompt)
        # Limit the length of the synthetic inputs to the maximum allowed
        synthetic_inputs = synthetic_inputs[:max_goldens_per_context]
        update_pbar(progress, pbar_generate_inputs_id, remove=False)

        update_pbar(progress, pbar_generate_goldens_id, remove=False)

        # Helper function to process each input in parallel
        async def process_input(
            input_index: int,
            data: SyntheticData,
            progress: Optional[Progress] = None,
        ):
            context_source_files: List[str] = []
            if source_files is not None and context_index < len(source_files):
                context_source = source_files[context_index]
                if isinstance(context_source, list):
                    context_source_files = context_source
                elif context_source:
                    context_source_files = [context_source]

            self._prepare_query(data.input, data.qa_type)
            evolved_input, evolutions_used, qa_type = data.input, [], data.qa_type
            if should_style:
                evolved_input, evolutions_used = await self._a_evolve_input(
                    input=data.input,
                    context=formatted_context,
                    num_evolutions=self.evolution_config.num_evolutions,
                    evolutions=self.evolution_config.evolutions,
                    progress=progress,
                    pbar_evolve_input_id=pbar_evolve_input_ids[input_index],
                    remove_pbar=False,
                )
                prompt = SynthesizerTemplate.rewrite_evolved_input(
                    evolved_input=evolved_input,
                    task=self.styling_config.task,
                )
                res: SyntheticData = await self._a_generate_schema(
                    prompt,
                    SyntheticData,
                    self.model,
                )
                qa_type = self._validate_generated_type(res).qa_type
                evolved_input = res.input
                update_pbar(progress, pbar_evolve_input_ids[input_index], remove=False)

            # Generate expected output
            expected_output = None
            if include_expected_output:
                expected_output_prompt = SynthesizerTemplate.generate_synthetic_expected_output(
                    input=evolved_input,
                    context="\n".join(context),
                    expected_output_format=self.styling_config.expected_output_format,
                )
                expected_output = await self._a_generate(expected_output_prompt)
                update_pbar(progress, pbar_evolve_input_ids[input_index], remove=False)

            # Create Golden
            golden = Golden(
                input=evolved_input,
                context=context,
                expected_output=expected_output,
                source_file=(context_source_files[0] if context_source_files else None),
                additional_metadata={
                    "qa_type": qa_type,
                    "initial_qa_type": data.qa_type,
                    "evolutions": evolutions_used,
                    "context_source_files": context_source_files,
                    "used_source_files": (data.used_source_files or []),
                    # "context_quality": (
                    #     context_scores[data_index]
                    #     if context_scores is not None
                    #     else None
                    # ),
                },
            )
            update_pbar(progress, pbar_generate_goldens_id, remove=False)
            return golden

        # Process all inputs in parallel using asyncio.gather
        tasks = [
            self.task_wrapper(semaphore, process_input, index, data, progress)
            for index, data in enumerate(synthetic_inputs)
        ]
        results = await asyncio.gather(*tasks)

        # Add remaining progress if not enough goldens generated
        update_pbar(progress, pbar_id, remove=False)
        remove_pbars(
            progress,
            pbar_evolve_input_ids + [pbar_generate_inputs_id, pbar_generate_goldens_id],
        )
        goldens.extend(results)
