# Среда для тестирования `rag_tool_asap`

Этот репозиторий — полный набор скриптов и кода для проверки компонента
`rag_tool_asap` из соседнего `self-service-asap`.

Evaluation pipeline делает три вещи:

- запускает и подготавливает MCP-компонент `rag_tool_asap`;
- собирает структурированные ответы инструмента `RAG_ASAP(..., return_contexts=True)`;
- считает RAGAS `0.3.2` и deterministic retrieval/ranking метрики по canonical ASAP dataset.

## Связь с компонентом

Компонент живёт здесь:

```text
../self-service-asap/services/components/rag_tool_asap
```

Его UI/конфигурационная спецификация описана в:

```text
../self-service-asap/packages/component_specs/src/self_service/component_specs/components/rag_tool_asap
```

`component_specs` задаёт `ComponentConf`: модели, CSV-файл, `triplet_top_k`,
`synthetic_top_n` и `force_rebuild`. Eval suite не дублирует эту схему, а запускает
реальный компонент с его `config.json` и проверяет MCP-контракт, который нужен для оценки.

## Датасет

Canonical dataset берётся из fixture компонента:

```text
../self-service-asap/services/components/rag_tool_asap/tests/files/asap.csv
```

Ожидаемый audit:

- raw rows: `10 000`;
- distinct chunks: `1 867`;
- query-bearing rows: `1 843`;
- document-only rows: `8 157`;
- distinct query IDs: `1 825`.

Главный denominator benchmark — `1 843` строки с вопросами. Дубликаты и неоднозначные
`query_id` сохраняются; результаты связываются по стабильному `sample_id` и исходной CSV-строке,
а не по тексту вопроса.

## Подготовка конфигов

В корне `self-service-asap-eval`:

```bash
cp config.example.toml config.toml
cp .env.example .env
```

`config.toml` задаёт путь к датасету, MCP URL, concurrency для вызовов компонента и настройки
RAGAS. По умолчанию eval ждёт компонент на:

```text
http://localhost:8100/mcp/
```

Ключи judge-моделей читаются из `.env` или окружения:

```text
JUDGE_LLM_API_KEY
JUDGE_LLM_BASE_URL
JUDGE_LLM_MODEL_NAME
JUDGE_EMBED_API_KEY
JUDGE_EMBED_BASE_URL
JUDGE_EMBED_MODEL_NAME
```

`inference.max_concurrency` управляет параллельными MCP-вызовами к компоненту.
`ragas.max_workers` управляет параллельностью judge-модели. Это разные сервисы, поэтому
значения можно настраивать независимо.

## Инициализация `rag_tool_asap`

Перед evaluation нужно поднять реальные зависимости компонента:

- Docker;
- OpenSearch и MinIO из `self-service-asap`;
- LLM endpoint для online-ответов;
- preprocessing LLM endpoint для генерации synthetic QA;
- embedding endpoint.

Заполните секреты в `../self-service-asap/secrets/*.env`. Скрипт создаст отсутствующие файлы
из `.example`, но реальные адреса и ключи для LLM/embedding нужно прописать вручную.

Для локального запуска моделей можно использовать отдельный launcher:

```bash
./scripts/launch-test-models.sh
```

Launcher по умолчанию запускает vLLM как `uv run --no-sync vllm`, поэтому `vllm` должен быть
доступен в `.venv` этого проекта. Если он ещё не установлен:

```bash
uv pip install vllm
```

Если нужно использовать системный или другой executable, задайте `VLLM_BIN`:

```bash
VLLM_BIN=/path/to/vllm ./scripts/launch-test-models.sh
```

Перед запуском серверов launcher проверяет доступность vLLM и завершится сразу, если executable
не найден.

По умолчанию launcher также задаёт `VLLM_USE_FLASHINFER_SAMPLER=0`, чтобы vLLM не падал на
FlashInfer JIT-компиляции в окружениях без CUDA header `curand.h`. Если CUDA toolkit установлен
полностью и нужен FlashInfer sampler, можно вернуть его:

```bash
VLLM_USE_FLASHINFER_SAMPLER=1 ./scripts/launch-test-models.sh
```

По умолчанию он запускает:

- large LM: `Qwen/Qwen2.5-32B-Instruct` на CUDA device `0`, port `7114`;
- small LM: `Qwen/Qwen2.5-7B-Instruct` на CUDA device `1`, port `7113`;
- embeddings: `jinaai/jina-embeddings-v3` на CUDA device `1`, port `3300`.

Логи пишутся в `logs/vllm/`. Скрипт ждёт готовности каждого OpenAI-compatible endpoint через
`/v1/models` и печатает `All model servers are ready.` только после успешной проверки всех трёх
серверов. Если процесс падает при старте или endpoint не поднимается за timeout, скрипт выводит
последние строки соответствующего log-файла и завершает работу.

Основные overrides:

```bash
LARGE_LM_CUDA_VISIBLE_DEVICES=2 \
SMALL_LM_CUDA_VISIBLE_DEVICES=3 \
EMB_CUDA_VISIBLE_DEVICES=2 \
./scripts/launch-test-models.sh
```

Также можно переопределить `LARGE_LM_MODEL`, `LARGE_LM_PORT`, `SMALL_LM_MODEL`,
`SMALL_LM_PORT`, `EMB_EMBEDDINGS_MODEL`, `EMB_PORT`, `TEST_MODEL_READY_TIMEOUT`
и `TEST_MODEL_LOG_DIR`. Полный список:

```bash
./scripts/launch-test-models.sh --help
```

После готовности моделей из `self-service-asap-eval`:

```bash
./scripts/init-rag-tool-asap.sh
```

Скрипт:

- запускает `opensearch` и `minio-storage`;
- ждёт готовности MinIO;
- загружает canonical `asap.csv` в `minio/datasets/rag_tool_asap/doc/asap.csv`;
- запускает `base_rag_tool_asap` через `self-service-asap/scenarios/compose.common.yaml`;
- ждёт готовности `http://localhost:8100/ping`.

Если инфраструктура уже поднята или датасет уже загружен:

```bash
./scripts/init-rag-tool-asap.sh --skip-infra --skip-upload
```

Посмотреть логи компонента:

```bash
cd ../self-service-asap/scenarios
docker compose -f compose.common.yaml logs -f base_rag_tool_asap
```

## Запуск evaluation

Полный baseline по всем вопросам:

```bash
./scripts/run-evaluation.sh
```

Это эквивалентно:

```bash
uv run asap-eval run --config config.toml --max-samples 0
```

`--max-samples 0` и любые значения меньше нуля означают “обработать весь датасет”.
`--max-workers` по умолчанию берётся из `config.toml`.

Smoke-run на 5 вопросах:

```bash
./scripts/run-evaluation.sh --max-samples 5
```

Запуск с другим CSV-датасетом без изменения `config.toml`:

```bash
./scripts/run-evaluation.sh --dataset-path data/squad_selected_full_en.csv
```

То же самое через окружение:

```bash
DATASET_PATH=data/squad_selected_full_en.csv ./scripts/run-evaluation.sh
```

Отдельная проверка MCP input contract для `RAG_ASAP`: валидный structured-запрос,
ошибочные аргументы, пустой запрос и сырой invalid JSON без запуска RAGAS:

```bash
./scripts/run-input-contract-test.sh
```

Тот же MCP endpoint можно дернуть напрямую через `curl`: скрипт `scripts/curl-mcp-request.sh`.

Посмотреть, как живой компонент отвечает на сохранённые вопросы, какие retrieval-контексты
и synthetic demonstrations он вернул:

```bash
./scripts/run-response-demo.sh
```

Вопросы для demo лежат в:

```text
data/rag_tool_demo_queries.json
```

Задать вопрос из CLI:

```bash
./scripts/run-response-demo.sh --question "Что находится на вершине главного здания Нотр-Дам?"
```

Переопределить параметры можно аргументами `asap-eval run`:

```bash
./scripts/run-evaluation.sh --max-samples 10 --max-workers 4 --overwrite
```

Также доступны низкоуровневые команды:

```bash
uv sync --frozen
uv run asap-eval audit --config config.toml --dataset-path data/squad_selected_full_en.csv
uv run asap-eval collect --config config.toml --dataset-path data/squad_selected_full_en.csv --max-samples 5
uv run asap-eval evaluate --config config.toml --dataset-path data/squad_selected_full_en.csv --run-dir results/<run-id> --max-workers 2
uv run asap-eval demo --config config.toml --limit 2
```

## Артефакты

Каждый collection создаёт директорию:

```text
results/<UTC timestamp>-<short dataset hash>/
```

Внутри:

- `run_manifest.json` — sanitized config, dataset audit, MCP preflight, версии, имена метрик,
  состояние git для `self-service-asap` и имена judge-моделей; ключи маскируются;
- `inference_samples.jsonl` — checkpointed inference record для каждой строки с вопросом;
- `ragas_input.jsonl` — ordered `SingleTurnSample` payloads для RAGAS;
- `scores.csv` — плоская таблица per-sample score с RAGAS-метриками, `context_hit`,
  `title_hit`, `context_reciprocal_rank`, `context_ndcg`, статусом и source identity;
- `scores.jsonl` — score records, объединённые с полной nested inference trace;
- `summary.json` и `summary.md` — агрегаты, NaN/failure counts, retrieval accuracies,
  `context_mrr` и `context_ndcg`.

`context_ndcg` считается по retrieval trace как сумма discounted hits точного `chunk_id`,
делённая на сумму discounts по всем возвращённым контекстам.

Resume работает по `sample_id`: уже записанные строки из `inference_samples.jsonl` не вызываются
повторно, если не передать `--overwrite` в `collect` или `run`.

RAGAS judging обычно дороже, чем collection ответов. Сначала проверьте smoke-run на 5-10
примерах, затем запускайте полный baseline.
