# Git Component

## Purpose

The Git component executes repository synchronization and commit/push workflows as asynchronous event-driven operations.

## Main location

- `app/core/components/git/logic/git_service.py`

## Responsibilities

- clone and pull repositories used by Lyndrix workflows
- support authenticated HTTPS and SSH-based repository access
- stage, commit, and push changes when requested
- serialize per-repository operations to avoid concurrent corruption
- emit status updates back onto the event bus

## Events

### Subscribes

- `git:sync`
- `git:commit_push`

### Emits

- `git:status_update`

## Integration notes

The Git component is a runtime utility used by other parts of the system. A key example is the plugin ecosystem, where plugin collection metadata can be synchronized through a Git-managed repository.
