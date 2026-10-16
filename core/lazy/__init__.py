"""Lazy agents: results by a deadline on a small, agreed share of a subscription.

A lazy task is a promise: a result, how to tell it is done, a deadline, and a
share of the subscription the agents may use. The work is cut into a few
steps - scout, work, verify - each one agent turn. Between steps nothing runs
and nothing is spent: the task is stored state, woken by its schedule.

The parts, each usable on its own:

- :mod:`core.lazy.windows` - the quota windows a subscription limits use in
  (five hours, seven days) and how they reset;
- :mod:`core.lazy.routine` - when the subscription is the user's own to use;
- :mod:`core.lazy.forecast` - how much the user is expected to spend, learned
  from what the provider reported;
- :mod:`core.lazy.budget` - how much the agents may still spend in a window;
- :mod:`core.lazy.estimate` - what a step costs, calibrated by real runs;
- :mod:`core.lazy.model` - tasks, steps, settings and the ledger of runs;
- :mod:`core.lazy.situation` - where every window stands right now;
- :mod:`core.lazy.planner` - when each step runs, whether each deadline holds,
  and what would make a missed one hold.

Planning is arithmetic, never a model call: it reruns on every change.
"""
