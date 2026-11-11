"""
LLM Prompts for Compact Summarization.

Provides structured prompts for generating high-quality conversation summaries
that preserve essential context for continuing work.

Inspired by Claude Code's compact prompt engineering with:
- Analysis block for draft thinking
- Structured summary sections
- Custom instruction support
"""

from typing import Optional
from .base import CompactionDirection


# Aggressive no-tools preamble to prevent model from calling tools
NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""

# Analysis instructions for different compact types.
#
# The summary is a technical work ledger, not a transcript: the dialogue
# messages themselves are preserved in the compacted session (they follow the
# summary), so the analysis focuses on what was done, found, changed, verified
# and coordinated - GRID tasks, sub-agent runs, files, errors, git state.
ANALYSIS_INSTRUCTION_BASE = """Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and architecture choices
   - Specific details like:
     - file names
     - class and function names and their signatures
     - what each read found and each edit changed, in one or two lines
     - DO NOT copy long verbatim code blocks into the summary: name the file
       and the symbol, and quote at most a line or two where the exact text
       matters (an error message, a signature, a config key)
     - file edits
   - GRID coordination state, with exact identifiers:
     - beads tasks: bead_id, title, status, dependencies
     - sub-agent runs: the orchestrate tool called, its context_id, status
       and outcome
     - verification runs: which tests and commands ran, and whether they passed
     - git state: commits with their hashes, the branch, uncommitted changes
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly."""

ANALYSIS_INSTRUCTION_PARTIAL = """Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Analyze the recent messages chronologically. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly."""

# Summary structure template: a focused technical work ledger. The dialogue
# messages (user and assistant) are preserved verbatim in the compacted
# session - they follow the summary - so the ledger carries the work, not a
# restatement of the talk.
SUMMARY_STRUCTURE = """Your summary should include the following sections:

1. Goal & Architecture Decisions: What this session is working towards - the user's request or the beads task it serves - and the architecture and technical decisions made so far, with the reasons that matter for continuing the work.
2. Tracker & Sub-agents State: The GRID coordination ledger, with exact identifiers: beads tasks (bead_id, title, status, dependencies), sub-agent runs (the orchestrate tool called, its context_id, status, outcome), and anything waiting on a reply, a run to finish, or a task to be closed.
3. Files Read & Modified: The files, classes and functions examined or changed, each with its path and a one-or-two-line note of what was found or changed. No long verbatim code dumps - reference the file and the symbol, and quote at most a line or two where the exact text matters.
4. Verification, Errors & Git State: Test and verification runs (which commands ran, whether they passed, which failures remain), errors hit and how each was fixed, and git state (commits with their hashes, the branch, uncommitted changes).
5. Current State & Immediate Next Step: Where the work stands right now - what is done, what is in progress - and the single next step to take, with the exact identifiers it needs (bead_id, context_id, file path). Ensure the step is DIRECTLY in line with the user's most recent request; do not start tangential or long-completed work without confirming with the user first.

The user's and assistant's dialogue messages are preserved verbatim in the compacted session and follow this summary, so do NOT restate or list them here: focus on the technical work ledger above - tool findings, file changes, errors and fixes, verification, coordination state, and the current state of the work.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Goal & Architecture Decisions:
   [What is being built or fixed, and the decisions that shape it]

2. Tracker & Sub-agents State:
   - beads: [bead_id] - [title], [status], [dependencies]
   - sub-agents: [orchestrate tool] context_id [ctx-...] - [status], [outcome]

3. Files Read & Modified:
   - [path/to/file.py] ([class_or_function]): [what was found or changed]

4. Verification, Errors & Git State:
   - [tests/commands]: [pass or fail, remaining failures]
   - git: [commit hash] on [branch], [uncommitted changes]

5. Current State & Immediate Next Step:
   [Precise current state, and the next step with its identifiers]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response. 

There may be additional summarization instructions provided in the included context. Examples of instructions include:
<example>
## Compact Instructions
When summarizing the conversation focus on typescript code changes and also remember the mistakes you made and how you fixed them.
</example>

<example>
# Summary instructions
When you are using compact - please focus on test output and code changes. Include file reads verbatim.
</example>

"""

SUMMARY_STRUCTURE_PARTIAL = """Your summary should include the following sections:

1. Primary Request and Intent: Capture the user's explicit requests and intents from the recent messages
2. Key Technical Concepts: List important technical concepts, technologies, and frameworks discussed recently.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List errors encountered and how they were fixed.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages from the recent portion that are not tool results.
7. Pending Tasks: Outline any pending tasks from the recent messages.
8. Current Work: Describe precisely what was being worked on immediately before this summary request.
9. Optional Next Step: List the next step related to the most recent work. Include direct quotes from the most recent conversation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Important Code Snippet]

4. Errors and fixes:
    - [Error description]:
      - [How you fixed it]

5. Problem Solving:
   [Description]

6. All user messages:
    - [Detailed non tool use user message]

7. Pending Tasks:
    - [Task 1]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the RECENT messages only (after the retained earlier context), following this structure and ensuring precision and thoroughness in your response.
"""

# Up-to partial compact (for context restoration)
SUMMARY_STRUCTURE_UP_TO = """Your task is to create a detailed summary of this conversation. This summary will be placed at the start of a continuing session; newer messages that build on this context will follow after your summary (you do not see them here). Summarize thoroughly so that someone reading only your summary and then the newer messages can fully understand what happened and continue the work.

{analysis_instruction}

Your summary should include the following sections:

1. Primary Request and Intent: Capture the user's explicit requests and intents in detail
2. Key Technical Concepts: List important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List errors encountered and how they were fixed.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results.
7. Pending Tasks: Outline any pending tasks.
8. Work Completed: Describe what was accomplished by the end of this portion.
9. Context for Continuing Work: Summarize any context, decisions, or state that would be needed to understand and continue the work in subsequent messages.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Important Code Snippet]

4. Errors and fixes:
    - [Error description]:
      - [How you fixed it]

5. Problem Solving:
   [Description]

6. All user messages:
    - [Detailed non tool use user message]

7. Pending Tasks:
    - [Task 1]

8. Work Completed:
   [Description of what was accomplished]

9. Context for Continuing Work:
   [Key context, decisions, or state needed to continue the work]

</summary>
</example>

Please provide your summary following this structure and ensuring precision and thoroughness in your response.
"""

NO_TOOLS_TRAILER = """

REMINDER: Do NOT call any tools. Respond with plain text only — an <analysis> block followed by a <summary> block. Tool calls will be rejected and you will fail the task."""


def get_compact_prompt(
    custom_instructions: Optional[str] = None
) -> str:
    """
    Get the prompt for full conversation summarization.
    
    Args:
        custom_instructions: Optional custom instructions for summarization
        
    Returns:
        Complete prompt for the LLM
    """
    prompt = NO_TOOLS_PREAMBLE + ANALYSIS_INSTRUCTION_BASE + SUMMARY_STRUCTURE
    
    if custom_instructions and custom_instructions.strip():
        prompt += f"""

Additional Instructions:
{custom_instructions}"""
    
    prompt += NO_TOOLS_TRAILER
    
    return prompt


def get_partial_compact_prompt(
    custom_instructions: Optional[str] = None,
    direction: CompactionDirection = CompactionDirection.FROM
) -> str:
    """
    Get the prompt for partial conversation summarization.
    
    Args:
        custom_instructions: Optional custom instructions
        direction: Direction of partial compact (from / up_to)
        
    Returns:
        Complete prompt for the LLM
    """
    if direction == CompactionDirection.UP_TO:
        template = SUMMARY_STRUCTURE_UP_TO.format(
            analysis_instruction=ANALYSIS_INSTRUCTION_BASE
        )
        prompt = NO_TOOLS_PREAMBLE + template
    else:
        prompt = NO_TOOLS_PREAMBLE + ANALYSIS_INSTRUCTION_PARTIAL + SUMMARY_STRUCTURE_PARTIAL
    
    if custom_instructions and custom_instructions.strip():
        prompt += f"""

Additional Instructions:
{custom_instructions}"""
    
    prompt += NO_TOOLS_TRAILER
    
    return prompt


def get_user_summary_message(
    summary: str,
    suppress_followup_questions: bool = False,
    transcript_path: Optional[str] = None,
    recent_messages_preserved: bool = False
) -> str:
    """
    Generate user-facing summary message.
    
    Args:
        summary: Raw summary text (may contain XML tags)
        suppress_followup_questions: Whether to suppress follow-up questions
        transcript_path: Optional path to full transcript
        recent_messages_preserved: Whether recent messages were preserved
        
    Returns:
        Formatted summary message
    """
    from .utils import format_compact_summary
    
    formatted_summary = format_compact_summary(summary)
    
    base = f"""This session is being continued from a previous conversation that ran out of context. The summary below is a technical work ledger of that earlier portion - goal and architecture decisions, tracker and sub-agent state, files read and modified, verification and errors, and the current state of the work. The dialogue messages themselves are preserved in the session and follow this summary.

{formatted_summary}"""
    
    if transcript_path:
        base += f"""

If you need specific details from before compaction (like exact code snippets, error messages, or content you generated), read the full transcript at: {transcript_path}"""
    
    if recent_messages_preserved:
        base += "\n\nRecent messages are preserved verbatim."
    
    if suppress_followup_questions:
        base += """

Continue the conversation from where it left off without asking the user any further questions. Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with "I'll continue" or similar. Pick up the last task as if the break never happened."""
    
    return base