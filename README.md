# discord-claudebot

A simple Discord bot that uses the Anthropic Claude API to answer questions.

## Commands

- `/ask <question>` — Ask Claude a question.

## Setup

```
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
python bot.py
```

Copy `.env.example` to `.env` and fill in your keys first.

## Files

```
.env.example       # template for secrets
.gitignore
requirements.txt   # Python deps
bot.py             # the whole bot lives here
```
