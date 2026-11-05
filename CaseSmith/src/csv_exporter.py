"""Final CSV exporter for the CaseSmith persona pipeline.

Flattens each completed interaction into one row per conversation turn:

    original CSV columns (id, dialect, open_question, open_answer)
    + persona_id, turn, improved_query, noisy_query, answer, domain, scenario,
      query_id, source

``improved_query`` is the clean (style-transferred) user query, ``noisy_query``
is the text actually used in the conversation (equal to the clean one when the
distractor did not fire), and ``answer`` is the assistant reply for that turn.
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

ORIGINAL_COLUMNS = ['id', 'dialect', 'open_question', 'open_answer']

NEW_COLUMNS = [
    'persona_id', 'turn', 'improved_query', 'noisy_query', 'answer',
    'domain', 'scenario', 'query_id', 'source',
]

OUTPUT_COLUMNS = ORIGINAL_COLUMNS + NEW_COLUMNS

# Columns that are identical for every turn of a query: only the first row of
# the query carries them, later rows leave them empty.
TURN_COLUMNS = ['turn', 'improved_query', 'noisy_query', 'answer']
QUERY_LEVEL_COLUMNS = [c for c in OUTPUT_COLUMNS if c not in TURN_COLUMNS]

FAILED_COLUMNS = ORIGINAL_COLUMNS + [
    'query_id', 'persona_id', 'stage', 'error', 'attempts',
]

FAILED_STATUSES = {'failed_query', 'failed_interaction'}


class CSVExporter:
    """Write the final turn-level CSV (and the failed-query list)."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        output_cfg = config.get('output', {}) or {}
        self.encoding = output_cfg.get('encoding', 'utf-8')
        self.lineterminator = output_cfg.get('lineterminator', '\r\n')
        self.include_unanswered = bool(output_cfg.get('include_unanswered', False))
        self.on_failure = output_cfg.get('on_failure', 'skip')

    @staticmethod
    def _conversation(interaction: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Accept both raw Interaction dicts (messages) and TrainingSample dicts."""
        messages = interaction.get('messages')
        if isinstance(messages, list):
            return [m for m in messages if isinstance(m, dict)]
        conversation = interaction.get('full_conversation')
        if isinstance(conversation, list):
            return [m for m in conversation if isinstance(m, dict)]
        return []

    @staticmethod
    def _answers_by_turn(conversation: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        answers: Dict[int, Dict[str, Any]] = {}
        fallback = 0
        for msg in conversation:
            if str(msg.get('role', '')).lower() != 'assistant':
                continue
            meta = msg.get('metadata') or {}
            turn = meta.get('turn')
            if not isinstance(turn, int):
                turn = fallback
            fallback = turn + 1
            answers.setdefault(turn, msg)
        return answers

    def interaction_to_rows(self, interaction: Dict[str, Any],
                            assignment: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Pair every user message with its assistant reply."""
        row = assignment.get('row') or {}
        base = {col: row.get(col, '') for col in ORIGINAL_COLUMNS}
        persona_id = assignment.get('persona_id') or interaction.get('persona_id', '')
        query_id = base.get('id', '') or assignment.get('query_id', '')
        source = assignment.get('source', '')
        persona_features = interaction.get('persona_features') or {}
        domain = assignment.get('inferred_domain') or persona_features.get('domain', '')
        scenario = assignment.get('inferred_scenario') or persona_features.get('scenario', '')

        conversation = self._conversation(interaction)
        answers = self._answers_by_turn(conversation)

        rows: List[Dict[str, Any]] = []
        fallback_turn = 0
        first_row = True
        for msg in conversation:
            if str(msg.get('role', '')).lower() != 'user':
                continue
            meta = msg.get('metadata') or {}
            turn0 = meta.get('turn')
            if not isinstance(turn0, int):
                turn0 = fallback_turn
            fallback_turn = turn0 + 1

            answer_msg = answers.get(turn0)
            if answer_msg is None and not self.include_unanswered:
                continue

            improved = meta.get('clean_version')
            if improved is None:
                improved = msg.get('content', '')

            row = {
                **base,
                'persona_id': persona_id,
                'turn': turn0 + 1,
                'improved_query': improved,
                'noisy_query': msg.get('content', ''),
                'answer': (answer_msg or {}).get('content', ''),
                'domain': domain,
                'scenario': scenario,
                'query_id': query_id,
                'source': source,
            }
            if not first_row:
                # Query-level values are already on the first row; do not repeat.
                for col in QUERY_LEVEL_COLUMNS:
                    row[col] = ''

            rows.append(row)
            first_row = False

        return rows

    def _write_csv(self, path: Path, fieldnames: List[str],
                   rows: List[Dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.tmp')
        with open(tmp, 'w', encoding=self.encoding, newline='') as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                lineterminator=self.lineterminator,
                extrasaction='raise',
            )
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)

    def export(self, assignments: List[Dict[str, Any]],
               interactions: Dict[str, Dict[str, Any]],
               output_path: str,
               failed_path: Optional[str] = None) -> Dict[str, Any]:
        """Export the final CSV.

        Args:
            assignments: Assignment state entries (one per sampled query).
            interactions: Mapping of query_id -> interaction dict.
            output_path: Destination CSV.
            failed_path: Optional destination for failed queries.
        """
        rows: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []
        pending = 0

        for assignment in assignments:
            status = assignment.get('status')
            interaction = interactions.get(assignment.get('query_id'))

            if interaction is None:
                if status in FAILED_STATUSES:
                    failed.append(assignment)
                elif status == 'done':
                    missing.append(assignment)
                else:
                    pending += 1
                continue

            rows.extend(self.interaction_to_rows(interaction, assignment))

        if self.on_failure == 'placeholder':
            for assignment in failed:
                row = assignment.get('row') or {}
                base = {col: row.get(col, '') for col in ORIGINAL_COLUMNS}
                rows.append({
                    **base,
                    'persona_id': assignment.get('persona_id', ''),
                    'turn': 1,
                    'improved_query': assignment.get('adapted_query') or '',
                    'noisy_query': '',
                    'answer': '',
                    'domain': assignment.get('inferred_domain') or '',
                    'scenario': assignment.get('inferred_scenario') or '',
                    'query_id': base.get('id', '') or assignment.get('query_id', ''),
                    'source': assignment.get('source', ''),
                })

        output = Path(output_path)
        self._write_csv(output, OUTPUT_COLUMNS, rows)

        if failed_path:
            failed_file = Path(failed_path)
            if failed:
                failed_rows = []
                for assignment in failed:
                    row = assignment.get('row') or {}
                    failed_rows.append({
                        **{col: row.get(col, '') for col in ORIGINAL_COLUMNS},
                        'query_id': row.get('id', '') or assignment.get('query_id', ''),
                        'persona_id': assignment.get('persona_id', ''),
                        'stage': assignment.get('status', ''),
                        'error': assignment.get('error', ''),
                        'attempts': assignment.get('attempts', 0),
                    })
                self._write_csv(failed_file, FAILED_COLUMNS, failed_rows)
            elif failed_file.exists():
                failed_file.unlink()

        summary = {
            'output_path': str(output),
            'rows': len(rows),
            'queries_written': len({r['query_id'] for r in rows if r['query_id']}),
            'failed_queries': len(failed),
            'missing_interactions': len(missing),
            'pending_queries': pending,
        }
        logging.info(f"CSV export summary: {summary}")
        return summary
