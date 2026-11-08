#!/usr/bin/env python3
"""Deterministic documentation from aggregate quality data; never loads raw runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

PROFILE_NAMES = {
    "cloud": "Облако · Gemini",
    "cascade": "Каскад · Haiku → Gemini",
    "routing": "Маршрутизация",
    "local": "Локально · qwen",
}


def number(value: object, digits: int = 4) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return "не измерено"
    rendered = f"{value:.{digits}f}"
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def markdown(quality_bytes: bytes, models_bytes: bytes) -> str:
    quality, models = json.loads(quality_bytes), json.loads(models_bytes)
    reports = {report["id"]: report for report in quality.get("reports", [])}
    catalog = quality.get("profile_catalog", {})
    report_rates = {
        profile: reports.get(catalog.get(profile, {}).get("report_id"), {}).get("economics", {}).get("currency_fx", {}).get("usd_kzt")
        for profile in PROFILE_NAMES
    }
    known_rates = sorted({value for value in report_rates.values() if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0})
    frozen_fx = ", ".join(number(value) for value in known_rates) if known_rates else "не указан"
    config_rate = models.get("usd_kzt")
    mismatch = bool(known_rates) and any(value != config_rate for value in known_rates)
    lines = [
        "# Экономика банка — воспроизводимые исходные данные",
        "",
        "Сгенерировано `scripts/bank_economics.py` только из агрегатов `web/data/quality.json` и `config/models.json`. "
        "Скрипт не читает транскрипты и не вызывает модели. Числа ниже — сохранённые замеры, не тарифное предложение и не банковский ROI.",
        "",
        f"- SHA256 quality: `{hashlib.sha256(quality_bytes).hexdigest()}`",
        f"- SHA256 models: `{hashlib.sha256(models_bytes).hexdigest()}`",
        f"- Курс в метаданных замеров: {frozen_fx} ₸/$; допущение отчёта, а не проверенный текущий курс. Курс каждого профиля указан в таблице.",
        f"- Текущая конфигурация: {number(config_rate)} ₸/$. " + ("НЕ СОВПАДАЕТ с курсом одного или нескольких замеров. " if mismatch else "") + "Сохранённые суммы не пересчитываются по текущей конфигурации; при отсутствии метаданных курс отчёта неизвестен.",
        "",
        "| Профиль | Диалогов | Записано ₸/диалог | Записано $/диалог | Курс замера, ₸/$ | Среднее время, с | Совпадение / размеченные ячейки | 95% ДИ | На ручную проверку |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    notes = []
    for profile, label in PROFILE_NAMES.items():
        entry = catalog.get(profile, {})
        report = reports.get(entry.get("report_id"), {})
        economics = report.get("economics", {})
        n = report.get("sample", {}).get("dialogues")
        counts = report.get("counts", {})
        total = counts.get("total", 0) - counts.get("unlabelled", 0)
        matched = counts.get("matched")
        agreement = f"{matched}/{total} ({number(matched / total * 100, 1)}%)" if total and isinstance(matched, int) else "не измерено"
        ci = report.get("agreement_ci", {})
        interval = f"{number(ci['lower'] * 100, 1)}–{number(ci['upper'] * 100, 1)}%" if isinstance(ci.get("lower"), (int, float)) and isinstance(ci.get("upper"), (int, float)) else "не измерено"
        review = "не измерено"
        if n and economics.get("review_known_dialogues") == n and isinstance(economics.get("review_count"), int):
            review = f"{economics['review_count']}/{n}"
        kzt, usd = economics.get("cost_per_dialogue_kzt"), economics.get("cost_per_dialogue_usd")
        local = entry.get("api_cost", {})
        if profile == "local" and local.get("amount") == 0 and local.get("currency") == "USD" and local.get("scope") == "local_only" and entry.get("cloud_fallback") is False:
            kzt = usd = 0
        fresh_seconds = economics.get("mean_seconds_per_dialogue") if economics.get("latency_provenance", {}).get("projection_allowed") is True else None
        lines.append(f"| {label} | {number(n, 0)} | {number(kzt)} | {number(usd)} | {number(report_rates[profile])} | {number(fresh_seconds, 2)} | {agreement} | {interval} | {review} |")
        provenance = economics.get("cost_provenance", {})
        notes.append(f"- **{label}**: {provenance.get('coverage_note') or ('Только $0 API; оборудование, электричество и сопровождение не измерены; качество 62,3% (ниже константы 83%) и 341 с/диалог измерены офлайн.' if profile == 'local' else 'Полнота исторических цен не подтверждена.')}")
        latency_note = economics.get("latency_provenance", {}).get("note")
        if latency_note:
            notes.append(f"  Время: {latency_note}")
    lines.extend([
        "", *notes, "",
        "Стоимость — по ценам моделей в каждом вызове; токены по моделям не разделены. "
        "В калькуляторе используются записанные `cost_kzt_full`, суммированные продюсером отчёта по проходам. "
        "Суммарные токены каскада не умножаются на единую цену. Неполная цена или неизвестная полнота не превращаются в доказанный коэффициент экономии.",
        "",
        "## Как считать сценарий",
        "",
        "Поля изначально пусты: чаты и звонки в месяц, ставка QA в ₸/час, минуты ручной проверки, месячный бюджет API и явно введённые расходы с начала месяца. "
        "Объём и трудозатраты — предположения пользователя. Доля звонков выводится из двух объёмов; она не вводится независимо.",
        "",
        "- `месячный API = чаты × средняя цена чата + звонки × средняя цена звонка`. Если у нужного канала нет замера, результат неизвестен. Общая средняя выборки не заменяет отсутствующий канал.",
        "- `время на 10 000 = 10 000 × взвешенное среднее секунд`. Только при явном `latency_provenance.projection_allowed=true`; повтор из кэша не измеряет свежий расчёт. Это последовательная модельная работа, не SLA и не обещание календарного времени при конкуренции.",
        "- `часы QA = объём × минуты / 60 × (1 − доля needs_review)`. Только явный итоговый `needs_review` всех диалогов образует долю; эскалация на Gemini не является ручной проверкой. Без полного наблюдения — неизвестно.",
        "- `ценность времени до прочих расходов = часы QA × ставка − API`. Это сценарий при прежней длительности проверки и контроле только помеченных диалогов. Контроль выборки, внедрение, ошибки, оборудование и сопровождение не измерены; показатель не является подтверждённым ROI или фактическим сокращением фонда оплаты труда.",
        "- `остаток = месячный бюджет − явно введённые расходы`. Пустые расходы не считаются нулём. Отрицательный остаток допустим и означает уже превышенный бюджет.",
        "",
        "## Бюджет перед применением",
        "",
        "`estimatePlanBudget` использует `pairs_to_evaluate × средняя цена пары`, если сервер передал пары. "
        "Пара означает диалог × критерий, а не вызов провайдера; распределённая цена пары — приблизительная оценка: пакетный вызов для одного изменённого критерия может стоить иначе. "
        "Без числа пар используются число диалогов и измерения каналов, если состав известен; иначе общая средняя с явной оговоркой. "
        "Текущее подключение в табло и редакторе передаёт выбранные диалоги, а не серверный план пар: это сценарий полной оценки, без учёта кэша и переиспользования критериев. "
        "Нулевой план стоит ноль; `accurate` не оценивается по замеру `fast`. Дополнительные повторы, длина новых диалогов и критерии меняют цену.",
        "",
        "Предупреждение не запускает прогон. Кнопка переключения вызывает только переданный родителем `onSwitchCascade`; она не обещает, что каскад укладывается в остаток. "
        "Во время выполнения плана поля и переключение отключены. Бюджеты сохраняются через `localStorage` с `try/catch`; блокировка хранилища не мешает расчёту. "
        "Виджеты одной страницы синхронизируют ввод, включая явно потраченную сумму. Учёта банковских расходов со стороны сервера здесь нет.",
        "",
        "## Подключение и проверка",
        "",
        "`mountCalculator(host, {quality, storage?, onChange?})` возвращает `destroy`, `updateQuality`, `getInputs`; подробности изначально свёрнуты. "
        "`mountBudget(host, {quality, plan, profile, busy?, storage?, onSwitchCascade?})` возвращает `destroy`, `update`. "
        "Модуль не делает сетевых запросов: агрегаты передаёт родитель. Четыре профиля: `cloud`, `cascade`, `routing`, `local`.",
        "",
        "Тесты: `node --test tests/frontend_bank_economics*.test.mjs`. Они используют синтетические измерения, проверяют формулы, неизвестные значения, бюджет, DOM, синхронизацию и детерминизм генератора. "
        "Browser Use отказал доступу к локальной странице с причиной `user declined permission`; скриншоты, фактические размеры целей и размещение 1440/projector не подтверждены. "
        "CSS наследует семантическую палитру приложения, задаёт минимум 44px, видимый фокус, адаптивную сетку и reduced-motion; DOM-тесты не заменяют проверку в браузере.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality", type=Path, default=Path("web/data/quality.json"))
    parser.add_argument("--models", type=Path, default=Path("config/models.json"))
    parser.add_argument("--output", type=Path, default=Path("docs/BANK_ECONOMICS.md"))
    args = parser.parse_args()
    args.output.write_text(markdown(args.quality.read_bytes(), args.models.read_bytes()), encoding="utf-8")


if __name__ == "__main__":
    main()
