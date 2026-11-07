# Приоритет ручной разметки DEV — только для координатора

**Не показывайте этот файл разметчикам:** варианты ниже раскрывают ответы моделей и машинных эталонов.
Для независимой разметки используйте `?label=1&queue=priority`: там только исходный диалог и один критерий.

Найдено спорных ячеек: **98**; очередь: **40** ячеек / **29** диалогов.
Это выбранные трудные случаи DEV, а не случайная выборка и не независимый контроль точности.

## Формула и ограничения

`priority = max(Gini моделей, доля несовпадений с машинными эталонами) × effective_weight × frequency`.
Каждая семья имеет массу 1; её доступные повторы/конфигурации делят эту массу поровну. Поэтому три Haiku и два Sonnet не становятся пятью независимыми голосами. Каскад — отдельная конфигурация, зависимая от Haiku/Gemini; независимость семейств также не предполагается.
`frequency` — доля спорных диалогов среди наблюдаемых диалогов по этому критерию; это наблюдаемый proxy повторяемости неоднозначной трактовки, не доказанная причина ошибок.
`effective_weight` равен весу критерия; для критического C8 — максимальному весу чек-листа. Кроме того, спорные критические флаги идут отдельной первой группой; затем остальные ячейки по priority. Иначе редкие грубые нарушения исчезли бы под частыми спорами о стиле общения. Это явно заданное правило безопасности при отборе, не изменение формулы итога.
Пустые, неизвестные и недопустимые ответы исключены, а не считаются отдельным вариантом. В спор включается только ячейка хотя бы с одним ответом модели. Равные приоритеты сортируются детерминированно.
Польза разметки и время работы человека **не измерены**. Число в колонке — эвристика, не ожидаемый прирост точности. Silver/gold_v2/gold_v2_full — машинные ориентиры, не независимые человеческие голоса.

## Очередь

| № | Диалог | Критерий | Приоритет | Ответы прогонов | Машинные ориентиры | Почему спорно |
|---:|---|---|---:|---|---|---|
| 1 | p4-ru-004 | C8 | 0.1154 | haiku-g10="no"; haiku-g9="yes"; sonnet-cli="no" | gold_v2="no"; gold_v2_full="no"; silver="no" | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся; критический флаг: вес 0 не обнуляет приоритет |
| 2 | p4-ru-004 | C4 | 4.6154 | haiku-g10=10; haiku-g9=10; sonnet-cli=20 | gold_v2=15.0; gold_v2_full=15.0; silver=15.0 | модельные ответы расходятся; модель и машинный эталон расходятся |
| 3 | p1-en-015 | C6 | 3.9231 | haiku-g10=7; haiku-g9=7; sonnet-cli=7 | gold_v2=4.0; gold_v2_full=4.0 | модель и машинный эталон расходятся |
| 4 | p1-ru-009 | C6 | 3.9231 | haiku-g10=7; haiku-g9=7; sonnet-cli=7 | gold_v2_full=4.0 | модель и машинный эталон расходятся |
| 5 | p1-ru-022 | C6 | 3.9231 | haiku-g10=10; haiku-g9=10; sonnet-cli=10 | gold_v2_full=4.0; silver=4.0 | модель и машинный эталон расходятся |
| 6 | p4-ru-001 | C6 | 3.9231 | haiku-g10=4; haiku-g9=4; sonnet-cli=10 | gold_v2=7.0; gold_v2_full=7.0; silver=7.0 | модельные ответы расходятся; модель и машинный эталон расходятся |
| 7 | p1-en-002 | C7 | 3.3077 | cascade="no"; deepseek="no"; gemini="no"; haiku-g10="no"; haiku-g9="no"; haiku-g9-repeat="no"; qwen="no"; sonnet-api="no"; sonnet-cli="no"; terra="no" | gold_v2_full="yes" | модель и машинный эталон расходятся |
| 8 | p1-en-008 | C4 | 3.2967 | cascade=10; deepseek=20; gemini=15; haiku-g10=10; haiku-g9=10; haiku-g9-repeat=10; qwen=20; sonnet-api=15; sonnet-cli=15; terra=10 | gold_v2_full=20.0 | модельные ответы расходятся; модель и машинный эталон расходятся |
| 9 | p1-ru-020 | C4 | 3.2967 | cascade=10; deepseek=20; gemini=20; haiku-g10=10; haiku-g9=10; haiku-g9-repeat=10; qwen=15; sonnet-api=15; sonnet-cli=15; terra=10 | gold_v2_full=20.0 | модельные ответы расходятся; модель и машинный эталон расходятся |
| 10 | p1-kz-015 | C6 | 3.0824 | cascade=10; deepseek=4; gemini=10; haiku-g10=10; haiku-g9=10; haiku-g9-repeat=10; qwen=7; sonnet-api=10; sonnet-cli=7; terra=10 | gold_v2_full=7.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 11 | p1-kz-005 | C6 | 2.9423 | haiku-g10=7; haiku-g9=4; sonnet-cli=7 | gold_v2=4.0; gold_v2_full=4.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 12 | p1-ru-003 | C6 | 2.9423 | haiku-g10=10; haiku-g9=7; sonnet-cli=4 | gold_v2_full=10.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 13 | p1-kz-015 | C4 | 2.9304 | cascade=20; deepseek=0; gemini=20; haiku-g10=10; haiku-g9=10; haiku-g9-repeat=0; qwen=10; sonnet-api=10; sonnet-cli=10; terra=20 | gold_v2_full=10.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 14 | p1-ru-005 | C4 | 2.9147 | cascade=15; deepseek=20; gemini=15; haiku-g10=15; haiku-g9=15; haiku-g9-repeat=10; qwen=20; sonnet-api=20; sonnet-cli=15; terra=10 | gold_v2=15.0; gold_v2_full=15.0; silver=15.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 15 | p4-ru-005 | C6 | 2.8022 | cascade=10; deepseek=7; gemini=10; haiku-g10=4; haiku-g9=4; haiku-g9-repeat=4; qwen=4; sonnet-api=7; sonnet-cli=7; terra=10 | gold_v2=7.0; gold_v2_full=7.0; silver=7.0 | модельные ответы расходятся; модель и машинный эталон расходятся |
| 16 | p4-ru-003 | C2 | 2.4615 | cascade="na"; deepseek="na"; gemini="na"; haiku-g10="na"; haiku-g9="no"; haiku-g9-repeat="no"; qwen="yes"; sonnet-api="na"; sonnet-cli="na"; terra="no" | gold_v2="no"; gold_v2_full="no"; silver="no" | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 17 | p1-ru-022 | C2 | 2.4231 | haiku-g10="na"; haiku-g9="no"; sonnet-cli="na" | gold_v2="no"; gold_v2_full="no"; silver="no" | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 18 | p1-en-007 | C6 | 2.3841 | cascade=4; deepseek=10; gemini=4; haiku-g10=10; haiku-g9=10; haiku-g9-repeat=7; qwen=10; sonnet-api=7; sonnet-cli=7; terra=10 | gold_v2_full=10.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 19 | p1-ru-026 | C6 | 2.3307 | cascade=4; deepseek=0; gemini=4; haiku-g10=7; haiku-g9=4; haiku-g9-repeat=4; qwen=4; sonnet-api=7; sonnet-cli=7; terra=7 | gold_v2=4.0; gold_v2_full=4.0; silver=4.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 20 | p1-kz-011 | C4 | 2.3077 | haiku-g10=20; haiku-g9=20; sonnet-cli=15 | gold_v2=20.0; gold_v2_full=20.0; silver=15.0 | модельные ответы расходятся; модель и машинный эталон расходятся; машинные эталоны расходятся |
| 21 | p1-kz-013 | C4 | 2.3077 | haiku-g10=15; haiku-g9=15; sonnet-cli=20 | gold_v2=20.0; gold_v2_full=20.0; silver=20.0 | модельные ответы расходятся; модель и машинный эталон расходятся |
| 22 | p1-ru-024 | C4 | 2.3077 | haiku-g10=20; haiku-g9=20; sonnet-cli=15 | gold_v2=20.0; gold_v2_full=20.0 | модельные ответы расходятся; модель и машинный эталон расходятся |
| 23 | p1-ru-025 | C2 | 2.3077 | cascade="na"; deepseek="yes"; gemini="na"; haiku-g10="yes"; haiku-g9="yes"; haiku-g9-repeat="yes"; qwen="na"; sonnet-api="na"; sonnet-cli="na"; terra="na" | gold_v2="yes"; gold_v2_full="yes"; silver="yes" | модельные ответы расходятся; модель и машинный эталон расходятся |
| 24 | p4-ru-001 | C4 | 2.3077 | haiku-g10=10; haiku-g9=10; sonnet-cli=15 |  | модельные ответы расходятся |
| 25 | p1-ru-026 | C4 | 2.3025 | cascade=0; deepseek=0; gemini=0; haiku-g10=0; haiku-g9=0; haiku-g9-repeat=5; qwen=5; sonnet-api=5; sonnet-cli=5; terra=5 | gold_v2=0.0; gold_v2_full=0.0; silver=0.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 26 | p4-ru-005 | C2 | 1.9780 | cascade="yes"; deepseek="yes"; gemini="yes"; haiku-g10="no"; haiku-g9="no"; haiku-g9-repeat="no"; qwen="na"; sonnet-api="na"; sonnet-cli="na"; terra="na" | gold_v2="yes"; gold_v2_full="yes"; silver="yes" | модельные ответы расходятся; модель и машинный эталон расходятся |
| 27 | p1-ru-016 | C6 | 1.9615 | haiku-g10=7; haiku-g9=7; sonnet-cli=4 | gold_v2=4.0; gold_v2_full=4.0; silver=4.0 | модельные ответы расходятся; модель и машинный эталон расходятся |
| 28 | p1-ru-029 | C6 | 1.9615 | haiku-g10=4; haiku-g9=4; sonnet-cli=7 | gold_v2_full=7.0 | модельные ответы расходятся; модель и машинный эталон расходятся |
| 29 | p4-ru-002 | C6 | 1.9615 | haiku-g10=7; haiku-g9=7; sonnet-cli=10 | gold_v2=10.0; gold_v2_full=10.0; silver=10.0 | модельные ответы расходятся; модель и машинный эталон расходятся |
| 30 | p4-ru-003 | C6 | 1.8637 | cascade=7; deepseek=10; gemini=7; haiku-g10=7; haiku-g9=4; haiku-g9-repeat=4; qwen=7; sonnet-api=4; sonnet-cli=7; terra=7 | gold_v2=7.0; gold_v2_full=7.0; silver=7.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 31 | p1-ru-023 | C4 | 1.8001 | cascade=20; deepseek=20; gemini=20; haiku-g10=15; haiku-g9=20; haiku-g9-repeat=15; qwen=20; sonnet-api=20; sonnet-cli=20; terra=10 | gold_v2=20.0; gold_v2_full=20.0; silver=20.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 32 | p1-ru-007 | C6 | 1.7436 | cascade=10; deepseek=7; gemini=10; haiku-g10=10; haiku-g9=7; haiku-g9-repeat=10; qwen=10; sonnet-api=7; sonnet-cli=7; terra=10 | gold_v2_full=10.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 33 | p4-ru-002 | C4 | 1.7308 | haiku-g10=20; haiku-g9=15; sonnet-cli=15 | gold_v2=15.0; gold_v2_full=15.0; silver=15.0 | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 34 | p4-ru-003 | C7 | 1.6538 | cascade="na"; deepseek="no"; gemini="na"; haiku-g10="no"; haiku-g9="no"; haiku-g9-repeat="no"; qwen="no"; sonnet-api="no"; sonnet-cli="na"; terra="na" | gold_v2="na"; gold_v2_full="na"; silver="na" | модельные ответы расходятся; модель и машинный эталон расходятся; повторы/варианты одной семьи расходятся |
| 35 | p1-en-001 | C7 | 1.6538 | cascade="no"; deepseek="no"; gemini="no"; haiku-g10="no"; haiku-g9="no"; haiku-g9-repeat="no"; qwen="no"; sonnet-api="no"; sonnet-cli="no"; terra="no" | gold_v2_full="yes"; silver="no" | модель и машинный эталон расходятся; машинные эталоны расходятся |
| 36 | p1-en-003 | C7 | 1.6538 | haiku-g10="no"; haiku-g9="no"; sonnet-cli="no" | gold_v2_full="yes"; silver="no" | модель и машинный эталон расходятся; машинные эталоны расходятся |
| 37 | p1-en-004 | C7 | 1.6538 | cascade="no"; deepseek="no"; gemini="no"; haiku-g10="no"; haiku-g9="no"; haiku-g9-repeat="no"; qwen="no"; sonnet-api="no"; sonnet-cli="no"; terra="no" | gold_v2_full="yes"; silver="no" | модель и машинный эталон расходятся; машинные эталоны расходятся |
| 38 | p1-en-005 | C7 | 1.6538 | cascade="no"; deepseek="no"; gemini="no"; haiku-g10="no"; haiku-g9="no"; haiku-g9-repeat="no"; qwen="no"; sonnet-api="no"; sonnet-cli="no"; terra="no" | gold_v2_full="yes"; silver="no" | модель и машинный эталон расходятся; машинные эталоны расходятся |
| 39 | p1-en-007 | C7 | 1.6538 | cascade="no"; deepseek="no"; gemini="no"; haiku-g10="no"; haiku-g9="no"; haiku-g9-repeat="no"; qwen="no"; sonnet-api="no"; sonnet-cli="no"; terra="no" | gold_v2_full="yes"; silver="no" | модель и машинный эталон расходятся; машинные эталоны расходятся |
| 40 | p1-en-008 | C7 | 1.6538 | cascade="no"; deepseek="no"; gemini="no"; haiku-g10="no"; haiku-g9="no"; haiku-g9-repeat="no"; qwen="no"; sonnet-api="no"; sonnet-cli="no"; terra="no" | gold_v2_full="yes"; silver="no" | модель и машинный эталон расходятся; машинные эталоны расходятся |

## Экспорт и сверка

Обычный `?label=1` экспортирует только полностью заполненный диалог. Приоритетная очередь экспортирует только явно отвеченные ячейки списка одним JSONL; пропуски не превращаются в ответы. Черновики разделены по разметчику, API, версии и хэшам исходных данных.
`scripts/score_labels.py` принимает такой частичный JSONL без смены схемы: `conv_id, criterion_id, answer, annotator, checklist_version, criterion_hash, checklist_hash, transcript_hash, annotation_source, annotated_at`. MAE итога появится только при всех критериях диалога.
Перед экспортом UI заново проверяет исходные хэши. Текущий scorer проверяет версию и хэш критерия, но не сверяет `checklist_hash` / `transcript_hash`; их наличие в JSONL само по себе не заменяет эту проверку. Scorer не изменялся (владелец — Claude).
Одного разметчика недостаточно для независимого согласия. Отдайте одну и ту же слепую очередь двум людям, сохраните их коды, затем разберите расхождения отдельно. Объективную точность проверяйте на другой, заранее фиксированной выборке.

## Воспроизведение

```sh
python scripts/disputed_cells.py --source-root <корень репозитория>
```

Чек-лист: `freedom-chat-quality`, версия **1**. Источник DEV — объединение tune/holdout из `eval/split.json`; историческая настройка на DEV известна.
Хэши ниже фиксируют входы; исходные JSONL не копировались в репозиторий.

| Источник | SHA-256 |
|---|---|
| w-int/results/eval_runs/or-hold-google-gemini-4-argon-g9.jsonl | `2b29871e7e325197253fc10edd8345a9da7c06c7d0e4970d054cd271243a6302` |
| w-int/results/eval_runs/or-hold-cascade-g9.jsonl | `f1d48b75e28e6cdde7c4ba57d35e5e1ab3b17f37399c69c7d797053d0ec2f202` |
| w-int/results/eval_runs/or-hold-anthropic-claude-sonnet-5-5-g9.jsonl | `ca19b08912a591a122ef04968af755851d2c067cec095a8757f84a3e56b6897e` |
| w-int/results/eval_runs/or-hold-qwen-qwen3-8-27b-g9.jsonl | `47512b1b57e172484e1e12907476e5ae4f9183419126b6d88640af0acc93fe21` |
| w-int/results/eval_runs/or-hold-deepseek-deepseek-v4-pro-g9.jsonl | `37b27f0cbdca93daaac5b66c095ec871a5c782e4750df8e9a5b6e52b66a57d45` |
| w-int/results/eval_runs/or-hold-openai-gpt-6-1-terra-g9.jsonl | `7105459973b2e0a222ae67e4fdcfad8422fca2a918655b7595d18bcc35f73e13` |
| w-int/results/eval_runs/hk-g9-all.jsonl | `b0d92ae52fc5ed7d9f1cdaccee6ac12f11cffae60f146e98a267891582628904` |
| w-int/results/eval_runs/hk-g10-all.jsonl | `2d7ae0adca83ca0abe8797817a4d68a72d15dc08fb3700694145651c3928cc35` |
| w-m1/results/eval_runs/m1-hold-haiku-m1-c4.jsonl | `1507a83f66be42c46114c05a554cb237227de0647a642a1223cb145524aaf6a2` |
| w-m1/results/eval_runs/m1-sonnet-g9-all.jsonl | `ca5f68f43a958308e1b65f27141b9cd131c09d9457ac5768f44b7ef321e7fc8d` |
| w-m1/eval/gold/silver.jsonl | `888d9b67ea75cd44cd6c9946a786c45229159ad078741f707529744b6102d476` |
| agents/reports/gold_v2.jsonl | `14e926481829d4c63c7671e0bec525af7f1260f4661bf27f5e57889d361a41d7` |
| agents/reports/gold_v2_full.jsonl | `017109086cfaf85f6abde28b3c58dff9f711704a33e97300a84a0be0c56eff0a` |
| v1.json | `b2dcca84a4fbbed99c65dd528b8877d37163314626707c88e99d4d173505ca18` |
| split.json | `a87886d63a4b5233c28227a0992d7c2a40f76cc5c75c792141bd0916e9a83dd6` |
