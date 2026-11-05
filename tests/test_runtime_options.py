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

import pytest
import typer

from colab_cli.client import Accelerator, Variant
from colab_cli.commands.session import resolve_runtime_options


@pytest.mark.parametrize(
    "value,expected",
    [
        ("T4", Accelerator.T4),
        ("t4", Accelerator.T4),
        ("L4", Accelerator.L4),
        ("G4", Accelerator.G4),
        ("H100", Accelerator.H100),
        ("a100", Accelerator.A100),
    ],
)
def test_resolve_gpu_is_case_insensitive(value, expected):
    variant, accelerator, _ = resolve_runtime_options(gpu=value)

    assert variant == Variant.GPU
    assert accelerator == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("v5e1", Accelerator.V5E1),
        ("V5E1", Accelerator.V5E1),
        ("v6e1", Accelerator.V6E1),
    ],
)
def test_resolve_tpu(value, expected):
    variant, accelerator, _ = resolve_runtime_options(tpu=value)

    assert variant == Variant.TPU
    assert accelerator == expected


@pytest.mark.parametrize("value", ["A10O", "foo", "V100", "t4x", " "])
def test_unknown_gpu_is_rejected(value):
    with pytest.raises(typer.BadParameter) as exc:
        resolve_runtime_options(gpu=value)

    message = exc.value.format_message()
    assert "Supported: T4, L4, G4, H100, A100" in message
    assert exc.value.param_hint == "--gpu"


@pytest.mark.parametrize("value", ["v4", "foo", "v5e", "tpu"])
def test_unknown_tpu_is_rejected(value):
    with pytest.raises(typer.BadParameter) as exc:
        resolve_runtime_options(tpu=value)

    message = exc.value.format_message()
    assert "Supported: V5E1, V6E1" in message
    assert exc.value.param_hint == "--tpu"


def test_no_accelerator_flags_requests_cpu():
    variant, accelerator, _ = resolve_runtime_options()

    assert variant == Variant.DEFAULT
    assert accelerator == Accelerator.NONE
