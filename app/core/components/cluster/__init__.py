"""Cluster component — coordination layer for N core instances (docs/ha/).

Service singleton: ``core.components.cluster.logic.cluster_service.cluster_service``.
Not loaded through a module ``entrypoint.py`` on purpose: it has to be up before
modules load (it hands out leases and the bus bridge to them).
"""
