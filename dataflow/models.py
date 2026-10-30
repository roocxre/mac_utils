"""The small local transformers on the CPU (onnxruntime + tokenizers; no torch at run time):

    privbert-opp115        PrivBERT fine-tuned on OPP-115: a policy passage's categories and
                           personal-information types. Optional and not distributed: without
                           it policy.py treats every passage as relevant
    nli-deberta-v3-base    cross-encoder/nli-deberta-v3-base (Apache-2.0): does a passage say X
    bert-base-NER          Xenova/bert-base-NER (MIT): organisation names in sharing passages
    all-MiniLM-L6-v2       sentence embeddings for the fallback field classifier (fields.py), run by
                           atlas/intent.py's numpy encoder

Each loads once per process.
"""
import json
import os

import numpy as np

from dataflow import MODELS_DIR

_loaded = {}


def path(name, *parts):
    return os.path.join(MODELS_DIR, name, *parts)


def available(name):
    return os.path.exists(path(name, "model.onnx"))


def _session(name, max_tokens=512):
    if name not in _loaded:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(path(name, "tokenizer.json"))
        tokenizer.enable_truncation(max_tokens)
        tokenizer.no_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        session = ort.InferenceSession(path(name, "model.onnx"), options, providers=["CPUExecutionProvider"])
        _loaded[name] = (tokenizer, session, [i.name for i in session.get_inputs()])
    return _loaded[name]


def unload():
    """Free the models' memory (the host keeps running while Edge is open)."""
    _loaded.clear()
    import gc
    gc.collect()


def _feed(inputs, encoded):
    feed = {"input_ids": np.array([encoded.ids], dtype=np.int64),
            "attention_mask": np.array([encoded.attention_mask], dtype=np.int64)}
    if "token_type_ids" in inputs:
        feed["token_type_ids"] = np.array([encoded.type_ids], dtype=np.int64)
    return feed


# ------------------------------------------------------------ PrivBERT on OPP-115

def privbert_labels():
    with open(path("privbert-opp115", "labels.json"), encoding="utf-8") as f:
        return json.load(f)


def privbert(texts, name="privbert-opp115"):
    """[{label: probability}] for each passage."""
    info = privbert_labels()
    tokenizer, session, inputs = _session(name, info.get("max_tokens", 384))
    out = []
    for text in texts:
        logits = session.run(None, _feed(inputs, tokenizer.encode(text)))[0][0]
        probs = 1 / (1 + np.exp(-logits))
        out.append({label: float(p) for label, p in zip(info["labels"], probs)})
    return out


# ------------------------------------------------------------ NLI

def nli(premise, hypotheses, name="nli-deberta-v3-base"):
    """{key: (P(entailment), P(contradiction))} for each hypothesis, one pair at a time (padding shifts scores)."""
    tokenizer, session, inputs = _session(name)
    with open(path(name, "config.json"), encoding="utf-8") as f:
        id2label = {int(k): v.lower() for k, v in json.load(f)["id2label"].items()}
    entail = next(i for i, v in id2label.items() if v.startswith("entail"))
    contra = next(i for i, v in id2label.items() if v.startswith("contra"))
    out = {}
    for key, hypothesis in hypotheses.items():
        logits = session.run(None, _feed(inputs, tokenizer.encode(premise, hypothesis)))[0][0]
        probs = np.exp(logits - logits.max())
        probs /= probs.sum()
        out[key] = (float(probs[entail]), float(probs[contra]))
    return out


# ------------------------------------------------------------ organisation names

def organisations(text, name="bert-base-NER"):
    """Organisation names in a passage (B-ORG/I-ORG spans, joined back from word pieces)."""
    tokenizer, session, inputs = _session(name)
    with open(path(name, "config.json"), encoding="utf-8") as f:
        id2label = {int(k): v for k, v in json.load(f)["id2label"].items()}
    encoded = tokenizer.encode(text)
    logits = session.run(None, _feed(inputs, encoded))[0][0]
    tags = [id2label[int(i)] for i in logits.argmax(-1)]
    spans, current = [], None
    for (start, end), tag, word_id in zip(encoded.offsets, tags, encoded.word_ids):
        if word_id is None or start == end:
            continue
        if tag in ("B-ORG", "I-ORG"):
            if current and (tag == "I-ORG" or start == current[1]):
                current[1] = end
            else:
                if current:
                    spans.append(current)
                current = [start, end]
        else:
            if current and start == current[1]:     # the rest of a word that started as an ORG
                current[1] = end
                continue
            if current:
                spans.append(current)
            current = None
    if current:
        spans.append(current)
    names = []
    for start, end in spans:
        found = text[start:end].strip(" .,;:()")
        if len(found) >= 2 and found not in names:
            names.append(found)
    return names


# ------------------------------------------------------------ sentence embeddings

def embed(texts, name="all-MiniLM-L6-v2"):
    """Mean-pooled, normalised sentence embeddings (atlas's numpy MiniLM, from its safetensors)."""
    if name not in _loaded:
        from atlas.intent import _Encoder
        _loaded[name] = _Encoder(path(name))
    encoder = _loaded[name]
    return np.array([encoder.encode(text) for text in texts])
