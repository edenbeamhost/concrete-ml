"""Tests the deployment APIs."""

import json
import os
import tempfile
import warnings
import zipfile
from functools import partial
from pathlib import Path
from shutil import copyfile

import numpy
import pytest
from torch import nn

from concrete.ml.deployment.fhe_client_server import (
    DeploymentMode,
    FHEModelClient,
    FHEModelDev,
    FHEModelServer,
)
from concrete.ml.pytest.torch_models import FCSmall
from concrete.ml.pytest.utils import MODELS_AND_DATASETS, get_model_name, instantiate_model_generic
from concrete.ml.quantization.quantized_module import QuantizedModule
from concrete.ml.sklearn.linear_model import SGDClassifier
from concrete.ml.torch.compile import compile_torch_model

# pylint: disable=too-many-statements,too-many-locals


# Add encrypted training with SGDClassifier manually
# FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/4460
MODELS_AND_DATASETS = MODELS_AND_DATASETS + [
    pytest.param(
        partial(SGDClassifier, fit_encrypted=True, parameters_range=(-1, 1)),
        {"n_samples": 100, "n_features": 10, "n_classes": 2, "n_informative": 10, "n_redundant": 0},
        id="SGDClassifier_Encrypted_Training",
    )
]


class OnDiskNetwork:
    """A network interaction on disk."""

    def __init__(self):
        # Create 3 temporary folder for server, client and dev with tempfile
        self.server_dir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.client_dir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.dev_dir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with

    def client_send_evaluation_key_to_server(self, serialized_evaluation_keys):
        """Send the public key to the server."""
        with open(self.server_dir.name + "/serialized_evaluation_keys.ekl", "wb") as f:
            f.write(serialized_evaluation_keys)

    def client_send_input_to_server_for_prediction(self, encrypted_input):
        """Send the input to the server."""
        with open(self.server_dir.name + "/serialized_evaluation_keys.ekl", "rb") as f:
            serialized_evaluation_keys = f.read()
        encrypted_prediction = FHEModelServer(self.server_dir.name).run(
            encrypted_input, serialized_evaluation_keys
        )
        with open(self.server_dir.name + "/encrypted_prediction.enc", "wb") as f:
            f.write(encrypted_prediction)

    def dev_send_model_to_server(self):
        """Send the model to the server."""
        copyfile(self.dev_dir.name + "/server.zip", self.server_dir.name + "/server.zip")

    def server_send_encrypted_prediction_to_client(self):
        """Send the encrypted prediction to the client."""
        with open(self.server_dir.name + "/encrypted_prediction.enc", "rb") as f:
            encrypted_prediction = f.read()
        return encrypted_prediction

    def dev_send_clientspecs_and_modelspecs_to_client(self):
        """Send the clientspecs and evaluation key to the client."""
        copyfile(self.dev_dir.name + "/client.zip", self.client_dir.name + "/client.zip")

    def cleanup(self):
        """Clean up the temporary folders."""
        self.server_dir.cleanup()
        self.client_dir.cleanup()
        self.dev_dir.cleanup()


# This is a known flaky test
# FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/4014
@pytest.mark.flaky
@pytest.mark.parametrize("model_class, parameters", MODELS_AND_DATASETS)
@pytest.mark.parametrize("n_bits", [2])
def test_client_server_sklearn(
    default_configuration,
    model_class,
    parameters,
    n_bits,
    load_data,
    check_is_good_execution_for_cml_vs_circuit,
    check_array_equal,
    check_float_array_equal,
):
    """Test the client-server interface for built-in models."""

    if get_model_name(model_class) == "KNeighborsClassifier":
        # Skipping KNN for this test
        # FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/4014
        pytest.skip("Skipping KNN, because FHE predictions and clear ones are differents.")

    # Generate random data
    x, y = load_data(model_class, **parameters)

    x_train = x[:-1]
    y_train = y[:-1]
    x_test = x[-1:]

    # Instantiate the model
    model = instantiate_model_generic(model_class, n_bits=n_bits)

    # Fit the model
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        model.fit(x_train, y_train)

    key_dir = default_configuration.insecure_key_cache_location

    # Running the simulation using a model that is not compiled should not be possible
    with pytest.raises(AttributeError, match=".* model is not compiled.*"):
        check_client_server_execution(
            x_test, model, key_dir, check_array_equal, check_float_array_equal
        )

    compilation_kwargs = {
        "X": x_train,
        "configuration": default_configuration,
    }

    # Compile the model
    fhe_circuit = model.compile(**compilation_kwargs)

    check_input_compression(model, fhe_circuit, is_torch=False, **compilation_kwargs)

    # Check that client and server files are properly generated
    check_client_server_files(model)

    max_bit_width = fhe_circuit.graph.maximum_integer_bit_width()
    print(f"Max width {max_bit_width}")

    # Compare the FHE predictions with the clear ones. Simulated predictions are not considered in
    # this test.
    check_is_good_execution_for_cml_vs_circuit(x_test, model, simulate=False, n_allowed_runs=1)

    # Check client/server FHE predictions vs the FHE predictions of the dev model
    check_client_server_execution(
        x_test, model, key_dir, check_array_equal, check_float_array_equal
    )


def test_client_server_custom_model(
    default_configuration,
    check_is_good_execution_for_cml_vs_circuit,
    check_array_equal,
    check_float_array_equal,
):
    """Test the client-server interface for a custom model (through a quantized module)."""

    # Generate random data
    x_train, x_test = numpy.random.rand(100, 2), numpy.random.rand(1, 2)

    key_dir = default_configuration.insecure_key_cache_location

    # Running the simulation using a QuantizedModule that is not compiled should not be possible
    with pytest.raises(AttributeError, match=".* quantized module is not compiled.*"):
        # Instantiate an empty QuantizedModule object
        quantized_module = QuantizedModule()

        check_client_server_execution(
            x_test, quantized_module, key_dir, check_array_equal, check_float_array_equal
        )

    torch_model = FCSmall(2, nn.ReLU)

    compilation_kwargs = {
        "torch_inputset": x_train,
        "configuration": default_configuration,
        "n_bits": 2,
    }

    # Get the quantized module from the model and compile it
    quantized_numpy_module = compile_torch_model(torch_model, **compilation_kwargs)

    check_input_compression(
        torch_model, quantized_numpy_module.fhe_circuit, is_torch=True, **compilation_kwargs
    )

    # Check that client and server files are properly generated
    check_client_server_files(quantized_numpy_module)

    # Check that the FHE execution is correct.
    check_is_good_execution_for_cml_vs_circuit(
        x_test, quantized_numpy_module, simulate=False, n_allowed_runs=1
    )

    check_client_server_execution(
        x_test, quantized_numpy_module, key_dir, check_array_equal, check_float_array_equal
    )


def check_client_server_files(model):
    """Test the client server interface API generates the expected file.

    This test expects that the given model has been trained and compiled in development.
    """
    # Create a new network
    disk_network = OnDiskNetwork()

    # And try to save it again
    fhe_model_dev = FHEModelDev(path_dir=disk_network.dev_dir.name, model=model)
    fhe_model_dev.save()

    # Check that re-saving the dev model fails
    with pytest.raises(
        Exception,
        match=(
            f"path_dir: {disk_network.dev_dir.name} is not empty. "
            "Please delete it before saving a new model."
        ),
    ):
        fhe_model_dev.save()

    client_zip_path = Path(disk_network.dev_dir.name) / "client.zip"
    server_zip_path = Path(disk_network.dev_dir.name) / "server.zip"

    # Check that client and server zip files has been generated
    assert (
        client_zip_path.is_file()
    ), f"Client files were not properly generated. Expected {client_zip_path} to be a file."
    assert (
        server_zip_path.is_file()
    ), f"Server files were not properly generated. Expected {server_zip_path} to be a file."

    processing_file_name = "serialized_processing.json"
    versions_file_name = "versions.json"

    # Check that the client.zip file has the processing and versions json files
    with zipfile.ZipFile(client_zip_path) as client_zip:
        with client_zip.open(processing_file_name, "r") as file:
            assert isinstance(
                json.load(file), dict
            ), f"{client_zip_path} does not contain a '{processing_file_name}' file."

        with client_zip.open(versions_file_name, "r") as file:
            assert isinstance(
                json.load(file), dict
            ), f"{client_zip_path} does not contain a '{versions_file_name}' file."

    # Check that the server.zip file has the versions json file
    with zipfile.ZipFile(server_zip_path) as server_zip:
        with server_zip.open("versions.json", "r") as file:
            assert isinstance(
                json.load(file), dict
            ), f"{server_zip_path} does not contain a '{versions_file_name}' file."

    # Clean up
    disk_network.cleanup()


def check_client_server_execution(
    x_test, model, key_dir, check_array_equal, check_float_array_equal
):
    """Test the client server interface API.

    This test expects that the given model has been trained and compiled in development. It
    basically replicates a production-like interaction and checks that results are on matching the
    development model.
    """
    # Create a new network
    disk_network = OnDiskNetwork()

    # Save development files
    fhe_model_dev = FHEModelDev(path_dir=disk_network.dev_dir.name, model=model)
    fhe_model_dev.save()

    # Send necessary files to server and client
    disk_network.dev_send_clientspecs_and_modelspecs_to_client()
    disk_network.dev_send_model_to_server()

    # Load the client
    fhe_model_client = FHEModelClient(
        path_dir=disk_network.client_dir.name,
        key_dir=key_dir,
    )
    fhe_model_client.load()

    # Load the server
    fhe_model_server = FHEModelServer(path_dir=disk_network.server_dir.name)
    fhe_model_server.load()

    # Client side : Generate all keys and serialize the evaluation keys for the server
    evaluation_keys = fhe_model_client.get_serialized_evaluation_keys()

    # Client side : Encrypt the data
    q_x_encrypted_serialized = fhe_model_client.quantize_encrypt_serialize(x_test)

    # Server side: Run the model over encrypted data
    q_y_pred_encrypted_serialized = fhe_model_server.run(q_x_encrypted_serialized, evaluation_keys)

    # Client side : Decrypt, de-quantize and post-process the result
    q_y_pred = fhe_model_client.deserialize_decrypt(q_y_pred_encrypted_serialized)
    y_pred = fhe_model_client.deserialize_decrypt_dequantize(q_y_pred_encrypted_serialized)

    # Dev side: Predict using the model and circuit from development
    q_x_test = model.quantize_input(x_test)
    q_y_pred_dev = model.fhe_circuit.encrypt_run_decrypt(q_x_test)
    y_pred_dev = model.dequantize_output(q_y_pred_dev)
    y_pred_dev = model.post_processing(y_pred_dev)

    # Check that both quantized and de-quantized (+ post-processed) results from the server are
    # matching the ones from the dec model
    check_float_array_equal(y_pred, y_pred_dev)
    check_array_equal(q_y_pred, q_y_pred_dev)

    # Clean up
    disk_network.cleanup()


def check_input_compression(model, fhe_circuit_compressed, is_torch, **compilation_kwargs):
    """Check that input compression properly reduces input sizes."""

    # Check that input ciphertext compression is enabled
    assert os.environ.get("USE_INPUT_COMPRESSION") == "1", "'USE_INPUT_COMPRESSION' is not enabled"

    compressed_size = fhe_circuit_compressed.size_of_inputs

    with pytest.MonkeyPatch.context() as mp_context:

        # Disable input ciphertext compression
        mp_context.setenv("USE_INPUT_COMPRESSION", "0")

        # Check that input ciphertext compression is disabled
        assert (
            os.environ.get("USE_INPUT_COMPRESSION") == "0"
        ), "'USE_INPUT_COMPRESSION' is not disabled"

        if is_torch:
            fhe_circuit_uncompressed = compile_torch_model(model, **compilation_kwargs).fhe_circuit
        else:
            fhe_circuit_uncompressed = model.compile(**compilation_kwargs)

        uncompressed_size = fhe_circuit_uncompressed.size_of_inputs

    # Make sure inputs are compressed by a given important factor
    input_compression_factor = 50

    assert input_compression_factor * compressed_size < uncompressed_size, (
        "Compressed input ciphertext's is not smaller than the uncompressed input ciphertext. Got "
        f"{compressed_size} bytes (compressed) and {uncompressed_size} bytes (uncompressed)."
    )


ERROR_MSG_BAD_MODE = "Mode must be either 'inference' or 'training'"
ERROR_MSG_NO_FHE_CIRCUIT = "Training FHE circuit does not exist."


@pytest.mark.parametrize("n_bits", [2])
@pytest.mark.parametrize(
    "mode, fit_encrypted, error_message",
    [
        ("invalid_mode", True, ERROR_MSG_BAD_MODE),
        ("INVALID_MODE", True, ERROR_MSG_BAD_MODE),
        ("train", True, ERROR_MSG_BAD_MODE),
        ("", True, ERROR_MSG_BAD_MODE),
        (None, True, None),
        ("inference", False, None),
        ("inference", True, None),
        ("training", False, ERROR_MSG_NO_FHE_CIRCUIT),
        ("training", True, None),
        (DeploymentMode.INFERENCE, False, None),
        (DeploymentMode.TRAINING, False, ERROR_MSG_NO_FHE_CIRCUIT),
        (DeploymentMode.TRAINING, True, None),
    ],
)
def test_save_mode_handling(n_bits, fit_encrypted, mode, error_message):
    """Test that the save method handles valid and invalid modes correctly."""

    # Generate random data
    x, y = numpy.random.rand(20, 2), numpy.random.randint(0, 2, 20)

    x_train = x[:-1]
    y_train = y[:-1]

    # Instantiate the model
    parameters_range = [-1, 1] if fit_encrypted else None
    model = instantiate_model_generic(
        partial(SGDClassifier, fit_encrypted=fit_encrypted, parameters_range=parameters_range),
        n_bits=n_bits,
    )

    # Fit the model
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        model.fit(x_train, y_train)

    # Compile
    model.compile(X=x_train)

    # Create FHEModelDev instance
    with tempfile.TemporaryDirectory() as temp_dir:
        model_dev = FHEModelDev(path_dir=temp_dir, model=model)

        if error_message:
            with pytest.raises((AssertionError, ValueError), match=error_message):
                model_dev.save(mode=mode)
        else:
            model_dev.save(mode=mode)
