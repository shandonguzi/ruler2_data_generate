"""
Enhanced Pipeline Module

Complete pipeline integrating persona generation, query generation,
interaction generation, distractor model, and training data collection.
"""

import io
import sys
import os
import json
import yaml
import random
import hashlib
import secrets
import logging
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional

from .persona_bank import PersonaBank
from .sampling import PersonaSampler
from .persona_spec import PersonaSpec, PersonaSpecStorage, generate_persona_id
from .llm_client import create_llm_client, LLMFormulator
from .query_generator import UserQueryGenerator
from .query_storage import QueryStorage
from .interaction_generator import InteractionGenerator, InteractionStorage
from .distractor import DistractorModel, SemanticDistractorModel, create_distractor_model
from .training_data import TrainingDataCollector, TrainingDataExporter
from .csv_exporter import CSVExporter
from .config_validation import validate_config
from .colored_logger import ColoredLogger, print_colored
from tqdm import tqdm

# Import token tracker
try:
    from .token_tracker import get_tracker
except ImportError:
    def get_tracker():
        return None


class TeeOutput:
    """Capture stdout/stderr and write to both terminal and log file."""

    def __init__(self, original_stream, log_file, stream_name='stdout'):
        self.original_stream = original_stream
        self.log_file = log_file
        self.stream_name = stream_name

    def write(self, message):
        if message.strip():  # Only log non-empty messages
            try:
                self.original_stream.write(message)
                self.original_stream.flush()
            except UnicodeEncodeError:
                # Handle encoding issues on Windows
                self.original_stream.write(message.encode('utf-8', errors='replace').decode('ascii', errors='replace'))
                self.original_stream.flush()

            try:
                self.log_file.write(message)
                self.log_file.flush()
            except Exception:
                pass
        elif message == '\n':
            self.original_stream.write(message)
            self.log_file.write(message)

    def flush(self):
        self.original_stream.flush()
        self.log_file.flush()

    def isatty(self):
        return False


class EnhancedPersonaGenerationPipeline:
    """
    Complete pipeline for generating synthetic training data for prompt optimization.

    Stages:
    1. Persona Generation
    2. System Prompt Formulation
    3. Query Generation with Style Transfer
    4. Multi-turn Interaction Generation
    5. Distractor/Noise Application
    6. Training Data Collection and Export
    """

    def __init__(self, config_path: str):
        """
        Initialize the enhanced pipeline.

        Args:
            config_path: Path to configuration file
        """
        self.config_path = config_path
        self.config = self._load_config()
        self._setup_logging()

        # Draw a fresh internal seed for this run. Sampling is intentionally
        # non-reproducible: each run should pick different queries and a
        # different query -> persona mapping.
        self._seed_value = self._resolve_seed()

        # Track which stages to run
        self.stages = self.config.get('experiment', {}).get('stages', {})

        # Incremental mode
        self.incremental = self.config.get('experiment', {}).get('incremental', False)
        
        # Initialize token tracker
        self.token_tracker = get_tracker()
        if self.token_tracker:
            logging.info("Token tracking enabled")

        # Initialize LLM client first (needed by other components)
        self.llm_client = self._initialize_llm_client()

        # Initialize components
        if self.stages.get('persona_generation', True):
            self.persona_bank = self._initialize_persona_bank()
            self.sampler = self._initialize_sampler()
            self.storage = self._initialize_storage()
            self.formulator = self._initialize_formulator()

        if self.stages.get('query_generation', True):
            self.query_generator = self._initialize_query_generator()

        if self.stages.get('query_generation', True) or self.stages.get('interaction_generation', True):
            self.query_storage = self._initialize_query_storage()

        # Initialize distractor first (needed for interaction generation)
        if self.stages.get('interaction_generation', True) or self.stages.get('distractor_application', True):
            self.distractor = self._initialize_distractor()

        # Initialize training_collector first (needed by interaction_storage for format conversion)
        if self.stages.get('training_data_export', True) or self.stages.get('interaction_generation', True):
            self.training_collector = TrainingDataCollector(self.config)

        if self.stages.get('training_data_export', True):
            self.training_exporter = TrainingDataExporter(self.config)

        if self.stages.get('interaction_generation', True):
            self.interaction_generator = self._initialize_interaction_generator()
            # Initialize storage for incremental saving (pass collector for TrainingSample format)
            interactions_output_dir = self.config.get('paths', {}).get('interactions', {}).get(
                'output_dir', 'output/interactions'
            )
            # Save raw Interaction dicts (messages + metadata) so the CSV
            # exporter can read clean/noisy query versions and answers.
            self.interaction_storage = InteractionStorage(interactions_output_dir, collector=None)

        logging.info("Enhanced pipeline initialized successfully")
        if self.incremental:
            logging.info("Incremental mode enabled: will skip stages with existing outputs")
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        with open(self.config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)

        # Fail-fast validation (prevents mid-run crashes)
        cfg, _issues = validate_config(cfg, config_path=self.config_path)
        return cfg
    
    def _setup_logging(self):
        """Setup logging configuration with full output capture."""
        log_dir = Path(self.config['paths']['logs']['dir'])
        log_dir.mkdir(parents=True, exist_ok=True)

        log_level = self.config['paths']['logs']['level']

        # Create timestamped log file
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file_path = log_dir / f'pipeline_{timestamp}.log'

        # Open log file with UTF-8 encoding
        self.log_file = open(log_file_path, 'w', encoding='utf-8')

        # Capture stdout and stderr
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = TeeOutput(self._original_stdout, self.log_file, 'stdout')
        sys.stderr = TeeOutput(self._original_stderr, self.log_file, 'stderr')

        # Setup logging with custom handler
        class Utf8FileHandler(logging.FileHandler):
            def __init__(self, filename, mode='a', encoding='utf-8'):
                super().__init__(filename, mode, encoding)

        class SafeStreamHandler(logging.StreamHandler):
            def emit(self, record):
                try:
                    msg = self.format(record)
                    stream = self.stream
                    try:
                        stream.write(msg + self.terminator)
                    except UnicodeEncodeError:
                        stream.write(msg.encode('utf-8', errors='replace').decode('ascii', errors='replace') + self.terminator)
                    self.flush()
                except Exception:
                    self.handleError(record)

        # Clear existing handlers
        root_logger = logging.getLogger()
        root_logger.handlers = []

        # Configure logging
        logging.basicConfig(
            level=getattr(logging, log_level),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                Utf8FileHandler(log_file_path),
                SafeStreamHandler()
            ]
        )

        logging.info(f"Log file: {log_file_path}")
    
    def _initialize_persona_bank(self) -> PersonaBank:
        """Initialize PersonaBank."""
        persona_path = self.config['paths']['persona_bank']['persona']

        logging.info(f"Loading persona bank from {persona_path}")
        return PersonaBank(persona_path)
    
    def _initialize_sampler(self) -> PersonaSampler:
        """Initialize PersonaSampler."""
        sampling_config_path = self.config['paths']['sampling']['config']

        logging.info(f"Initializing sampler with config: {sampling_config_path}")
        return PersonaSampler(
            sampling_config_path,
            self.persona_bank,
            overrides={'seed': self._seed_value},
            llm_client=self.llm_client,
        )
    
    def _initialize_storage(self) -> PersonaSpecStorage:
        """Initialize PersonaSpecStorage."""
        output_dir = self.config['paths']['persona_specs']['output_dir']
        format = self.config['paths']['persona_specs']['format']
        
        logging.info(f"Initializing storage at {output_dir} with format {format}")
        return PersonaSpecStorage(output_dir, format)
    
    def _initialize_llm_client(self):
        """Initialize LLM client."""
        api_config = self.config['api']
        
        logging.info(f"Initializing LLM client for provider: {api_config['provider']}")
        return create_llm_client(api_config)
    
    def _initialize_formulator(self) -> LLMFormulator:
        """Initialize LLMFormulator."""
        template_path = self.config['formulation']['system_prompt_template']
        max_retries = self.config['formulation']['max_retries']
        retry_delay = self.config['formulation']['retry_delay']
        validate_output = self.config['formulation']['validate_output']
        min_prompt_length = self.config['formulation']['min_prompt_length']
        
        logging.info(f"Initializing formulator with template: {template_path}")
        return LLMFormulator(
            self.llm_client,
            template_path,
            max_retries,
            retry_delay,
            validate_output,
            min_prompt_length
        )
    
    def _initialize_query_generator(self) -> UserQueryGenerator:
        """Initialize UserQueryGenerator."""
        logging.info("Initializing query generator")
        return UserQueryGenerator(self.config, self.llm_client)

    def _initialize_query_storage(self) -> QueryStorage:
        """Initialize QueryStorage for Stage 2 persistence."""
        output_dir = self.config.get('paths', {}).get('queries', {}).get('output_dir', 'output/queries')
        logging.info(f"Initializing query storage at {output_dir}")
        return QueryStorage(output_dir)
    
    def _initialize_interaction_generator(self) -> InteractionGenerator:
        """Initialize InteractionGenerator with distractor integration."""
        logging.info("Initializing interaction generator")
        # Pass distractor to interaction generator for real-time noise injection
        distractor = self.distractor if hasattr(self, 'distractor') else None
        return InteractionGenerator(self.config, distractor=distractor)
    
    def _initialize_distractor(self):
        """Initialize DistractorModel or SemanticDistractorModel based on config."""
        logging.info("Initializing distractor model")
        use_semantic = self.config.get('distractor', {}).get('use_semantic', False)
        if use_semantic:
            logging.info("Using semantic distractor model")
        else:
            logging.info("Using legacy distractor model")
        return create_distractor_model(self.config, use_semantic=use_semantic, llm_client=self.llm_client)

    def _check_personas_exist(self, num_personas: int) -> tuple[bool, List[Dict[str, Any]]]:
        """
        Check if enough personas already exist.

        Args:
            num_personas: Required number of personas

        Returns:
            Tuple of (has_enough, existing_personas)
        """
        if not hasattr(self, 'storage'):
            return False, []

        existing = self.storage.load_all()
        existing_dicts = [p.to_dict() for p in existing]

        if len(existing_dicts) >= num_personas:
            return True, existing_dicts[:num_personas]
        return False, existing_dicts

    def _check_training_data_exists(self) -> bool:
        """Check if training data files already exist."""
        output_dir = Path(self.config['paths']['training_data']['output_dir'])
        train_file = output_dir / "train_samples.json"
        val_file = output_dir / "val_samples.json"
        test_file = output_dir / "test_samples.json"

        return train_file.exists() and val_file.exists() and test_file.exists()

    def _load_existing_training_data(self) -> Dict[str, Any]:
        """Load existing training data statistics."""
        output_dir = Path(self.config['paths']['training_data']['output_dir'])
        stats_file = output_dir / "statistics.json"

        if stats_file.exists():
            with open(stats_file, 'r', encoding='utf-8') as f:
                stats = json.load(f)
            return {
                'total_samples': stats.get('total_samples', 0),
                'sample_files': {
                    'train': str(output_dir / "train_samples.json"),
                    'val': str(output_dir / "val_samples.json"),
                    'test': str(output_dir / "test_samples.json")
                },
                'stats_file': str(stats_file)
            }
        return None

    def _get_batch_size(self, num_personas: int) -> int:
        """Resolve batch size for streaming pipeline."""
        raw = self.config.get('experiment', {}).get('batch_size')
        try:
            batch_size = int(raw) if raw is not None else num_personas
        except Exception:
            batch_size = num_personas
        if batch_size <= 0:
            batch_size = num_personas
        return min(batch_size, num_personas)

    def _load_existing_queries(self) -> Dict[str, List[Dict[str, Any]]]:
        """Load persisted Stage 2 queries from disk."""
        if not hasattr(self, 'query_storage'):
            return {}
        return self.query_storage.load_all()

    def _load_interaction_summary(self) -> tuple[Dict[str, int], Dict[str, set], set]:
        """
        Load interaction counts and used query_ids per persona (from storage index/files).

        Returns:
            success_count, used_query_ids_by_persona, personas_with_unknown_query_ids
        """
        success_count: Dict[str, int] = {}
        used_query_ids_by_persona: Dict[str, set] = {}
        unknown_query_id_personas: set = set()

        interactions_output_dir = self.config.get('paths', {}).get('interactions', {}).get(
            'output_dir', 'output/interactions'
        )
        storage = getattr(self, 'interaction_storage', None)
        if storage is None:
            storage = InteractionStorage(interactions_output_dir, collector=None)

        index = getattr(storage, 'index', None) or {'interactions': []}
        for entry in index.get('interactions', []):
            persona_id = entry.get('persona_id')
            if not persona_id:
                continue
            success_count[persona_id] = success_count.get(persona_id, 0) + 1
            query_id = entry.get('query_id')
            if not query_id and entry.get('interaction_id'):
                try:
                    data = storage.load(entry['interaction_id'])
                    query_id = (data.get('metadata') or {}).get('query_id')
                except Exception:
                    query_id = None
            if query_id:
                used_query_ids_by_persona.setdefault(persona_id, set()).add(query_id)
            else:
                unknown_query_id_personas.add(persona_id)

        return success_count, used_query_ids_by_persona, unknown_query_id_personas

    def _dedupe_queries(self, queries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove duplicate queries based on query_id (fallback to text)."""
        seen = set()
        deduped = []
        for q in queries:
            if not isinstance(q, dict):
                continue
            qid = q.get('query_id')
            key = qid or (q.get('original_query'), q.get('adapted_query'))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(q)
        return deduped

    # ------------------------------------------------------------------
    # Query-driven orchestration
    # ------------------------------------------------------------------

    def _resolve_seed(self) -> int:
        """Draw a fresh internal seed for this run.

        There is deliberately no user-facing seed: query sampling and the
        query -> persona mapping should differ on every run. The value is
        logged and stored in the state file only to identify the run.
        """
        value = secrets.randbits(32)
        return value

    def _seed(self) -> int:
        """Return the effective seed used for sampling and persona assignment."""
        return self._seed_value

    def _resolve_num(self, cli_num: Optional[int]) -> int:
        """Resolve how many CSV queries to process."""
        if cli_num is not None:
            num = int(cli_num)
        else:
            configured = self.config.get('query_selection', {}).get('num')
            num = int(configured) if configured else len(self.query_generator.query_dataset)

        total = len(self.query_generator.query_dataset)
        if num <= 0:
            raise ValueError(f"--num must be a positive integer, got {num}")
        if num > total:
            print_colored(
                f"[WARN] Requested {num} queries but the dataset only has {total}; using {total}",
                'WARNING'
            )
            num = total
        return num

    def _resolve_pool_size(self, num: int, cli_num_personas: Optional[int]) -> int:
        """Persona pool size: min(num, configured num_personas)."""
        configured = cli_num_personas
        if configured is None:
            configured = self.config.get('persona_generation', {}).get('num_personas')
        try:
            configured = int(configured) if configured else num
        except (TypeError, ValueError):
            configured = num
        if configured <= 0:
            configured = num
        return min(num, configured)

    def _state_file(self) -> Path:
        state_dir = Path(self.config.get('paths', {}).get('state_dir', 'output/state'))
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / 'query_assignments.json'

    def _run_signature(self, num: int, pool_size: int) -> Dict[str, Any]:
        input_csv = self.config.get('paths', {}).get('input_csv', '')
        digest = ''
        try:
            with open(input_csv, 'rb') as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            logging.warning(f"Could not hash input CSV: {input_csv}")
        return {
            'input_csv': input_csv,
            'input_sha256': digest,
            'num': num,
            'seed': self._seed(),
            'persona_pool_size': pool_size,
        }

    def _save_assignments(self, state: Dict[str, Any]) -> None:
        state_file = self._state_file()
        tmp_file = state_file.with_suffix('.json.tmp')
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, state_file)

    def _load_assignments(self) -> Dict[str, Any]:
        state_file = self._state_file()
        if not state_file.exists():
            raise RuntimeError(
                f"No assignment state found at {state_file}. "
                "Run --stage all (or query/interaction) first."
            )
        with open(state_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _recover_completed_from_storage(self, state: Dict[str, Any]) -> int:
        """
        Mark assignments as done when a matching interaction already exists.

        InteractionStorage writes each interaction (and index entry) as soon as
        it finishes, so a batch interrupted mid-way can resume without redoing
        the completed conversations. Persona ids must match to avoid picking up
        interactions from an unrelated run.
        """
        storage = getattr(self, 'interaction_storage', None)
        if storage is None:
            return 0

        state_created = state.get('created_at')
        if not state_created:
            # Only recover interactions produced by this run. Without a
            # creation timestamp we could pick up a same query/persona pair
            # from an older, unrelated run.
            return 0

        index = getattr(storage, 'index', {}) or {}
        by_query: Dict[str, Dict[str, Any]] = {}
        for entry in index.get('interactions', []):
            query_id = entry.get('query_id')
            if query_id and query_id not in by_query:
                by_query[query_id] = entry

        recovered = 0
        for assignment in state.get('assignments', []):
            if assignment.get('status') == 'done' and assignment.get('interaction_id'):
                continue
            entry = by_query.get(assignment.get('query_id'))
            if not entry:
                continue
            if entry.get('persona_id') and entry['persona_id'] != assignment.get('persona_id'):
                continue
            entry_created = entry.get('created_at')
            if entry_created and entry_created < state_created:
                continue

            interaction_id = entry.get('interaction_id')
            path = Path(entry.get('filepath') or '')
            if not path.exists():
                path = storage.output_dir / f"{interaction_id}.json"
            if not path.exists():
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue

            assignment['interaction_id'] = interaction_id
            assignment['num_turns'] = entry.get('num_turns') or data.get('num_turns')
            assignment['status'] = 'done'
            assignment['error'] = None
            recovered += 1

        if recovered:
            print_colored(
                f"[Stage 3] Recovered {recovered} completed interactions from storage",
                'INFO'
            )
        return recovered

    def _ensure_persona_pool(self, pool_size: int, fresh: bool = False) -> List[Dict[str, Any]]:
        """Build the persona pool.

        By default stored personas are reused (and topped up when short).
        With ``fresh`` the pool is regenerated from scratch for this run.
        """
        if fresh:
            print_colored(
                f"\n[Stage 1] Generating a fresh persona pool ({pool_size} personas)...",
                'STAGE'
            )
            personas = self._run_persona_generation(pool_size)
            if not personas:
                raise RuntimeError("Persona pool is empty; cannot assign personas to queries.")
            return personas

        existing: List[Dict[str, Any]] = []
        if hasattr(self, 'storage'):
            existing = [p.to_dict() for p in self.storage.load_all()]

        if len(existing) > pool_size:
            # Random subset; with a random run seed this differs between runs.
            existing = random.Random(self._seed()).sample(existing, pool_size)

        missing = pool_size - len(existing)
        if missing > 0:
            print_colored(f"\n[Stage 1] Generating {missing} personas...", 'STAGE')
            existing.extend(self._run_persona_generation(missing))
        else:
            print_colored(f"\n[Stage 1] Persona pool ready ({len(existing)} personas)", 'INFO')

        if not existing:
            raise RuntimeError("Persona pool is empty; cannot assign personas to queries.")

        return existing[:pool_size]

    def _signature_matches(self, old: Dict[str, Any], new: Dict[str, Any]) -> bool:
        """Compare run signatures (the seed is intentionally excluded)."""
        keys = ['input_csv', 'input_sha256', 'num', 'persona_pool_size']
        return all(old.get(key) == new.get(key) for key in keys)

    def _load_or_create_assignments(self, num: int, personas: List[Dict[str, Any]],
                                    restart: bool = False) -> Dict[str, Any]:
        """
        Load the query -> persona assignment state, or create it.

        Query selection is a random sample without replacement (CSV order is
        kept when num equals the dataset size). Each selected query gets a
        randomly chosen persona from the pool, with replacement.
        """
        state_file = self._state_file()
        signature = self._run_signature(num, len(personas))

        if state_file.exists() and not restart:
            state = self._load_assignments()
            if self._signature_matches(state.get('run_signature') or {}, signature):
                print_colored(
                    f"[Stage 2] Resuming {len(state.get('assignments', []))} "
                    f"query assignments from {state_file}",
                    'INFO'
                )
                return state
            raise RuntimeError(
                "Existing assignment state does not match this run "
                "(input CSV, --num or persona pool size changed). "
                "Re-run with --restart to discard it."
            )

        if restart and state_file.exists():
            archive_dir = state_file.parent / (
                f"archive_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
            )
            archive_dir.mkdir(parents=True, exist_ok=True)
            state_file.replace(archive_dir / state_file.name)
            print_colored(f"[Stage 2] Archived previous state to {archive_dir}", 'INFO')

        dataset = self.query_generator.query_dataset.get_all_queries()
        total = len(dataset)
        if num >= total:
            selected = list(dataset)
        else:
            selected = random.Random(self._seed()).sample(dataset, num)

        assignment_rng = random.Random(self._seed() + 1)
        assignments: List[Dict[str, Any]] = []
        for query_data in selected:
            metadata = query_data.get('metadata', {}) or {}
            persona = assignment_rng.choice(personas)
            assignments.append({
                'query_id': query_data.get('query_id'),
                'row_index': metadata.get('_row_index'),
                'row': {col: value for col, value in metadata.items() if col != '_row_index'},
                'source': query_data.get('source', ''),
                'persona_id': persona.get('persona_id'),
                'persona_features': persona.get('features', {}),
                'system_prompt': persona.get('system_prompt'),
                'status': 'pending',
                'original_query': query_data.get('text', ''),
                'adapted_query': None,
                'was_style_transferred': None,
                'inferred_domain': None,
                'inferred_scenario': None,
                'target_length': None,
                'target_turns': None,
                'prompt_type': None,
                'interaction_id': None,
                'num_turns': None,
                'attempts': 0,
                'error': None,
            })

        state = {
            'run_signature': signature,
            'created_at': datetime.now().isoformat(),
            'assignments': assignments,
        }
        self._save_assignments(state)
        print_colored(
            f"[Stage 2] Sampled {len(assignments)} queries and assigned personas "
            f"from a pool of {len(personas)}",
            'INFO'
        )
        return state

    def _style_transfer_one(self, assignment: Dict[str, Any]) -> Dict[str, Any]:
        """Apply style transfer to a single assignment (thread-safe entry point)."""
        row = dict(assignment.get('row') or {})
        row['_row_index'] = assignment.get('row_index')
        query_data = {
            'query_id': assignment.get('query_id'),
            'text': row.get('open_question') or assignment.get('original_query', ''),
            'source': assignment.get('source', ''),
            'metadata': row,
        }
        assignment['attempts'] = assignment.get('attempts', 0) + 1
        try:
            if row.get('case_factors') or row.get('dialogue_plan_json'):
                factors = json.loads(row['case_factors'])
                plan = json.loads(row['dialogue_plan_json'])
                max_turns = self.interaction_generator.max_turns
                if not isinstance(factors, dict) or not factors:
                    raise ValueError("case_factors must be a nonempty JSON object")
                if 'turn_range' in plan:
                    bounds = plan['turn_range']
                    minimum = plan.get('minimum_turns', 1)
                    if (not isinstance(bounds, list) or len(bounds) != 2
                            or any(type(value) is not int for value in bounds)
                            or not 1 <= bounds[0] <= bounds[1]
                            or type(minimum) is not int or minimum < 1):
                        raise ValueError("Invalid turn_range or minimum_turns")
                    lower, upper = max(bounds[0], minimum), min(bounds[1], max_turns)
                    if lower > upper:
                        raise ValueError(
                            f"No eligible turn count for {bounds}, minimum_turns={minimum}, "
                            f"max_turns={max_turns}"
                        )
                    turns = assignment.get('required_turns')
                    if turns is None:
                        turns = random.randint(lower, upper)
                        assignment['required_turns'] = turns
                    elif type(turns) is not int or not lower <= turns <= upper:
                        raise ValueError("Saved required_turns is outside the eligible range")
                else:
                    turns = plan['required_turns']
                if type(turns) is not int or not 1 <= turns <= max_turns:
                    raise ValueError(f"required_turns must be between 1 and max_turns ({max_turns})")
                template = (Path(__file__).resolve().parents[1] / 'prompts'
                            / 'seed_topic_extraction.txt').read_text(encoding='utf-8')
                raw = self.llm_client.generate(template.replace('{question}', query_data['text']))
                raw = raw.strip()
                if raw.startswith('```') and raw.endswith('```'):
                    raw = raw.partition('\n')[2][:-3].strip()
                topic = json.loads(raw)
                if (not isinstance(topic, dict) or not isinstance(topic.get('topic'), str)
                        or not topic['topic'].strip()):
                    raise ValueError("Expected a nonempty topic string")
                assignment.update(original_query=query_data['text'], status='styled', error=None,
                                  planned_case={'topic': topic['topic'].strip(),
                                                'case_factors': factors, 'required_turns': turns})
                return assignment

            result = self.query_generator.generate_for_query(
                query_data, assignment.get('persona_features') or {}
            )
            assignment.update({
                'original_query': result['original_query'],
                'adapted_query': result['adapted_query'],
                'was_style_transferred': result['was_style_transferred'],
                'inferred_domain': result['inferred_domain'],
                'inferred_scenario': result['inferred_scenario'],
                'target_length': result['target_length'],
                'target_turns': result['target_turns'],
                'prompt_type': result['prompt_type'],
                'status': 'styled',
                'error': None,
            })
        except Exception as e:
            logging.error(f"Style transfer failed for {assignment.get('query_id')}: {e}")
            assignment['status'] = 'failed_query'
            assignment['error'] = str(e)
        return assignment

    def _run_style_transfer(self, assignments: List[Dict[str, Any]]) -> None:
        if not assignments:
            return
        max_workers = self.config.get('interaction_generation', {}).get('max_workers', 5)
        print_colored(f"\n[Stage 2] Style-transforming {len(assignments)} queries...", 'STAGE')
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            list(tqdm(
                executor.map(self._style_transfer_one, assignments),
                total=len(assignments),
                desc="Style transfer",
                unit="query",
                ncols=100,
                colour='blue',
            ))

    def _build_interaction_tasks(self, assignments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build the task list expected by InteractionGenerator."""
        reformulate = self.config.get('interaction_generation', {}).get(
            'reformulate_persona_prompt', False
        )
        tasks = []
        for assignment in assignments:
            features = dict(assignment.get('persona_features') or {})
            if assignment.get('inferred_domain'):
                features['domain'] = assignment['inferred_domain']
            if assignment.get('inferred_scenario'):
                features['scenario'] = assignment['inferred_scenario']

            system_prompt = assignment.get('system_prompt')
            if reformulate:
                try:
                    system_prompt = self.formulator.formulate(features)
                except Exception as e:
                    logging.warning(
                        f"Failed to regenerate prompt for "
                        f"{assignment.get('persona_id')}, using original: {e}"
                    )

            original_query = (
                assignment.get('original_query')
                or (assignment.get('row') or {}).get('open_question', '')
            )
            query_data = {
                'query_id': assignment.get('query_id'),
                'original_query': original_query,
                'adapted_query': assignment.get('adapted_query') or original_query,
                'was_style_transferred': assignment.get('was_style_transferred', False),
                'inferred_domain': assignment.get('inferred_domain', 'general'),
                'inferred_scenario': assignment.get('inferred_scenario', 'casual'),
                'target_length': assignment.get('target_length'),
                'target_turns': assignment.get('target_turns') or 3,
                'planned_case': assignment.get('planned_case'),
                'prompt_type': assignment.get('prompt_type'),
                'source_dataset': assignment.get('source', ''),
                'metadata': {
                    **(assignment.get('row') or {}),
                    '_row_index': assignment.get('row_index'),
                },
            }
            tasks.append({
                'persona_id': assignment.get('persona_id'),
                'persona_features': features,
                'system_prompt': system_prompt,
                'queries': [query_data],
            })
        return tasks

    def _run_interactions(self, assignments: List[Dict[str, Any]]) -> List[Any]:
        if not assignments:
            return []
        max_workers = self.config.get('interaction_generation', {}).get('max_workers', 5)
        tasks = self._build_interaction_tasks(assignments)
        print_colored(f"\n[Stage 3] Generating {len(tasks)} interactions...", 'STAGE')
        interactions = self.interaction_generator.generate_interactions_batch(
            tasks,
            show_progress=True,
            max_workers=max_workers,
            storage=getattr(self, 'interaction_storage', None),
        )

        by_query_id: Dict[str, Any] = {}
        for interaction in interactions:
            query_id = (interaction.metadata or {}).get('query_id')
            if query_id:
                by_query_id[query_id] = interaction

        for assignment in assignments:
            interaction = by_query_id.get(assignment.get('query_id'))
            if interaction is None:
                assignment['status'] = 'failed_interaction'
                assignment['error'] = (
                    assignment.get('error') or 'interaction generation returned no result'
                )
                assignment['attempts'] = assignment.get('attempts', 0) + 1
                continue
            assignment['interaction_id'] = interaction.interaction_id
            assignment['num_turns'] = interaction.num_turns
            assignment['status'] = 'done'
            assignment['error'] = None

        return interactions

    @staticmethod
    def _iter_batches(items: List[Any], size: int):
        for index in range(0, len(items), size):
            yield items[index:index + size]

    def _load_completed_interactions(
        self, assignments: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """Load interaction JSONs for every assignment that finished."""
        storage = getattr(self, 'interaction_storage', None)
        interactions: Dict[str, Dict[str, Any]] = {}
        for assignment in assignments:
            interaction_id = assignment.get('interaction_id')
            if not interaction_id:
                continue
            try:
                if storage is not None:
                    interactions[assignment['query_id']] = storage.load(interaction_id)
                else:
                    path = Path(
                        self.config.get('paths', {}).get('interactions', {}).get(
                            'output_dir', 'output/interactions'
                        )
                    ) / f"{interaction_id}.json"
                    with open(path, 'r', encoding='utf-8') as f:
                        interactions[assignment['query_id']] = json.load(f)
            except FileNotFoundError:
                logging.warning(
                    f"Interaction file missing for {assignment.get('query_id')}: {interaction_id}"
                )
        return interactions

    def _run_export_stages(self, assignments: List[Dict[str, Any]],
                           stage: str) -> Dict[str, Any]:
        """Run the training-data export and/or final CSV export."""
        interactions = self._load_completed_interactions(assignments)
        results: Dict[str, Any] = {'interactions_loaded': len(interactions)}

        if stage in ('all', 'training') and self.stages.get('training_data_export', True):
            if interactions and hasattr(self, 'training_exporter'):
                print_colored("\n[Stage 5] Collecting and Exporting Training Data...", 'STAGE')
                results['training_data'] = self._run_training_data_export(
                    list(interactions.values())
                )
                print_colored("[OK] Exported training data", 'SUCCESS')

        exporter = CSVExporter(self.config)
        output_csv = self.config.get('paths', {}).get('output_csv', 'output/QA_case_improved.csv')
        failed_csv = self.config.get('paths', {}).get('failed_csv', 'output/failed_queries.csv')
        print_colored(f"\n[Stage 5] Exporting final CSV to {output_csv}...", 'STAGE')
        results['csv'] = exporter.export(
            assignments=assignments,
            interactions=interactions,
            output_path=output_csv,
            failed_path=failed_csv,
        )
        print_colored(
            f"[OK] Wrote {results['csv']['rows']} rows "
            f"({results['csv']['queries_written']} queries) to {output_csv}",
            'SUCCESS'
        )
        return results

    def run(self, num: Optional[int] = None, num_personas: Optional[int] = None,
            stage: str = 'all', restart: bool = False, resume: bool = False,
            fresh_personas: bool = False) -> Dict[str, Any]:
        """
        Run the query-driven pipeline.

        Args:
            num: Number of CSV queries to process (default: config or all rows).
            num_personas: Persona pool size (default: config or num).
            stage: all / persona / query / interaction / export / training.
            restart: Archive existing assignment state and re-sample.
            resume: Continue the existing run instead of starting a new sample.
            fresh_personas: Generate a brand-new persona pool for this run.
        """
        # Resume must continue with the seed recorded in the existing state.
        if resume:
            state_file = self._state_file()
            if not state_file.exists():
                raise RuntimeError("--resume requested but no assignment state was found.")
            stored_seed = (self._load_assignments().get('run_signature') or {}).get('seed')
            if stored_seed is not None:
                self._seed_value = int(stored_seed)

        # Every full run starts a new sample. Explicit stages keep using the
        # existing assignment state so the persona -> query -> interaction
        # workflow still works.
        if stage == 'all' and not resume:
            restart = True

        print_colored("=" * 80, 'STAGE')
        print_colored("Starting CaseSmith Persona Query Pipeline", 'STAGE')
        print_colored(f"Run id (random seed): {self._seed_value}", 'INFO')
        if self.incremental:
            print_colored("Mode: INCREMENTAL (completed queries are skipped)", 'INFO')
        print_colored("=" * 80, 'STAGE')

        results: Dict[str, Any] = {
            'personas': [],
            'interactions': [],
            'seed': self._seed_value,
        }

        if stage in ('export', 'training'):
            state = self._load_assignments()
            assignments = state.get('assignments', [])
            results['assignments'] = assignments
        else:
            num = self._resolve_num(num)
            pool_size = self._resolve_pool_size(num, num_personas)
            personas = self._ensure_persona_pool(pool_size, fresh=fresh_personas)
            results['personas'] = personas

            state = self._load_or_create_assignments(num, personas, restart=restart)
            assignments = state.get('assignments', [])
            results['assignments'] = assignments
            results['num_queries'] = len(assignments)

            if self._recover_completed_from_storage(state):
                self._save_assignments(state)

            batch_size = self._get_batch_size(max(len(assignments), 1))

            if stage in ('all', 'query', 'interaction'):
                pending_style = [
                    a for a in assignments
                    if a.get('status') == 'pending'
                    or (not (a.get('adapted_query') or a.get('planned_case'))
                        and a.get('status') != 'done')
                ]
                for batch in self._iter_batches(pending_style, batch_size):
                    self._run_style_transfer(batch)
                    self._save_assignments(state)
                results['num_styled'] = sum(
                    1 for a in assignments if a.get('adapted_query') or a.get('planned_case')
                )
                print_colored(
                    f"[Stage 2] Style transfer complete: "
                    f"{results['num_styled']}/{len(assignments)} queries",
                    'SUCCESS'
                )

            if stage in ('all', 'interaction'):
                pending_interactions = [
                    a for a in assignments
                    if a.get('status') in ('styled', 'failed_interaction')
                ]
                for batch in self._iter_batches(pending_interactions, batch_size):
                    batch_interactions = self._run_interactions(batch)
                    results['interactions'].extend(batch_interactions)
                    self._save_assignments(state)

                if hasattr(self, 'distractor') and self.distractor and self.distractor.enabled:
                    print_colored(
                        "[Stage 4] Distractor already applied in real-time during Stage 3",
                        'INFO'
                    )
                results['num_done'] = sum(
                    1 for a in assignments if a.get('status') == 'done'
                )
                print_colored(
                    f"[Stage 3] Interactions complete: "
                    f"{results['num_done']}/{len(assignments)} queries",
                    'SUCCESS'
                )

        if stage in ('all', 'export', 'training'):
            results.update(self._run_export_stages(assignments, stage))

        print_colored("\n" + "=" * 80, 'SUCCESS')
        print_colored("Pipeline Complete", 'SUCCESS')
        print_colored("=" * 80, 'SUCCESS')
        self._print_summary(results)

        if self.token_tracker:
            self._export_token_statistics()

        self._cleanup_logging()
        return results

    def _cleanup_logging(self):
        """Restore stdout/stderr and close log file."""
        try:
            if hasattr(self, '_original_stdout'):
                sys.stdout = self._original_stdout
            if hasattr(self, '_original_stderr'):
                sys.stderr = self._original_stderr
            if hasattr(self, 'log_file') and self.log_file:
                self.log_file.close()
        except Exception:
            pass
    
    def _run_persona_generation(self, num_personas: Optional[int]) -> List[Dict[str, Any]]:
        """Run persona generation stage."""
        if num_personas is None:
            num_personas = self.config['persona_generation']['num_personas']
        
        personas = []
        
        # Use tqdm for progress bar
        for i in tqdm(range(num_personas), desc="Generating personas", 
                     unit="persona", ncols=100, colour='green'):
            # Sample features
            features = self.sampler.sample_persona()
            
            # Generate persona ID
            persona_id = generate_persona_id(features)
            
            # Generate system prompt
            try:
                system_prompt = self.formulator.formulate(features)
            except Exception as e:
                logging.error(f"Failed to generate prompt for {persona_id}: {str(e)}")
                system_prompt = None
            
            # Create PersonaSpec
            persona_spec = PersonaSpec(
                persona_id=persona_id,
                features=features,
                system_prompt=system_prompt,
                metadata={
                    'num_features': len(features),
                    'has_prompt': system_prompt is not None
                }
            )
            
            # Save
            self.storage.save(persona_spec)
            personas.append(persona_spec.to_dict())
        
        return personas
    
    def _run_query_generation(self, personas: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """Run query generation stage."""
        queries_map = self.query_generator.generate_queries_batch(personas)
        return queries_map
    
    def _prepare_interaction_tasks(self, personas: List[Dict[str, Any]],
                                   queries_map: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """
        Prepare persona-query task entries for interaction generation.

        For each query, the persona's domain/scenario are overwritten with the
        inferred values from the query to ensure consistency.
        """
        personas_with_queries = []

        for persona in personas:
            persona_id = persona['persona_id']
            if persona_id not in queries_map:
                continue
            queries = queries_map[persona_id]

            for query_data in queries:
                # Copy persona features and update domain/scenario from query
                updated_features = persona['features'].copy()

                inferred_domain = query_data.get('inferred_domain')
                inferred_scenario = query_data.get('inferred_scenario')

                if inferred_domain:
                    updated_features['domain'] = inferred_domain
                if inferred_scenario:
                    updated_features['scenario'] = inferred_scenario

                # Regenerate system prompt with updated features
                try:
                    updated_system_prompt = self.formulator.formulate(updated_features)
                except Exception as e:
                    logging.warning(f"Failed to regenerate prompt for {persona_id}, using original: {str(e)}")
                    updated_system_prompt = persona.get('system_prompt')

                personas_with_queries.append({
                    'persona_id': persona_id,
                    'persona_features': updated_features,
                    'system_prompt': updated_system_prompt,
                    'queries': [query_data]
                })

        return personas_with_queries

    def _run_interaction_generation(self, personas: List[Dict[str, Any]],
                                   queries_map: Dict[str, List[Dict[str, Any]]],
                                   existing_success_count: Optional[Dict[str, int]] = None,
                                   target_per_persona: Optional[int] = None) -> List[Any]:
        """
        Run interaction generation stage with supplement rounds.

        If some interactions fail, new queries are generated for deficient
        personas and additional rounds are attempted until every persona
        reaches queries_per_persona successful interactions (or the
        supplement budget is exhausted).
        """
        max_workers = self.config.get('interaction_generation', {}).get('max_workers', 5)
        storage = getattr(self, 'interaction_storage', None)
        queries_per_persona = target_per_persona or self.config.get('query_generation', {}).get(
            'selection', {}
        ).get('queries_per_persona', 1)
        max_supplement_rounds = self.config.get('interaction_generation', {}).get('max_supplement_rounds', 3)

        all_interactions = []
        success_count: Dict[str, int] = {}
        for p in personas:
            pid = p['persona_id']
            base = 0
            if existing_success_count:
                base = existing_success_count.get(pid, 0)
            success_count[pid] = base
        current_queries_map = queries_map

        for round_num in range(max_supplement_rounds + 1):  # +1 for initial round
            # Prepare tasks
            personas_with_queries = self._prepare_interaction_tasks(personas, current_queries_map)

            if not personas_with_queries:
                break

            round_label = "" if round_num == 0 else f" (supplement round {round_num}/{max_supplement_rounds})"
            logging.info(f"Prepared {len(personas_with_queries)} persona-query pairs{round_label}")

            # Generate interactions
            interactions = self.interaction_generator.generate_interactions_batch(
                personas_with_queries,
                show_progress=True,
                max_workers=max_workers,
                storage=storage
            )
            all_interactions.extend(interactions)

            # Update success count
            for inter in interactions:
                success_count[inter.persona_id] = success_count.get(inter.persona_id, 0) + 1

            # Check which personas still need more
            deficient = []
            for persona in personas:
                pid = persona['persona_id']
                needed = queries_per_persona - success_count.get(pid, 0)
                if needed > 0:
                    deficient.append((persona, needed))

            if not deficient:
                break

            # Reached supplement budget
            if round_num >= max_supplement_rounds:
                for persona, needed in deficient:
                    print_colored(
                        f"Persona {persona['persona_id']} still needs {needed} more interactions "
                        f"after {max_supplement_rounds} supplement rounds",
                        'WARNING'
                    )
                break

            # Supplement: generate new queries for deficient personas
            if not hasattr(self, 'query_generator'):
                logging.warning("Cannot supplement queries: query_generator not available")
                break

            total_needed = sum(n for _, n in deficient)
            print_colored(
                f"Supplementing: {len(deficient)} personas need {total_needed} more interactions "
                f"(round {round_num + 1}/{max_supplement_rounds})...",
                'INFO'
            )

            current_queries_map = {}
            has_queries = False
            for persona, needed in deficient:
                pid = persona['persona_id']
                new_queries = self.query_generator.generate_queries_for_persona(
                    persona.get('features', {}),
                    num_queries=needed
                )
                if new_queries:
                    # Persist supplement queries
                    if hasattr(self, 'query_storage'):
                        try:
                            self.query_storage.append_persona(pid, new_queries)
                        except Exception as e:
                            logging.warning(f"Failed to persist supplement queries for {pid}: {str(e)}")
                    # Update global queries_map for downstream use
                    existing = queries_map.get(pid, [])
                    queries_map[pid] = self._dedupe_queries(existing + new_queries)

                    current_queries_map[pid] = new_queries[:needed]
                    has_queries = True
                else:
                    print_colored(f"No more queries available for persona {pid}", 'WARNING')

            if not has_queries:
                print_colored("No more queries available in dataset, stopping supplement", 'WARNING')
                break

        return all_interactions
    
    def _run_distractor(self, interactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Run distractor application stage (DEPRECATED).
        
        Note: Distractor is now applied in real-time during interaction generation.
        This method is kept for backward compatibility.
        """
        logging.info("Distractor is now applied during interaction generation, skipping batch application.")
        return interactions
    
    def _run_training_data_export(self, interactions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Run training data export stage (no automatic labeling)."""
        # Collect training samples (just trajectories, no labeling)
        samples = self.training_collector.collect_from_interactions(interactions)
        
        # Export samples (all to train_samples.json with timestamp, no split)
        sample_files = self.training_exporter.export_samples(
            samples,
            split=False,
            use_timestamp=True
        )
        
        # Export statistics (with timestamp)
        stats_file = self.training_exporter.export_statistics(samples, use_timestamp=True)
        
        return {
            'total_samples': len(samples),
            'sample_files': sample_files,
            'stats_file': stats_file
        }
    
    def _print_summary(self, results: Dict[str, Any]):
        """Print pipeline summary."""
        print_colored("\nPIPELINE SUMMARY:", 'INFO')
        print_colored(f"  Personas: {len(results.get('personas', []))}", 'INFO')
        if 'num_queries' in results:
            print_colored(f"  Queries Sampled: {results['num_queries']}", 'INFO')
        if 'num_styled' in results:
            print_colored(f"  Queries Style-transferred: {results['num_styled']}", 'INFO')
        if 'num_done' in results:
            print_colored(f"  Queries Completed: {results['num_done']}", 'INFO')
        print_colored(f"  Interactions Generated: {len(results.get('interactions', []))}", 'INFO')

        if 'training_data' in results:
            td = results['training_data']
            print_colored(f"  Training Samples: {td['total_samples']}", 'INFO')

        if 'csv' in results:
            csv_summary = results['csv']
            print_colored(f"  CSV Rows: {csv_summary['rows']}", 'INFO')
            print_colored(f"  CSV Queries: {csv_summary['queries_written']}", 'INFO')
            print_colored(f"  Failed Queries: {csv_summary['failed_queries']}", 'INFO')
            print_colored(f"  Output CSV: {csv_summary['output_path']}", 'INFO')
        
        # Print token usage summary if tracker is available
        if self.token_tracker:
            print_colored("\nTOKEN USAGE SUMMARY:", 'INFO')
            stats = self.token_tracker.get_statistics()
            summary = stats['summary']
            print_colored(f"  Total API Calls: {summary['total_calls']}", 'INFO')
            print_colored(f"  Total Input Tokens: {summary['total_input_tokens']:,}", 'INFO')
            print_colored(f"  Total Output Tokens: {summary['total_output_tokens']:,}", 'INFO')
            print_colored(f"  Total Tokens: {summary['total_tokens']:,}", 'INFO')
    
    def _export_token_statistics(self):
        """Export token usage statistics to file."""
        if not self.token_tracker:
            return
        
        try:
            # Create output directory
            output_dir = Path(self.config['paths']['training_data']['output_dir'])
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # Generate timestamped filename
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            token_stats_file = output_dir / f'token_usage_{timestamp}.json'
            
            # Export statistics
            self.token_tracker.export_to_file(str(token_stats_file), include_records=True)
            
            print_colored(f"\n✓ Token usage statistics exported to: {token_stats_file}", 'SUCCESS')
            
            # Also print detailed summary to console
            print_colored("\n" + "=" * 80, 'INFO')
            self.token_tracker.print_summary()
            
        except Exception as e:
            logging.error(f"Failed to export token statistics: {str(e)}")
