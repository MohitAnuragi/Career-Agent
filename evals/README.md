# Evaluation

Two questions, measured separately:

1. **Retrieval:** does hybrid search actually beat keyword-only and vector-only, and does the LLM reranker help?
2. **Agent:** given a user message, does the agent pick the right tool with the right arguments?

> **Running in Docker:** the scripts write to `evals/data` and `evals/results`, so mount the project folder:
> `docker compose run --rm -v "$PWD:/app" api python -m evals.snapshot export` (PowerShell: `-v "${PWD}:/app"`).

## 1. Retrieval

The live index changes every night, so labels would rot. The eval runs on a **frozen snapshot** in a
separate database (`career_agent_eval`).

```bash
# copy the current live index (with embeddings) into the eval DB and a portable file
python -m evals.snapshot export          # -> evals/data/snapshot.jsonl.gz + loads career_agent_eval
python -m evals.snapshot load            # rebuild career_agent_eval from the file (e.g. on another machine)

# pooled relevance labels: union of top-20 from every method, graded 0-3 by an LLM judge
python -m evals.label                    # -> evals/data/qrels.json   (needs GEMINI_API_KEY)

# metrics per method
python -m evals.run_retrieval            # -> evals/results/retrieval.json + Markdown table
```

**Metrics:** nDCG@10 (graded), Recall@20 and MRR (relevant = grade ≥ 2), plus p50/p95 latency.

**Honest caveats, worth stating in interviews:**
- Labels come from an LLM judge using pooling. Unjudged documents count as non-relevant (standard TREC practice).
  Spot-check at least 50 labels by hand and record agreement in `qrels.json` → `"human_checked"`.
- If the judge and the reranker are the same model, the rerank score is biased upwards. Set `JUDGE_MODEL`
  to a different (ideally stronger) model than `GEMINI_MODEL`.

## 2. Agent tool selection

`agent_cases.json` holds messages with the expected first tool and a subset of expected arguments.

```bash
python -m evals.run_agent                # -> evals/results/agent.json  (needs GEMINI_API_KEY)
```

Reports tool-selection accuracy, argument accuracy and mean latency per step.
