"""Independent literal PDF oracle for cross-language mock scoring tests."""
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from itertools import product
import json
from pathlib import Path


def cases():
    checklist = json.loads((Path(__file__).resolve().parents[1] / 'data/checklists/v1.json').read_text())
    domains = [(['yes', 'no'] if c['answer_type'] == 'binary' else c['scale']) + (['na'] if c['na_allowed'] else []) for c in checklist['criteria']]
    for values in product(*domains):
        points, weight = Fraction(), Fraction()
        for c, answer in zip(checklist['criteria'], values):
            if answer == 'na' or c['weight'] == 0:
                continue
            weight += Fraction(str(c['weight']))
            if c['answer_type'] == 'binary':
                points += c['weight'] if answer == 'yes' else 0
            else:
                points += Fraction(str(answer)) / Fraction(str(max(c['scale']))) * Fraction(str(c['weight']))
        exact = points * 100 / weight if weight else None
        total = None if exact is None else float((Decimal(exact.numerator) / Decimal(exact.denominator)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
        yield {'answers': dict(zip([c['id'] for c in checklist['criteria']], values)), 'total': total, 'critical': values[-1] == 'yes'}


if __name__ == '__main__':
    print(json.dumps(list(cases()), separators=(',', ':')))
