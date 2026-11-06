---
title: Welcome to your brain (read me first)
kind: reference
tags: [brain, getting-started]
scope: personal
pinned: true
---
This is a demo note. Delete the demo notes once you have your own.

## What this is
A small server that stores your notes and lets AI agents (Claude Code, Claude Desktop, claude.ai, any MCP client) **search, read and write** them, from any device.

## How to use it day to day
- Just talk to your agent. Ask "what did I decide about X?" and it searches the brain.
- Tell it "remember that ..." and it writes a note for you.
- Open the web UI (the server's root URL) to browse, read and edit notes by hand. Bodies render as markdown.

## How search works
Keyword search (SQLite FTS5, BM25) with a recency boost. Every note also gets a **summary**, **find-it questions** (how you'd search for it) and **atoms** (exact errors, commands, decisions and their reasons). Specific keywords work best: names, error text, hostnames.
