# ground-control-server — the public installer copy

⚠️ This is NOT the live server. It's the simplified, generalized copy that strangers
install with one command (`curl … install.sh | bash`). Separate public git repo
(github.com/PhilipBuonforte/ground-control-server).

**The live daily-driver server is `../pocket-claude/` — that is the source of truth.**
Edit that one for real behavior. **Code files here are GENERATED — never edit them
here.** Sync with `python3 ../pocket-claude/make_public.py` (whitelist copy + secret
scan; it hard-fails if a key/token pattern appears in the output). The only files
maintained directly in this repo are the public-only ones: `install.sh`, `README.md`,
`requirements.txt`, `.gitignore`, this file.

Full map in `../WORKSPACE.md`; hard rules in `../.claude/rules/invariants.md` (terminal
is truth, EZ not tmux, one Claude per session, alerts via relay).
