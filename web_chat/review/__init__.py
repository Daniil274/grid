"""Reviews: a user's report of a bad answer, frozen as evidence for the admins.

The magnifier-with-a-bug under an agent's answer asks what went wrong and
freezes what the agent had and did (:mod:`web_chat.review.evidence`): the
conversation up to that answer, the steps and tool calls of its turn, the
agent runs, the context sent to the model, the agent's SDK session and the
configuration it ran with, at the running commit. Secrets are scrubbed and
sizes bounded (:mod:`web_chat.review.redact`) before anything is stored.

Clicking is the user's consent: that conversation, up to that answer, is then
visible to the admins. A review is kept outside every user's space
(:mod:`web_chat.review.store`); :mod:`web_chat.review.desk` decides who may
file and read one.
"""
