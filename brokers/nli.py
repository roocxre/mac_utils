"""The small NLI model that sorts broker replies the rules couldn't: cross-encoder/nli-deberta-v3-xsmall
(Apache-2.0), int8 ONNX for arm64, in models/nli-deberta-v3-xsmall (87 MB). onnxruntime on the CPU,
a few milliseconds per hypothesis; no torch, nothing leaves the machine.

classify(text) tests each fixed hypothesis against the reply and returns the one most entailed,
with its probability. Below the threshold (config.json nli.threshold) replies.py makes it a
manual task.
"""
import os

import numpy as np

from brokers import ROOT

MODEL_DIR = os.path.join(os.path.dirname(ROOT), "models", "nli-deberta-v3-xsmall")
ENTAILMENT = 1              # config.json id2label: contradiction, entailment, neutral
MAX_TOKENS = 512
# Worded as the broker writes to you ("your information"): first-person hypotheses score far lower.
HYPOTHESES = {
    "completed": "Your information has been deleted or removed.",
    "needs_info": "You are asked to send more information.",
    "needs_id": "You are asked to send a copy of your ID.",
    "refused_residency": "Your request is refused because you are not a resident of that state.",
    "refused_other": "Your request is refused.",
    "no_record": "No records about you were found.",
}

_model = {}


def _load():
    if "session" not in _model:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(os.path.join(MODEL_DIR, "tokenizer.json"))
        tokenizer.enable_truncation(MAX_TOKENS)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        _model["tokenizer"] = tokenizer
        _model["session"] = ort.InferenceSession(os.path.join(MODEL_DIR, "model.onnx"), options,
                                                 providers=["CPUExecutionProvider"])
    return _model["tokenizer"], _model["session"]


def available():
    return os.path.exists(os.path.join(MODEL_DIR, "model.onnx"))


def entailment(premise, hypotheses):
    """{name: P(entailment)} for each hypothesis."""
    # One pair at a time: padded batches shift this model's scores.
    tokenizer, session = _load()
    out = {}
    for name, hypothesis in hypotheses.items():
        encoded = tokenizer.encode(premise, hypothesis)
        logits = session.run(None, {"input_ids": np.array([encoded.ids], dtype=np.int64),
                                    "attention_mask": np.array([encoded.attention_mask], dtype=np.int64)})[0][0]
        probs = np.exp(logits - logits.max())
        out[name] = float(probs[ENTAILMENT] / probs.sum())
    return out


def classify(text, max_chars=1500):
    """(label, probability, {label: probability})."""
    scores = entailment(" ".join(text.split())[:max_chars], HYPOTHESES)
    label = max(scores, key=scores.get)
    return label, scores[label], scores
