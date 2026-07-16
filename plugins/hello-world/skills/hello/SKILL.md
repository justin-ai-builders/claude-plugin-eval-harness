---
name: hello
description: Give a short hello-world greeting. Use when the user invokes the hello skill and optionally supplies a name.
argument-hint: "[name]"
allowed-tools: []
---

# Hello

Reply with exactly one line and no other commentary.

If the user supplies a name, reply:

```text
Hello, <name>!
```

Replace `<name>` with the full name supplied by the user.

If the user does not supply a name, reply:

```text
Hello, world!
```
