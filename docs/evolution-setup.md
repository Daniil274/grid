# Эволюция Grid в контейнерах: руководство оператора

Как с нуля поднять контур самоулучшения Grid, пользоваться им и обслуживать его.
Устройство и гарантии описаны в [self-improvement.md](self-improvement.md), агенты
мастерской — в [examples/system-admin/README.md](../examples/system-admin/README.md).

## Как это устроено

```
 хост (оператор)                          Docker
 ─────────────────────────────            ──────────────────────────────────────
 grid-control serve  :8010  ◄──── API ────  workshop  (grid-agent-system:local)
   evolution.git (stable)                   system-admin, клон stable, UI :8001
   policy.json (сценарии)
   experiments.db (журнал)   ── docker ──►  испытания: baseline | candidate
                                            + verifier (python:3.11-slim)
                                            + egress-прокси (только egress_hosts)
```

* **Контроллер** работает на хосте, владеет веткой `stable`, политикой и журналом.
  Только он управляет Docker.
* **Мастерская** — контейнер с системой администратора. Она получает `stable`,
  готовит кандидатов и отправляет их контроллеру. Файлов хоста и Docker socket у
  неё нет.
* **Испытания** — одноразовые контейнеры, по одному на каждый прогон baseline или
  кандидата. Сеть у них внутренняя, наружу можно выйти только через egress-прокси.

## 1. Требования

| Что | Зачем |
|---|---|
| Docker Desktop (Windows, macOS) или Docker Engine с Compose v2 (Linux) | образы, мастерская, испытания |
| Python 3.10+ и Git на хосте | контроллер `grid-control` |
| Grid, установленный на хосте: `pip install -e .` в корне репозитория | даёт команды `grid-control`, `grid-workshop` |
| Ключ провайдера моделей, например `OPENROUTER_API_KEY` | администратор, роутер и агенты кандидатов |
| 15 ГБ свободного места | образы (~2,3 ГБ каждый), кандидаты, кэш сборки |

## 2. Образы

```powershell
docker compose build grid          # grid-agent-system:local: runtime кандидатов и мастерской
docker pull python:3.11-slim       # верификатор и egress-прокси
```

Первая сборка идёт долго: ставятся FFmpeg, Git, Node, CodeGraph, Dolt и Beads.
Версии внешних инструментов зафиксированы в `Dockerfile` (`CODEGRAPH_VERSION`,
`DOLT_VERSION`/`DOLT_SHA256`, `BEADS_VERSION`/`BEADS_SHA256`), архивы
проверяются по sha256. Чтобы обновить инструмент, поменяйте версию и контрольную
сумму вместе.

В контекст сборки не попадают локальные данные (`.dockerignore`): `.env`,
`logs/`, `traces/`, каталоги `data/` (в том числе `core/data/timeline.db` с
историей разговоров), `.grid`, `.codegraph`, `workspace/`.

Проверка образа:

```powershell
docker run --rm --entrypoint sh grid-agent-system:local -c "codegraph --version && bd --version && dolt version && grid-workshop --help"
```

Кандидат собирается поверх этого образа: контроллер копирует закоммиченные файлы
кандидата в `/opt/grid/candidate`. Поэтому образ задаёт только зависимости.
Изменение `requirements.txt` или внешних программ требует пересборки образа, а
кандидаты после этого нужно оценить заново: контроллер записывает в журнал точный
ID образа, на котором шла оценка.

## 3. Каталог оператора

Держите всё хозяйство контроллера **вне** репозитория Grid и вне папок,
доступных мастерской. Примеры ниже используют `C:\grid-evolution`, на Linux и
macOS подойдёт `~/grid-evolution`.

```powershell
New-Item -ItemType Directory C:\grid-evolution
grid-control init --repo C:\grid-evolution\evolution.git --from C:\Users\me\grid --ref HEAD
```

`init` создаёт голый репозиторий, в котором `stable` указывает на выбранный
коммит. Незакоммиченные изменения рабочей копии в него не попадают.

## 4. Политика

```powershell
Copy-Item grid_control\policy.example.json C:\grid-evolution\policy.json
```

| Поле | Значение |
|---|---|
| `runtime_image` | образ из шага 2, обычно `grid-agent-system:local` |
| `verifier_image` | `python:3.11-slim` (верификатор и egress-прокси, только stdlib) |
| `checks` | HTTP-проверки запуска: путь, статус, значение по JSON Pointer |
| `scenarios` | **закрытый** приёмочный набор: сообщение в чат и ожидания (`system`, `agent`, `tools_called`, `tools_not_called`, `output_matches`, `timeout_seconds`) |
| `dev_scenarios` | открытый набор для `control_trial`, мастерская видит его целиком |
| `environment_names` | имена переменных окружения контроллера, которые передаются кандидату, например `["OPENROUTER_API_KEY"]` |
| `egress_hosts` | хосты, до которых кандидат может дойти по HTTPS, например `["openrouter.ai"]`, допустим шаблон `*.example.com` |
| `repetitions` | повторы для каждой стороны, порядок baseline и кандидата чередуется |
| `min_improvement` | 0 — не хуже baseline; больше 0 — нужен прирост доли пройденных проверок |
| `scenario_pass_rate` | доля повторов, в которых сценарий должен пройти: 1.0 (по умолчанию) — во всех; 0.66 при 3 повторах — в большинстве. Роутер на модели иногда ошибается на пограничных фразах, и строгое правило отклоняет верные правки. HTTP-проверки всегда должны проходить во всех повторах |
| `auto_promote` | `true` переносит принятого кандидата в `stable` без оператора (по умолчанию `false`) |

Сценарии в примере — отправная точка. **Приёмочный набор замените своим** и
храните только в `policy.json` оператора: всё, что лежит в репозитории, мастерская
видит в своём клоне и может под это подогнать. Сценарий только на маршрутизацию
(без `tools_*` и `output_matches`) останавливается на событии `routed` и стоит
один вызов роутера. Сценарий с проверкой ответа проходит ход агента целиком.

Стоимость эксперимента примерно `(checks + scenarios) × repetitions × 2` прогонов.

## 5. Токен и ключи

Токен связывает мастерскую с контроллером, длина — не меньше 32 символов:

```powershell
$env:GRID_CONTROL_TOKEN = -join ((1..48) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
$env:OPENROUTER_API_KEY = "<ключ>"
```

```bash
export GRID_CONTROL_TOKEN=$(python -c "import secrets; print(secrets.token_hex(24))")
export OPENROUTER_API_KEY=<ключ>
```

Ключи задаются в окружении процессов и не пишутся в файлы политики. Контроллер
передаёт кандидату только переменные из `environment_names`. В отчёты значения
не попадают.

## 6. Контроллер

В том же окне, где заданы переменные:

```powershell
grid-control --state C:\grid-evolution\experiments.db serve `
  --repo C:\grid-evolution\evolution.git --policy C:\grid-evolution\policy.json --port 8010
```

Проверка из другого окна с тем же токеном:

```powershell
curl.exe -H "Authorization: Bearer $env:GRID_CONTROL_TOKEN" http://127.0.0.1:8010/scenarios
```

Контроллер слушает `127.0.0.1`. Docker Desktop доступен из контейнеров как
`host.docker.internal`, поэтому открывать порт наружу не нужно. На Linux
`host.docker.internal` указывает на мост Docker: запустите контроллер с
`--host 172.17.0.1` (адрес `docker0`, см. `ip addr show docker0`).

При перезапуске контроллер доделывает эксперименты из очереди. Прерванные
посреди прогона помечаются `failed`, их контейнеры удаляются.

## 7. Мастерская

В окне с `GRID_CONTROL_TOKEN` и `OPENROUTER_API_KEY`:

```powershell
docker compose --profile evolution up -d workshop
docker compose logs -f workshop
```

При первом старте `grid-workshop init` клонирует `stable` в том
`grid_workshop-data` (`/workspace/grid`). В логе будет
`Workshop /workspace/grid tracks stable <sha>`. Интерфейс откроется на
<http://localhost:8001>. `GRID_CONTROL_URL` по умолчанию указывает на
`http://host.docker.internal:8010`.

В контейнер передаются только адрес и токен контроллера и ключ модели. `.env`
не подключается. Если переменная не задана, `grid-workshop init` завершается с
понятной ошибкой.

Мастерская переживает перезапуски: при следующем старте `init` лишь обновляет
`refs/remotes/controller/stable`, незавершённая работа остаётся на месте. Начать
с чистого листа:

```powershell
docker compose --profile evolution rm -sf workshop
docker volume rm grid_workshop-data
```

(`docker compose down -v` не используйте: он удалит и тома SearXNG и Valkey.)

## 8. Работа

Задачу администратору ставят в чате мастерской, например: «Сообщения про нарезку
лучших моментов из ролика должны уходить в систему video. Уточни описание и
отправь кандидата». Администратор сам откроет эксперимент, поручит правку
специалисту, прогонит `control_trial` на открытых сценариях и отправит кандидата.
В ответе он называет ID эксперимента.

Оператор проверяет результат:

```powershell
grid-control --state C:\grid-evolution\experiments.db show <id>
git -C C:\grid-evolution\evolution.git diff <baseline> <candidate>
grid-control --state C:\grid-evolution\experiments.db promote <id>
```

`show` выдаёт полный журнал: образы, каждый прогон, подробности сценариев (их
мастерская не видит) и решение. `promote` переносит `stable` на кандидата и
отказывается, если `stable` сдвинулся после baseline эксперимента: такого
кандидата надо оценить заново. Следующий `control_begin` в мастерской начнёт уже
с нового `stable`.

## 9. Выкатка в рабочую установку

`promote` меняет только ветку `stable` репозитория эволюции. Перенести её в
рабочую копию и обновить установку:

```powershell
git fetch C:\grid-evolution\evolution.git stable:evolution/stable
git merge --ff-only evolution/stable      # или просмотрите и слейте вручную
docker compose up -d --build grid
```

Автоматического переключения рабочей установки пока нет (см. «Следующие части
полного цикла» в self-improvement.md).

## 10. Обслуживание

| Задача | Команда |
|---|---|
| Прибрать ресурсы эксперимента, прерванного вместе с контроллером | `grid-control --state … cleanup <id>` |
| Образы кандидатов (копятся по два на эксперимент) | `docker image ls --filter label=grid.experiment`, затем `docker image prune -a --filter label=grid.experiment` |
| Кэш сборки | `docker builder du`; `docker builder prune` освобождает место, но следующая сборка пойдёт с нуля |
| Журнал | `experiments.db` из `--state`; события хранят всё, включая подробности сценариев |
| Новый runtime после смены зависимостей | `docker compose build grid`, затем оценивать кандидатов заново |

## 11. Если что-то не работает

| Симптом | Причина и решение |
|---|---|
| `grid-workshop: The workshop requires GRID_CONTROL_URL and GRID_CONTROL_TOKEN` | не задан токен в окне, из которого запущен compose |
| `Controller is unreachable` в мастерской | контроллер не запущен, другой порт или на Linux он слушает `127.0.0.1` вместо адреса моста |
| `Controller refused (401)` | токены контроллера и мастерской различаются |
| `docker image … No such image: grid-agent-system:local` в эксперименте | образ из шага 2 не собран или в политике другое имя |
| baseline и кандидат оба 0.0 | runtime не запускает Grid: запустите образ кандидата вручную (`docker run --rm grid-candidate:<id>-<sha>`) и прочитайте ошибку старта |
| сценарии падают с `error: … 403 Key limit exceeded` или `401` | исчерпан лимит или неверен ключ провайдера в окружении контроллера |
| сценарии падают с `cannot open chat` или ответа нет до таймаута | модель недоступна: хоста провайдера нет в `egress_hosts` или имя переменной ключа не указано в `environment_names` |
| `docker version` висит, сборка обрывается `rpc error … EOF` | завис Docker Desktop: перезапустите его, прерванные эксперименты контроллер пометит `failed` |
| `Experiment queue is full` | одновременно не больше четырёх экспериментов и испытаний; дождитесь завершения |

## 12. Что изолировано и что нет

* Мастерская не видит файлов хоста, `.env`, Docker socket и закрытых сценариев.
  Её единственный канал к контроллеру — API с токеном, который принимает только
  коммиты.
* Кандидат не видит журнала, политики и исходников верификатора. Сеть у него
  внутренняя, выход только через прокси к `egress_hosts` на порт 443. Корневая
  ФС только для чтения, привилегии сброшены, лимиты памяти, CPU и процессов.
* Ключи из `environment_names` кандидат получает: код кандидата может ими
  пользоваться в пределах разрешённых хостов. Дайте контуру отдельный ключ с
  лимитом расходов.
* Оператор Docker видит окружение контейнеров.
