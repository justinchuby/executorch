# Copyright 2024-2025 Arm Limited and/or its affiliates.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

from pathlib import Path
from typing import cast, Dict, List, Literal, Optional, Tuple

import numpy as np
import torch
import tosa_reference_model
from executorch.backends.arm.arm_backend import get_tosa_version, is_tosa

from executorch.backends.arm.test.conftest import is_option_enabled
from executorch.backends.arm.tosa_specification import TosaSpecification
from executorch.exir.lowered_backend_module import LoweredBackendModule

from packaging.version import Version
from torch.export import ExportedProgram
from torch.fx.node import Node

from torch.overrides import TorchFunctionMode
from tosa import TosaGraph

logger = logging.getLogger(__name__)
logger.setLevel(logging.CRITICAL)


class QuantizationParams:
    __slots__ = ["node_name", "zp", "scale", "qmin", "qmax", "dtype"]

    # todo: zps and scales can be per tensors or per channel => a list??
    def __init__(
        self,
        node_name: str,
        zp: int,
        scale: float,
        qmin: int,
        qmax: int,
        dtype: torch.dtype,
    ):
        self.node_name = node_name  # not need I think, but good for error check
        self.zp = zp
        self.scale = scale
        self.qmin = qmin
        self.qmax = qmax
        self.dtype = dtype


def _get_input_names(program: ExportedProgram) -> list[str]:
    """
    Get a list[str] with the names of the inputs to this model.

    Args:
        program (ExportedProgram): The program to get input names from.
    Returns:
        A list of strings with the names of the model input.
    """
    input_names = []

    # E.g. bias and weights are 'placeholders' as well. This is used to
    # get only the use inputs.
    usr_inputs = program.graph_signature.user_inputs
    for node in program.graph.nodes:
        if node.op == "placeholder" and node.name in usr_inputs:
            input_names.append(node.name)

    return input_names


def _get_input_quantization_params(
    program: ExportedProgram,
) -> list[QuantizationParams]:
    """
    Get input QuantizationParams in a program, maximum one per input to the program.
    Args:
        program (ExportedProgram): The program to get input quantization parameters from.
    Returns:
        list[QuantizationParams]: The found quantization parameters.
    Raises:
        RuntimeError if no quantization parameters are found.
    """

    quant_params = []
    input_names = _get_input_names(program)
    num_inputs = len(input_names)
    for node in program.graph.nodes:
        if (
            node.target == torch.ops.quantized_decomposed.quantize_per_tensor.default
            and node.args[0].name in input_names
        ):
            qp = QuantizationParams(
                node_name=node.args[0].name,
                scale=node.args[1],
                zp=node.args[2],
                qmin=node.args[3],
                qmax=node.args[4],
                dtype=node.args[5],
            )
            quant_params.append(qp)
            if (
                len(quant_params) == num_inputs
            ):  # break early if we have all the inputs quantized parameters
                break
    if len(quant_params) == 0:
        raise RuntimeError("No Quantization parameters found in exported model.")
    return quant_params


def _get_output_nodes(program: ExportedProgram) -> list[Node]:
    """
    Get output node to this model.

    Args:
        program (ExportedProgram): The program to get the output nodes from.
    Returns:
        The nodes that are the outputs of the 'program'.
    """
    output_nodes = []
    for node in program.graph.nodes:
        if node.op == "output":
            for output in node.args[0]:
                output_nodes.append(output)
    if len(output_nodes) == 0:
        raise RuntimeError("No output nodes found.")
    else:
        return output_nodes


def _get_output_quantization_params(
    output_nodes: list[Node],
) -> List[QuantizationParams]:
    """
    Get output QuantizationParams from a program.
    Args:
        output_nodes (list(Node)): A list of output nodes to get output quantization parameters from.
    Returns:
        QuantizationParams: The found quantization parameters.
    Raises:
        RuntimeError if no output quantization parameters are found.
    """
    quant_params = []
    for node in output_nodes:
        if node.target == torch.ops.quantized_decomposed.dequantize_per_tensor.default:
            quant_params.append(
                QuantizationParams(
                    node_name=node.args[0].name,
                    scale=node.args[1],
                    zp=node.args[2],
                    qmin=node.args[3],
                    qmax=node.args[4],
                    dtype=node.args[5],
                )
            )
    if len(quant_params) == 0:
        raise RuntimeError("No Quantization parameters not found in exported model.")
    return quant_params


class TosaReferenceModelDispatch(TorchFunctionMode):
    """A context manager for executing call_delegate nodes using the reference model"""

    def _tosa_dispatch(self, lowered_backend_module: LoweredBackendModule, inputs):
        tosa_buffer = lowered_backend_module.processed_bytes
        compile_specs = lowered_backend_module.compile_specs
        if not is_tosa(compile_specs):
            raise RuntimeError(
                "Model needs to be compiled to tosa to run reference model."
            )
        tosa_version = get_tosa_version(compile_specs)

        return run_tosa_graph_static(tosa_buffer, tosa_version, inputs)

    def __torch_function__(self, func, types, args=..., kwargs=None):
        if isinstance(func, torch._higher_order_ops.executorch_call_delegate.ExecutorchCallDelegate):  # type: ignore
            lowered_backend_module = cast(LoweredBackendModule, args[0])
            if lowered_backend_module.backend_id == "ArmBackend":
                return self._tosa_dispatch(lowered_backend_module, args[1:])
            else:
                logger.warning(
                    f"Ran model with TosaReferenceModelDispatch but call_delegate with {lowered_backend_module.backend_id=} != 'ArmBackend'."
                )

        kwargs = kwargs or {}
        return func(*args, **kwargs)


"""
A class to store parameters needed for running programs, either in tosa or .pte format.
"""


class RunnerUtil:
    def __init__(
        self,
        intermediate_path: str,
        tosa_ref_model_path: Optional[str] = None,
    ):
        self.intermediate_path = intermediate_path
        self.tosa_ref_model_path = tosa_ref_model_path or "tosa_reference_model"
        assert self.intermediate_path is None or os.path.exists(
            self.intermediate_path
        ), f"TOSA artifact path don't exist! Path: {self.intermediate_path}"

        self.is_quantized: bool = False
        self.input_names: list[str] = None
        self.output_name: str = None
        self.qp_input: list[QuantizationParams] = None
        self.qp_output: list[QuantizationParams] = None
        self.timeout = 480
        self.target_board: str = None

        self._has_init_run = False

    def init_run(
        self,
        exported_program: ExportedProgram,
        edge_program: ExportedProgram,
        is_quantized: bool,
        target_board: str,
    ):

        self.input_names = _get_input_names(edge_program)
        self.output_nodes = _get_output_nodes(exported_program)

        self.is_quantized = is_quantized
        self.target_board = target_board

        if is_quantized:
            self.qp_input = _get_input_quantization_params(exported_program)
            self.qp_output = _get_output_quantization_params(self.output_nodes)
        else:
            self.qp_input = [None] * len(self.input_names)
            self.qp_output = [None] * len(self.output_nodes)

        self._has_init_run = True

    def set_timeout(self, timeout: int):
        self.timeout = timeout

    def run_corstone(
        self,
        inputs: Tuple[torch.Tensor],
    ) -> list[torch.Tensor]:

        assert (
            self._has_init_run
        ), "RunnerUtil needs to be initialized using init_run() before running Corstone FVP."
        if self.target_board not in ["corstone-300", "corstone-320"]:
            raise RuntimeError(f"Unknown target board: {self.target_board}")

        pte_path = os.path.join(self.intermediate_path, "program.pte")
        assert os.path.exists(pte_path), f"Pte path '{pte_path}' not found."

        for input_name, quant_param, data in zip(
            self.input_names, self.qp_input, inputs
        ):
            save_bytes(self.intermediate_path, data, False, input_name, quant_param)

        out_path = os.path.join(self.intermediate_path, "out")

        input_paths = []
        for name in self.input_names:
            input_paths.append(
                os.path.join(self.intermediate_path, f"{name}.bin"),
            )
        elf_path = os.path.join(
            "cmake-out",
            f"arm_semihosting_executor_runner_{self.target_board}",
            "arm_executor_runner",
        )
        assert os.path.exists(
            elf_path
        ), f"Did not find build arm_executor_runner in path {elf_path}, run setup_testing.sh?"

        cmd_line = f"executor_runner -m {pte_path} -o {out_path}"

        for input_path in input_paths:
            cmd_line += f" -i {input_path}"

        ethos_u_extra_args = ""
        if is_option_enabled("fast_fvp"):
            ethos_u_extra_args = ethos_u_extra_args + "--fast"

        command_args = {
            "corstone-300": [
                "FVP_Corstone_SSE-300_Ethos-U55",
                "-C",
                "ethosu.num_macs=128",
                "-C",
                "mps3_board.visualisation.disable-visualisation=1",
                "-C",
                "mps3_board.telnetterminal0.start_telnet=0",
                "-C",
                "mps3_board.uart0.out_file='-'",
                "-C",
                "cpu0.semihosting-enable=1",
                "-C",
                "cpu0.semihosting-stack_base=0",
                "-C",
                f"ethosu.extra_args='{ethos_u_extra_args}'",
                "-C",
                "cpu0.semihosting-heap_limit=0",
                "-C",
                f"cpu0.semihosting-cmd_line='{cmd_line}'",
                "-a",
                elf_path,
                "--timelimit",
                f"{self.timeout}",
            ],
            "corstone-320": [
                "FVP_Corstone_SSE-320",
                "-C",
                "mps4_board.subsystem.ethosu.num_macs=128",
                "-C",
                "mps4_board.visualisation.disable-visualisation=1",
                "-C",
                "vis_hdlcd.disable_visualisation=1",
                "-C",
                "mps4_board.telnetterminal0.start_telnet=0",
                "-C",
                "mps4_board.uart0.out_file='-'",
                "-C",
                "mps4_board.uart0.unbuffered_output=1",
                "-C",
                "mps4_board.uart0.shutdown_on_eot=1",
                "-C",
                "mps4_board.subsystem.cpu0.semihosting-enable=1",
                "-C",
                "mps4_board.subsystem.cpu0.semihosting-stack_base=0",
                "-C",
                "mps4_board.subsystem.cpu0.semihosting-heap_limit=0",
                "-C",
                f"mps4_board.subsystem.ethosu.extra_args='{ethos_u_extra_args}'",
                "-C",
                f"mps4_board.subsystem.cpu0.semihosting-cmd_line='{cmd_line}'",
                "-a",
                elf_path,
                "--timelimit",
                f"{self.timeout}",
            ],
        }

        result = _run_cmd(command_args[self.target_board], check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to run {command_args[self.target_board]}\nOutput:\n{result.stdout.decode()}\nError: {result.stderr.decode()}"
            )
        result_stdout = result.stdout.decode()

        error_regex = r"(^[EF][: ].*$)|(^.*Hard fault.*$)|(^.*Assertion.*$)"

        # Check for errors in the output
        # regex to check for error or fault messages in stdout from FVP
        if re.compile(error_regex, re.MULTILINE).search(result_stdout):
            raise RuntimeError(
                f"Corstone simulation failed:\ncmd: {command_args[self.target_board]}\n, log: \n {result_stdout}\n{result.stderr.decode()}"
            )
        output_np = []
        for i, node in enumerate(self.output_nodes):
            tosa_ref_output = np.fromfile(
                os.path.join(self.intermediate_path, f"out-{i}.bin"), dtype=np.float32
            )
            output_shape = node.meta["val"].shape
            output_np.append(torch.from_numpy(tosa_ref_output).reshape(output_shape))
        return tuple(output_np)

    def run_tosa_graph(
        self, graph: TosaGraph, inputs: list[np.ndarray] | list[torch.Tensor]
    ) -> torch.Tensor:
        """Runs the TOSA reference model with inputs and returns the result."""
        data_np = [
            prep_data_for_save(
                input, self.is_quantized, self.input_names[i], self.qp_input[i]
            )
            for i, input in enumerate(inputs)
        ]
        # tosa_profile: 0 = Base Inference, 1 = Main Inference, 2 = Main Training.
        tosa_profile = 0 if self.is_quantized else 1
        debug_mode = "ALL" if logger.level <= logging.DEBUG else None
        outputs, status = tosa_reference_model.run(
            graph,
            data_np,
            verbosity=_tosa_refmodel_loglevel(logger.level),
            tosa_profile=tosa_profile,
            initialize_variable_tensor_from_numpy=1,  # True
            debug_mode=debug_mode,
        )

        assert (
            status == tosa_reference_model.GraphStatus.TOSA_VALID
        ), "Non-valid TOSA given to reference model."

        outputs_torch = []
        for output in outputs:
            output = torch.from_numpy(output)
            if self.is_quantized:
                # Need to dequant back to FP32 for comparison with torch output
                quant_param = self.qp_output
                assert (
                    quant_param is not None
                ), "There are no quantization parameters, check output parameters"
                output = (output.to(torch.float32) - quant_param.zp) * quant_param.scale
            outputs_torch.append(output)
        return tuple(outputs_torch)

    def run_tosa_ref_model(
        self,
        inputs: Tuple[torch.Tensor],
    ) -> list[torch.Tensor]:
        """
        Run TOSA reference model using the tosa_reference_model program.

        In order to do that we need:
        1. desc.json, which points to files needed by tosa_reference_model.
        2. output.tosa, which is the TOSA buffer that describes the model we're
           trying to run.

        These two files are created by arm_backend.py as part of partition stage

        All these files are saved on disk in self.intermediate_path.

        Args:
            inputs (Tuple[torch.Tensor]): The input data to run the TOSA

        Returns:
            torch.Tensor: The output of the TOSA reference model, as a torch
                tensor.

        Here's a sample desc.json file:
        {
            "tosa_file": "output.tosa",
            "ifm_name": [
                "arg0_1"
            ],
            "ifm_file": [
                "arg0_1.npy"
            ],
            "ofm_name": [
                "quantized_decomposed_dequantize_per_tensor_default_1"
            ],
            "ofm_file": [
                "ref-quantized_decomposed_dequantize_per_tensor_default_1.npy"
            ],
            "expected_return_code": 0,
            "expected_failure": false
        }

        Todo:
            * It would be nice to not rely on files on disk. Should be possible
              as a next step. See:
              https://review.mlplatform.org/plugins/gitiles/tosa/reference_model/#executable-usage
        """

        assert (
            self._has_init_run
        ), "RunnerUtil needs to be initialized using init_run() before running tosa reference."

        all_desc_file_paths = [
            str(path) for path in Path(self.intermediate_path).glob("desc*.json")
        ]
        assert (
            all_desc_file_paths
        ), f"No TOSA description file found in '{self.intermediate_path}'."
        if len(all_desc_file_paths) != 1:
            raise NotImplementedError(
                "Graphs with more than one partition are currently not supported."
            )

        desc_file_path = all_desc_file_paths[0]
        assert os.path.exists(
            desc_file_path
        ), f"desc_file_path: {desc_file_path} does not exist"

        # Save the input data to disk as a .npy file, since that's what the TOSA
        # reference model expects. Name of the file must match the name in
        # desc.json, which is the tensor name from the graph + .npy
        for input_name, quant_param, data in zip(
            self.input_names, self.qp_input, inputs, strict=True
        ):
            save_npy(
                self.intermediate_path, data, self.is_quantized, input_name, quant_param
            )

        # Run the TOSA reference model via command line, this will produce a
        # .npy file with the result (aka OFM).
        assert (
            shutil.which(self.tosa_ref_model_path) is not None
        ), f"tosa_reference_model tool not found, did you run examples/arm/setup.sh? Path: {self.tosa_ref_model_path}"

        cmd_ref_model = [
            self.tosa_ref_model_path,
            "--test_desc",
            desc_file_path,
            "-l",
            _tosa_refmodel_loglevel(logger.level),
        ]
        _run_cmd(cmd_ref_model)

        # Load desc.json, just to get the name of the output file above
        with open(desc_file_path) as f:
            desc_json = json.load(f)

        tosa_ref_outputs = []
        for ofm_file in desc_json["ofm_file"]:
            ofm_file_npy = os.path.join(self.intermediate_path, ofm_file)

            # Load the output file (OFM) and return it as a numpy array
            tosa_ref_output = np.load(ofm_file_npy)

            if self.is_quantized:
                # Need to dequant back to FP32 for comparison with torch output
                # Convert to int32 prior to dequantize the output
                if tosa_ref_output.dtype == np.int8:
                    tosa_ref_output = tosa_ref_output.astype(np.int32)
                quant_param = self.qp_output
                if quant_param is not None:
                    # I.e. bool output is possible for quantized models
                    tosa_ref_output = (
                        tosa_ref_output - quant_param.zp
                    ) * quant_param.scale

            if tosa_ref_output.dtype == np.double:
                tosa_ref_output = tosa_ref_output.astype("float32")
            elif tosa_ref_output.dtype == bool:
                # retain the bool output though for boolean related comparisons
                tosa_ref_output = tosa_ref_output.astype("bool")

            # tosa_output is a numpy array, convert to torch tensor for comparison
            tosa_ref_outputs.append(torch.from_numpy(tosa_ref_output))

        return tosa_ref_outputs


def prep_data_for_save(
    data: torch.Tensor,
    is_quantized: bool,
    input_name: str,
    quant_param: QuantizationParams,
):
    data_np = np.array(data.detach(), order="C").astype(
        f"{data.dtype}".replace("torch.", "")
    )

    if is_quantized:
        assert quant_param.node_name in input_name, (
            f"The quantization params name '{quant_param.node_name}' does not "
            f"match the input tensor name '{input_name}'."
        )
        data_np = (
            ((data_np / np.float32(quant_param.scale)) + quant_param.zp)
            .round()
            .clip(quant_param.qmin, quant_param.qmax)
            .astype(
                f"{quant_param.dtype}".replace("torch.", "")
            )  # Use string format of dtype to convert to numpy dtype
        )
    return data_np


def save_npy(
    path: str,
    data,
    is_quantized: bool,
    input_name: str,
    quant_param: QuantizationParams,
) -> str:
    """Serializes and saves 'data' as a .npy file, possibly quantizing it before.

    Parameters:
        path: the directory where to save the data.
        data: the data to save.
        is_quantized: whether to quantize the data before saving it.
        input_name: the name of the file, without file-ending.
        quant_param: the parameters to use for quantization.
    Returns:
        the full file path of the output.
    """
    data_np = prep_data_for_save(data, is_quantized, input_name, quant_param)
    file_path = os.path.join(path, input_name + ".npy")
    np.save(file_path, data_np, allow_pickle=False)

    return file_path


def save_bytes(
    path: str,
    data,
    is_quantized: bool,
    input_name: str,
    quant_param: QuantizationParams,
) -> str:
    """Serializes and saves 'data' in byte format, possibly quantizing it before.

    Parameters:
        path: the directory where to save the data.
        data: the data to save.
        is_quantized: whether to quantize the data before saving it.
        input_name: the name of the file, without file-ending.
        quant_param: the parameters to use for quantization.
    Returns:
        the full file path of the output.
    """
    data_np = prep_data_for_save(data, is_quantized, input_name, quant_param)
    file_path = os.path.join(path, input_name + ".bin")
    with open(file_path, "w+b") as f:
        data_np_bytes = data_np.tobytes()
        f.write(data_np_bytes)

    return file_path


def _run_cmd(cmd: List[str], check=True) -> subprocess.CompletedProcess[bytes]:
    """
    Run a command and check for errors.

    Args:
    cmd (List[str]): The command to run as a list.
    """
    try:
        result = subprocess.run(cmd, check=check, capture_output=True)
        return result
    except subprocess.CalledProcessError as e:
        arg_string = " ".join(cmd)
        raise RuntimeError(
            f"Failed running command {arg_string}\nStderr: {e.stderr.decode()}\nStdout: {e.stdout.decode()}"
        )


def dbg_tosa_fb_to_json(tosa_fb: bytes) -> Dict:
    """
    This function is used to dump the TOSA flatbuffer to a human readable
    format, using flatc. It is used for debugging purposes.
    """

    tmp = tempfile.mkdtemp()
    tosa_input_file = os.path.join(tmp, "output.tosa")
    with open(tosa_input_file, "wb") as f:
        f.write(tosa_fb)

    arm_backend_path = os.path.realpath(os.path.dirname(__file__) + "/..")
    tosa_schema_file = os.path.join(
        arm_backend_path, "third-party/serialization_lib/schema/tosa.fbs"
    )
    assert os.path.exists(
        tosa_schema_file
    ), f"tosa_schema_file: {tosa_schema_file} does not exist"
    assert shutil.which("flatc") is not None
    cmd_flatc = [
        "flatc",
        "--json",
        "--strict-json",
        "-o",
        tmp,
        "--raw-binary",
        "-t",
        tosa_schema_file,
        "--",
        tosa_input_file,
    ]
    _run_cmd(cmd_flatc)
    with open(os.path.join(tmp, "output.json"), "r") as f:
        json_out = json.load(f)

    # Cast float tensors to proper dtype.
    try:
        for region in json_out["regions"]:
            for block in region["blocks"]:
                for tensor in block["tensors"]:
                    if "data" in tensor:
                        if tensor["type"] == "FP32":
                            data = np.array(tensor["data"])
                            data = data.astype(np.int8)
                            data = np.frombuffer(data, dtype=np.float32)
                        data = data.reshape(tensor["shape"])
                        tensor["data"] = data
    except Exception:
        # This is just nice-to-have if it works, don't care if it fails.
        pass

    return json_out


def _tosa_refmodel_loglevel(loglevel: int) -> str:
    """Converts a logging loglevel to tosa_reference_model logginglevel,
    returned as string.
    """
    loglevel_map = {
        logging.INFO: "INFO",
        logging.CRITICAL: "LOW",
        logging.ERROR: "LOW",
        logging.WARNING: "MED",
        logging.DEBUG: "HIGH",
        logging.NOTSET: "MED",
    }
    clamped_logging_level = max(min(loglevel // 10 * 10, 50), 0)
    return loglevel_map[clamped_logging_level]


def run_tosa_graph_static(
    graph: TosaGraph,
    tosa_version: TosaSpecification,
    inputs: list[torch.Tensor],
) -> list[torch.Tensor]:
    """Runs the TOSA reference model with inputs and returns the result."""
    inputs_np = [input.numpy() for input in inputs]
    transpose_data_format(inputs_np, to="NHWC")

    tosa_release = tosa_version.version

    if tosa_release > Version("0.80"):
        logger.warning("The reference model is only tested for TOSA v0.80")

    # tosa_profile: 0 = Base Inference, 1 = Main Inference, 2 = Main Training.
    tosa_profile = 1 if tosa_version.support_float() else 0
    debug_mode = "ALL" if logger.level <= logging.DEBUG else None
    outputs_np, status = tosa_reference_model.run(
        graph,
        inputs_np,
        verbosity=_tosa_refmodel_loglevel(logger.level),
        tosa_profile=tosa_profile,
        initialize_variable_tensor_from_numpy=1,  # True
        debug_mode=debug_mode,
    )

    assert (
        status == tosa_reference_model.GraphStatus.TOSA_VALID
    ), "Non-valid TOSA given to reference model."

    transpose_data_format(outputs_np, to="NCHW")
    return [torch.from_numpy(output) for output in outputs_np]


def transpose_data_format(data: list[np.ndarray], to: Literal["NHWC", "NCHW"]):
    if to == "NCHW":
        dim_order = (0, 3, 1, 2)
    if to == "NHWC":
        dim_order = (0, 2, 3, 1)
    for i in range(len(data)):
        if hasattr(data[i], "shape") and len(data[i].shape) == 4:
            # Copy is needed to force actual data conversion, not setting stride.
            data[i] = np.transpose(data[i], dim_order).copy()
