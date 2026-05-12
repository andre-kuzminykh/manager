# CEO Brain section — apply to hmnd_dev_dashboard

> Подготовлено в 1 commit'е, **9/9 tests passed locally**. У моего signing-server'a другая area, в `hmnd_dev_dashboard` репо commit'нуть не вышло — поэтому даю готовые файлы + patch.

## Что внутри

| Файл | Куда положить в репо `hmnd_dev_dashboard` |
|---|---|
| `_backend_services_ceo_brain.py` | `backend/services/ceo_brain.py` (новый) |
| `_frontend_sections_ceo_brain.py` | `frontend/sections/ceo_brain.py` (новый) |
| `_frontend_main.py` | `frontend/main.py` (заменить) |
| `_tests_test_ceo_brain.py` | `tests/test_ceo_brain.py` (новый) |
| `_docs_SPEC_CEO_BRAIN.md` | `docs/SPEC_CEO_BRAIN.md` (новый) |
| `CEO_BRAIN.patch` | git apply патч одной командой |

## Apply через patch (рекомендуется)

```bash
git clone https://github.com/andre-kuzminykh/hmnd_dev_dashboard.git
cd hmnd_dev_dashboard
git checkout -b ceo-brain-section
git apply /path/to/CEO_BRAIN.patch
git add backend/services/ceo_brain.py \
        frontend/sections/ceo_brain.py \
        frontend/main.py \
        tests/test_ceo_brain.py \
        docs/SPEC_CEO_BRAIN.md
git commit -m "Add CEO Brain section: tasks + meetings + tech health"
git push -u origin ceo-brain-section
# создать PR в main
```

## Apply вручную (если patch не приложится)

Скопируй файлы с префиксом `_<path>_` в соответствующие места:

```bash
cd hmnd_dev_dashboard
cp /path/to/_backend_services_ceo_brain.py backend/services/ceo_brain.py
cp /path/to/_frontend_sections_ceo_brain.py frontend/sections/ceo_brain.py
cp /path/to/_frontend_main.py frontend/main.py
cp /path/to/_tests_test_ceo_brain.py tests/test_ceo_brain.py
cp /path/to/_docs_SPEC_CEO_BRAIN.md docs/SPEC_CEO_BRAIN.md
```

## Verify

```bash
pip install psycopg pandas plotly streamlit pytest
pytest tests/test_ceo_brain.py -v
# expect: 9 passed
```

## Deploy

В env Streamlit'a добавь:
```
CEO_BRAIN_DB_URL=postgresql://zoom_colleague:Zm9JxLg2nQpRtVc4@34.62.139.101:5433/slack_tasks?sslmode=disable
```

Перезапусти Streamlit. В сайдбаре появится **🧠 CEO Brain** между Alerts и Settings.

Без env-var section покажет warning «не настроена» и остальной dashboard продолжит работать.

## Что отображает CEO Brain

- **Filters**: Period (7/14/30/90/custom) + Source (telegram/slack/zoom/fireflies/email/manual/recurring/All)
- **4 KPI cards**: Tasks extracted / TG messages seen / Zoom meetings / Fireflies meetings
- **Tasks by source** — bar chart с цветами per-source
- **Top assignees** — horizontal bar chart top-15
- **Daily timeseries** — line chart per source
- **Tasks by status / priority** — pie charts
- **Recent meetings** — table с Google Doc link, summary chars, published flag
- **Tech health**: orphans counts + last processed timestamps

Все queries read-only (через role `zoom_colleague`). Spec в `docs/SPEC_CEO_BRAIN.md`.
