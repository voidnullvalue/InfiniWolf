#!/usr/bin/env python3
"""Explain why each floor's candidate won campaign selection.

The trace is opt-in: ordinary generation does not build these records. Each row
shows the six lexicographic terms in their real order. Lower defect counts are
represented as negative values in ``ranking_key`` because selection uses max().
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infiniwolf.config import CampaignConfig, GenerationQuality
from infiniwolf.generator import generate_campaign


TERM_LABELS = {
    "severe_defects": "severe",
    "live_diagnostics": "diagnostics",
    "corpus_similarity": "corpus",
    "gameplay_mean": "gameplay",
    "composition_mean": "composition",
    "campaign_contrast": "contrast",
}


def _candidate(trace: dict, number: int) -> dict:
    return next(row for row in trace["candidates"]
                if row["candidate"] == number)


def print_trace(traces: list[dict]) -> None:
    print("Ranking key: (-severe, -diagnostics, corpus, gameplay, composition, contrast); max wins")
    for trace in traces:
        winner = _candidate(trace, trace["winner"])
        runner = (_candidate(trace, trace["runner_up"])
                  if trace["runner_up"] is not None else None)
        print()
        summary = (f"Floor {trace['floor']} — {trace['mode']}, "
                   f"{trace['active_pool']} pool; candidate {trace['winner']} won")
        if runner is not None:
            summary += f" over candidate {trace['runner_up']}"
        print(summary)
        decisive = trace["decisive_term"]
        if decisive and runner is not None:
            left = winner["terms"][decisive]["value"]
            right = runner["terms"][decisive]["value"]
            relation = "<" if decisive in ("severe_defects", "live_diagnostics") else ">"
            print(f"Decision: {decisive} ({left:.4f} {relation} {right:.4f})")
        elif runner is None:
            print("Decision: no runner-up in the active pool")
        else:
            print("Decision: exact ranking-key tie; stable pool order won")

        print(" cand  state       severe  diagnostics  corpus  gameplay  composition  contrast")
        print(" ----  ----------  ------  -----------  ------  --------  -----------  --------")
        for row in trace["candidates"]:
            if row["winner"]:
                state = "WINNER"
            elif row["eligible"]:
                state = "eligible"
            else:
                state = "excluded"
            terms = row["terms"]
            print(
                f" {row['candidate']:>4}  {state:<10}"
                f"  {terms['severe_defects']['value']:>6}"
                f"  {terms['live_diagnostics']['value']:>11}"
                f"  {terms['corpus_similarity']['value']:>6.3f}"
                f"  {terms['gameplay_mean']['value']:>8.3f}"
                f"  {terms['composition_mean']['value']:>11.3f}"
                f"  {terms['campaign_contrast']['value']:>8.3f}"
            )
        for row in trace["candidates"]:
            details = row["live_diagnostics"]
            if details:
                print(f"   candidate {row['candidate']} diagnostics: {', '.join(details)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", required=True,
                        help="integer, 0x-prefixed integer, or stable text seed")
    parser.add_argument(
        "--generation-quality",
        choices=[quality.value for quality in GenerationQuality],
        default=GenerationQuality.THOROUGH.value,
        help="candidate pool policy (default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true",
                        help="emit the complete machine-readable trace")
    args = parser.parse_args(argv)

    quality = GenerationQuality(args.generation_quality)
    config = CampaignConfig.with_seed(args.seed, generation_quality=quality)
    traces: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="infiniwolf-explain-") as directory:
        generate_campaign(
            config, Path(directory) / "campaign.pk3",
            selection_trace=traces.append,
        )

    payload = {
        "seed": config.seed,
        "generation_quality": quality.value,
        "ranking_order": list(TERM_LABELS),
        "floors": traces,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Seed: {config.seed}  Quality: {quality.value}")
        print_trace(traces)
    return 0


if __name__ == "__main__":
    sys.exit(main())
