# Domain Docs

How engineering skills should consume this repository’s domain documentation when exploring the codebase.

## Before exploring, read these

- `CONTEXT.md` at the repository root.
- `docs/adr/`: read ADRs that touch the area about to be changed.

If these files do not exist, proceed silently. Do not flag their absence or suggest creating them upfront. Domain-modeling skills create them lazily when terms or decisions are resolved.

## File structure

This repository uses a single-context layout:

```
/
├── CONTEXT.md
├── docs/
│   └── adr/
└── src/
```

## Use the glossary’s vocabulary

When output names a domain concept—in an issue title, refactor proposal, hypothesis, or test name—use the term defined in `CONTEXT.md`. Do not drift to synonyms the glossary explicitly avoids.

If a needed concept is missing from the glossary, reconsider whether the terminology belongs to the project or note the gap for domain modeling.

## Flag ADR conflicts

If proposed work contradicts an existing ADR, surface the conflict explicitly rather than silently overriding it.
