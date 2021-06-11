from pathlib import Path
from typing import Dict, List, Optional
from pydantic import FilePath
from pydantic.errors import PathNotAFileError
from fastapi import HTTPException, UploadFile, File

from jina import __default_host__, Flow
from jina.enums import PeaRoleType, SocketType
from jina.helper import ArgNamespace, cached_property, random_port

from .. import __dockerhost__
from ..helper import get_workspace_path
from ..models.enums import WorkspaceState
from ..stores import workspace_store as store
from ..models import DaemonID, FlowModel, PodModel, PeaModel


class FlowDepends:
    def __init__(self, workspace_id: DaemonID, filename: str) -> None:
        self.workspace_id = workspace_id
        self.filename = filename
        self.id = DaemonID('jflow')
        self.params = FlowModel(
            uses=self.filename, workspace_id=self.workspace_id.jid, identity=self.id
        )
        self.validate()

    def localpath(self) -> Path:
        try:
            return FilePath.validate(
                Path(get_workspace_path(self.workspace_id, self.filename))
            )
        except PathNotAFileError as e:
            raise HTTPException(
                status_code=404,
                detail=f'File `{self.filename}` not found in workspace `{self.workspace_id}`',
            )

    @cached_property
    def port_expose(self):
        """
        Sets `port_expose` for the Flow started in `mini-jinad`.
        NOTE: this port needs to be exposed before starting `mini-jinad`, hence set here.
        """
        f = Flow.load_config(str(self.localpath()))
        return f.args.port_expose

    def validate(self):
        self.params.port_expose = self.port_expose
        self.ports = {f'{self.port_expose}/tcp': self.port_expose}


class PeaDepends:
    _kind = 'pea'

    def __init__(self, workspace_id: DaemonID, pea: PeaModel):
        # Deepankar: adding quotes around PeaModel breaks things
        self.workspace_id = workspace_id
        self.params = pea
        self.id = DaemonID('jpea')
        self.validate()

    @property
    def host_in(self):
        """
        DOCKER_HOST = 'host.docker.internal'

        SINGLETON
        =========
        `runtime_cls`: `ZEDRuntime`
            `host_in`, `host_out`: set to `DOCKER_HOST`, as it would talk to gateway/other pods
            `ports`: map `port_in` & `port_out` if `socket_in` & `socket_out` are BIND.
                     map `port_ctrl`, ignore `port_exppse`

        `runtime_cls`: `ContainerRuntime`
            `host_in`,`host_out`: don't change. Handled interally in `jina pod`
            `ports`: handled internally in `ContainerRuntime`

        PARALLEL
        ========
        `runtime_cls`: `ZEDRuntime`
            `host_in`, `host_out`: set to `DOCKERHOST`
                host config handled interally in `jina pod` wouldn't work here, as they need to talk to head/tail
                peas which are on `DOCKER_HOST`
            `ports`: don't map `port_in` & `port_out` as they're always CONNECT.
                     map `port_ctrl`??, ignore `port_exppse`

        `runtime_cls`: `ContainerRuntime` or `ZEDRuntime`
            `host_in`, `host_out`: set to `DOCKERHOST`
                host config handled interally in `jina pod` wouldn't work here, as they need to talk to head/tail
                peas which are on `DOCKER_HOST`
            `ports`: handled internally in `ContainerRuntime`

        HEAD/TAIL
        =========
        `runtime_cls`:  `ZEDRuntime` (always??)
            `host_in`, `host_out`: set to `DOCKER_HOST`, as they would talk to gateway/other pods.
            `ports`: map `port_in` & `port_out` if `socket_in` & `socket_out` are BIND.
                     map `port_ctrl`, ignore `port_exppse`

        TODO: check the host_in/host_out for CONNECT sockets
        TODO: HEAD/TAIL - can `uses_before` or `uses_after` be `docker://`
        """
        return (
            __dockerhost__
            if self.params.runtime_cls == 'ZEDRuntime'
            or PeaRoleType.from_string(self.params.pea_role) == PeaRoleType.PARALLEL
            else self.params.host_in
        )

    @property
    def host_out(self):
        return (
            __dockerhost__
            if self.params.runtime_cls == 'ZEDRuntime'
            or PeaRoleType.from_string(self.params.pea_role) == PeaRoleType.PARALLEL
            else self.params.host_in
        )

    @cached_property
    def ports(self) -> Dict:
        _mapping = {
            'port_in': 'socket_in',
            'port_out': 'socket_out',
            'port_ctrl': 'socket_ctrl',
        }
        # Map only "bind" ports for HEAD, TAIL & SINGLETON
        if self.params.runtime_cls == 'ContainerRuntime':
            # For `ContainerRuntime`, port mapping gets handled internally
            return {}
        if PeaRoleType.from_string(self.params.pea_role) != PeaRoleType.PARALLEL:
            return {
                f'{getattr(self.params, i)}/tcp': getattr(self.params, i)
                for i in self.params.__fields__
                if i in _mapping
                and SocketType.from_string(
                    getattr(self.params, _mapping[i], 'PAIR_BIND')
                ).is_bind
            }
        else:
            return {f'{self.params.port_ctrl}/tcp': self.params.port_ctrl}

    def validate(self):
        # Each pea is inside a container
        self.params.host_in = self.host_in
        self.params.host_out = self.host_out
        self.params.identity = self.id
        self.params.workspace_id = self.workspace_id


class PodDepends(PeaDepends):
    def __init__(self, workspace_id: DaemonID, pod: PodModel):
        self.workspace_id = workspace_id
        self.params = pod
        self.validate()
        self.id = DaemonID('jpod')


class WorkspaceDepends:
    def __init__(
        self, id: Optional[DaemonID] = None, files: List[UploadFile] = File(None)
    ) -> None:
        self.id = id if id else DaemonID('jworkspace')
        self.files = files

        from ..tasks import __task_queue__

        if self.id not in store:
            # when id doesn't exist in store, create it.
            store.add(id=self.id, value=WorkspaceState.PENDING)
            __task_queue__.put((self.id, self.files))

        if self.id in store and store[self.id].state == WorkspaceState.ACTIVE:
            # when id exists in the store and is "active", update it.
            store.update(id=self.id, value=WorkspaceState.PENDING)
            __task_queue__.put((self.id, self.files))

        self.j = {self.id: store[self.id]}
