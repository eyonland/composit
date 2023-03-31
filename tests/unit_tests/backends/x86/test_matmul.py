from __future__ import annotations

import pytest

from ctypes import cdll, c_float, POINTER
import pathlib
import time
import subprocess

from loguru import logger
import matplotlib.pyplot as plt
import numpy as np

import composit as cnp
from composit.hash import deterministic_hash
from composit.tilelab.tile_view import propagate_tile_views
from composit.tilelab.tilization_level import TilizationLevel
from composit.tilelab.tile import tilize_tensor
from composit.backends.x86.kernels.matmul import generate_kernel, generate_data

FILE_DIR = pathlib.Path(__file__).parent.resolve()

FLAGS = [
    "-std=c++2a",
    "-Ofast",
    "-march=native",
    "-fno-exceptions",
    "-mavx2",
    "-msse4",
    "-mfma",
    "-maes",
    "-shared",
    "-fPIC",
    "-Wall",
    "-Wno-deprecated",
    "-Wno-unused-function",
    "-Wno-multichar",
    "-Wno-subobject-linkage",
    "-Wno-format",
]


def run_torch(np_input_a, np_input_b, num_iterations):
    logger.info("Run torch")
    import torch

    torch.set_num_threads(1)

    torch_a = torch.from_numpy(np_input_a)
    torch_b = torch.from_numpy(np_input_b)

    # Call once to set up torch data structures
    for _ in range(10):
        output = torch_a @ torch_b

    execution_times = []
    for i in range(num_iterations):
        start = time.time_ns()
        output = torch_a @ torch_b
        end = time.time_ns()
        execution_times.append(end - start)

    execution_times = np.asarray(execution_times) / 1e6
    logger.info(f"Average Execution Time: {execution_times.mean()} milliseconds")
    logger.info(f"Minimum Execution Time: {execution_times.min()} milliseconds")
    logger.info(f"Maximum Execution Time: {execution_times.max()} milliseconds")
    return execution_times


def run_pnp_kernel(
    test_output_path,
    num_iterations,
    input_a_flat_array,
    input_b_flat_array,
    golden_output_flat_array,
):
    kernel_name = "matmul"
    source_file = str(FILE_DIR / f"{kernel_name}.cpp")
    assembly = test_output_path / f"{kernel_name}.s"
    assembly.unlink(missing_ok=True)
    assembly = str(assembly)
    command = ["g++", source_file, "-I", str(test_output_path), *FLAGS, "-S", "-fverbose-asm", "-o", assembly]
    logger.info(f"Compile Source Code to Assembly: \"{' '.join(command)}\"")
    result = subprocess.run(command)
    assert result.returncode == 0

    shared_library = test_output_path / f"{kernel_name}.so"
    shared_library.unlink(missing_ok=True)
    shared_library = str(shared_library)
    command = ["g++", assembly, "-fPIC", "-shared", "-o", shared_library]
    logger.info(f"Compile Assembly to Binary: \"{' '.join(command)}\"")
    result = subprocess.run(command)
    assert result.returncode == 0

    logger.info("Run kernel")
    matmul_kernel = cdll.LoadLibrary(shared_library)

    output_flat_array = np.zeros_like(golden_output_flat_array)

    def cast_array(flat_array):
        c_float_p = POINTER(c_float)
        return flat_array.ctypes.data_as(c_float_p)

    execution_times = []
    for _ in range(num_iterations):

        start = time.time_ns()

        output_flat_array.fill(0.0)

        matmul_kernel.run(
            cast_array(input_a_flat_array),
            len(input_a_flat_array),
            cast_array(input_b_flat_array),
            len(input_b_flat_array),
            cast_array(output_flat_array),
            len(output_flat_array),
        )
        end = time.time_ns()
        execution_times.append(end - start)

        assert np.allclose(output_flat_array, golden_output_flat_array, atol=1e-5, rtol=1e-6)

    execution_times = np.asarray(execution_times) / 1e6
    logger.info(f"Average Execution Time: {execution_times.mean()} milliseconds")
    logger.info(f"Minimum Execution Time: {execution_times.min()} milliseconds")
    logger.info(f"Maximum Execution Time: {execution_times.max()} milliseconds")
    return execution_times


def run_matmul(
    test_output_path,
    num_iterations: int,
    compare_against_torch: bool,
    transpose_b_levels: list[str],
    use_avx_manually: bool,
    input_a_shape: tuple[int, ...],
    l1_cache_a_shape: tuple[int, ...],
    input_b_shape: tuple[int, ...],
    l1_cache_b_shape: tuple[int, ...],
):

    logger.info("Creating composit graph")
    input_var_a = cnp.nn.variable(name="input_var_a", shape=input_a_shape)
    input_var_b = cnp.nn.variable(name="input_var_b", shape=input_b_shape)
    output_var = input_var_a @ input_var_b

    logger.info("Initializing random inputs")
    np.random.seed(0)
    np_input_a = np.random.uniform(-0.5, 0.5, input_var_a.shape)
    np_input_b = np.random.uniform(-0.5, 0.5, input_var_b.shape)

    logger.info("Creating and propagating tile views")
    input_var_to_scheme = {
        input_var_a: [
            TilizationLevel(level_name="l1_cache", tile_shape=l1_cache_a_shape),
        ],
        input_var_b: [
            TilizationLevel(level_name="l1_cache", tile_shape=l1_cache_b_shape),
        ],
    }
    tile_views = propagate_tile_views(output_var, inputs=input_var_to_scheme)

    logger.info("Tilizing tensor")
    tilized_input_a = tilize_tensor(np_input_a, tile_views[input_var_a].hierarchy)
    tilized_input_b = tilize_tensor(np_input_b, tile_views[input_var_b].hierarchy)
    tilized_golden_output = tilize_tensor(np_input_a @ np_input_b, tile_views[output_var].hierarchy)

    test_output_path.mkdir(parents=True, exist_ok=True)

    logger.info("Generating kernel")
    generate_kernel(
        test_output_path,
        tilized_input_a,
        tilized_input_b,
        transpose_b_levels=transpose_b_levels,
        use_avx_manually=use_avx_manually,
    )

    logger.info("Generating data")
    input_a_flat_array, input_b_flat_array, golden_output_flat_array = generate_data(
        tilized_input_a,
        tilized_input_b,
        tilized_golden_output,
        transpose_b_levels=transpose_b_levels,
    )

    fig, ax = plt.subplots()
    if compare_against_torch:
        torch_execution_times = run_torch(np_input_a, np_input_b, num_iterations)
        ax.plot(torch_execution_times, color="red")

    pnp_execution_times = run_pnp_kernel(
        test_output_path,
        num_iterations,
        input_a_flat_array,
        input_b_flat_array,
        golden_output_flat_array,
    )

    ax.plot(pnp_execution_times, color="green")

    def center_y_axis(axes):
        y_max = np.abs(axes.get_ylim()).max()
        axes.set_ylim(ymin=0, ymax=y_max)

    center_y_axis(ax)
    fig.savefig(test_output_path / "execution_times.png")
    fig.clf()


@pytest.mark.parametrize("num_iterations", [1000])
@pytest.mark.parametrize("compare_against_torch", [False])
@pytest.mark.parametrize("transpose_b_levels", [[], ["atomic"], ["l1_cache"], ["atomic", "l1_cache"]])
@pytest.mark.parametrize("use_avx_manually", [False, True])
@pytest.mark.parametrize("input_a_shape", [(1, 128, 128)])
@pytest.mark.parametrize("l1_cache_a_shape", [(1, 64, 64)])
@pytest.mark.parametrize("input_b_shape", [(128, 128)])
@pytest.mark.parametrize("l1_cache_b_shape", [(64, 64)])
def test_matmul(
    request,
    num_iterations,
    compare_against_torch: bool,
    transpose_b_levels: list[str],
    use_avx_manually: bool,
    input_a_shape: tuple[int, ...],
    l1_cache_a_shape: tuple[int, ...],
    input_b_shape: tuple[int, ...],
    l1_cache_b_shape: tuple[int, ...],
):
    test_name = request.node.name
    test_output_path = FILE_DIR / "test_output" / str(deterministic_hash(test_name))

    run_matmul(
        test_output_path,
        num_iterations,
        compare_against_torch,
        transpose_b_levels,
        use_avx_manually,
        input_a_shape,
        l1_cache_a_shape,
        input_b_shape,
        l1_cache_b_shape,
    )


if __name__ == "__main__":
    run_matmul(
        FILE_DIR / "test_output" / "custom",
        num_iterations=25,
        compare_against_torch=True,
        transpose_b_levels=["atomic", "l1_cache"],
        use_avx_manually=True,
        input_a_shape=(1, 2048, 2048),
        l1_cache_a_shape=(1, 64, 64),
        input_b_shape=(2048, 2048),
        l1_cache_b_shape=(64, 64),
    )