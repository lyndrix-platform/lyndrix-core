"""Work assignment (core API 1.8): deterministic, balanced, role-aware, minimal
reshuffle on membership changes; single mode assigns everything to this instance.

Run inside the lyndrix-core runtime:
    pytest tests/cluster/test_assignment.py -v
"""
import types

from core.components.cluster.logic.assignment import WorkAssigner


def _inst(iid, roles=("api", "leader"), alive=True, draining=False):
    return types.SimpleNamespace(instance_id=iid, roles=list(roles), alive=alive, meta={"draining": draining})


def _svc(me, instances=None, roles=("api", "leader", "runner")):
    return types.SimpleNamespace(instance_id=me, roles=list(roles), _instances=list(instances or []))


def test_single_mode_assigns_everything_to_self():
    a = WorkAssigner(_svc("solo"))
    assert a.workers("w", count=3) == ["solo"]
    assert a.my_shard("w", [1, 2, 3]) == [1, 2, 3]
    assert a.is_worker("w")
    assert a.workers("w", roles=["runner"]) == ["solo"]
    assert a.workers("w", roles=["gpu"]) == []


def test_every_instance_computes_the_same_answer_and_it_balances():
    members = [_inst(f"n{i}") for i in range(4)]
    views = [WorkAssigner(_svc(f"n{i}", members)) for i in range(4)]
    keys = [f"host-{i}" for i in range(200)]
    shards = [v.shard("probe", keys) for v in views]
    assert all(s == shards[0] for s in shards)                      # agreement without a round trip
    sizes = sorted(len(v) for v in shards[0].values())
    assert sum(sizes) == 200 and len(sizes) == 4 and sizes[0] >= 25    # no empty worker, roughly even
    mine = [set(v.my_shard("probe", keys)) for v in views]
    assert set().union(*mine) == set(keys) and sum(len(m) for m in mine) == 200  # disjoint cover
    assert len(views[0].workers("w", count=2)) == 2
    assert views[0].workers("w", count=2) == views[3].workers("w", count=2)


def test_membership_change_moves_only_the_leaving_workers_keys():
    members = [_inst(f"n{i}") for i in range(4)]
    keys = [f"k{i}" for i in range(300)]
    before = WorkAssigner(_svc("n0", members)).shard("probe", keys)
    members[2].alive = False  # n2 died
    after = WorkAssigner(_svc("n0", members)).shard("probe", keys)
    assert "n2" not in after
    for iid in ("n0", "n1", "n3"):
        assert set(before[iid]) <= set(after[iid])                 # nobody loses keys
    moved = set(before["n2"])
    assert set().union(*after.values()) == set(keys) and moved     # n2's keys were re-homed


def test_roles_and_draining_filter_candidates_and_count_caps():
    members = [_inst("api1", ["api", "leader"]), _inst("api2", ["api", "leader"]),
               _inst("run1", ["api", "runner"]), _inst("run2", ["runner"], draining=True)]
    a = WorkAssigner(_svc("api1", members))
    assert a.workers("probe", count=5, roles=["runner"]) == ["run1"]  # run2 drains, count capped
    assert a.workers("probe", count=1, roles=["api"]) and len(a.workers("x", count=3)) == 3
    assert a.my_shard("probe", ["h1", "h2"], roles=["runner"]) == []  # api1 is not a runner
    rep = {r["name"]: r for r in a.report()}
    assert rep["probe"]["workers"] == ["run1"] and rep["probe"]["is_worker"] is False
