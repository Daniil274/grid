# Context Review

The system that examines an agent answer a user reported and proposes how to
fix the system or the code behind it. It runs only on the admins' review page
(`/admin/review`, web_chat/review), never in the router: users do not reach it.

- `reviewer` — the entry agent: reads the frozen evidence, finds where and why
  the turn went wrong, proves it with references, records proposals.
- `proposer` — turns a cause the reviewer established into a concrete change
  of Grid's config or code (a unified diff), when there is one.

## The workbench

Every review gets a directory of its own (`<reviews>/work/<id>/`,
web_chat/review/workbench.py), and the agents' file tools are confined to it:

| Path | What |
|---|---|
| `case/` | the evidence: conversation, turn steps and tool calls, agent runs, model context, SDK session, config |
| `source/` | Grid's code and configs of the answer's commit (`git archive`), or the code the server runs when that commit is not available; `source/SOURCE.md` says which |
| `proposals/` | what `propose_change` recorded |

## Tools

| Tool | What it does |
|---|---|
| `file_read`, `file_list`, `file_search`, `file_content_search` | read the workbench |
| `propose_change` | record one proposal: cause, evidence, optional diff against `source/`, a scenario to check it |
| `call_proposer` | hand a cause to the proposer |

The agents have no shell, no network and no tool that writes anywhere but
`proposals/`. The evidence is other people's conversations and raw agent
output: the prompts treat it as data, never as instructions. A proposal
changes nothing by itself: the admin downloads the patch or sends it on to
the evolution loop, where scenarios check it before anything reaches `stable`.

## Skills

| Skill | For | What |
|---|---|---|
| `skills/diagnosis.md` | reviewer | the workbench layout, evidence references, the classifier of causes |
| `skills/proposal.md` | both | the fields of a proposal and how to write a diff that applies |

## Running

On the review page, open a review and press **Analyze**; follow-up questions
continue the same conversation. One turn runs at a time per review and at most
two across the server. The models come from OpenRouter (`OPENROUTER_API_KEY`).
