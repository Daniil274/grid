"""Lazy agents in the web chat: the service, its runner and its page's API.

core.lazy decides what to run when; this package does it in a user's space:

- :mod:`web_chat.lazy.accounts` - which subscription an agent runs on, and the
  windows of one that reports nothing about them;
- :mod:`web_chat.lazy.prompts`  - what a step asks of the agent;
- :mod:`web_chat.lazy.runner`   - one step as a chat turn of the task's own
  conversation, watched and stopped at a safe point when it must;
- :mod:`web_chat.lazy.service`  - the tasks, the plan, the wake-up loop;
- :mod:`web_chat.lazy.http`     - the routes of the /lazy page.

A step is an ordinary turn: its trace, checkpoints and spend are kept like any
other, the user's turn limits count it, a socket that opens the conversation
watches it live, and a planned server restart resumes it.
"""
