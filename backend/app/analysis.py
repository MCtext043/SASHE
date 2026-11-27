"""Explainable calculations for the quality review. No regulatory decision is made here."""
from collections import defaultdict
from hashlib import sha256


MEASURED = ("tests", "process", "stability")
EVENTS = ("deviations", "oos", "changes", "complaints", "capa", "recalls",
          "regulatory", "validation", "equipment", "agreements")


def number(value):
    return float(str(value).replace(",", "."))


def quality_signals(records, near_limit_pct: float = 15.0):
    groups = defaultdict(list)
    for record in records:
        if record.category not in MEASURED:
            continue
        data = record.data
        groups[(record.category, data["parameter"], data["unit"])].append({
            "id": record.id, "date": record.record_date.isoformat(), "batch_id": record.batch_id,
            "value": number(data["result"]), "lower": number(data["lower_limit"]),
            "upper": number(data["upper_limit"]),
        })
    result = []
    for (category, parameter, unit), points in groups.items():
        points.sort(key=lambda p: (p["date"], p["id"]))

        def signal(kind, title, rule, evidence, detail):
            key = sha256(f"{category}|{parameter}|{unit}|{kind}".encode()).hexdigest()[:16]
            result.append({"key": key, "kind": kind, "title": title, "category": category,
                "parameter": parameter, "unit": unit, "rule": rule, "detail": detail,
                "evidence_ids": [p["id"] for p in evidence],
                "batch_ids": list(dict.fromkeys(p["batch_id"] for p in evidence if p["batch_id"])),
                "points": evidence})

        outside = [p for p in points if p["value"] < p["lower"] or p["value"] > p["upper"]]
        if outside:
            signal("outside", "Результат вне указанных границ",
                   "Результат ниже нижней или выше верхней границы из файла.", outside,
                   f"{len(outside)} результат(ов) вне границ спецификации.")
        if len(points) >= 4:
            last = points[-4:]
            if all(a["value"] > b["value"] for a, b in zip(last, last[1:])):
                signal("falling", "Показатель последовательно снижается",
                       "Четыре последних результата строго убывают.", last,
                       f"{last[0]['value']:g} → {last[-1]['value']:g} {unit}; все четыре результата необходимо оценить.")
            if all(a["value"] < b["value"] for a, b in zip(last, last[1:])):
                signal("rising", "Показатель последовательно растёт",
                       "Четыре последних результата строго возрастают.", last,
                       f"{last[0]['value']:g} → {last[-1]['value']:g} {unit}; все четыре результата необходимо оценить.")
        last = points[-1]
        width = last["upper"] - last["lower"]
        if width > 0 and last["lower"] <= last["value"] <= last["upper"]:
            if last["value"] - last["lower"] <= width * near_limit_pct / 100:
                signal("lower_near", "Значение приближается к нижней границе",
                       f"Последнее значение в пределах {near_limit_pct:g}% ширины диапазона от нижней границы.", [last],
                       f"{last['value']:g} {unit}; нижняя граница {last['lower']:g} {unit}.")
            elif last["upper"] - last["value"] <= width * near_limit_pct / 100:
                signal("upper_near", "Значение приближается к верхней границе",
                       f"Последнее значение в пределах {near_limit_pct:g}% ширины диапазона от верхней границы.", [last],
                       f"{last['value']:g} {unit}; верхняя граница {last['upper']:g} {unit}.")
    priority = {"outside": 0, "falling": 1, "rising": 1, "lower_near": 2, "upper_near": 2}
    return sorted(result, key=lambda item: (priority[item["kind"]], item["category"], item["parameter"]))


def year_metrics(records):
    grouped = defaultdict(list)
    for record in records:
        grouped[record.category].append(record)
    batches = len(grouped["batches"])
    counts = {key: len(grouped[key]) for key in ("batches", "deviations", "complaints", "oos")}
    counts["open_capa"] = sum(1 for r in grouped["capa"] if
        r.data.get("status", "").strip().lower() not in {"закрыто", "выполнено", "closed", "completed"})
    rates = {key: round(counts[key] * 100 / batches, 2) if batches else None
             for key in ("deviations", "complaints", "oos")}
    values = defaultdict(list)
    for category in MEASURED:
        for record in grouped[category]:
            data = record.data
            values[(category, data["parameter"], data["unit"])].append(number(data["result"]))
    averages = [{"category": category, "parameter": parameter, "unit": unit,
                 "mean": round(sum(numbers) / len(numbers), 2), "n": len(numbers)}
                for (category, parameter, unit), numbers in sorted(values.items())]
    return {"counts": counts, "rates_per_100_batches": rates, "averages": averages}
