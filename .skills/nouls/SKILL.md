---
name: nouls
description: Help users work with nouls, the semantic linter and language server built on TypeSafe yes/no questions. Use when the user asks about nouls rules, configuration, thresholds, labels or stats, or wants to add a rule or language.
---

# nouls Skill

This skill helps you work with nouls.

## When to Use This Skill

Use this skill when:

- User asks about nouls features or capabilities
- User wants to add or tune a rule in `nouls.yaml`
- User wants to add a language through tree-sitter node types
- User needs help with the nouls CLI or language server
- User wants to label findings or read `nouls stats`

## Project Information

- **Description**: Semantic linter and language server that asks TypeSafe yes/no questions about every function
- **Author**: Ben O'Mahony
- **Documentation**: See docs/index.md for full documentation
- **Source**: src/nouls/

## Quick Reference

### CLI Usage

```bash
nouls check src/
nouls rules
nouls label src/app.py 42 unit_mismatch false
nouls review unit_mismatch
nouls stats thresholds --ask
nouls serve
```

### Rule shape

```yaml
rules:
  ledger_sign:
    question: Does the function add a debit where the domain requires subtracting it, or the reverse?
    message: Debit and credit signs look inverted. Subtract debits and add credits
    severity: error
    threshold: 0.9
    languages: [python]
    files: ["*/ledger/*.py"]
```

Questions must be a single yes/no judgement about the function in the `function` state field. Never ask for a number.

Write each `message` in plain language, say precisely what is wrong, then suggest how to fix it. The reader may be a user, a developer or an agent, so name the exact command, setting or code to change.

## Resources

- Check docs/index.md for comprehensive documentation
- Check llms.txt for LLM-friendly documentation summary
- Check src/nouls/defaults.yaml for built in languages and rules
- Check src/nouls/ for implementation details
