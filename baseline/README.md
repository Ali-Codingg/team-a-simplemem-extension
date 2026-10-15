# SimpleMem Baseline

This folder contains the SimpleMem baseline implementation and evaluation using the LoCoMo dataset.

## Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Configure your OpenAI API key and model settings in the project's `config.py`. Do not commit API keys to GitHub.

## Run

Run the LoCoMo evaluation:

```bash
python test_locomo10.py --num-samples 1 --result-file results_test1.json
```

`--num-samples` specifies the number of conversations, not questions.

## Project Structure

- `simplemem/` — Core SimpleMem implementation
- `test_ref/` — Evaluation dataset and reference files
- `main.py` — SimpleMem system entry point
- `test_locomo10.py` — LoCoMo evaluation
- `test_embedding.py` — Embedding test
- `test_simplemem_llm.py` — LLM integration test
- `test_ollama.py` — Ollama test
- `results_test1.json` — Saved evaluation results
