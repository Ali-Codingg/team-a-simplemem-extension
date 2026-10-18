
import argparse
import os
from getpass import getpass
from time import perf_counter

from typesafe_sdk import Score, TypeSafeClient

from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

MODEL = "jev-1.13.0"

RUBRICS = {
    "semantic": [
        "Meaning-based similarity is unlikely to add value.",
        "Semantic similarity could help find relevant memories.",
        "Paraphrases or conceptual meaning are important for retrieval.",
    ],
    "lexical": [
        "Exact keyword matching offers little value.",
        "Distinctive words could help locate relevant memories.",
        "Specific names, phrases, or keywords are central to retrieval.",
    ],
    "symbolic": [
        "No identifiable entity, date, or structured filter is present.",
        "An entity or time reference may help narrow retrieval.",
        "Explicit names, dates, or identifiers strongly support filtering.",
    ],
    "multi_hop": [
        "The question likely needs one directly relevant memory.",
        "The question likely requires connecting two facts.",
        "The question likely requires linking several memories or events.",
    ],
}

INSTRUCTIONS = {
    "semantic": "How useful would semantic vector retrieval be?",
    "lexical": "How useful would lexical BM25 retrieval be?",
    "symbolic": "How useful would structured metadata retrieval be?",
    "multi_hop": "How much multi-hop reasoning does this question require?",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--question",
        default="Where did Alice work before moving to Boston, "
                "and when did she leave that job?",
    )
    args = parser.parse_args()

    if not os.environ.get("TYPESAFE_API_KEY"):
        os.environ["TYPESAFE_API_KEY"] = getpass(
            "TypeSafe API key: "
        )

    questions = {
        name: Score(
            instructions=INSTRUCTIONS[name],
            criteria=levels,
        )
        for name, levels in RUBRICS.items()
    }

    start = perf_counter()

    with TypeSafeClient() as client:
        response = client.system_one(
            state={"question": args.question},
            questions=questions,
            model=MODEL,
        )

    elapsed_ms = (perf_counter() - start) * 1000

    print("\nQUESTION:", args.question)
    print("MODEL:", response.model)

    print("\nJEV RETRIEVAL-PLANNING SCORES")

    for name, levels in RUBRICS.items():
        answer = response.scores[name]
        normalized = answer.score / (len(levels) - 1)

        print(f"\n{name.upper()}")
        print(f"  Raw score: {answer.score:.4f}")
        print(f"  Normalized: {normalized:.4f}")
        print(f"  Confidence: {answer.confidence:.4f}")
        print(f"  Probabilities: {answer.probabilities}")

    print("\nAPI USAGE")
    print("  Logical Jev requests: 1")
    print("  Input tokens:", response.usage.input_tokens)
    print("  Output tokens:", response.usage.output_tokens)
    print(f"  SDK request latency: {elapsed_ms:.2f} ms")


if __name__ == "__main__":
    main()
