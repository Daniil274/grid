# Durable Workflows: дизайн первой реализации

Статус: проект. 2026-10-10, задача workspace-gkv, ветка `feature/durable-workflows` в `/workspace/grid-next`.
Первичники: код grid (`[G]` — CodeGraph/прямое чтение), отчёт `reports/orchestration-comparison.md` (§5 P0–P2 — вход). Ничто здесь не реализовано до закрытия тестов §10.

## 1. Цели / не-цели

Цели ядра: (1) исполняемый typed workflow/DAG — шаги типами, `depends_on`, валидация графа до запуска; (2) durable journal (SQLite) + crash reconciliation — после падения ни один ран не остаётся «тихо RUNNING» (P0-1); (3) bounded concurrency (`max_parallel` 1..16 на ран + глобальный семафор); (4) дедлайны: `queue_deadline` от постановки и `work_deadline` от получения слота (P0-2; сейчас — от launch, `core/background_agents.py:144-151` `[G]`); (5) структурированные отказы, не-retryable для WRITE вне write_paths (P0-3); (6) бюджет-пауза `paused_budget` + resume (P1-3); (7) gate-шаги + bounded review loops; (8) init_tools required/optional (сейчас всегда мягкая деградация, §2.3 отчёта `[G]`); (9) идемпотентность: авто-retry только безопасных kind; для WRITE после crash — `UNKNOWN_EFFECT` + явная verify-политика.

НЕ обещаем: exactly-once (только at-least-once + идемпотентность/verify); авто-resume WRITE без механизма верификации; строгий нулевой бюджетный overshoot; многопроцессный кластер; event-rebuild UI, MCP fan-out, benchmark §6 (следующая волна). Существующие `orchestrate`/`parallel`/`pipeline_registry`/`background_agents` не меняются.

## 2. Размещение и стек

Пакет `core/workflows/` + новый модуль тулов `tools/workflow_tools.py` (регистрируется существующим автодискавером `_discover_tool_modules` `[G]`). Редактирований существующих файлов — ноль. Стек: Python ≥3.11, pydantic v2, stdlib `sqlite3` (WAL); новых зависимостей нет. БД: свой `<logs>/workflow_runs.db` (не общий с `agent_sessions.db`).

## 3. Типы (`core/workflows/spec.py`)

```python
StepKind = Literal["read", "egress", "write", "exec", "agent", "gate"]

class InitToolCall(BaseModel):          # extra="forbid", frozen
    name: str; parameters: dict = {}; required: bool = True

class StepSpec(BaseModel):              # extra="forbid"
    id: str; kind: StepKind
    tool: str | None = None             # read/egress/write/exec/gate
    task: str | None = None             # agent
    arguments: dict | None = None
    depends_on: list[str] = []
    write_paths: list[str] = []          # kind="write": непусто
    timeout_seconds: int | None = None  # work_deadline; только ужесточает
    idempotent: bool = False
    max_retries: int = 0                # 0..3; только idempotent или kind in {read,egress,gate}
    required: bool = True               # optional: сбой => skipped, зависимые skipped

class GateSpec(StepSpec): kind="gate"; check: str; max_review_rounds: int = 2  # 1..5

class WorkflowSpec(BaseModel):          # extra="forbid"
    name: str; goal: str
    steps: list[StepSpec]               # 1..256
    max_parallel: int = 4               # 1..16
    deadline_seconds: int | None = None # lifetime рана
    budget_usd: float | None = None
    init_tools: list[InitToolCall] = []

def validate(spec: WorkflowSpec) -> list[SpecError]: ...
def pin(spec: WorkflowSpec) -> str: ...  # sha256 канонического JSON (P1-4)
```

## 4. Журнал (`core/workflows/journal.py`, `schema.sql`)

```sql
PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
CREATE TABLE workflow_runs (            -- run_id='wf-'+uuid4hex[:12] PK
  spec_sha256 TEXT NOT NULL,            -- pin: конфигурация идентифицируема (P1-4)
  spec_json TEXT NOT NULL,              -- замороженная копия спека
  context_id TEXT, user_id TEXT,
  status TEXT NOT NULL,                 -- §5; стартует 'queued'
  created_at/updated_at TEXT NOT NULL, deadline_at TEXT,
  budget_usd_micros INTEGER, spent_usd_micros INTEGER DEFAULT 0, queue_deadline_at TEXT,
  final_report TEXT);                   -- собирается из статусов/гейтов, §9
CREATE TABLE workflow_steps (           -- PK (run_id, step_id); status по умолчанию 'pending'
  attempts INTEGER DEFAULT 0, result_ref TEXT,  -- указатель на evidence, НЕ доверенный текст модели
  error_code TEXT, error_detail TEXT, started_at TEXT, finished_at TEXT);
CREATE TABLE step_attempts (            -- PK (run_id, step_id, attempt); пишется ДО эффекта write/exec
  outcome TEXT NOT NULL,                -- started|completed|failed|timed_out|interrupted
  effect_status TEXT NOT NULL DEFAULT 'none',  -- none|unknown|confirmed
  idempotency_key TEXT);
CREATE TABLE workflow_events (         -- append-only, seq PK; seed P2-1
  run_id TEXT, at TEXT, event TEXT, payload TEXT);
```

```python
class WorkflowJournal:
    def __init__(self, db_path): ...    # открывает/мигрирует schema
    def create_run(run: RunRow) / set_run_status(run_id, status, *, note=None)
    def set_step_status(run_id, step_id, status, **cols)
    def begin_attempt(run_id, step_id, attempt, *, idempotency_key, effect_status)  # ДО эффекта
    def finish_attempt(run_id, step_id, attempt, outcome, *, effect_status=None)
    def append_event(run_id, event, payload) / add_spend(run_id, micro_usd)
    def load_runs(*, statuses=None) / load_steps(run_id) / close()
```

SQLite-вызовы синхронные (WAL, короткие) — принятый компромисс, §11.

## 5. Статусы

Ран: `queued → running → (completed|failed|interrupted|paused_budget)`; `cancelling → emergency_stopped` из любого активного. Шаг: `pending → ready → running → (completed|failed|interrupted)`; `skipped` (отмена/провал зависимости/required=False/gate). Переходы однонаправлены; перезапуск шага — только через `resume_workflow` (новая attempt). После crash в журнале НЕ существует ранов `queued/running/cancelling` (инвариант I5) — отличие от текущего in-memory состояния (`pipeline_registry.py:104-106` `[G]`).

## 6. Отказы (`core/workflows/failures.py`)

```python
FailureCode = Literal["SPEC_INVALID","POLICY_VIOLATION","TIMEOUT","TRANSIENT_ERROR",
  "PERMANENT_ERROR","GATE_FAILED","BUDGET_PAUSED","INTERRUPTED","UNKNOWN_EFFECT",
  "DEPENDENCY_FAILED","INIT_REQUIRED_FAILED","MALFORMED"]

class StepFailure(BaseModel):           # extra="forbid"
    step_id: str; code: FailureCode; retryable: bool   # вычисляет код (I8), не модель
    detail: str; allowed_write_paths: list[str] | None = None; attempt: int = 0
```

`POLICY_VIOLATION/UNKNOWN_EFFECT/DEPENDENCY_FAILED/INIT_REQUIRED_FAILED/GATE_FAILED` — всегда `retryable=False` (повтор запрещённого пути невозможен — координатору отдаются `allowed_write_paths`). `TRANSIENT_ERROR/TIMEOUT` — retryable только при `idempotent or kind in {read,egress,gate}`. Текст модели не доверяется: `result_ref` → evidence; в отчёте он лишь `unverified_claim`.

## 7. Crash reconciliation и resume (`core/workflows/reconcile.py`)

```python
WriteVerifyPolicy = Literal["verify", "never"]   # default "verify"
async def reconcile(journal, *, checkpoints: Checkpoints | None = None) -> ReconcileReport
async def resume_workflow(run_id, *, journal, write_policy="verify",
                          budget_usd_micros: int | None = None) -> WorkflowRunView
```

На старте процесса: раны `running/queued/cancelling` → `interrupted`; их шаги `running` → `interrupted`, а write/exec-попытки получают `effect_status="unknown"`. `queued`-раны без начатых WRITE перезапускаются автоматически. Resume: `verify` — для шага с `unknown` сверка workspace с checkpoint ДО попытки (`core/checkpoints.py::Checkpoints` `[G]` — shadow-git diff): нет изменений в write_paths → эффект не наступил, шаг перезапускается; есть → `UNKNOWN_EFFECT` (не-retryable), решение за координатором. `never` — сразу `UNKNOWN_EFFECT`. `read/egress/gate` — безопасный перезапуск; `agent`-шаги считаются безэффектными только если без WRITE-тулов. Гарантия — «раны не теряются и не фиктивно живы»; НЕ гарантия — «WRITE ровно один раз».

## 8. Движок (`core/workflows/engine.py`)

```python
StepInvoker = Callable[[StepSpec, dict], Awaitable[StepResult]]  # единственная точка исполнения:
# существующий вызов тулов с ActionGate-медиацией и delegated_state (как в orchestrate [G]);
# agent-шаги — factory.create_dynamic_agent + run_agent_object_simple с таймаутом шага.
class WorkflowEngine:
    def __init__(self, *, journal, invoke: StepInvoker, max_global_parallel: int = 16, ...): ...
    async def run(self, run_id, spec, *, deadline_at=None) -> WorkflowRunView
    async def cancel(self, run_id, reason) -> WorkflowRunView
```

Готовность шага = все `depends_on` completed; семафор `min(max_parallel, max_global_parallel)`; WRITE одного рана сериализуются per-lane lock (`core/parallel_lanes` `[G]`). Дедлайны: ран `deadline_at` wall-clock → отмена in-flight; шаг `timeout_seconds` → `wait_for` ПОСЛЕ слота (P0-2); `queue_deadline` → `TIMEOUT` без модельных вызовов. Бюджет: перед каждым новым шагом `spent + in_flight_reserved > budget` → новых запусков нет, ран `paused_budget`; spent — из `tally_spend()`/`SpendTally` (`core/model_access.py` `[G]`); overshoot ≤ in-flight×цена_шага (строгий 0 не обещается); `resume_workflow(budget_usd=...)` продолжает. Гейты: `GateRunner` проверяет приёмочный контракт (system_quality/тул) над evidence; провал → `GATE_FAILED`, зависимые `skipped`; review loop ≤ `max_review_rounds`, исчерпание → терминальный `failed` с раундом.

## 9. API и LLM-тул

```python
async def start_workflow(spec: WorkflowSpec, *, context_id=None, user_id=None) -> WorkflowRunView
async def get_workflow(run_id) / list_workflows(*, statuses=None, limit=50)
async def resume_workflow(run_id, *, write_policy="verify", budget_usd=None) -> WorkflowRunView
async def cancel_workflow(run_id, reason: str) -> WorkflowRunView
```

`tools/workflow_tools.py`: тулы `workflow_run(spec_json, goal, ...)` (валидация → структурированные `SPEC_INVALID` со списком проблем), `workflow_status(run_id)`, `workflow_resume(run_id, ...)`; все — через ActionGate как обычные тулы. `orchestrate` не трогается; тул появляется у агентов только по явной конфигурации системы. `final_report` — машиночитаемый JSON: per-step status/failure/result_ref, результаты гейтов, spent_usd, wall_seconds, `unknown_effects`.

## 10. Тесты (локально, без платных сервисов)

Запуск: `python3 -m pytest tests/test_workflow_*.py -q` в копии (python3=/usr/local/bin/python, openai-agents 0.7.0, pytest 9.1.1 — проверено; fake-модели/MOCK_TOOLS уже в репо; asyncio_mode=auto). Реальных 60s-ожиданий нет: fake clock/малые таймауты с запасом (урок P0-2 — гонка на границе таймера).

- `test_workflow_spec.py` — I1–I2, pin стабилен; цикл/висячая dep/дубликат id/write без write_paths/пересечение зон без depends_on → SPEC_INVALID.
- `test_workflow_journal.py` — reopen после «crash» сохраняет статусы; события append-only; конкурентные записи сериализуются; «запись до эффекта» (I3).
- `test_workflow_engine.py` — порядок по DAG; ≤max_parallel (счётчик); work_deadline от слота (fake clock: 5 шагов, work≈timeout+запас, max_parallel=4 → все completed, 0 timed_out-в-очереди); бюджет-пауза/resume; POLICY_VIOLATION не перезапускается; required init fail-fast / optional деградация; gate провал → зависимые skipped; review ≤ лимита.
- `test_workflow_reconcile.py` — «kill -9»: running WRITE → interrupted + UNKNOWN_EFFECT; resume verify по fake checkpoint → перезапуск/ошибка; `never` → UNKNOWN_EFFECT; read перезапускается.
- `test_workflow_tool.py` — регистрация автодискавером; end-to-end на MOCK_TOOLS/fake factory; SPEC_INVALID структурирован.

Регресс: полный существующий `python3 -m pytest tests -x -q` проходит без изменений (никакие существующие модули не редактируются).

## 11. Риски

Один писатель на БД (файловый lock + `BEGIN IMMEDIATE`; кластер вне scope). Синхронный sqlite в event loop (WAL/короткие; план Б — `asyncio.to_thread`). LLM пишет плохие спеки → валидация + лимиты (256 шагов/16 параллелизма/3 ретрая). `agent` с WRITE-тулами — осторожно как эффектные (§7). `UNKNOWN_EFFECT` не проглатывается молча (I10). Fake vs real clock — только fake.

## 12. Инварианты (для тестов/ревью)

I1 validate==[] ⇒ DAG ацикличен, deps существуют, id уникальны. I2 write ⇒ write_paths непусты; пересечение WRITE-зон ⇒ есть путь зависимости. I3 attempt с `unknown` для write/exec коммитится ДО вызова тула. I4 переходы только §5; перезапуск — новая attempt. I5 после reconcile нет ранов queued/running/cancelling. I6 WRITE-шаг запускается только без пересечения зон с concurrently-running шагами. I7 ≤ max_parallel на ран и ≤ max_global_parallel всего. I8 retryable вычисляет код; модель не влияет. I9 effect_status: `unknown` до верификации; `confirmed` только через verify. I10 `completed` невозможен при непроваленных гейтах или непустом `unknown_effects` без решения координатора. I11 существующие модули не изменены; тул — только по конфигурации системы.

## 13. Декомпозиция (непересекающиеся компоненты следующей волны)

| # | Компонент | Файлы (только его) | Зависит от | Критерий |
|---|---|---|---|---|
| C1 | Spec/валидация | `core/workflows/spec.py` | pydantic | test_workflow_spec.py зелёный; I1–I2, pin |
| C2 | Журнал | `core/workflows/journal.py`, `schema.sql` | Row-типы C1 | test_workflow_journal.py; I3–I4, reopen |
| C3 | Движок | `core/workflows/engine.py`, `failures.py` | контракты C1–C2; parallel_lanes, factory.failures, model_access (read-only) | test_workflow_engine.py; I6–I8 |
| C4 | Reconcile/resume | `core/workflows/reconcile.py` | контракт C2; checkpoints (read-only) | test_workflow_reconcile.py; I5, I9 |
| C5 | API+тул | `core/workflows/api.py`, `tools/workflow_tools.py` | контракты C1–C4; AgentFactory (read-only) | test_workflow_tool.py; весь suite без регрессий; I10–I11 |

Порядок: C1 ∥ C2 → C3 ∥ C4 → C5; C3 и C4 связаны только контрактом C2.

## 14. Этапы

1. C1+C2 (типы, валидация, журнал, pin) — совместимость: ничего не меняется.
2. C3+C4 (движок: lanes/дедлайны/бюджет/гейты; reconcile/resume).
3. C5 (API + тул `workflow_run` по конфигурации; init_tools required/optional; регресс-прогон всего suite).
4. Следующая волна (вне плана): event-rebuild UI (P2-1 поверх `workflow_events`), MCP fan-out (P1-1), журнал для `pipeline_registry`/`background_agents`, benchmark §6 отчёта.

## 15. Честные границы

Guarantees: durability статусов; финализация всех ранов после crash (I5); bounded concurrency/дедлайны; структурированные не-слепые отказы; бюджет-пауза с ограниченным overshoot; гейты с bounded review; авто-retry только безопасных kind. NOT guarantees: exactly-once; авто-resume WRITE без верификации; строгий нулевой overshoot; многопроцессность; производительность (не измерено — benchmark отдельной задачей).
