"""Frontier scoring: run a fixed, auto-graded probe set against every usable model and turn accuracy into a 0-100
strength score (written to scores.yaml; the catalog's priors are only used until a model is calibrated).

The probes are short questions with one exact answer (arithmetic, logic, code tracing, constrained reasoning),
weighted by difficulty, so the score separates small, mid and frontier models without needing an LLM judge.
Re-run it whenever providers swap model versions under the same name."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import yaml

from .router import Router

# (prompt, answer regex, weight)
PROBES: list[tuple[str, str, float]] = [
    ("What is 17 * 23? Answer with the number only.", r"^\s*391\s*$", 1),
    ("What is the remainder when 2^100 is divided by 7? Answer with the number only.", r"^\s*2\s*$", 2),
    ("How many positive divisors does 360 have? Answer with the number only.", r"^\s*24\s*$", 2),
    ("A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How many cents does the ball "
     "cost? Answer with the number only.", r"^\s*5\s*$", 1),
    ("What is printed by this Python code?\nx = [1, 2, 3]\ny = x\ny.append(4)\nprint(len(x) + sum(y))\n"
     "Answer with the output only.", r"^\s*14\s*$", 2),
    ("What is printed?\ndef f(n):\n    return n if n < 2 else f(n-1) + f(n-2)\nprint(f(15))\n"
     "Answer with the output only.", r"^\s*610\s*$", 2),
    ("How many ways can you climb 10 stairs taking 1 or 2 steps at a time? Answer with the number only.",
     r"^\s*89\s*$", 2),
    ("What is the sum of all primes below 50? Answer with the number only.", r"^\s*328\s*$", 3),
    ("If all bloops are razzies and all razzies are lazzies, are all bloops definitely lazzies? Answer yes or no.",
     r"^\s*yes\W*$", 1),
    ("I have 3 boxes; exactly one has a prize. I pick box 1, the host (who knows) opens box 3 which is empty, "
     "and offers a switch to box 2. What is the probability of winning if I switch? Answer as a fraction a/b.",
     r"^\s*2\s*/\s*3\s*$", 2),
    ("How many trailing zeros does 100! have? Answer with the number only.", r"^\s*24\s*$", 3),
    ("What is the smallest positive integer n such that n! is divisible by 1000? Answer with the number only.",
     r"^\s*15\s*$", 3),
    ("How many integers from 1 to 1000 inclusive are divisible by neither 2, 3 nor 5? Answer with the number only.",
     r"^\s*266\s*$", 4),
    ("What is printed?\ns = 'mississippi'\nprint(sum(1 for i in range(len(s)-1) if s[i] == s[i+1]))\n"
     "Answer with the output only.", r"^\s*3\s*$", 3),
    ("The sequence a(1)=1, a(n+1) = a(n) + (sum of digits of a(n)). What is a(10)? Answer with the number only.",
     r"^\s*62\s*$", 4),
    ("How many 4-digit positive integers have digits that sum to 9? Answer with the number only.", r"^\s*165\s*$", 5),
    ("What is the number of lattice paths from (0,0) to (6,6) using unit steps right or up that never go above "
     "the diagonal y = x? Answer with the number only.", r"^\s*132\s*$", 5),
    ("Find the last two digits of 7^2026. Answer with the two digits only.", r"^\s*49\s*$", 5),
    ("How many permutations of 1..6 have no fixed points? Answer with the number only.", r"^\s*265\s*$", 5),
    ("What is the minimum number of moves to solve the Tower of Hanoi with 9 disks? Answer with the number only.",
     r"^\s*511\s*$", 2),
    ("Using each of 1, 3, 4, 6 exactly once with + - * / and parentheses, make 24. Answer with the expression only.",
     "EVAL24:1346", 6),
    ("What is the sum of the digits of 2^50? Answer with the number only.", r"^\s*76\s*$", 6),
    # harder ones: these separate the hyperfrontier tier from plain frontier models
    ("How many spanning trees does the 4x4 grid graph (16 vertices, 24 edges) have? Answer with the number only.",
     r"^\s*100,?352\s*$", 6),
    ("In how many ways can a 4x8 rectangle be tiled with 1x2 dominoes? Answer with the number only.",
     r"^\s*2,?245\s*$", 5),
    ("How many squarefree integers are there from 1 to 10000 inclusive? Answer with the number only.",
     r"^\s*6,?083\s*$", 6),
    ("How many integer points (x, y) satisfy x^2 + y^2 <= 100? Answer with the number only.", r"^\s*317\s*$", 5),
    ("What is printed?\nfs = [lambda: i for i in range(3)]\ndef d(x, acc=[]):\n    acc.append(x)\n    return len(acc)\n"
     "print(sum(f() for f in fs) * 10 + d(1) + d(2) + d(3))\nAnswer with the output only.", r"^\s*66\s*$", 5),
    ("How many binary strings of length 20 contain no three consecutive 1s? Answer with the number only.",
     r"^\s*223,?317\s*$", 6),
    ("How many integer partitions does 50 have? Answer with the number only.", r"^\s*204,?226\s*$", 5),
]


def _eval24(text: str, digits: str) -> bool:
    """Any expression using exactly the given digits once each that evaluates to 24."""
    from fractions import Fraction
    for line in reversed((text or "").strip().splitlines()):
        expr = re.sub(r"[^0-9+\-*/(). ]", "", line.split("=")[0]).strip()
        if not expr or sorted(re.findall(r"\d", expr)) != sorted(digits):
            continue
        try:
            val = eval(re.sub(r"(\d+)", r"Fraction(\1)", expr), {"__builtins__": {}}, {"Fraction": Fraction})
        except Exception:  # noqa: BLE001
            continue
        return val == 24
    return False


def grade(text: str, pattern: str) -> bool:
    if pattern.startswith("EVAL24:"):
        return _eval24(text, pattern.split(":", 1)[1])
    t = (text or "").strip()
    t = re.sub(r"^```\w*\n?|```$", "", t).strip()
    last = t.splitlines()[-1] if t else ""
    return bool(re.search(pattern, last, re.I) or re.search(pattern, t, re.I))


async def score_model(router: Router, m, concurrency: int = 3) -> tuple[float, int]:
    sem = asyncio.Semaphore(concurrency)
    total = sum(w for _, _, w in PROBES)

    async def one(prompt, pattern, w):
        async with sem:
            try:
                d = await router.call(m, {"messages": [{"role": "user", "content": prompt}], "temperature": 0,
                                          "max_tokens": 2048})
                return w if grade(d["choices"][0]["message"].get("content") or "", pattern) else 0.0
            except Exception:  # noqa: BLE001
                return None
    res = await asyncio.gather(*(one(*p) for p in PROBES))
    answered = [r for r in res if r is not None]
    if len(answered) < len(PROBES) * 0.7:   # too many failures to trust the number
        return -1, len(answered)
    return round(100 * sum(answered) / total, 1), len(answered)


async def calibrate(router: Router, out_path: str | Path, only: list[str] | None = None) -> dict:
    existing = {}
    if Path(out_path).exists():
        existing = yaml.safe_load(Path(out_path).read_text()) or {}
    for m in router.catalog.models.values():
        if only and m.id not in only:
            continue
        if not router.catalog.providers[m.provider].api_key:
            continue
        s, n = await score_model(router, m)
        if s >= 0:
            existing[m.id] = {"score": s, "probes": n}
            print(f"{m.id}: {s}")
        else:
            print(f"{m.id}: skipped ({n}/{len(PROBES)} probes answered)")
        Path(out_path).write_text(yaml.safe_dump(existing, sort_keys=True))
    return existing
