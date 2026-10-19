"""Run selected P2.6d live dialogue scenarios only on the isolated database."""

import argparse

from scripts import evaluate_p26c_deepseek as suite
from scripts.p26d_eval_guard import assert_eval_database
from scripts.p26d_metered_model import BudgetedModel, cost_summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True,
                        choices=("core", "search", "remaining"))
    args = parser.parse_args()
    assert_eval_database()
    suite.STATE = suite.ROOT / "data/generated/p26d-live-state.json"
    suite.REPORT = suite.ROOT / f"data/generated/p26d-{args.batch}-report.json"
    suite.MeteredModel = BudgetedModel
    groups = {
        "core": ("flight-book", "flight-change", "flight-cancel",
                 "hotel-book", "hotel-change", "hotel-cancel",
                 "rental-book", "rental-change", "rental-cancel"),
        "search": ("hello", "followup", "policy", "real-policy",
                   "flight-search", "hotel-search", "rental-search",
                   "select-search", "select-second", "no-flight"),
        "remaining": ("dual", "missing-date"),
    }
    selected = groups[args.batch]
    suite.evaluate(set(selected), max_calls=len(selected))
    print("P2.6d budget:", cost_summary())


if __name__ == "__main__":
    main()
