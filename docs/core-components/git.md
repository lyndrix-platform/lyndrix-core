# Git Component

## Purpose

The Git component executes repository sync and commit/push operations as asynchronous event-driven workflows.

## Main location

- `app/core/components/git/logic/git_service.py`

## Responsibilities

- Clone/pull repositories for platform workflows
- Handle authenticated HTTPS and SSH sync modes
- Stage/commit/push repository updates
- Serialize per-repository operations with async locks
- Emit operation status updates

## Events

### Subscribes

- `git:sync`
- `git:commit_push`

### Emits

- `git:status_update`
