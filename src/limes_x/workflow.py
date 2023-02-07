from __future__ import annotations
import os, sys
from pathlib import Path
from typing import Any, Callable, Iterable
import json
import uuid
from threading import Thread, Condition
import signal
from datetime import datetime as dt

from .execution.solver import DependencySolver
from .common.utils import PrivateInit
# from .compute_module import Item, ComputeModule, Params, JobContext, JobResult
from .execution.instances import JobInstance, ItemInstance
from .execution.modules import ComputeModule, Item, JobContext, JobResult, Params
from .execution.executors import Executor

class JobError(Exception):
     def __init__(self, message=""):
        self.message = message
        super().__init__(self.message)

class WorkflowState(PrivateInit):
    _FILE_NAME = 'workflow_state.json'
    def __init__(self, path: str|Path, steps: list[ComputeModule], **kwargs) -> None:
        super().__init__(_key=kwargs.get('_key'))
        self._ids: set[str] = set()
        self._job_instances: dict[str, JobInstance] = {}
        self._item_lookup: dict[str, list[ItemInstance]] = {}
        self._given_item_instances: list[str] = []

        self._pending_jobs: dict[str, JobInstance] = {}
        self._item_instance_reservations: dict[ItemInstance, set[JobInstance]] = {}

        self._steps = steps
        self._path = Path(path)
        self._changed = False

    def _register_item_inst(self, ii: ItemInstance):
        ilst = self._item_lookup.get(ii.item_name, [])
        ilst.append(ii)
        self._item_lookup[ii.item_name] = ilst

    def Save(self):
        if not self._changed: return
        jobs_by_step = {}
        for ji in self._job_instances.values():
            k = ji.step.name
            d = jobs_by_step.get(k, {})
            d[ji.GetID()] = ji.ToDict()
            jobs_by_step[k] = d

        item_instances = {}
        for k, instances in self._item_lookup.items():
            d = item_instances.get(k, {})
            for ii in instances: d[ii.GetID()] = ii.ToDict()
            item_instances[k] = d

        modules = {}
        for m in self._steps:
            md = {}
            ins = []
            for i in m.inputs:
                d = {"item": i.key}
                ins.append(d)
            md["in"] = ins
            md["input_groups"] = dict((k.key, v.key) for k, v in m._group_by.items())
            md["out"] = [i.key for i in m.outputs]
            if len(m.output_mask)>0: md["unused_out"] = [i.key for i in m.output_mask]
            modules[m.name] = md
        
        state = {
            "modules": modules,
            "module_executions": jobs_by_step,
            "item_instances": item_instances,
            "given": self._given_item_instances,
            "item_instance_reservations": dict((ii.GetID(), [ji.GetID() for ji in jis]) for ii, jis in self._item_instance_reservations.items()),
            "pending_jobs": list(self._pending_jobs),
        }
        with open(self._path.joinpath(self._FILE_NAME), 'w') as j:
            json.dump(state, j, indent=4)

    @classmethod
    def LoadFromDisk(cls, workspace: str|Path, steps: list[ComputeModule]):
        cm_ref = dict((c.name, c) for c in steps)
        workspace = Path(workspace)

        def _flatten(instances_by_type: dict):
            return [tup for g in [[(type, hash, data) for hash, data in insts.items()] for type, insts in instances_by_type.items()] for tup in g]

        with open(workspace.joinpath(cls._FILE_NAME)) as j:
            serialized_state = json.load(j)

            for name, md in serialized_state["modules"].items():
                ins = {Item(i["item"]) for i in md["in"]}
                outs = {Item(i) for i in md["out"]}
                cm = cm_ref[name]
                assert cm.inputs == ins
                assert cm.outputs == outs
                cm.output_mask = {Item(i) for i in md.get("unused_out", [])}

            job_instances: dict[str, JobInstance] = {}
            item_instances: dict[str, ItemInstance] = {}

            given = set(serialized_state["given"])
            todo_items = _flatten(serialized_state["item_instances"])
            todo_jobs = _flatten(serialized_state["module_executions"])
            job_outputs: dict[str, dict] = {}

            while len(todo_items)>0 or len(todo_jobs)>0:
                found = False
                while len(todo_items)>0:
                    item_name, id, obj = todo_items[0]
                    ii = ItemInstance.FromDict(item_name, id, obj, job_instances, given)
                    if ii is None: break
                    found = True
                    todo_items.pop(0)
                    item_instances[id] = ii
                
                while len(todo_jobs)>0:
                    module_name, id, obj = todo_jobs[0]
                    ji = JobInstance.FromDict(cm_ref[module_name], id, obj, item_instances)
                    if ji is None: break
                    found = True
                    todo_jobs.pop(0)
                    ji.complete = True
                    job_instances[id] = ji

                    outs = obj.get("outputs")
                    if outs is not None:
                        job_outputs[ji.GetID()] = outs

                if not found:
                    raise ValueError("failed to load state, the save may be corrupted")

            for jid, outs in job_outputs.items():
                outs = dict((ik, item_instances[v] if isinstance(v, str) else [item_instances[iik] for iik in v]) for ik, v in outs.items())
                job_instances[jid].AddOutputs(outs)

            state = WorkflowState(workspace, steps, _key=cls._initializer_key)
            state._given_item_instances = list(given)
            state._ids.update(item_instances)
            state._ids.update(job_instances)
            state._job_instances = job_instances
            state._pending_jobs = dict((k, job_instances[k]) for k in serialized_state["pending_jobs"])
            state._item_instance_reservations = dict(
                (item_instances[ik], {job_instances[rk] for rk in jids})
                for ik, jids in serialized_state["item_instance_reservations"].items()
            )

            for ii in item_instances.values():
                lst = state._item_lookup.get(ii.item_name, [])
                lst.append(ii)
                state._item_lookup[ii.item_name] = lst
            return state

    @classmethod
    def MakeNew(cls, workspace: str|Path, steps: list[ComputeModule], given: dict[Item, list[Path]]):
        assert len({m.name for m in steps})==len(steps), f"duplicate compute module name"
        state = WorkflowState(workspace, steps, _key=cls._initializer_key)
        for ii in [ItemInstance(state._gen_id, i, p) for i, ps in given.items() for p in ps]:
            state._register_item_inst(ii)
            state._given_item_instances.append(ii.GetID())

        produced: dict[Item, ComputeModule] = {}
        for step in steps:
            step.output_mask = set()
            for item in step.outputs:
                if item in produced:
                    print(f"[{item.key}] is already produced by [{produced[item].name}], masking this output of [{step.name}]")
                    step.MaskOutput(item)
                elif item in given:
                    print(f"[{item.key}] is given, masking this output of [{step.name}]")
                    step.MaskOutput(item)
                else:
                    produced[item] = step

        state.Update()
        return state

    @classmethod
    def ResumeIfPossible(cls, workspace: str|Path, steps: list[ComputeModule], given: dict[Item, list[Path]]):
        workspace = Path(workspace)
        if os.path.exists(workspace.joinpath(cls._FILE_NAME)):
            return WorkflowState.LoadFromDisk(workspace, steps)
        else:
            assert given is not None
            return WorkflowState.MakeNew(workspace, steps, given)

    def _gen_id(self, id_len: int):
        while True:
            id = uuid.uuid4().hex[:id_len]
            if id not in self._ids: break 
        self._ids.add(id)
        return id

    def _satisfies(self, module: ComputeModule):
        for i in module.inputs:
            if i.key not in self._item_lookup: return False
            haves = self._item_lookup[i.key]
            reserved = 0
            for ii in haves:
                if ii in self._item_instance_reservations:
                    jis = self._item_instance_reservations[ii]
                    if any(ji.step.name == module.name for ji in jis):
                        reserved += 1
            if reserved >= len(haves): return False
        return True

    def GetPendingJobs(self):
        return list(self._pending_jobs.values())        

    def _group_by(self, item_name: str, by_name: str):
        def _next_steps(step: ComputeModule):
            outs = step.GetUnmaskedOutputs()
            for cm in self._steps:
                if any((i in outs) for i in cm.inputs):
                    yield cm

        for ji in self._pending_jobs.values():
            step = ji.step
            steps_to_check = [step]
            while len(steps_to_check) > 0:
                curr = steps_to_check.pop()
                if any(i.key==item_name for i in curr.GetUnmaskedOutputs()):
                    return {} # still pending
                steps_to_check += _next_steps(curr)
        
        _parent_cache = {}
        groups: dict[ItemInstance, set[ItemInstance]] = {}
        if item_name == by_name: return dict((i, {i}) for i in self._item_lookup[item_name])

        one_path: list[ItemInstance|JobInstance] = []
        def _handle_base_case(candidate: ItemInstance, member: ItemInstance, path: list[ItemInstance|JobInstance]):
            if candidate in _parent_cache:
                parent = _parent_cache[candidate]
            elif candidate.item_name == by_name:
                nonlocal one_path
                if len(one_path)==0: one_path = path
                parent = candidate
            else:
                return False

            grp = groups.get(parent, set())
            grp.add(member)
            groups[parent] = grp
            for inst in path:
                _parent_cache[inst] = parent
            return True

        todo: list[tuple[ItemInstance, ItemInstance, list[ItemInstance|JobInstance]]] = [
            (ii, ii, [ii]) for ii in self._item_lookup[item_name]
        ]
        while len(todo)>0:
            original, ii, path = todo.pop() # dfs to maximize utility of parent cache
            if _handle_base_case(ii, original, path): continue

            ji = ii.made_by
            if ji is None: continue # is original input
            consumed = ji.ListInputInstances()
            todo += [(original, in_i, path+[ji, in_i]) for in_i in consumed]

        return groups

    def Update(self):
        for module in self._steps:
            if not self._satisfies(module): continue
            class _namespace:
                def __init__(self) -> None:
                    self.space: dict[str, ItemInstance|list[ItemInstance]] = {}
                    self.grouped_by: set[ItemInstance] = set()

                def Copy(self):
                    new = _namespace()
                    new.space = self.space.copy()
                    new.grouped_by = self.grouped_by.copy()
                    return new

            class Namespaces:
                def __init__(self) -> None:
                    self.namespaces: list[_namespace] = [_namespace()]

                def AddMapping(self, i: str, inst_or_list: ItemInstance|list[ItemInstance]):
                    for ns in self.namespaces:
                        ns.space[i] = inst_or_list

                # assumes all instances are of the same item
                def Split(self, groups: list[list[ItemInstance]], grouped_bys: list[ItemInstance]|None=None):
                    def _kv(grp: list[ItemInstance]):
                        rep = grp[0].item_name
                        return (rep, grp if len(grp)>1 else grp[0])

                    if len(groups) == 1:
                        grp = groups[0]
                        self.AddMapping(*_kv(grp))

                    if grouped_bys is None:
                        _grouped_bys = [None for _ in groups]
                    else:
                        assert len(grouped_bys) == len(groups), f"|group by| != |groups| {grouped_bys}, {groups}"
                        _grouped_bys = grouped_bys

                    new_nss = []
                    for ns in self.namespaces:
                        for gb, grp in zip(_grouped_bys, groups):
                            clone = ns.Copy()
                            if gb is not None:
                                clone.grouped_by.add(gb)
                                ns.grouped_by.add(gb)
                            rep, v = _kv(grp)
                            clone.space[rep] = v
                            new_nss.append(clone)
                    self.namespaces = new_nss

                def MergeGroups(self, item_name: str, groups: dict[ItemInstance, set[ItemInstance]]):
                    found = False
                    for gb, group in groups.items():
                        for ns in self.namespaces:
                            if gb in ns.grouped_by:
                                found = True
                                ns.space[item_name] = list(group)
                                break

                    if not found:
                        self.Split([list(g) for g in groups.values()], [k for k in groups])

            namespaces = Namespaces()
            outer_continue = False
            for item in module.inputs:
                item_name = item.key
                instances: list[ItemInstance] = []
                for inst in self._item_lookup[item_name]:
                    jis = self._item_instance_reservations.get(inst, set())
                    if any(ji.step == module for ji in jis): continue
                    instances.append(inst)

                if len(instances) == 0: # outer break!
                    outer_continue=True; break

                have_array = len(instances)>1
                item_grouped_by = module.Grouped(item)
                want_array = item_grouped_by is not None
                    
                ## join & group by ##
                if want_array:
                    groups = self._group_by(item_name, item_grouped_by.key)
                    if len(groups) == 0:
                        outer_continue=True; break

                    for k in list(groups):
                        grp = groups[k]
                        if any(module in [ji.step for ji in self._item_instance_reservations.get(ii, set())] for ii in grp):
                            del groups[k]

                    namespaces.MergeGroups(item_name, groups)

                ## split, 1 each ##
                elif not want_array and have_array:
                    namespaces.Split([[i] for i in instances])

                ## 1 to 1 ##
                elif not want_array and not have_array:
                    namespaces.AddMapping(item_name, instances[0])

            if outer_continue: continue
            for ns in namespaces.namespaces:
                job_inst = JobInstance(self._gen_id, module, ns.space)
                self._pending_jobs[job_inst.GetID()] = job_inst
                self._job_instances[job_inst.GetID()] = job_inst
                for ii in job_inst.ListInputInstances():
                    lst = self._item_instance_reservations.get(ii, set())
                    lst.add(job_inst)
                    self._item_instance_reservations[ii] = lst
            self._changed = True

    def RegisterJobComplete(self, job_id: str, created: dict[Item, Any]):
        del self._pending_jobs[job_id]
        job_inst = self._job_instances[job_id]
        job_inst.complete = True

        expected_outputs = job_inst.step.GetUnmaskedOutputs()
        outs: dict[str, ItemInstance|list[ItemInstance]] = {}
        for item, vals in created.items():
            if item not in expected_outputs: continue
            if not isinstance(vals, list): vals = [vals]
            insts = []
            for value in vals:
                inst = ItemInstance(self._gen_id, item, value, made_by=job_inst)
                self._register_item_inst(inst)
                insts.append(inst)
            outs[item.key] = insts if len(insts)>1 else insts[0]
        job_inst.AddOutputs(outs)

class Sync:
    def __init__(self) -> None:
        self.lock = Condition()
        self.queue = []

    def PushNotify(self, item: JobResult|None=None):
        with self.lock:
            self.queue.append(item)
            self.lock.notify()

    def WaitAll(self) -> list[JobResult|None]:
        with self.lock:
            if len(self.queue)==0:
                self.lock.wait()

            results = self.queue.copy()
            self.queue.clear()
            return results

class TerminationWatcher:
  kill_now = False
  def __init__(self, sync: Sync):
    signal.signal(signal.SIGINT, self.exit_gracefully)
    signal.signal(signal.SIGTERM, self.exit_gracefully)
    self.sync = sync

  def exit_gracefully(self, *args):
    print('stop requested')
    self.kill_now = True
    self.sync.PushNotify()

class Workflow:
    INPUT_DIR = Path("inputs")
    def __init__(self, compute_modules: list[ComputeModule]|Path|str, reference_folder: Path|str) -> None:
        if isinstance(compute_modules, Path) or isinstance(compute_modules, str):
            compute_modules = ComputeModule.LoadSet(compute_modules)

        self._compute_modules = compute_modules
        self._reference_folder = Path(os.path.abspath(reference_folder))
        if not self._reference_folder.exists():
            os.makedirs(self._reference_folder)
        else:
            assert os.path.isdir(self._reference_folder), f"reference folder path exists, but is not a folder: {self._reference_folder}"
        self._solver = DependencySolver([c.GetTransform() for c in compute_modules])

    def Setup(self, install_type: str):
        for step in self._compute_modules:
            step.Setup(self._reference_folder, install_type)

    def _calculate(self, given: Iterable[Item], targets: Iterable[Item]):
        given_k = {x.key for x in given}
        targets_k = {x.key for x in targets} 
        steps, dep_map = self._solver.Solve(given_k, targets_k)
        return steps, dep_map

    def _check_feasible(self, steps: list[ComputeModule], targets: Iterable[Item], dep_map: dict[str, list[ComputeModule]]):
        targets = set(targets)
        products = set()
        for cm in steps:
            products = products.union(cm.outputs)
        missing = targets - products
        assert missing == set(), f"no module produces these items [{', '.join(str(i) for i in missing)}]"

        for cm in steps:
            deps = dep_map[cm.name]
            for i, g in cm._group_by.items():
                assert any(g in pre.inputs for pre in deps), f"invalid grouping: [{g.key}] is not upstream of [{i.key}] for module [{cm.name}]"

    def Run(self, workspace: str|Path, targets: Iterable[Item],
        given: dict[Item, str|Path|list[str|Path]],
        executor: Executor, params: Params=Params(),
        _catch_errors: bool = True):
        if isinstance(workspace, str): workspace = Path(os.path.abspath(workspace))
        if not workspace.exists():
            os.makedirs(workspace)
        params.reference_folder = self._reference_folder

        # abs. path before change to working dir
        sys.path = [os.path.abspath(p) for p in sys.path]
        abs_path_if_path = lambda p: Path(os.path.abspath(p)) if isinstance(p, Path) else p
        abs_given = dict((k, [abs_path_if_path(p) for p in v] if isinstance(v, list) else [abs_path_if_path(v)]) for k, v in given.items())

        def _timestamp():
            return f"{dt.now().strftime('%H:%M:%S')}>"

        sync = Sync()
        watcher = TerminationWatcher(sync)
        def _run_job_async(jobi: JobInstance, procedure: Callable[[], JobResult]):
            def _job():
                try:
                    result = procedure()
                except Exception as e:
                    result = JobResult(
                        exit_code = 1,
                        error_message = str(e),
                        made_by = jobi.GetID(),
                    )
                sync.PushNotify(result)
        
            th = Thread(target=_job)
            th.start()

        def _run():
            # make links for inputs in workspace
            input_dir = self.INPUT_DIR
            os.makedirs(input_dir, exist_ok=True)
            inputs: dict[Item, list[Path]] = {}
            for item, values in abs_given.items():
                parsed = []
                for p in values:
                    if isinstance(p, str):
                        parsed.append(p)
                        continue
                    assert os.path.exists(p), f"given [{p}] doesn't exist"
                    linked = input_dir.joinpath(p.name)
                    if linked.exists(): os.remove(linked)
                    os.symlink(p, linked)
                    parsed.append(linked)
                inputs[item] = parsed
            _steps, dep_map = self._calculate(inputs, targets)
            if _steps is False:
                print(f'no solution exists')
                return
            steps: list[ComputeModule] = [s.reference for s in _steps]
            self._check_feasible(steps, targets, dep_map)
            state = WorkflowState.ResumeIfPossible('./', steps, inputs)
            state.Save()

            if len(state.GetPendingJobs()) == 0:
                print(f'nothing to do')
                return

            print(f'linearized plan: [{" -> ".join(s.name for s in steps)}]')
            executor.PrepareRun(steps, self.INPUT_DIR, params)

            jobs_ran: dict[str, JobInstance] = {}
            while not watcher.kill_now:
                pending_jobs = state.GetPendingJobs()
                if len(pending_jobs) == 0: break

                for job in pending_jobs:
                    if watcher.kill_now:
                        raise KeyboardInterrupt()

                    jid = job.GetID()
                    if jid in jobs_ran: continue
                    header = f"{_timestamp()} {job.step.name}:{jid}"
                    print(f"{header} started")
                    _run_job_async(job, lambda: executor.Run(job, workspace, params.Copy()))
                    jobs_ran[jid] = job

                try:
                    for result in sync.WaitAll():
                        if result is None:
                            raise KeyboardInterrupt()
                        job_instance = jobs_ran[result.made_by]
                        header = f"{_timestamp()} {job_instance.step.name}:{result.made_by}"
                        if not result.error_message is None:
                            print(f"{header} failed: [{result.error_message}]")
                        else:
                            print(f"{header} completed")
                        state.RegisterJobComplete(result.made_by, result.manifest)
                except KeyboardInterrupt:
                    print("force stopped")
                    return
                state.Update()
                state.Save()

        original_dir = os.getcwd()
        def _wrap_and_run():
            os.makedirs(workspace, exist_ok=True)
            os.chdir(workspace)
            _run()
            print("done")

        if not _catch_errors:
            _wrap_and_run()
            os.chdir(original_dir)
        else:
            try:
                _wrap_and_run()
            except Exception as e:
                print(f"ERROR: {e}")
            finally:
                os.chdir(original_dir)
 