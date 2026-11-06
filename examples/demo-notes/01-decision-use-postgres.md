---
title: "Decision: use Postgres for the recipe app"
kind: decision
tags: [recipe-app, database]
scope: recipe-app
---
Chose **Postgres** over SQLite for the recipe app.

Why: we want full-text search with ranking, and two users writing at once. SQLite would work for one user, but we expect to share it with family.

Revisit if hosting cost matters more than concurrency.
