#!/usr/bin/env python3
"""Run the 40 CampaignIQ eval questions against one OpenRouter :free model.

Usage:
  python3 campaigniq_free_eval.py <model-id> [--outdir DIR] [--max-tokens N]

- One request per question, fresh context each (setup prompt as system msg),
  mirroring the eval doc's fresh-chat rule. No threads.
- 5s spacing between requests (stays under 20 req/min).
- Validates Q1: aborts if the answer is empty or reasoning-only.
- Saves incrementally to JSONL; rerunning resumes unfinished questions.
- On daily-cap 429s it stops gracefully; rerun later/tomorrow to resume.
"""

import importlib.util
import json
import os
import re
import sys
import time

JUNCTION = "/home/hatch/workspace/llm-junction/junction.py"
PROMPTS = "/home/hatch/workspace/goals/project-ai-learning/files/campaigniq-eval-80-prompts.md"

spec = importlib.util.spec_from_file_location("junction", JUNCTION)
j = importlib.util.module_from_spec(spec)
spec.loader.exec_module(j)


def load_eval():
    src = open(PROMPTS).read()
    setup_m = re.search(r"## Setup Prompt.*?\n\n(.*?)\n\n---", src, re.S)
    setup_lines = [ln[2:] for ln in setup_m.group(1).splitlines() if ln.startswith("> ")]
    setup = "\n".join(setup_lines).strip()
    questions = []
    for m in re.finditer(r"\*\*P-(\d+) \(TC-\d+\)\*\* — (.+)", src):
        questions.append((f"P-{m.group(1)}", m.group(2).strip()))
    assert len(questions) == 40, f"expected 40 questions, got {len(questions)}"
    return setup, questions


def answer_is_usable(text):
    t = (text or "").strip()
    if len(t) < 20:
        return False
    if t.startswith("[model returned no final answer"):
        return False
    return True


def ask_once(model, messages, max_tokens):
    """Single attempt, no internal retry. Returns (text, error)."""
    try:
        doc = j._request(j.BASE + "/chat/completions",
                         {"model": model, "messages": messages,
                          "max_tokens": max_tokens},
                         authed=True, timeout=120)
        choice = doc["choices"][0]["message"]
        text = (choice.get("content") or "").strip()
        if not text:
            reasoning = choice.get("reasoning") or ""
            if isinstance(reasoning, str) and reasoning.strip():
                text = ("[model returned no final answer; showing its reasoning trace]\n"
                        + reasoning.strip()[:3000])
        return text, doc.get("model", model), None
    except RuntimeError as exc:
        return "", model, str(exc)


def main():
    model = sys.argv[1]
    outdir = os.path.join(
        "/home/hatch/workspace/goals/project-ai-learning/files/campaigniq-free-eval",
        *[a.split("=", 1)[1] for a in sys.argv if a.startswith("--outdir=")] or ["runs"])
    max_tokens = 1024
    for a in sys.argv:
        if a.startswith("--max-tokens="):
            max_tokens = int(a.split("=", 1)[1])
    os.makedirs(outdir, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", model).strip("-")
    jsonl_path = os.path.join(outdir, f"{slug}.jsonl")

    setup, questions = load_eval()
    done = {}
    if os.path.exists(jsonl_path):
        with open(jsonl_path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    done[e["qid"]] = e
                except ValueError:
                    pass
    print(f"model={model} done={len(done)}/40 jsonl={jsonl_path}", flush=True)

    daily_cap_hit = False
    for qid, qtext in questions:
        if qid in done and done[qid].get("answer"):
            continue
        if daily_cap_hit:
            break
        messages = [{"role": "system", "content": setup},
                    {"role": "user", "content": qtext}]
        text, used_model, err = ask_once(model, messages, max_tokens)
        if err and "429" in err:
            # Rate limited (account daily cap or upstream saturation).
            # One patient retry after 90s, then stop gracefully and resume later.
            print(f"{qid}: 429 ({err[:80]}), waiting 90s once", flush=True)
            time.sleep(90)
            text, used_model, err = ask_once(model, messages, max_tokens)
            if err and "429" in err:
                print(f"{qid}: still rate limited, stopping gracefully. "
                      f"Rerun the same command to resume.", flush=True)
                daily_cap_hit = True
                break
        if err:
            print(f"{qid}: ERROR {err[:120]}", flush=True)
            entry = {"qid": qid, "question": qtext, "answer": "",
                     "error": err[:300], "model": model}
        else:
            if qid == "P-01" and not answer_is_usable(text):
                print(f"Q1 answer unusable, aborting run:\n{text[:300]}",
                      flush=True)
                entry = {"qid": qid, "question": qtext, "answer": text,
                         "error": "ABORTED: Q1 unusable", "model": used_model}
                with open(jsonl_path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
                sys.exit(3)
            print(f"{qid}: ok ({len(text)} chars via {used_model})", flush=True)
            entry = {"qid": qid, "question": qtext, "answer": text,
                     "model": used_model}
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        j._log_usage(used_model if not err else model, not err,
                     (err or "")[:120])
        time.sleep(5)

    # Compile readable markdown from everything saved so far.
    entries = {}
    if os.path.exists(jsonl_path):
        with open(jsonl_path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    entries[e["qid"]] = e
                except ValueError:
                    pass
    md_path = os.path.join(outdir, f"{slug}.md")
    with open(md_path, "w") as f:
        f.write(f"# CampaignIQ free-model eval — {model}\n\n")
        f.write(f"Answered: {sum(1 for e in entries.values() if e.get('answer'))}/40\n\n")
        for qid, qtext in questions:
            e = entries.get(qid, {})
            f.write(f"## {qid} — {qtext}\n\n")
            f.write((e.get("answer") or f"*{e.get('error', 'not run')}*") + "\n\n")
    print(f"DONE answered={sum(1 for e in entries.values() if e.get('answer'))}/40 "
          f"md={md_path}", flush=True)


if __name__ == "__main__":
    main()
