# Mac Utils

Small background helpers for macOS, a hotkey assistant, and email privacy tools, installed one
component at a time. Everything runs on your Mac: there's no server, no account and no telemetry.

- **The assistant (atlas):** Ctrl+Option+Space opens a prompt for opening, finding, reading and
  searching files, running a shell command, and the battery, memory and do-not-disturb helpers.
- **Everyday helpers:** copy text from any part of the screen, screenshots straight to the
  clipboard, tidy copied code and links, unpack downloads, guard your audio.
- **Tidying up:** archive a cluttered Desktop, clear developer and package caches, trim logs.
- **Focus:** quit distracting apps, block adult sites.
- **Email and privacy:** unsubscribe from what you don't read, ask data brokers to delete what they
  hold on you, and an Edge extension that shows who receives what you type into a web form.

Requires an Apple Silicon Mac with macOS 14 (Sonoma) or later.

## Install

### With the installer (recommended)

1. Download `MacUtils-<version>.dmg` from the [Releases](https://github.com/roocxre/mac_utils/releases) page.
2. Open it and drag **Mac Utils** to Applications.
3. Open Mac Utils. Unless the release says it is signed and notarized, macOS refuses the first time
   ("Apple could not verify…"). Click **Done**, go to **System Settings → Privacy & Security**,
   scroll to the message about Mac Utils and click **Open Anyway**. You only do this once.
4. Tick the components you want and click **Install**. The installer sets up its own Python and
   downloads the packages and models it needs. Some setups need more than a click (macOS
   permissions, signing in to Gmail, loading the Edge extension); the **Setup** screen walks you
   through each one.

Open Mac Utils again any time to add, reinstall or remove components, or check for problems.

### From a clone

```sh
git clone https://github.com/roocxre/mac_utils.git
cd mac_utils
./install.sh --components assistant,clipboard,screentext   # or: --components all
```

`install.sh` gets a pinned `uv` (checked against its SHA-256), installs a pinned Python 3.14,
creates `.venv` from `uv.lock`, downloads the models pinned in `models.lock.json`, runs each
component's setup and loads its launchd agents. The code runs from your clone. Run it again
without `--components` to update everything you've installed.

In a clone, the screen-text helper is compiled on your Mac, which needs the Xcode Command Line
Tools (`xcode-select --install`). The installer app ships it prebuilt.

## Components

| Component | What it does | Runs |
|---|---|---|
| **atlas, the assistant** | Ctrl+Option+Space: a prompt for commands (see below) | always |
| Clipboard tidy | Dedents copied code; turns a copied URL into a Markdown link without tracking parameters | always |
| Copy text from the screen | Cmd+Shift+2: select a region and copy its text (Apple's on-device text recognition) | always |
| Screenshots to the clipboard | Copies each new screenshot to the clipboard as an image | when the Desktop changes |
| Long command alerts | An alert when a terminal command that ran over 15 seconds finishes | always |
| Unpack downloads | Unpacks archives in Downloads into a folder and trashes the archive | when Downloads changes |
| Audio guard | Pauses and mutes when headphones drop to the speakers or the Mac sleeps; switches to a display's speakers | always |
| Popup inbox | Shows messages your own scripts drop in a folder as banners that stay until clicked | always |
| Desktop and Downloads archiver | Moves Desktop screenshots to `~/Archive/Screenshots` and items untouched for 14 days to `~/Archive/YYYY-MM`; trashes old installers | daily 00:00 |
| Developer cache cleaner | Removes Python caches, and `node_modules`/`target` of projects idle for 30 days | Sundays 02:00 |
| Log trimmer | Deletes crash reports older than 7 days, empties logs over 50 MB | Saturdays 01:00 |
| Package cache cleaner | Cleans the Homebrew, pip, npm, pnpm, yarn and uv caches | 1st of the month |
| App blocker | Quits Music, TV, Podcasts, Tips, Freeform and Image Playground when they open; answers the Play key so Music stops launching | always |
| Adult-site blocker | Quits the browser when its active tab is on the [OISD NSFW](https://oisd.nl/) list. Two agents restart each other so it can't be switched off on a whim; remove it with the installer or `manage.py remove sitemonitor` | always |
| Gmail connection | Signs in to one Gmail account and checks it every 5 minutes for the email agents | every 5 min |
| Inbox unsubscriber | Proposes senders to unsubscribe from or block, from what you do with their mail | Sundays 09:00 |
| Data broker removal | Sends deletion requests to data brokers and follows them up | every 10 min |
| Where does my email go | An Edge extension that shows who receives what you type in each field | hourly |
| Pock keeper | Relaunches [Pock](https://pock.app/), the Touch Bar app, if it dies | every 30 min |

The inbox unsubscriber and the broker agent need the Gmail connection, and their dashboards open
inside the assistant; the installer adds what a component needs when you tick it. Default lists
(the apps the blocker quits, the folders the archiver tidies, the apps "free up memory" quits) are
constants at the top of each script.

## The assistant

Press **Ctrl+Option+Space** and type a command. `help` lists them; `tools <words>` finds the
command that does something (`tools stale cleanup`).

| Type | Does |
|---|---|
| `~/Doc`, `/usr/lo` | Go to Folder: completes the path and opens it in Finder |
| `open safari`, `open downloads`, `open <file>` | Opens an app, a known folder or a file found by name |
| `find thesis` | Name search across the whole Mac, your own files first; pick one to reveal it |
| `search inside invoice in downloads` | Finds files by what's in them (Spotlight), newest first; `pdfs`, `images`, `code`… narrow it |
| `ls ~/Archive`, `read notes.txt` | Lists a folder; shows the start of a text file (never keys, `.env` or `.ssh`) |
| `$ du -sh ~/Downloads` | Runs one line of zsh as you, never as admin. Anything that could delete, move or overwrite asks first |
| `install firefox` | Looks it up in Homebrew, shows what it found, and on your yes runs `brew install` in Terminal |
| `free up memory` | Quits the apps on the always-quit list, then lists the next biggest to pick from |
| `battery helper` | Quits Dock apps unused for a day, then shows charge, time left and the most power-hungry processes |
| `power saving on` / `off` | Low Power Mode, short display sleep, no Power Nap and a dimmer screen, on battery only. The power settings need a one-time sudoers rule (the battery dashboard's **Copy setup command**) |
| `lock keyboard 2m`, `lock everything 1m` | Ignores keys, clicks, or both, for a while (to wipe them). Ctrl+Option+Cmd+Esc unlocks |
| `dnd on`, `dnd off`, `dnd auto` | Do not disturb for the popup inbox. Auto turns it on while the screen is shared, a display is connected or the lid is closed |
| `status`, `logs clipboard`, `restart screentext` | An agent's state, the end of its log, start/stop/restart |
| `run screenshots now`, `preview dev` | Runs a scheduled job once now, or shows what it would do |
| `inbox report`, `broker report`, `dataflow site example.com` | The email agents' dashboards and reports |

Loose wording is fine: a small sentence-embedding model (all-MiniLM-L6-v2, on the CPU, a few
milliseconds) restates "pull up my resume" as `open resume`. It can only reuse words you typed, and
commands that change something need a clearer match than ones that only look. Anything that still
isn't a command gets the closest commands as suggestions. No language model is involved.

## Email and privacy

### Signing in to Gmail (once)

The email agents use your own Google Cloud project, so nothing passes through anyone else's server.

1. At [console.cloud.google.com](https://console.cloud.google.com), create a project (any name).
2. **APIs & Services → Library → Gmail API → Enable.**
3. **OAuth consent screen:** user type **External**; add your own address as a test user; then
   **Publish app** so it says "In production". Left in Testing, the sign-in stops working after
   7 days. No verification is needed for your own use.
4. **Clients** (or Credentials → Create credentials → OAuth client ID) → **Desktop app** → Create →
   **Download JSON**.
5. In the installer's Setup screen choose that file, or from the install folder run
   `.venv/bin/python -m mail setup ~/Downloads/client_secret_….json`.
6. Google warns that it hasn't verified the app. It's yours: choose **Advanced → Go to … (unsafe)**
   and keep both permissions ticked (read and modify mail, send mail).

The tokens are kept in your login Keychain. `mail status` (at the prompt) shows the sign-in and the
last poll. If Google revokes the sign-in, a banner tells you to run setup again.

### Inbox unsubscriber

It watches what you do with each sender's mail (opened, deleted unread, archived, replied, marked
as spam) and once a week proposes senders to unsubscribe from or block. Every rule applies to one
category of mail from a sender: blocking a shop's ads never touches its receipts. Receipts, codes,
statements and anyone you've written to are never proposed.

Click the weekly banner, or type `inbox report`, to open the dashboard; pick senders and press
**Keep**, **Unsubscribe** or **Block**. Unsubscribing tries the one-click standard (RFC 8058), then
`mailto:`, then the link in a headless browser that stops at any password or card field and never
types. A block moves that category of mail to the Trash as it arrives. The weights and rules are in
`unsub/config.json` (see Settings to change them).

### Data broker removal

It asks data brokers to delete what they hold on you, tracks each request, and asks again if a
listing comes back. Nothing is sent until you enter your details in the broker dashboard's
**Your details** (`broker report`) and approve. Each request sends only the details that broker
requires, and a ledger records what went to whom. Your details are kept in an encrypted database
(SQLCipher) whose key is in your Keychain.

- **Requests are voluntary.** They go through each broker's own opt-out process and claim no
  residency or legal right. If a law where you live gives you a right to deletion, add it to
  `jurisdiction.laws`, and your attorney general's complaint page to `jurisdiction.regulators`
  (keyed by your state's two letters), in your settings file (see Settings). Requests to the
  brokers it covers then cite it.
- **Sending:** at most 50 emails a day, from your Gmail address, plus web forms. It never solves a
  CAPTCHA, uploads an ID, pays, makes an account or fills in a detail you didn't approve; those
  become manual tasks.
- **The registry** of about 900 brokers is built on your Mac at install by `python -m brokers
  import`, from California's data broker registry and community lists (see `THIRD_PARTY_NOTICES.md`).
  It isn't redistributed here.
- **Replies** are sorted by rules and a small local model (nli-deberta-v3-xsmall); anything unclear
  becomes a manual task.

### Where does my email go (the Edge extension)

Under every field you can type into, a small badge shows who receives what you type there: the
scripts and companies that listen to the field, read it or send it, which of them are data brokers,
and what the site's privacy policy says. It blocks identity-resolution, session-replay and broker
requests, cancels third-party requests that carry your email address, and turns on Global Privacy
Control. On risky fields it runs a canary test: a hidden browser types a throwaway `@example.com`
address and watches where it goes, without submitting.

Setup, once:

1. Install the component; the installer registers the Mac helper with Edge.
2. In Edge, open `edge://extensions`, turn on **Developer mode**, choose **Load unpacked** and pick
   the `dataflow/extension` folder in the install folder (the Setup screen has a button for it).

Your own addresses come from the Gmail connection; without it, list them as `extra_emails` in your
`dataflow` settings (see Settings). Policies are read with local models (nli-deberta-v3-base and
bert-base-NER), and findings are kept for 90 days in the state folder.

In a clone, the setup writes this install's extension key into `dataflow/extension/manifest.json`;
don't commit that change.

## Settings

Each email component's defaults are in its `config.json`, which is replaced on every update. Put
your changes in the state folder instead, as `<component>-config.json` holding only the keys you
change, for example `~/Library/Application Support/MacUtils/state/brokers-config.json`:

```json
{"jurisdiction": {"states": ["NY"], "checked": "2026-11-01"}}
```

The components with settings are `unsub`, `brokers` and `dataflow`.

## macOS permissions

macOS asks for these the first time an agent needs them. The agents run on the installer's Python,
so the prompts and System Settings show **python3.14**.

| Permission | Asked by |
|---|---|
| Accessibility | the assistant (keyboard lock), the adult-site blocker (to read the address in Firefox-family browsers) |
| Screen Recording | copy text from the screen |
| Automation | the adult-site blocker (once per browser), audio guard (Music, Spotify) |
| Files and folders | the archiver, unpack downloads, screenshots to the clipboard, the developer cache cleaner |

## Managing

From the install folder (`~/Library/Application Support/MacUtils/app`, or your clone):

```sh
.venv/bin/python manage.py components            # what's installed
.venv/bin/python manage.py status                # loaded? last exit code? anything on stderr?
.venv/bin/python manage.py doctor                # problems: not loaded, running old code, errors, setup left to do
.venv/bin/python manage.py add <component ...>   # install and load a component's agents
.venv/bin/python manage.py remove <component ...>
.venv/bin/python manage.py setup <component ...> [--only step,...]
.venv/bin/python manage.py install [agent ...]   # rewrite plists and reload, e.g. after editing a script
.venv/bin/python manage.py uninstall-all [--keep-state]
```

Each command also takes `--json`. The cleanup scripts accept `--dry-run`:
`.venv/bin/python agents/stale_archiver.py --dry-run`.

### Uninstalling

Use **Manage → Uninstall everything** in the installer, or `manage.py uninstall-all`. It unloads
every agent and removes the plists, the Edge helper registration and the install record; without
`--keep-state` it also removes the state and log folders. It prints how to remove the two Keychain
items and the Playwright browser cache, which it leaves alone.

## Where things are

| What | Where |
|---|---|
| Code and its Python (installer) | `~/Library/Application Support/MacUtils/app` |
| State: databases, caches, settings | `~/Library/Application Support/MacUtils/state` |
| Logs | `~/Library/Logs/MacUtils/<script>.log`, plus `<agent>.stderr.log` |
| launchd agents | `~/Library/LaunchAgents/io.github.roocxre.macutils.<agent>.plist` |
| Keychain | `io.github.roocxre.macutils.gmail`, `io.github.roocxre.macutils.brokers` |

The popup inbox reads JSON files dropped into `~/Library/Application Support/MacUtils/state/inbox/`;
the format is at the top of `agents/message_popup.py`.

## Building the installer

On an Apple Silicon Mac with the Xcode Command Line Tools:

```sh
UV_BINARY=/path/to/uv scripts/build-app.sh   # build/Mac Utils.app; use the uv pinned in install.sh
scripts/build-dmg.sh                         # build/MacUtils-<version>.dmg
"build/Mac Utils.app/Contents/MacOS/Mac Utils" --self-test "build/Mac Utils.app/Contents/Resources/payload"
```

Builds are signed ad hoc. For a release, set `MACUTILS_SIGN_IDENTITY="Developer ID Application: …"`
and `MACUTILS_NOTARY_PROFILE=<notarytool keychain profile>` for both scripts to sign with a
Developer ID, notarize and staple.

## Privacy

Nothing leaves your Mac except the Gmail API calls the email agents make for you; the requests,
searches and form submissions to data brokers you approve; the canary tests' page visits; and the
downloads listed in `THIRD_PARTY_NOTICES.md` (models, the broker sources, tracker and blocklists).

## Licence

MIT (`LICENSE`). Models, data and lists fetched at install or run time keep their own licences; see
`THIRD_PARTY_NOTICES.md`.
