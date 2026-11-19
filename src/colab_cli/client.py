# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import abc
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Union
from urllib.parse import urljoin, urlparse

import requests
from pydantic import BaseModel, Field, TypeAdapter

from colab_cli.consumption import (
    CcuInfo,
    ConsumptionUserInfo,
    consumption_user_info_from_tunnel,
)
from colab_cli.utils import get_status_code

# Standard Colab Headers
ACCEPT_JSON_HEADER = {"key": "Accept", "value": "application/json"}
COLAB_CLIENT_AGENT_HEADER = {
    "key": "X-Colab-Client-Agent",
    "value": "colab-cli",
}
COLAB_XSRF_TOKEN_HEADER = {"key": "X-Goog-Colab-Token", "value": ""}


@dataclass
class ColabEnvironment(abc.ABC):
    domain: str
    api: str
    public_api: str = "https://colaboratory.googleapis.com"


@dataclass
class Prod(ColabEnvironment):
    domain: str = "https://colab.research.google.com"
    api: str = "https://colab.pa.googleapis.com"
    public_api: str = "https://colaboratory.googleapis.com"


def uuid_to_web_safe_base64(uuid_val: uuid.UUID) -> str:
    uuid_str = str(uuid_val)
    transformed = uuid_str.replace("-", "_")
    padding = "." * (44 - len(uuid_str))
    return transformed + padding


class Accelerator(str, Enum):
    NONE = "NONE"
    G4 = "G4"
    T4 = "T4"
    L4 = "L4"
    A100 = "A100"
    H100 = "H100"
    V5E1 = "V5E1"
    V6E1 = "V6E1"


class Variant(str, Enum):
    DEFAULT = "DEFAULT"
    GPU = "GPU"
    TPU = "TPU"


class AssignmentVariant(int, Enum):
    DEFAULT = 0
    GPU = 1
    TPU = 2


class Shape(int, Enum):
    STANDARD = 0
    HIGH_RAM = 1


# Accelerators that only exist in a single (high-memory) shape; the assign
# endpoint ignores shape=hm for these (colab-vscode forces STANDARD).
HIGH_MEM_ONLY_ACCELERATORS = frozenset(
    {Accelerator.L4, Accelerator.V5E1, Accelerator.V6E1}
)


def resolve_assign_shape(
    accelerator: Optional[Accelerator],
    *,
    high_mem: bool = False,
) -> Optional[Shape]:
    """Map CLI intent to the shape query param for /tun/m/assign.

    Returns ``Shape.HIGH_RAM`` when high memory was requested and the
    accelerator supports a choice; otherwise ``None`` (omit the URL param).
    """
    if not high_mem:
        return None
    if accelerator in HIGH_MEM_ONLY_ACCELERATORS:
        return None
    return Shape.HIGH_RAM


def shape_display_label(shape: Union[Shape, str, int, None]) -> str:
    """Human-friendly label for sessions/status output."""
    if shape in (Shape.HIGH_RAM, "HIGH_RAM", 1):
        return "High-RAM"
    return "Standard"


class RuntimeProxyInfo(BaseModel):
    token: str
    token_expires_in_seconds: int = Field(..., alias="tokenExpiresInSeconds")
    url: str

    def expires_at(self) -> datetime:
        """Absolute expiry; call right after receiving the response."""
        return datetime.now(timezone.utc) + timedelta(
            seconds=self.token_expires_in_seconds
        )


class ListedAssignment(BaseModel):
    accelerator: Accelerator
    endpoint: str = ""
    # Public API runtime resource name (e.g. "runtimes/<id>"); may be empty
    # when listed via the legacy tunnel API.
    name: str = ""
    variant: AssignmentVariant
    machine_shape: Shape = Field(..., alias="machineShape")
    runtime_proxy_info: RuntimeProxyInfo = Field(..., alias="runtimeProxyInfo")


class ListedAssignments(BaseModel):
    assignments: List[ListedAssignment]


class PublicVariant(str, Enum):
    VARIANT_UNSPECIFIED = "VARIANT_UNSPECIFIED"
    VARIANT_CPU = "VARIANT_CPU"
    VARIANT_GPU = "VARIANT_GPU"
    VARIANT_TPU = "VARIANT_TPU"


class PublicShape(str, Enum):
    SHAPE_UNSPECIFIED = "SHAPE_UNSPECIFIED"
    SHAPE_STANDARD = "SHAPE_STANDARD"
    SHAPE_HIGHMEM = "SHAPE_HIGHMEM"


class ConnectionInfo(BaseModel):
    expire_time: str = Field(..., alias="expireTime")
    endpoint: Optional[str] = None
    url: str
    token: str


class RuntimeSpecKey(BaseModel):
    variant: PublicVariant
    accelerator: str
    shape: PublicShape


class Runtime(BaseModel):
    name: str
    version: Optional[str] = None
    connection_info: ConnectionInfo = Field(..., alias="connectionInfo")
    runtime_spec: RuntimeSpecKey = Field(..., alias="runtimeSpec")


class ListRuntimesResponse(BaseModel):
    runtimes: Optional[List[Runtime]] = None


class Status(BaseModel):
    code: Optional[int] = None
    message: Optional[str] = None
    details: Optional[List[Dict]] = None


class Operation(BaseModel):
    name: Optional[str] = None
    done: Optional[bool] = None
    metadata: Optional[Dict] = None
    error: Optional[Status] = None
    response: Optional[Dict] = None


class PostAssignmentResponse(BaseModel):
    accelerator: Accelerator
    endpoint: str
    runtime_name: Optional[str] = None
    runtime_proxy_info: RuntimeProxyInfo = Field(..., alias="runtimeProxyInfo")
    variant: AssignmentVariant


class GetAssignmentResponse(BaseModel):
    acc: str = Field(..., alias="acc")
    nbh: str = Field(..., alias="nbh")
    token: str = Field(..., alias="token")
    variant: Variant = Field(..., alias="variant")


class GetUnassignRequest(BaseModel):
    token: str


class Assignment(BaseModel):
    endpoint: str
    runtime_name: Optional[str] = None
    runtime_proxy_info: RuntimeProxyInfo = Field(..., alias="runtimeProxyInfo")


def normalize_proxy_url(url: object) -> str:
    return url.rstrip("/") if isinstance(url, str) else ""


def proxy_info_from_connection(conn: ConnectionInfo) -> RuntimeProxyInfo:
    expires_in = 3600
    try:
        dt = datetime.fromisoformat(conn.expire_time.replace("Z", "+00:00"))
        expires_in = max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        pass
    return RuntimeProxyInfo(
        token=conn.token,
        tokenExpiresInSeconds=expires_in,
        url=conn.url,
    )


def _map_public_runtime_spec(
    spec: RuntimeSpecKey,
) -> tuple[AssignmentVariant, Accelerator, Shape]:
    shape = (
        Shape.HIGH_RAM if spec.shape == PublicShape.SHAPE_HIGHMEM else Shape.STANDARD
    )
    if spec.variant == PublicVariant.VARIANT_GPU:
        variant = AssignmentVariant.GPU
    elif spec.variant == PublicVariant.VARIANT_TPU:
        variant = AssignmentVariant.TPU
    else:
        variant = AssignmentVariant.DEFAULT
    try:
        accelerator = Accelerator(spec.accelerator)
    except ValueError:
        accelerator = Accelerator.NONE
    return variant, accelerator, shape


_GRPC_TO_HTTP_STATUS = {
    3: 400,
    4: 504,
    5: 404,
    6: 409,
    7: 403,
    8: 429,
    9: 412,
    16: 401,
}


XSSI_PREFIX = ")]}'\n"
TUN_ENDPOINT = "/tun/m"


class ColabRequestError(Exception):
    def __init__(self, message, request, response, response_body=None):
        super().__init__(message)
        self.request = request
        self.response = response
        self.response_body = response_body


class TooManyAssignmentsError(Exception):
    pass


class Client:
    def __init__(
        self,
        env: ColabEnvironment,
        session,
        logger=None,
        use_public_api: bool = False,
        settings_store=None,
    ):
        self.colab_domain = env.domain
        self.colab_api_domain = env.api
        self.colab_public_api = env.public_api
        self.session = session
        self.logger = logger or logging.getLogger(__name__)
        self.use_public_api = use_public_api
        self.settings_store = settings_store

    def _auth_error_hint(self, endpoint: str, status_code: int) -> str:
        if status_code not in (401, 403) or self.settings_store is None:
            return ""
        host = urlparse(endpoint).hostname
        settings = self.settings_store.load()
        if host == urlparse(self.colab_public_api).hostname:
            settings.public_api_supported = False
            self.settings_store.save(settings)
            if settings.tunnel_api_supported:
                return " (Public API access denied; pass --no-public-api to use the tunnel API)"
        elif host == urlparse(self.colab_domain).hostname:
            settings.tunnel_api_supported = False
            self.settings_store.save(settings)
            if settings.public_api_supported:
                return " (Tunnel API access denied; pass --public-api to use the Public API)"
        return " (Run 'colab test-public-api' or 'colab whoami' to check auth access)"

    def _strip_xssi_prefix(self, v: str) -> str:
        if not v.startswith(XSSI_PREFIX):
            return v
        return v[len(XSSI_PREFIX) :]

    def _issue_request(
        self,
        endpoint: str,
        method: str = "GET",
        headers: Dict[str, str] = None,
        params: Dict[str, str] = None,
        schema: Optional[BaseModel] = None,
        **kwargs,
    ):
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.hostname in urlparse(self.colab_domain).hostname:
            if params is None:
                params = {}
            params["authuser"] = "0"

        request_headers = headers.copy() if headers else {}
        request_headers[ACCEPT_JSON_HEADER["key"]] = ACCEPT_JSON_HEADER["value"]
        request_headers[COLAB_CLIENT_AGENT_HEADER["key"]] = COLAB_CLIENT_AGENT_HEADER[
            "value"
        ]

        # Slow allocations may outlive AuthorizedSession's default read timeout.
        if "timeout" not in kwargs:
            timeout = os.getenv("COLAB_CLI_REQUEST_TIMEOUT")
            if timeout:
                kwargs["timeout"] = float(timeout)

        self.logger.debug(f"Request: {method} {endpoint}")
        self.logger.debug(f"Params: {params}")

        response = self.session.request(
            method, endpoint, headers=request_headers, params=params, **kwargs
        )

        self.logger.debug(f"Request Headers: {response.request.headers}")
        self.logger.debug(f"Response: {response.status_code} {response.reason}")
        self.logger.debug(f"Response Headers: {response.headers}")
        self.logger.debug(f"Response Body: {response.text}")
        if not response.ok:
            hint = self._auth_error_hint(endpoint, response.status_code)
            raise ColabRequestError(
                f"Failed to issue request {method} {endpoint}: {response.reason}{hint}",
                request=response.request,
                response=response,
                response_body=response.text,
            )

        body = self._strip_xssi_prefix(response.text)
        if not body:
            return
        # Some endpoints return a non-empty body but the caller doesn't care
        # about the response content — skip pydantic validation entirely when
        # no schema was supplied.
        if schema is None:
            return
        return TypeAdapter(schema).validate_python(json.loads(body))

    def get_consumption_user_info(self) -> ConsumptionUserInfo:
        """Fetch account-level CCU balance and usage rate from the session backend."""
        url = urljoin(self.colab_domain, f"{TUN_ENDPOINT}/ccu-info")
        ccu = self._issue_request(url, schema=CcuInfo)
        return consumption_user_info_from_tunnel(ccu)

    def check_api_access(self) -> tuple[bool, bool]:
        try:
            self._list_assignments_public()
            pub_ok = True
        except Exception:
            pub_ok = False
        try:
            self._list_assignments_tunnel()
            tun_ok = True
        except Exception:
            tun_ok = False
        if self.settings_store is not None:
            settings = self.settings_store.load()
            settings.public_api_supported = pub_ok
            settings.tunnel_api_supported = tun_ok
            self.settings_store.save(settings)
        return pub_ok, tun_ok

    def list_assignments(self) -> List[ListedAssignment]:
        if self.use_public_api:
            return self._list_assignments_public()
        return self._list_assignments_tunnel()

    def _list_assignments_tunnel(self) -> List[ListedAssignment]:
        url = urljoin(self.colab_domain, f"{TUN_ENDPOINT}/assignments")
        resp = self._issue_request(url, schema=ListedAssignments)
        return resp.assignments if resp else []

    def get_runtime(self, runtime_name: str) -> Runtime:
        if not runtime_name.startswith("runtimes/"):
            runtime_name = f"runtimes/{runtime_name}"
        url = urljoin(self.colab_public_api, f"/v1beta/{runtime_name}")
        return self._issue_request(url, schema=Runtime)

    def _list_assignments_public(self) -> List[ListedAssignment]:
        url = urljoin(self.colab_public_api, "/v1beta/runtimes")
        resp = self._issue_request(url, schema=ListRuntimesResponse)
        if not resp or not resp.runtimes:
            return []

        assignments = []
        for r in resp.runtimes:
            mapped_variant, mapped_accelerator, mapped_shape = _map_public_runtime_spec(
                r.runtime_spec
            )
            assignments.append(
                ListedAssignment(
                    accelerator=mapped_accelerator,
                    endpoint=r.connection_info.endpoint or "",
                    name=r.name,
                    variant=mapped_variant,
                    machineShape=mapped_shape,
                    runtimeProxyInfo=proxy_info_from_connection(r.connection_info),
                )
            )
        return assignments

    def unassign(
        self,
        endpoint: str,
        runtime_name: Optional[str] = None,
        url: Optional[str] = None,
    ):
        if self.use_public_api:
            return self._unassign_public(endpoint, runtime_name=runtime_name, url=url)
        return self._unassign_tunnel(endpoint, url=url)

    def _unassign_tunnel(self, endpoint: str, url: Optional[str] = None):
        if not endpoint and url:
            by_url = {
                u: a.endpoint
                for a in self._list_assignments_tunnel()
                if a.endpoint and (u := normalize_proxy_url(a.runtime_proxy_info.url))
            }
            endpoint = by_url.get(normalize_proxy_url(url), "")
        req_url = urljoin(self.colab_domain, f"{TUN_ENDPOINT}/unassign/{endpoint}")
        resp = self._issue_request(req_url, schema=GetUnassignRequest)
        headers = {COLAB_XSRF_TOKEN_HEADER["key"]: resp.token}
        return self._issue_request(
            req_url, method="POST", headers=headers, schema=BaseModel
        )

    def _unassign_public(
        self,
        endpoint: str,
        runtime_name: Optional[str] = None,
        url: Optional[str] = None,
    ):
        target = runtime_name
        if not target:
            if endpoint.startswith("runtimes/"):
                target = endpoint
            elif endpoint.startswith("r-"):
                target = f"runtimes/{endpoint}"
            elif endpoint or url:
                assignments = self._list_assignments_public()
                by_endpoint = {
                    a.endpoint: a.name for a in assignments if a.endpoint and a.name
                }
                by_url = {
                    u: a.name
                    for a in assignments
                    if a.name and (u := normalize_proxy_url(a.runtime_proxy_info.url))
                }
                norm_url = normalize_proxy_url(url)
                target = (by_endpoint.get(endpoint) if endpoint else None) or (
                    by_url.get(norm_url) if norm_url else None
                )
        if not target:
            return None
        if not target.startswith("runtimes/"):
            target = f"runtimes/{target}"
        req_url = urljoin(self.colab_public_api, f"/v1beta/{target}")
        return self._issue_request(
            req_url, method="DELETE", params={"allowMissing": "true"}, schema=None
        )

    def _call_with_assign_retries(self, fn):
        retries = max(0, int(os.getenv("COLAB_CLI_ASSIGN_RETRIES", "3")))
        backoff = max(0.0, float(os.getenv("COLAB_CLI_RETRY_BACKOFF", "10")))
        for attempt in range(retries + 1):
            try:
                return fn()
            except (ColabRequestError, requests.exceptions.ReadTimeout) as e:
                status = (
                    get_status_code(e) if isinstance(e, ColabRequestError) else None
                )
                retryable = isinstance(
                    e, requests.exceptions.ReadTimeout
                ) or status in (502, 503, 504)
                if not retryable or attempt >= retries:
                    raise
                delay = backoff * (2**attempt)
                self.logger.warning(
                    "Assignment request failed; retrying in %.0fs", delay
                )
                time.sleep(delay)

    def assign(
        self,
        notebook_hash: uuid.UUID,
        variant: Optional[Variant] = None,
        accelerator: Optional[Accelerator] = None,
        shape: Optional[Shape] = None,
    ) -> Union[PostAssignmentResponse, Assignment]:
        if self.use_public_api:
            return self._assign_public(notebook_hash, variant, accelerator, shape)
        return self._assign_tunnel(notebook_hash, variant, accelerator, shape)

    def _assign_public(
        self,
        notebook_hash: uuid.UUID,
        variant: Optional[Variant] = None,
        accelerator: Optional[Accelerator] = None,
        shape: Optional[Shape] = None,
    ) -> Union[PostAssignmentResponse, Assignment]:
        runtime_id = f"r-{notebook_hash}"
        request_id = str(uuid.uuid4())
        if variant == Variant.GPU:
            pub_variant = PublicVariant.VARIANT_GPU.value
        elif variant == Variant.TPU:
            pub_variant = PublicVariant.VARIANT_TPU.value
        else:
            pub_variant = PublicVariant.VARIANT_CPU.value
        pub_acc = accelerator.value if accelerator else Accelerator.NONE.value
        pub_shape = (
            PublicShape.SHAPE_HIGHMEM.value
            if shape == Shape.HIGH_RAM
            else PublicShape.SHAPE_STANDARD.value
        )
        create_url = urljoin(self.colab_public_api, "/v1beta/runtimes")
        op = self._call_with_assign_retries(
            lambda: self._issue_request(
                create_url,
                method="POST",
                params={"runtimeId": runtime_id, "requestId": request_id},
                json={
                    "runtimeSpec": {
                        "variant": pub_variant,
                        "accelerator": pub_acc,
                        "shape": pub_shape,
                    }
                },
                schema=Operation,
            )
        )

        max_polls = max(1, int(os.getenv("COLAB_CLI_PUBLIC_WAIT_POLLS", "10")))
        polls = 0
        while not op.done:
            if polls >= max_polls:
                raise TimeoutError(
                    f"Timed out waiting for runtime creation operation {op.name}"
                )
            polls += 1
            wait_url = urljoin(self.colab_public_api, f"/v1/{op.name}:wait")
            op = self._call_with_assign_retries(
                lambda: self._issue_request(
                    wait_url, params={"timeout": "30s"}, schema=Operation
                )
            )

        if op.error is not None:
            code = op.error.code
            msg = op.error.message or "Runtime creation failed"
            if code == 9:
                raise TooManyAssignmentsError(msg)
            if code == 6:
                rt = self.get_runtime(f"runtimes/{runtime_id}")
                return Assignment(
                    endpoint=rt.connection_info.endpoint or "",
                    runtime_name=rt.name,
                    runtimeProxyInfo=proxy_info_from_connection(rt.connection_info),
                )
            fake_resp = requests.Response()
            fake_resp.status_code = _GRPC_TO_HTTP_STATUS.get(code, 500)
            fake_resp.reason = msg
            raise ColabRequestError(
                msg,
                request=None,
                response=fake_resp,
                response_body=op.model_dump_json(),
            )

        rt = Runtime.model_validate(op.response)
        mapped_variant, mapped_acc, _ = _map_public_runtime_spec(rt.runtime_spec)
        return PostAssignmentResponse(
            accelerator=mapped_acc,
            endpoint=rt.connection_info.endpoint or "",
            runtime_name=rt.name,
            runtimeProxyInfo=proxy_info_from_connection(rt.connection_info),
            variant=mapped_variant,
        )

    def _assign_tunnel(
        self,
        notebook_hash: uuid.UUID,
        variant: Optional[Variant] = None,
        accelerator: Optional[Accelerator] = None,
        shape: Optional[Shape] = None,
    ) -> Union[PostAssignmentResponse, Assignment]:
        assignment = self._get_assignment(notebook_hash, variant, accelerator, shape)
        if isinstance(assignment, Assignment):
            return assignment

        retries = max(0, int(os.getenv("COLAB_CLI_ASSIGN_RETRIES", "3")))
        backoff = max(0.0, float(os.getenv("COLAB_CLI_RETRY_BACKOFF", "10")))
        for attempt in range(retries + 1):
            try:
                res = self._post_assignment(
                    notebook_hash, assignment.token, variant, accelerator, shape
                )
                break
            except (ColabRequestError, requests.exceptions.ReadTimeout) as e:
                status = (
                    get_status_code(e) if isinstance(e, ColabRequestError) else None
                )
                if status == 412:
                    raise TooManyAssignmentsError(str(e))
                retryable = isinstance(
                    e, requests.exceptions.ReadTimeout
                ) or status in (502, 503, 504)
                if not retryable or attempt >= retries:
                    raise
                delay = backoff * (2**attempt)
                self.logger.warning(
                    "Assignment request failed; retrying in %.0fs", delay
                )
                time.sleep(delay)
                # Reconcile an uncertain POST before submitting the same request again.
                assignment = self._get_assignment(
                    notebook_hash, variant, accelerator, shape
                )
                if isinstance(assignment, Assignment):
                    return assignment

        return res

    def _build_assign_url(
        self,
        notebook_hash: uuid.UUID,
        variant: Optional[Variant] = None,
        accelerator: Optional[Accelerator] = None,
        shape: Optional[Shape] = None,
    ) -> str:
        url = urljoin(self.colab_domain, f"{TUN_ENDPOINT}/assign")
        params = {"nbh": uuid_to_web_safe_base64(notebook_hash)}
        if variant:
            params["variant"] = variant.value
        if accelerator:
            params["accelerator"] = accelerator.value
        if shape == Shape.HIGH_RAM:
            params["shape"] = "hm"

        req = requests.Request("GET", url, params=params)
        prep = req.prepare()
        return prep.url

    def _get_assignment(
        self,
        notebook_hash: uuid.UUID,
        variant: Optional[Variant] = None,
        accelerator: Optional[Accelerator] = None,
        shape: Optional[Shape] = None,
    ) -> Union[GetAssignmentResponse, Assignment]:
        url = self._build_assign_url(notebook_hash, variant, accelerator, shape)
        return self._issue_request(url, schema=Union[GetAssignmentResponse, Assignment])

    def _post_assignment(
        self,
        notebook_hash: uuid.UUID,
        xsrf_token: str,
        variant: Optional[Variant] = None,
        accelerator: Optional[Accelerator] = None,
        shape: Optional[Shape] = None,
    ) -> PostAssignmentResponse:
        url = self._build_assign_url(notebook_hash, variant, accelerator, shape)
        headers = {COLAB_XSRF_TOKEN_HEADER["key"]: xsrf_token}
        return self._issue_request(
            url, method="POST", headers=headers, schema=PostAssignmentResponse
        )
