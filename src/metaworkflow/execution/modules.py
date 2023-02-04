from __future__ import annotations
import os, sys
from pathlib import Path
import shutil
import importlib
from typing import Callable, Iterable, Any, Literal
import json

from .solver import Transform
from ..common.utils import AutoPopulate, PrivateInit

class Item:
    _hashes: dict[str, int] = {}
    _last_hash = 0

    def __repr__(self) -> str:
        return f'<i:{self.key}>'

    def __init__(self, key: str) -> None:
        self.key = key
        if key in Item._hashes:
            self._hash = Item._hashes[key]
        else:
            Item._last_hash += 1
            self._hash = Item._last_hash
            Item._hashes[key] = self._hash

    def __eq__(self, __o: object) -> bool:
        if not isinstance(__o, Item): return False
        return __o.key == self.key

    def __hash__(self) -> int:
        return self._hash

ManifestDict = dict[Item, Path]

class Params:
    def __init__(self,
        file_system_wait_sec: int=5,
        threads: int=4,
        mem_gb: int=8,
    ) -> None:
        self.file_system_wait_sec = file_system_wait_sec
        self.threads = threads
        self.mem_gb = mem_gb

    def Copy(self):
        cp = Params(**self.__dict__)
        return cp

    def ToDict(self):
        return self.__dict__

    @classmethod
    def FromDict(cls, d: dict):
        p = Params()
        for k, v in d.items():
            setattr(p, k, v)
        return p

class JobContext(AutoPopulate):
    __FILE_NAME = 'context.json'
    __BL = {'shell', 'output_folder', 'lib', 'ref'}
    shell_prefix: str
    params: Params
    shell: Callable[[str], int]
    output_folder: Path
    manifest: dict[Item, Path|list[Path]]
    job_id: str
    lib: Path
    ref: Path

    def Save(self, workspace: Path):
        folder = workspace.joinpath(self.output_folder)
        if not folder.exists(): os.makedirs(folder)
        with open(folder.joinpath(JobContext.__FILE_NAME), 'w') as j:
            d = {}
            for k, v in self.__dict__.items():
                if k.startswith('_'): continue
                if k in self.__BL: continue
                v: Any = v
                if v is None: continue
                v = { # switch
                    'shell': lambda: None,
                    'params': lambda: v.ToDict(),
                    'manifest': lambda: dict((mk.key, [str(p) for p in mv] if isinstance(mv, list) else str(mv)) for mk, mv in v.items()),
                }.get(k, lambda: str(v))()
                d[k] = v
            json.dump(d, j, indent=4)
            return d

    @classmethod
    def LoadFromDisk(cls, output_folder: Path):
        with open(output_folder.joinpath(JobContext.__FILE_NAME)) as j:
            d = json.load(j)
            kwargs = {}
            for k in d:
                if k in cls.__BL: continue
                v: Any = d[k]
                v = { # switch
                    'shell': lambda: None,
                    'params': lambda: Params.FromDict(v),
                    'output_folder': lambda: Path(v),
                    'manifest': lambda: dict((Item(mk), [Path(p) for p in mv] if isinstance(mv, list) else Path(mv)) for mk, mv in v.items()),
                }.get(k, lambda: str(v))()
                kwargs[k] = v
            if 'shell_prefix' not in d:
                kwargs['shell_prefix'] = ''
            if 'output_folder' not in d:
                kwargs['output_folder'] = output_folder
            return JobContext(**kwargs)

class JobResult(AutoPopulate):
    commands: list[str]
    error_message: str|None
    made_by: str
    manifest: dict[Item, Path|list[Path]]
    resource_log: list[str]
    err_log: list[str]
    out_log: list[str]

    def ToDict(self):
        d = {}
        for k, v in self.__dict__.items():
            v: Any = v
            if v is None: continue
            v = { # switch
                "manifest": lambda: dict((mk.key, [str(p) for p in mv] if isinstance(mv, list) else str(mv)) for mk, mv in v.items()),
            }.get(k, lambda: v)()
            d[k] = v
        return d

    @classmethod
    def FromDict(cls, d: dict):
        man_dict = lambda v: dict((Item(mk), [Path(p) for p in mv] if isinstance(mv, list) else Path(mv)) for mk, mv in v.items())
        kwargs = {}
        for k in d:
            v: Any = d[k]
            v = { # switch
                "exit_code": lambda: int(v),
                "manifest": lambda: man_dict(v) if isinstance(v, dict) else [man_dict(x) for x in v],
            }.get(k, lambda: v)()
            kwargs[k] = v
        return JobResult(**kwargs)

class ModuleExistsError(FileExistsError):
    pass

class ComputeModule(PrivateInit):
    DEFINITION_FILE_NAME = 'definition.py'
    _group_by: dict[Item, Item] # key grouped by val
    def __init__(self,
        procedure: Callable[[JobContext], JobResult],
        inputs: set[Item],
        group_by: dict[Item, Item],
        outputs: set[Item],
        location: str|Path,
        name: str|None = None,
        threads: int|None = None,
        memory_gb: int|None = None,
        **kwargs
    ) -> None:

        super().__init__(_key=kwargs.get('_key'))
        self.name = procedure.__name__ if name is None else name
        assert self.name != ""
        assert len(inputs.intersection(outputs)) == 0
        self.inputs = inputs
        self._group_by = group_by
        self.outputs = outputs
        self._procedure = procedure
        self.location = Path(location).absolute()
        self.output_mask: set[Item] = set()
        self.threads = threads
        self.memory_gb = memory_gb

    def Grouped(self, item: Item):
        return self._group_by.get(item)

    @classmethod
    def LoadSet(cls, modules_path: str|Path):
        modules_path = Path(modules_path)
        compute_modules = []
        for dir in os.listdir(modules_path):
            mpath = modules_path.joinpath(dir)
            if not os.path.isdir(mpath): continue
            try:
                m = ComputeModule._load(mpath)
                compute_modules.append(m)
            except AssertionError:
                print(f"[{dir}] failed to load")
                continue
        return compute_modules

    @classmethod
    def _load(cls, folder_path: str|Path):
        folder_path = Path(os.path.abspath(folder_path))
        name = str(folder_path).split('/')[-2] # the folder name

        err_msg = f"module [{name}] at [{folder_path}] appears to be corrupted"
        assert os.path.exists(folder_path), err_msg
        assert os.path.isfile(folder_path.joinpath(f'lib/{cls.DEFINITION_FILE_NAME}')), err_msg
        # assert os.path.isfile(folder_path.joinpath("__main__.py")), err_msg
        original_path = sys.path
        # os.chdir(folder_path.joinpath('..'))
        sys.path = [str(folder_path.joinpath('lib'))]+sys.path
        try:
            import definition as mo # type: ignore
            importlib.reload(mo)

            module: ComputeModule = mo.MODULE

            return module
        except ImportError:
            raise ImportError(err_msg)
        finally:
            sys.path = original_path

    def __repr__(self) -> str:
        return f'<m:{self.name}>'

    def __eq__(self, __o: object) -> bool:
        if not isinstance(__o, ComputeModule): return False
        return self.name == __o.name

    def MaskOutput(self, item: Item):
        if item in self.outputs: self.output_mask.add(item)

    def GetUnmaskedOutputs(self):
        return self.outputs.difference(self.output_mask)

    # this is getting outdated
    def GetTransform(self):
        if Transform.Exists(self.name):
            return Transform.Get(self.name)
        else:
            return Transform.Create(
                {x.key for x in self.inputs},
                {x.key for x in self.outputs},
                unique_name=self.name,
                reference=self,
            )

class ModuleBuilder:
    _groupings: dict[Item, Item]
    _inputs: set[Item]
    _outputs: set[Item]
    _location: Path
    _name: str
    _threads: int
    _memory_gb: int

    def __init__(self) -> None:
        self._groupings = {}
        self._inputs = set()
        self._outputs = set()

    def SetProcedure(self, procedure: Callable[[JobContext], JobResult]):
        self._procedure = procedure
        return self

    def AddInput(self, input: Item, groupby: Item|None=None):
        assert input not in self._inputs, f"{input} already added"
        self._inputs.add(input)
        if groupby is not None:
            self._groupings[input] = groupby
        return self

    def PromiseOutput(self, output: Item):
        assert output not in self._outputs, f"{output} already added"
        self._outputs.add(output)
        return self

    def SetHome(self, definition_file: str, name: str|None=None):
        def_path = Path(definition_file)
        assert def_path.exists(), f"{def_path} doesn't exist"
        toks = definition_file.split('/')
        assert toks[-1] == ComputeModule.DEFINITION_FILE_NAME, f"the module's definition file must be named {ComputeModule.DEFINITION_FILE_NAME}"
        if name is None:
            assert len(toks)>=3 and toks[-3] != "", f"can't infer name from {def_path}"
            name = toks[-3]
        self._name = name
        self._location = Path(os.path.abspath(def_path.joinpath('../..')))
        return self

    def SuggestedResources(self, threads: int, memory_gb: int):
        assert threads>0
        assert memory_gb>0
        self._threads = threads
        self._memory_gb = memory_gb
        return self

    def Build(self):
        assert len(self._outputs) > 0, f"module has no outputs and so is not useful"
        cm = ComputeModule(
            _key=ComputeModule._initializer_key,
            procedure=self._procedure,
            inputs=self._inputs,
            group_by=self._groupings,
            outputs=self._outputs,
            location=self._location,
            name=self._name,
        )
        return cm

    @classmethod
    def GenerateTemplate(cls, 
        modules_folder: str|Path,
        name: str,
        on_exist: Literal['error']|Literal['overwrite']|Literal['skip']='error'):
        modules_folder = Path(modules_folder)

        name = name.replace('/', '_').replace(' ', '-')
        module_root = Path.joinpath(modules_folder, name)

        def _make_folders():
            os.makedirs(module_root.joinpath('lib'))
            os.makedirs(module_root.joinpath('ref'))

        if os.path.exists(module_root):
            if on_exist=='overwrite':
                shutil.rmtree(module_root, ignore_errors=True)
                _make_folders()
            elif on_exist=='error':
                raise ModuleExistsError(f"module [{name}] already exists at [{modules_folder}]")
            elif on_exist=='skip':
                print(f'module [{name}] already exits! skipping...')
                return ComputeModule._load(module_root)
        else:
            _make_folders()

        try:
            HERE = Path('/'.join(os.path.realpath(__file__).split('/')[:-1]))
        except NameError:
            HERE = Path(os.getcwd())
        
        template_file_name = 'template_module_definition.py'
        shutil.copy(HERE.joinpath(template_file_name), module_root.joinpath('lib').joinpath(ComputeModule.DEFINITION_FILE_NAME))
        for path, dirs, files in os.walk(module_root):
            for f in files:
                os.chmod(os.path.join(path, f), 0o775)

        return ComputeModule._load(module_root)