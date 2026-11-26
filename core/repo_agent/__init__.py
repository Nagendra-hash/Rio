"""
core/repo_agent — Universal Repository Manager, Skill Connector and Task
Execution Agent for JARVIS.

Modules
    security    URL/name validation, static risk scan, secret redaction
    manifests   install manifests, skill manifests, ~/JarvisTools registry
    manager     inspect / install / update / remove lifecycle
    jobs        durable job store (SQLite, dedup, cancel, recovery)
    skills      typed runner for allowlisted skill operations
    vimax       first adapter: HKUDS/ViMax video creation
    notify      best-effort Hermes notifications for finished jobs
"""
