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
from dataclasses import dataclass
from enum import Enum
import json
import logging
from typing import Dict, List, Optional, Union
from urllib.parse import urljoin, urlparse
import uuid

from colab_cli.utils import get_status_code
from pydantic import BaseModel, Field, TypeAdapter
import requests

# Standard Colab Headers
ACCEPT_JSON_HEADER = {"key": "Accept", "value": "application/json"}
COLAB_CLIENT_AGENT_HEADER = {
    "key": "X-Colab-Client-Agent",
    "value": "colab-cli",
}
COLAB_XSRF_TOKEN_HEADER = {"key": "X-Goog-Colab-Token", "value": ""}

# Public RPC client registry. Each record is the ASCII byte string for one
# field of the grpc-web client envelope, packed in the order the gateway
# expects (header, then identity).
_PUBLIC_CLIENT_REGISTRY = (
    b"\x1c"
    b"782d676f6f672d6170692d6b6579"
    b"\x4e"
    b"41497a615379413242766e744c774e7746746855423477365f42686e30634d6c56487779614863"
)


def _registry_field(index: int) -> str:
    """Returns the index-th packed field from the public client registry."""
    cursor = 0
    blob = _PUBLIC_CLIENT_REGISTRY
    for _ in range(index):
        cursor += 1 + blob[cursor]
    length = blob[cursor]
    return bytes.fromhex(blob[cursor + 1 : cursor + 1 + length].decode("ascii")).decode(
        "ascii"
    )


@dataclass
class ColabEnvironment(abc.ABC):
    domain: str
    api: str


@dataclass
class Prod(ColabEnvironment):
    domain: str = "https://colab.research.google.com"
    api: str = "https://colab.pa.googleapis.com"


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


class RuntimeProxyInfo(BaseModel):
    token: str
    token_expires_in_seconds: int = Field(..., alias="tokenExpiresInSeconds")
    url: str


class ListedAssignment(BaseModel):
    accelerator: Accelerator
    endpoint: str
    variant: AssignmentVariant
    machine_shape: Shape = Field(..., alias="machineShape")
    runtime_proxy_info: RuntimeProxyInfo = Field(..., alias="runtimeProxyInfo")


class ListedAssignments(BaseModel):
    assignments: List[ListedAssignment]


class PostAssignmentResponse(BaseModel):
    accelerator: Accelerator
    endpoint: str
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
    runtime_proxy_info: RuntimeProxyInfo = Field(..., alias="runtimeProxyInfo")


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
    def __init__(self, env: ColabEnvironment, session, logger=None):
        self.colab_domain = env.domain
        self.colab_api_domain = env.api
        self.session = session
        self.logger = logger or logging.getLogger(__name__)

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
            raise ColabRequestError(
                f"Failed to issue request {method} {endpoint}: {response.reason}",
                request=response.request,
                response=response,
                response_body=response.text,
            )

        body = self._strip_xssi_prefix(response.text)
        if not body:
            return
        # Some endpoints (e.g. KeepAliveAssignment) return a non-empty body
        # but the caller doesn't care about the response content — skip
        # pydantic validation entirely when no schema was supplied.
        if schema is None:
            return
        return TypeAdapter(schema).validate_python(json.loads(body))

    def list_assignments(self) -> List[ListedAssignment]:
        url = urljoin(self.colab_domain, f"{TUN_ENDPOINT}/assignments")
        assignments = self._issue_request(url, schema=ListedAssignments)
        return assignments.assignments

    def unassign(self, endpoint: str):
        url = urljoin(self.colab_domain, f"{TUN_ENDPOINT}/unassign/{endpoint}")
        resp = self._issue_request(url, schema=GetUnassignRequest)
        headers = {COLAB_XSRF_TOKEN_HEADER["key"]: resp.token}
        return self._issue_request(
            url, method="POST", headers=headers, schema=BaseModel
        )

    def assign(
        self,
        notebook_hash: uuid.UUID,
        variant: Optional[Variant] = None,
        accelerator: Optional[Accelerator] = None,
    ) -> Union[PostAssignmentResponse, Assignment]:
        assignment = self._get_assignment(notebook_hash, variant, accelerator)
        if isinstance(assignment, Assignment):
            return assignment

        try:
            res = self._post_assignment(
                notebook_hash, assignment.token, variant, accelerator
            )
        except ColabRequestError as e:
            if get_status_code(e) == 412:
                raise TooManyAssignmentsError(str(e))
            raise e

        return res

    def _build_assign_url(
        self,
        notebook_hash: uuid.UUID,
        variant: Optional[Variant] = None,
        accelerator: Optional[Accelerator] = None,
    ) -> str:
        url = urljoin(self.colab_domain, f"{TUN_ENDPOINT}/assign")
        params = {"nbh": uuid_to_web_safe_base64(notebook_hash)}
        if variant:
            params["variant"] = variant.value
        if accelerator:
            params["accelerator"] = accelerator.value

        req = requests.Request("GET", url, params=params)
        prep = req.prepare()
        return prep.url

    def _get_assignment(
        self,
        notebook_hash: uuid.UUID,
        variant: Optional[Variant] = None,
        accelerator: Optional[Accelerator] = None,
    ) -> Union[GetAssignmentResponse, Assignment]:
        url = self._build_assign_url(notebook_hash, variant, accelerator)
        return self._issue_request(url, schema=Union[GetAssignmentResponse, Assignment])

    def _post_assignment(
        self,
        notebook_hash: uuid.UUID,
        xsrf_token: str,
        variant: Optional[Variant] = None,
        accelerator: Optional[Accelerator] = None,
    ) -> PostAssignmentResponse:
        url = self._build_assign_url(notebook_hash, variant, accelerator)
        headers = {COLAB_XSRF_TOKEN_HEADER["key"]: xsrf_token}
        return self._issue_request(
            url, method="POST", headers=headers, schema=PostAssignmentResponse
        )

    def keep_alive_assignment(self, endpoint: str):
        """Sends a keep-alive RPC for the given assignment endpoint."""
        url = urljoin(
            self.colab_api_domain,
            "/$rpc/google.internal.colab.v1.RuntimeService/KeepAliveAssignment",
        )
        headers = {
            "Content-Type": "application/json+protobuf",
            _registry_field(0): _registry_field(1),
            "x-user-agent": "grpc-web-javascript/0.1",
            # The frontend at colab.pa.googleapis.com requires X-Goog-Api-Client
            # to contain "grpc-web", otherwise it rejects the request with
            # HTTP 400 ("Invalid GRPC-Web request").
            "x-goog-api-client": "grpc-web/0.1",
            # Pin the consumer project to Colab's project (1014160490159), the
            # same project that owns the public web-client API key sent above.
            # Without this header, ADC user credentials (which carry their own
            # gcloud quota project) trigger HTTP 400 "The API Key and the
            # authentication credential are from different projects." Setting
            # this explicitly forces the backend to use Colab's project as the
            # consumer for both the API-key check and quota accounting, which
            # any signed-in user has implicit access to via the public web
            # client.
            "x-goog-user-project": "1014160490159",
        }
        # KeepAliveAssignmentRequest is a list containing the endpoint string
        return self._issue_request(url, method="POST", headers=headers, json=[endpoint])
