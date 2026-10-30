"""Restates a loosely worded request as one of the assistant's commands, without a language model.

A small sentence-embedding transformer (all-MiniLM-L6-v2, 22M parameters, kept in this
project's models/ folder) turns the request into a vector. The nearest labelled example phrases
vote for an intent; the target (app, file, agent) is then the request's own content words.
It runs on the CPU with numpy in a few milliseconds and keeps no process-wide state beyond
the ~90 MB of weights.

The result is only ever fed back through the normal command parser, and every word of the
target comes from the request, so it can rephrase what was asked but not invent a name.
"""
import glob
import hashlib
import json
import math
import os
import re
import threading

import numpy as np

from lib.common import state_path

# The project's own copy; the Hugging Face cache is only a fallback if that goes missing.
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "models", "all-MiniLM-L6-v2")
MODEL_GLOB = os.path.expanduser(
    "~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/snapshots/*")
MAX_TOKENS = 48
MIN_SCORE = 0.50      # nearest example must be at least this similar
MIN_MARGIN = 0.03     # and beat the best example of any other intent by this much
NO_INTENT = "none"
# Intents that change something need a clearer win than ones that only look.
ACTION_INTENTS = {"restart", "stop", "start", "run", "install", "batteryhelper", "powersave"}
ACTION_MARGIN = 0.15
# The status questions (how's the battery, what time is it) sit close to questions no command answers (what
# version of macos, is it bad to charge overnight): those scored 0.55-0.58 and the real ones 0.6 and up. Missing
# one only means it is reported as not a command.
LOOK_INTENTS = {"disklevel", "memorylevel", "timeinfo", "uptimelevel", "networkinfo"}
LOOK_SCORE = 0.60
LOOK_MARGIN = 0.10

# intent -> example phrasings. Several per intent; "none" covers small talk and anything else.
EXAMPLES = {
    "open": [
        "pull up my resume", "bring up the calendar", "launch safari", "start up spotify",
        "i want to use vs code", "can you open my notes", "get me into the downloads folder",
        "show me the desktop folder", "fire up the terminal", "open the thesis pdf",
        "take me to my documents", "i need photoshop open", "switch to discord",
        "could you bring up spotify for me", "can you open up the calculator", "throw up my budget file", "get chrome going", "i'd like to see my photos app",
    ],
    "find": [
        "i'm looking for the scripts folder", "where did i put my tax return",
        "do i have a firefox dmg anywhere", "find my resume", "locate that invoice pdf",
        "is there a copy of the budget spreadsheet on here", "search my mac for holiday photos",
        "i'm hunting for my thesis", "do i have any vlc installers lying around",
        "have i got a backup of that script somewhere", "track down the old invoice", "where on earth is my scan of the passport",
        "where is that screenshot i took yesterday", "hunt down the config file",
    ],
    "ls": [
        "what's sitting in downloads", "list my documents folder", "what files are on the desktop",
        "show me what's in the archive folder", "what's inside the projects directory",
        "contents of my home folder", "list everything in downloads",
        "what's in my downloads folder", "what do i have in documents", "what's in the pictures folder",
    ],
    "read": [
        "show me what notes.txt says", "read me the readme", "what does the log file say",
        "print out my todo file", "let me see what's in config.json", "peek at the notes file",
        "what does todo.txt say", "what's written in the readme", "tell me what the notes file says",
    ],
    "install": [
        "get firefox on this thing", "install vlc", "download and install slack",
        "i need zoom installed", "put obsidian on my mac", "can you set up docker for me",
        "grab the spotify app", "brew install wget", "get slack on here", "i want vlc on this mac",
        "can you get me discord", "install the zoom app",
    ],
    "status": [
        "is everything still running", "are the background agents ok", "what's running right now",
        "how are the daemons doing", "give me a status report", "is everything alive",
        "check on the agents", "is the clipboard agent up", "is the notifier alive",
        "how's the audio guard doing",
    ],
    "restart": [
        "the clipboard thing is acting up, kick it", "restart the screen text agent",
        "reload the audio guard", "give the clipboard agent a restart", "turn the clipboard thing off and on again",
        "the popup agent is stuck, restart it", "reboot the notifier", "bounce the clipboard agent",
    ],
    "stop": [
        "shut off the clipboard agent", "stop the notifier", "kill the screen text agent",
        "turn off the audio guard", "disable the command notifier", "make the clipboard thing stop",
        "make the screen text thing stop", "i don't want the notifier running anymore",
    ],
    "start": [
        "turn on the clipboard agent", "start the audio guard", "enable the notifier again",
        "bring the screen text agent back", "switch the popup agent on",
    ],
    "fixpock": [
        "the touch bar is frozen", "pock is stuck", "fix my touch bar", "the touchbar is blank again",
        "pock stopped responding", "unstick the touch bar",
    ],
    "audio": [
        "where is the sound going", "which speakers am i using", "what's my audio output",
        "is my mac muted", "what output device is it using", "why is there no sound",
    ],
    "inbox": [
        "did anyone message me", "any popups waiting", "what messages are pending",
        "show me my message inbox", "did i miss a message from someone",
    ],
    "inboxreport": [
        "which newsletters should i unsubscribe from", "help me unsubscribe from junk email", "open the spam report",
        "who keeps spamming me", "show me the unsubscribe dashboard", "i get way too many marketing emails",
        "what emails do i never read", "clean up my email subscriptions", "what junk mail am i getting",
        "review the senders to block", "show me my email report", "too many newsletters in my gmail",
    ],
    "brokerreport": [
        "which data brokers have my information", "get me off people search sites", "remove my info from data brokers",
        "open the data broker report", "who is selling my personal data", "am i on spokeo or whitepages",
        "delete my address from the internet", "show the opt out requests", "how are my data removal requests going",
        "what do people finder sites show about me", "data broker opt outs",
    ],
    "memory": [
        "free up some memory", "my mac is running out of ram", "clear some memory",
        "everything is slow, close the junk", "what's eating my memory", "close the apps i don't need",
        "make some room in ram",
    ],
    "batteryhelper": [
        "what's draining my battery", "which apps are eating my battery", "my battery is dying fast, what's using it",
        "show me the battery dashboard", "what's using all the power", "open the battery helper",
        "why is my battery going down so quickly", "show me power usage by app", "find the battery hogs",
    ],
    "batterylevel": [
        "how much battery do i have", "what's my battery at", "battery percentage", "is my laptop charging",
        "how much charge is left", "what percent is the battery",
        "how's the battery doing", "how is my battery", "battery status", "how much juice have i got",
        "how long will the battery last", "am i plugged in",
    ],
    # How the mac is doing, answered by a look (info/shortcuts), never the model: quick enough to say at once.
    "disklevel": [
        "how much disk space do i have", "how much storage is left", "how full is my disk", "am i running out of space",
        "how's the disk doing", "free space on the drive",
    ],
    "memorylevel": [
        "how's my memory looking", "how much ram is free", "how's memory doing", "what's the memory pressure",
        "how much memory am i using",
    ],
    "timeinfo": [
        "what time is it", "what's the time", "what's the date today", "what day is it", "what's today's date",
        "tell me the time",
    ],
    "uptimelevel": [
        "how long has the mac been on", "when did i last restart", "what's the uptime", "how long since the last reboot",
    ],
    "networkinfo": [
        "what's my ip address", "how's the wifi", "am i connected to the internet", "what's my router",
        "how's the network",
    ],
    "powersave": [
        "save battery", "make my battery last longer", "turn on low power mode", "i need to stretch my battery",
        "go into battery saving mode", "conserve power", "switch on power saving", "i'm low on battery, help it last",
        "put the mac in power saver",
        # and back: which way is settled from the words (_OFF), as the two read almost the same to the encoder
        "turn off power saving", "back to normal power", "disable low power mode", "i'm plugged in, undo the battery saving",
        "switch power saving off", "stop saving battery",
    ],
    "clipboard": [
        "clean up what i just copied", "fix the clipboard formatting", "tidy my clipboard",
        "dedent the code i copied",
    ],
    "capture": [
        "copy the text off my screen", "grab the text from part of the screen",
        "ocr a region of the screen", "i want the words from that image on screen",
    ],
    "logs": [
        "show me the clipboard log", "what has the audio guard been logging",
        "let me see the logs for the notifier", "what happened in the pock keeper's log",
    ],
    "run": [
        "clean up my screenshots now", "go ahead and unpack the downloads", "run the dev cleanup",
        "tidy the package caches right now", "trim the logs now", "archive my desktop screenshots",
    ],
    "preview": [
        "what would the dev cleaner delete", "show me what the cache cleanup would do before i run it",
        "what would the archiver get rid of", "dry run the log trimmer", "what will the downloads unpacker do",
        "preview the screenshot cleanup", "what is the dev cleanup going to remove if i run it",
    ],
    "repos": [
        "are there any git repos in the dev folder", "which of my folders are git repositories",
        "what git repos do i have", "find the git repositories under code", "is there a repo anywhere in documents",
        "list the repos in my projects folder", "any git repositories inside downloads",
        "which projects in src are under git", "any repos in work", "which repos are in my scripts folder",
        "repos under documents", "about how many git repositories are under the projects folder", "do i have any repos in the git folder", "show me the repositories in code",
    ],
    NO_INTENT: [
        "how are you today", "tell me a joke", "what's the weather like", "thanks", "hello",
        "who made you", "why are you so grumpy", "what is the capital of france",
        "i'm bored", "good morning", "do you like music", "i'm tired today",
        "explain how a transformer works", "write me a poem", "what should i have for dinner",
        "are you mad at me", "can you help me", "i don't know what to do",
        "my mac is a nice machine", "tell me something interesting", "you're annoying",
        "what's the meaning of life", "say something", "how's it going", "lol",
        "what is a battery made of", "how do lithium batteries work", "is it bad to keep my laptop plugged in",
        # about one repository, not which folders are repositories: no command does that ($ git … does)
        "what's the git status of the api folder", "what's the git status of ~/dev/site", "git status of ~/projects/app", "git status in ~/scripts",
        "commit my changes in the site repo", "is the repo clean",
        "what branch is the project on", "what was the last commit in the api repo", "push the repo", "git diff of the website",
    ],
}

# Words that carry the command, not the thing it is about. Removed to leave the target.
_STOP = set("""
a an the my me i we you it its this that these those of to for from on in at by with about
and or but so if then than too very just please pls can could would will shall should may
might do does did done get got go going want need like help let us lets make give take put
bring pull up down out off back again now right still also any some all every anywhere
somewhere around here there is are was were be been am what whats what's which who whom how
when where why open launch start run fire switch show display see view read print peek look
looking search find locate hunt hunting list contents content inside file files folder
folders directory directories throw anything something anyone earth homebrew homebrews have say says said lying sitting hunt hunting does app apps application program programs thing things stuff
install installed download set setup grab brew kick restart reload stop kill shut turn
enable disable status running agent agents daemon daemons background log logs
""".split())
_OFF = re.compile(r"\b(?:off|disable|undo|stop|normal|back|revert|plugged|charging|full performance)\b", re.IGNORECASE)
_REPO_WORDS = set("git repo repos repository repositories github projects project under inside subfolders subfolder".split())
_WORD = re.compile(r"[a-z0-9][a-z0-9._~/\-]*[a-z0-9]|[a-z0-9]", re.IGNORECASE)
_POSSESSIVE = re.compile(r"['’]s\b", re.IGNORECASE)


# ---------------------------------------------------------------- the encoder (numpy)

class _Encoder:
    """MiniLM sentence encoder: BERT, 6 layers, 384 wide, mean pooled and normalised."""

    HEADS = 12
    LAYERS = 6

    def __init__(self, directory):
        from safetensors.numpy import load_file
        from tokenizers import Tokenizer

        weights = load_file(os.path.join(directory, "model.safetensors"))
        self.w = {k: v.astype(np.float32) for k, v in weights.items()}
        self.tok = Tokenizer.from_file(os.path.join(directory, "tokenizer.json"))
        self.tok.no_padding()   # the shipped config pads to 128; pads would dilute the mean
        self.tok.enable_truncation(max_length=MAX_TOKENS)

    @staticmethod
    def _layer_norm(x, scale, bias, eps=1e-12):
        mean = x.mean(-1, keepdims=True)
        var = ((x - mean) ** 2).mean(-1, keepdims=True)
        return (x - mean) / np.sqrt(var + eps) * scale + bias

    @staticmethod
    def _gelu(x):
        return 0.5 * x * (1.0 + np.tanh(0.7978845608 * (x + 0.044715 * x ** 3)))

    def _linear(self, x, prefix):
        return x @ self.w[prefix + ".weight"].T + self.w[prefix + ".bias"]

    def encode(self, text):
        w = self.w
        ids = np.array(self.tok.encode(text).ids)
        x = (w["embeddings.word_embeddings.weight"][ids]
             + w["embeddings.position_embeddings.weight"][: len(ids)]
             + w["embeddings.token_type_embeddings.weight"][0])
        x = self._layer_norm(x, w["embeddings.LayerNorm.weight"], w["embeddings.LayerNorm.bias"])
        n, width = x.shape
        head = width // self.HEADS
        for i in range(self.LAYERS):
            p = f"encoder.layer.{i}."
            q = self._linear(x, p + "attention.self.query").reshape(n, self.HEADS, head).transpose(1, 0, 2)
            k = self._linear(x, p + "attention.self.key").reshape(n, self.HEADS, head).transpose(1, 0, 2)
            v = self._linear(x, p + "attention.self.value").reshape(n, self.HEADS, head).transpose(1, 0, 2)
            scores = q @ k.transpose(0, 2, 1) / math.sqrt(head)
            scores = np.exp(scores - scores.max(-1, keepdims=True))
            scores /= scores.sum(-1, keepdims=True)
            context = (scores @ v).transpose(1, 0, 2).reshape(n, width)
            x = self._layer_norm(x + self._linear(context, p + "attention.output.dense"),
                                 w[p + "attention.output.LayerNorm.weight"],
                                 w[p + "attention.output.LayerNorm.bias"])
            hidden = self._gelu(self._linear(x, p + "intermediate.dense"))
            x = self._layer_norm(x + self._linear(hidden, p + "output.dense"),
                                 w[p + "output.LayerNorm.weight"], w[p + "output.LayerNorm.bias"])
        pooled = x.mean(0)
        return pooled / (np.linalg.norm(pooled) or 1.0)


_lock = threading.Lock()
_state = {}


def _load():
    """Load the encoder and the example vectors once (examples cached on disk)."""
    with _lock:
        if _state:
            return _state
        directories = sorted(glob.glob(MODEL_GLOB))
        if os.path.exists(os.path.join(MODEL_DIR, "model.safetensors")):
            directories.append(MODEL_DIR)
        if not directories:
            raise FileNotFoundError(f"sentence encoder not found in {MODEL_DIR}")
        encoder = _Encoder(directories[-1])
        labels, phrases = [], []
        for intent, items in EXAMPLES.items():
            for phrase in items:
                labels.append(intent)
                phrases.append(phrase)
        key = hashlib.sha256(json.dumps(["v2", phrases, directories[-1]]).encode()).hexdigest()[:16]
        cache = state_path(f"intent-examples-{key}.npy")
        try:
            vectors = np.load(cache)
        except (OSError, ValueError):
            vectors = np.stack([encoder.encode(p) for p in phrases])
            np.save(cache, vectors)
        _state.update(encoder=encoder, labels=labels, vectors=vectors)
        return _state


def classify(request):
    """Return (intent, score, margin). Intent is NO_INTENT when nothing is close enough."""
    s = _load()
    vector = s["encoder"].encode(request)
    sims = s["vectors"] @ vector
    best = {}
    for label, sim in zip(s["labels"], sims):
        best[label] = max(best.get(label, -1.0), float(sim))
    ranked = sorted(best.items(), key=lambda item: -item[1])
    (top, score), runner_up = ranked[0], ranked[1][1]
    margin = score - runner_up
    if top == NO_INTENT or score < MIN_SCORE or margin < MIN_MARGIN \
            or (top in ACTION_INTENTS and margin < ACTION_MARGIN) \
            or (top in LOOK_INTENTS and (score < LOOK_SCORE or margin < LOOK_MARGIN)):
        return NO_INTENT, score, margin
    return top, score, margin


# ---------------------------------------------------------------- turning an intent into a command

def content_words(request):
    request = _POSSESSIVE.sub("", request)   # "what's" -> "what", not "what" + "s"
    return [w for w in _WORD.findall(request)
            if w.lower().strip(".") not in _STOP and (len(w) > 1 or w.isdigit())]


def _agents_in(request, core):
    """Agents named in the request, matching single words and neighbouring pairs."""
    from atlas import agent_commands

    words = [w.lower() for w in _WORD.findall(request)]
    found = []
    for size in (2, 1):
        for i in range(len(words) - size + 1):
            phrase = " ".join(words[i:i + size])
            if phrase in _STOP or len(phrase) < 3:
                continue
            agent = agent_commands.ALIASES.get(phrase)
            joined = phrase.replace(" ", "")      # "screen text" -> screentext
            if agent is None and joined in core.manage.AGENTS:
                agent = joined
            if agent and agent not in found:
                found.append(agent)
    return found


def translate(request):
    """Restate a loosely worded request as one command, or None. Raises nothing."""
    from atlas import assistant_core as core

    try:
        intent, score, margin = classify(request)
    except Exception:
        return None
    command = _command_for(intent, request, core)
    return command


def _command_for(intent, request, core):
    if intent == NO_INTENT:
        return None
    fixed = {"fixpock": "fixpock", "audio": "audio", "inbox": "inbox", "inboxreport": "inbox report", "brokerreport": "broker report", "memory": "free up memory",
             "batteryhelper": "battery helper", "batterylevel": "battery", "clipboard": "clean clipboard", "capture": "capture text",
             "disklevel": "disk", "uptimelevel": "uptime", "memorylevel": "info about=memory", "timeinfo": "info about=time",
             "networkinfo": "info about=network"}
    if intent in fixed:
        return fixed[intent]
    if intent == "powersave":
        return "power saving off" if _OFF.search(request) else "power saving on"
    agents = _agents_in(request, core)
    if intent == "status":
        return f"status {agents[0]}" if agents else "status"
    if intent in ("restart", "stop", "start"):
        return f"{intent} {agents[0]}" if agents else None
    if intent == "logs":
        return f"logs {agents[0]}" if agents else None
    if intent in ("run", "preview"):
        from atlas import agent_commands
        agent = next((a for a in agents if a in agent_commands.JOBS), None)
        if intent == "preview":
            return f"preview {agent}" if agent and agent_commands.JOBS[agent][1] else None
        return f"run {agent} now" if agent else None
    words = content_words(request)
    if intent == "repos":
        # The folder is whatever you named besides the word for what you are after; none named means everywhere.
        from atlas import tools
        named = [w for w in words if w.lower() not in _REPO_WORDS]
        where = ""          # no folder of yours among the words (how MANY repos): look everywhere
        for candidate in [" ".join(named)] + named:     # the words together, then each alone (how many … in ~/projects)
            try:
                if candidate and tools.resolve(candidate, "dir"):
                    where = candidate
                    break
            except Exception:
                continue
        return "files where=" + (f'"{where}"' if " " in where else where or "home") + " kind=repo"
    if not words:
        return None
    return f"{intent} {' '.join(words)}"
