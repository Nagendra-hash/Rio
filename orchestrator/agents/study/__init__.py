"""
orchestrator/agents/study/ — the Phase 15 Study Partner agent.

A study specialist on the standard BaseAgent contract that REUSES shared
infrastructure instead of duplicating it:

    learning.py       goal/subject parsing, the spaced-repetition ladder
                      (box 1 → 5), mastery scoring, adaptive difficulty.
    resources.py      resource discovery through the permitted-source
                      toolbox (web search + local KB as caller
                      `study_agent`) — never a fabricated link.
    engine.py         study plans, explanations from RAG evidence,
                      exercise/quiz generation, grading, progress.
    profile.py        the study vault reader/writer boundary (memory/study/).
    study_agent.py    the BaseAgent wiring it together.

Disabled by default (shadow-mode convention): JARVIS_STUDY_AGENT=on.

THE HARD RULES (enforced in code, in safety.py)
    1. NO EXAM CHEATS — live exams, graded assignments, someone else's
       homework are refused in code with a fixed message. The agent helps
       LEARN, not subvert.
    2. NO MEDICAL/LEGAL STUDY ADVICE BOUNDARY — explanations are grounded
       in the user's own RAG library or found resources and always carry
       citations; an explanation from thin evidence says so.
    3. TEACHING NOT ANSWERING — quizzes and exercises are generated from
       grounded evidence; grading is deterministic; adapting difficulty
       follows the mastered/unmastered record, never a vibe.
"""
